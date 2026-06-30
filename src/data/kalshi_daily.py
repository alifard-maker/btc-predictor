"""Kalshi daily/hourly threshold and range-band market discovery."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.data.kalshi import KalshiClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KalshiContractMarket:
  ticker: str
  event_ticker: str
  title: str
  strike_type: str  # greater | less | between
  floor_strike: float | None
  cap_strike: float | None
  close_time: datetime
  open_time: datetime
  yes_bid: float | None
  yes_ask: float | None
  subtitle: str
  series_ticker: str

  @property
  def yes_mid(self) -> float | None:
    if self.yes_bid is not None and self.yes_ask is not None:
      return (self.yes_bid + self.yes_ask) / 2
    return self.yes_bid or self.yes_ask

  def to_dict(self) -> dict[str, Any]:
    return {
      "ticker": self.ticker,
      "event_ticker": self.event_ticker,
      "title": self.title,
      "strike_type": self.strike_type,
      "floor_strike": self.floor_strike,
      "cap_strike": self.cap_strike,
      "close_time": self.close_time.isoformat(),
      "open_time": self.open_time.isoformat(),
      "yes_bid": self.yes_bid,
      "yes_ask": self.yes_ask,
      "yes_mid": round(self.yes_mid, 4) if self.yes_mid is not None else None,
      "subtitle": self.subtitle,
      "series_ticker": self.series_ticker,
    }


@dataclass(frozen=True)
class DailyEventBook:
  event_ticker: str
  series_ticker: str
  frequency: str
  close_time: datetime
  title: str
  threshold_markets: list[KalshiContractMarket]
  range_markets: list[KalshiContractMarket]


class KalshiDailyMarkets:
  """Fetch threshold (above/below) and range-band contracts."""

  def __init__(self, cfg: dict[str, Any], *, daily_cfg: dict[str, Any] | None = None):
    self.cfg = cfg
    self.kalshi = KalshiClient(cfg)
    dcfg = daily_cfg if daily_cfg is not None else cfg.get("daily", {})
    self.threshold_series: list[str] = list(
      dcfg.get("threshold_series", ["KXBTCD"])
    )
    self.range_series: list[str] = list(dcfg.get("range_series", ["KXBTC"]))
    self.max_markets_per_event = int(dcfg.get("max_markets_per_event", 1000))
    self.max_event_candidates = int(dcfg.get("max_event_candidates", 6))
    self.discovery_cache_sec = float(dcfg.get("discovery_cache_sec", 75))
    self._book_cache: tuple[DailyEventBook | None, float] | None = None
    self._events_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}
    self._markets_cache: dict[tuple[str, str], tuple[list[KalshiContractMarket], float]] = {}

  def _discovery_cache_ttl(self) -> float:
    from src.trading.kalshi_circuit import get_circuit_breaker

    base = self.discovery_cache_sec
    circuit = get_circuit_breaker()
    if circuit and circuit.throttle_discovery():
      return max(base, 90.0)
    return base

  def _cache_fresh(self, ts: float) -> bool:
    return time.monotonic() - ts < self._discovery_cache_ttl()

  def _parse_market(self, row: dict[str, Any], series: str) -> KalshiContractMarket | None:
    close_raw = row.get("close_time")
    open_raw = row.get("open_time")
    if not close_raw or not open_raw:
      return None
    floor = row.get("floor_strike")
    cap = row.get("cap_strike")
    return KalshiContractMarket(
      ticker=str(row.get("ticker", "")),
      event_ticker=str(row.get("event_ticker", "")),
      title=str(row.get("title", "")),
      strike_type=str(row.get("strike_type", "")),
      floor_strike=float(floor) if floor not in (None, "") else None,
      cap_strike=float(cap) if cap not in (None, "") else None,
      close_time=self.kalshi._parse_ts(close_raw),
      open_time=self.kalshi._parse_ts(open_raw),
      yes_bid=self.kalshi._dollars(row.get("yes_bid_dollars")),
      yes_ask=self.kalshi._dollars(row.get("yes_ask_dollars")),
      subtitle=str(row.get("subtitle") or row.get("yes_sub_title") or ""),
      series_ticker=series,
    )

  def _fetch_open_markets(self, series: str, *, event_ticker: str | None = None) -> list[KalshiContractMarket]:
    """Fetch open markets — prefer event_ticker to get the full strike ladder."""
    if not self.kalshi.enabled:
      return []
    if event_ticker:
      return self._fetch_event_markets(series, event_ticker)
    out: list[KalshiContractMarket] = []
    cursor: str | None = None
    pages = 0
    while pages < 10 and len(out) < self.max_markets_per_event:
      params: dict[str, Any] = {
        "series_ticker": series,
        "status": "open",
        "limit": min(1000, self.max_markets_per_event),
      }
      if cursor:
        params["cursor"] = cursor
      try:
        data = self.kalshi.get("/markets", params=params)
      except Exception as e:
        log.warning("Kalshi %s markets fetch failed: %s", series, e)
        break
      for row in data.get("markets", []):
        m = self._parse_market(row, series)
        if m:
          out.append(m)
      cursor = data.get("cursor")
      pages += 1
      if not cursor:
        break
    return out

  def _fetch_event_markets(self, series: str, event_ticker: str) -> list[KalshiContractMarket]:
    key = (series, event_ticker)
    cached = self._markets_cache.get(key)
    if cached and self._cache_fresh(cached[1]):
      return cached[0]

    out: list[KalshiContractMarket] = []
    cursor: str | None = None
    pages = 0
    while pages < 10 and len(out) < self.max_markets_per_event:
      params: dict[str, Any] = {
        "event_ticker": event_ticker,
        "status": "open",
        "limit": min(1000, self.max_markets_per_event),
      }
      if cursor:
        params["cursor"] = cursor
      try:
        data = self.kalshi.get("/markets", params=params)
      except Exception as e:
        log.warning("Kalshi %s event %s markets failed: %s", series, event_ticker, e)
        break
      for row in data.get("markets", []):
        m = self._parse_market(row, series)
        if m:
          out.append(m)
      cursor = data.get("cursor")
      pages += 1
      if not cursor:
        break
    self._markets_cache[key] = (out, time.monotonic())
    return out

  def _fetch_open_events(self, series: str) -> list[dict[str, Any]]:
    cached = self._events_cache.get(series)
    if cached and self._cache_fresh(cached[1]):
      return cached[0]

    out: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while pages < 10:
      params: dict[str, Any] = {
        "series_ticker": series,
        "status": "open",
        "limit": 200,
      }
      if cursor:
        params["cursor"] = cursor
      try:
        data = self.kalshi.get("/events", params=params)
      except Exception as e:
        log.warning("Kalshi %s events fetch failed: %s", series, e)
        break
      out.extend(data.get("events", []))
      cursor = data.get("cursor")
      pages += 1
      if not cursor:
        break
    self._events_cache[series] = (out, time.monotonic())
    return out

  @staticmethod
  def _event_strike_time(event: dict[str, Any]) -> datetime | None:
    raw = event.get("strike_date")
    if not raw:
      return None
    return KalshiClient._parse_ts(raw)

  def _rank_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Soonest-settling open events first."""
    now = datetime.now(timezone.utc)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for event in events:
      close = self._event_strike_time(event)
      if close is None or close <= now:
        continue
      hours = (close - now).total_seconds() / 3600.0
      if hours > 48:
        continue
      ranked.append((hours, event))
    ranked.sort(key=lambda x: x[0])
    return [e for _, e in ranked[: self.max_event_candidates]]

  def _ranges_for_threshold_event(
    self,
    range_series: str,
    threshold_event: str,
    ref_close: datetime,
  ) -> list[KalshiContractMarket]:
    range_events = self._fetch_open_events(range_series)
    if not range_events:
      return []
    suffix = threshold_event.split("-", 1)[-1] if "-" in threshold_event else ""
    best_ticker: str | None = None
    best_delta = float("inf")
    for event in range_events:
      ticker = str(event.get("event_ticker", ""))
      if suffix and ticker.endswith(suffix):
        best_ticker = ticker
        best_delta = 0.0
        break
      close = self._event_strike_time(event)
      if close is None:
        continue
      delta = abs((close - ref_close).total_seconds())
      if delta < best_delta:
        best_delta = delta
        best_ticker = ticker
    if not best_ticker or best_delta > 120:
      return []
    return [
      m
      for m in self._fetch_event_markets(range_series, best_ticker)
      if m.strike_type == "between"
    ]

  @staticmethod
  def _threshold_strike_values(markets: list[KalshiContractMarket]) -> list[float]:
    out: list[float] = []
    for m in markets:
      if m.strike_type == "greater" and m.floor_strike is not None:
        out.append(float(m.floor_strike))
      elif m.strike_type == "less" and m.cap_strike is not None:
        out.append(float(m.cap_strike))
    return out

  @staticmethod
  def _band_mids(markets: list[KalshiContractMarket]) -> list[float]:
    mids: list[float] = []
    for m in markets:
      if m.strike_type != "between":
        continue
      low = float(m.floor_strike or 0)
      high = float(m.cap_strike or low)
      if low > 0 and high > low:
        mids.append((low + high) / 2)
    return mids

  def _event_bracket_score(
    self,
    markets: list[KalshiContractMarket],
    reference_price: float,
  ) -> float:
    """Higher = strikes/bands better bracket reference BRTI."""
    thresholds = [m for m in markets if m.strike_type in ("greater", "less")]
    ranges = [m for m in markets if m.strike_type == "between"]
    strikes = self._threshold_strike_values(thresholds)
    mids = self._band_mids(ranges)
    score = 0.0
    if strikes:
      lo, hi = min(strikes), max(strikes)
      if lo <= reference_price <= hi:
        nearest = min(strikes, key=lambda s: abs(s - reference_price))
        score += 10_000 - abs(nearest - reference_price)
      else:
        gap = min(abs(reference_price - lo), abs(reference_price - hi))
        score -= gap
    if mids:
      if any(
        float(m.floor_strike or 0) <= reference_price <= float(m.cap_strike or 0)
        for m in ranges
      ):
        score += 5_000
      else:
        nearest_mid = min(mids, key=lambda m: abs(m - reference_price))
        score -= abs(nearest_mid - reference_price)
    now = datetime.now(timezone.utc)
    hours_left = max(0.1, (min(m.close_time for m in markets) - now).total_seconds() / 3600)
    if hours_left <= 2:
      score += 500
    return score

  def _make_book(
    self,
    thresholds: list[KalshiContractMarket],
    ranges: list[KalshiContractMarket],
    used_threshold_series: str,
    used_range_series: str,
  ) -> DailyEventBook | None:
    if not thresholds and not ranges:
      return None
    ref = thresholds[0] if thresholds else ranges[0]

    freq = "daily" if used_threshold_series in ("BTCD", "BTC", "ETHD", "ETH") else "hourly"
    if used_threshold_series.startswith("KX") or used_range_series.startswith("KX"):
      freq = "hourly" if freq != "daily" else freq

    thresholds = sorted(thresholds, key=lambda m: m.floor_strike or m.cap_strike or 0)
    ranges = sorted(ranges, key=lambda m: m.floor_strike or 0)

    return DailyEventBook(
      event_ticker=ref.event_ticker,
      series_ticker=used_threshold_series or used_range_series,
      frequency=freq,
      close_time=ref.close_time,
      title=ref.title,
      threshold_markets=thresholds,
      range_markets=ranges,
    )

  def active_book(self, reference_price: float | None = None) -> DailyEventBook | None:
    """Pick soonest Kalshi event and load its full strike ladder via event_ticker."""
    if self._book_cache:
      book, ts = self._book_cache
      if self._cache_fresh(ts):
        return book

    candidates: list[tuple[float, float, DailyEventBook]] = []
    now = datetime.now(timezone.utc)
    hourly_mode = any(str(s).startswith("KX") for s in self.threshold_series)

    for thresh_series in self.threshold_series:
      for event in self._rank_events(self._fetch_open_events(thresh_series)):
        event_ticker = str(event.get("event_ticker", ""))
        if not event_ticker:
          continue
        threshold_all = [
          m
          for m in self._fetch_event_markets(thresh_series, event_ticker)
          if m.strike_type in ("greater", "less")
        ]
        if not threshold_all:
          continue
        ref_close = threshold_all[0].close_time

        for range_series in self.range_series:
          ranges = self._ranges_for_threshold_event(
            range_series, event_ticker, ref_close
          )
          book = self._make_book(
            threshold_all, ranges, thresh_series, range_series
          )
          if not book:
            continue

          score = 0.0
          if reference_price and reference_price > 0:
            score = self._event_bracket_score(
              book.threshold_markets + book.range_markets,
              reference_price,
            )
            if thresh_series.startswith("KX"):
              score += 250
            if range_series.startswith("KX"):
              score += 100
          else:
            score = 1.0

          hours_left = max(0.01, (book.close_time - now).total_seconds() / 3600.0)
          candidates.append((score, hours_left, book))

    if not candidates:
      self._book_cache = (None, time.monotonic())
      return None
    if hourly_mode:
      book = min(candidates, key=lambda x: x[1])[2]
    else:
      book = max(candidates, key=lambda x: x[0])[2]
    self._book_cache = (book, time.monotonic())
    return book
