"""Kalshi daily/hourly threshold and range-band market discovery."""

from __future__ import annotations

import logging
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

  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    self.kalshi = KalshiClient(cfg)
    dcfg = cfg.get("daily", {})
    self.threshold_series: list[str] = list(
      dcfg.get("threshold_series", ["BTCD", "KXBTCD"])
    )
    self.range_series: list[str] = list(dcfg.get("range_series", ["BTC", "KXBTC"]))
    self.max_markets_per_event = int(dcfg.get("max_markets_per_event", 200))

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

  def _fetch_open_markets(self, series: str) -> list[KalshiContractMarket]:
    if not self.kalshi.enabled:
      return []
    out: list[KalshiContractMarket] = []
    cursor: str | None = None
    pages = 0
    while pages < 5 and len(out) < self.max_markets_per_event:
      params: dict[str, Any] = {
        "series_ticker": series,
        "status": "open",
        "limit": 200,
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

  def _pick_nearest_event(
    self,
    markets: list[KalshiContractMarket],
  ) -> str | None:
    now = datetime.now(timezone.utc)
    by_event: dict[str, list[KalshiContractMarket]] = {}
    for m in markets:
      if m.open_time <= now < m.close_time:
        by_event.setdefault(m.event_ticker, []).append(m)
    if not by_event:
      # fallback: soonest close among open status markets
      for m in markets:
        if m.close_time > now:
          by_event.setdefault(m.event_ticker, []).append(m)
    if not by_event:
      return None
    return min(by_event.keys(), key=lambda ev: min(x.close_time for x in by_event[ev]))

  def active_book(self) -> DailyEventBook | None:
    """Nearest settling event with threshold + range legs."""
    threshold_all: list[KalshiContractMarket] = []
    range_all: list[KalshiContractMarket] = []
    used_threshold_series = ""
    used_range_series = ""

    for series in self.threshold_series:
      batch = self._fetch_open_markets(series)
      if batch:
        threshold_all = batch
        used_threshold_series = series
        break

    for series in self.range_series:
      batch = self._fetch_open_markets(series)
      if batch:
        range_all = batch
        used_range_series = series
        break

    if not threshold_all and not range_all:
      return None

    event = self._pick_nearest_event(threshold_all or range_all)
    if not event:
      return None

    thresholds = [
      m for m in threshold_all
      if m.event_ticker == event and m.strike_type in ("greater", "less")
    ]
    ref_close = thresholds[0].close_time if thresholds else None
    if ref_close is None and range_all:
      ref_close = min(m.close_time for m in range_all)

    ranges = [m for m in range_all if m.strike_type == "between"]
    if ref_close is not None:
      ranges = [
        m for m in ranges
        if abs((m.close_time - ref_close).total_seconds()) < 120
      ]
    else:
      ranges = [m for m in ranges if m.event_ticker == event]

    ref = thresholds[0] if thresholds else (ranges[0] if ranges else None)
    if not ref:
      return None

    freq = "daily" if used_threshold_series in ("BTCD", "BTC") else "hourly"
    if used_threshold_series.startswith("KX") or used_range_series.startswith("KX"):
      freq = "hourly" if freq != "daily" else freq

    thresholds.sort(key=lambda m: m.floor_strike or 0)
    ranges.sort(key=lambda m: m.floor_strike or 0)

    return DailyEventBook(
      event_ticker=event,
      series_ticker=used_threshold_series or used_range_series,
      frequency=freq,
      close_time=ref.close_time,
      title=ref.title,
      threshold_markets=thresholds,
      range_markets=ranges,
    )
