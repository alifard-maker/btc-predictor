"""Kalshi sports series / event / market discovery for the sports arb module."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.data.kalshi import KalshiClient

log = logging.getLogger(__name__)

# Prefer single-game moneylines; deprioritize / skip props & futures series.
_SERIES_GAME_HINTS = ("GAME", "MATCH")
_SERIES_GAME_JUNK = (
  "EVERYGAME",
  "VIEWER",
  "FANATICS",
  "FIRSTPLACE",
  "SECONDPLACE",
  "CELEBRITY",
  "ALLSTAR",
  "ASGAME",
  "HRDERBY",
  "GAMESPREAD",
  "GAMETOTAL",
  "GAMETD",
  "GAMESACK",
  "GAMEFG",
  "GAMETO",
  "EXACTMATCH",
  "DRAFTMATCH",
  "SERIESGAME",
  "GAMESPLAYED",
  "PTSALLGAMES",
  "3PTALL",
  "SUMMERGAME",
  "PULLGAME",
  "FTGAME",  # first team? / special — keep MLB main GAME
)
_SERIES_SKIP_SUBSTR = (
  "LEADER",
  "PLAYOFF",
  "MVP",
  "AWARD",
  "FUTURE",
  "CHAMP",
  "CYYOUNG",
  "HOMER",
  "DIVISION",
  "OUTRIGHT",
  "SETWINNER",
  "BTTS",
  "TOTAL",
  "CORNER",
  "SHOT",
  "DRAFT",
  "WINS-",
  "SPREAD",
  "PROP",
  "RECORD",
  "DERBY",
  "NEXTMANAGER",
  "HR",
)


def sports_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict((cfg or {}).get("sports") or {})


def sports_enabled(cfg: dict[str, Any] | None) -> bool:
  return bool(sports_cfg(cfg).get("enabled", False))


def kalshi_market_url(
  *,
  series_ticker: str | None = None,
  event_ticker: str | None = None,
  market_ticker: str | None = None,
) -> str | None:
  """Build a Kalshi web URL for an event/market.

  Site paths look like:
    https://kalshi.com/markets/{series_lower}/{event_ticker}
    https://kalshi.com/markets/{series_lower}/{event_ticker}/{market_ticker}
  Pasting a bare event ticker at kalshi.com/<ticker> 404s.
  """
  event = str(event_ticker or "").strip()
  market = str(market_ticker or "").strip()
  series = str(series_ticker or "").strip()
  if not series and event and "-" in event:
    series = event.split("-", 1)[0]
  if not series or not event:
    return None
  series_l = series.lower()
  if market:
    return f"https://kalshi.com/markets/{series_l}/{event}/{market}"
  return f"https://kalshi.com/markets/{series_l}/{event}"


@dataclass(frozen=True)
class SportsMarketQuote:
  ticker: str
  event_ticker: str
  series_ticker: str
  title: str
  subtitle: str
  yes_bid: float | None
  yes_ask: float | None
  no_bid: float | None
  no_ask: float | None
  close_time: datetime | None
  status: str

  @property
  def effective_no_ask(self) -> float | None:
    if self.no_ask is not None:
      return float(self.no_ask)
    if self.yes_bid is not None:
      return max(0.0, min(1.0, 1.0 - float(self.yes_bid)))
    return None

  def to_dict(self) -> dict[str, Any]:
    return {
      "ticker": self.ticker,
      "event_ticker": self.event_ticker,
      "series_ticker": self.series_ticker,
      "title": self.title,
      "subtitle": self.subtitle,
      "yes_bid": self.yes_bid,
      "yes_ask": self.yes_ask,
      "no_bid": self.no_bid,
      "no_ask": self.no_ask,
      "effective_no_ask": self.effective_no_ask,
      "close_time": self.close_time.isoformat() if self.close_time else None,
      "status": self.status,
    }


@dataclass(frozen=True)
class SportsEventBook:
  event_ticker: str
  series_ticker: str
  title: str
  close_time: datetime | None
  markets: list[SportsMarketQuote]

  def to_dict(self) -> dict[str, Any]:
    return {
      "event_ticker": self.event_ticker,
      "series_ticker": self.series_ticker,
      "title": self.title,
      "close_time": self.close_time.isoformat() if self.close_time else None,
      "markets": [m.to_dict() for m in self.markets],
      "market_count": len(self.markets),
    }


class SportsMarketDiscovery:
  """Fetch open Kalshi sports events and quotes (REST)."""

  def __init__(self, cfg: dict[str, Any], *, kalshi: KalshiClient | None = None):
    self.cfg = cfg
    self.scfg = sports_cfg(cfg)
    self.kalshi = kalshi or KalshiClient(cfg)
    self._series_cache: tuple[list[str], float] | None = None
    self._events_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}
    self._books_cache: tuple[list[SportsEventBook], float] | None = None
    self._last_books_stale: bool = False
    self._pause_s = max(0.0, float(self.scfg.get("request_pause_ms", 150)) / 1000.0)

  def _throttle(self) -> None:
    if self._pause_s > 0:
      time.sleep(self._pause_s)

  @staticmethod
  def _safe_parse_ts(raw: Any) -> datetime | None:
    if raw is None or raw == "":
      return None
    try:
      return KalshiClient._parse_ts(str(raw))
    except (TypeError, ValueError):
      return None

  @staticmethod
  def _series_is_skipped(ticker: str) -> bool:
    t = str(ticker or "").upper()
    if not t:
      return True
    return any(s in t for s in _SERIES_SKIP_SUBSTR)

  @staticmethod
  def _series_is_game_like(ticker: str) -> bool:
    t = str(ticker or "").upper()
    if not any(h in t for h in _SERIES_GAME_HINTS):
      return False
    if SportsMarketDiscovery._series_is_skipped(t):
      return False
    # Allow KXMLBGAME etc.; reject "GAME" junk suffixes/prefixes
    if any(j in t for j in _SERIES_GAME_JUNK):
      # Keep canonical *GAME / *MATCH when junk is only a false substring
      # e.g. KXMLBGAME contains no junk tokens above as full tokens...
      # FTGAME is in junk — KXMLBGAME does not contain FTGAME. Good.
      return False
    return True

  def list_series_tickers(self) -> list[str]:
    explicit = [str(s).strip() for s in (self.scfg.get("series_tickers") or []) if str(s).strip()]
    if explicit:
      return explicit[: int(self.scfg.get("max_series", 40))]

    preferred = [
      str(s).strip()
      for s in (self.scfg.get("preferred_series_tickers") or [])
      if str(s).strip()
    ]

    cache_sec = float(self.scfg.get("discovery_cache_sec", 90))
    if self._series_cache and (time.monotonic() - self._series_cache[1]) < cache_sec:
      return list(self._series_cache[0])

    category = str(self.scfg.get("category") or "Sports")
    discovered: list[str] = []
    cursor: str | None = None
    pages = 0
    # Pull a wide pool — Kalshi returns props first; we re-rank to GAME/MATCH.
    max_discover = int(self.scfg.get("max_series_discover", 400))
    while pages < 20 and len(discovered) < max_discover:
      params: dict[str, Any] = {"limit": 200, "category": category}
      if cursor:
        params["cursor"] = cursor
      self._throttle()
      try:
        data = self.kalshi.get("/series", params=params)
      except Exception as exc:
        log.warning("sports series discovery failed: %s", exc)
        break
      for row in data.get("series") or []:
        t = str(row.get("ticker") or "").strip()
        if t:
          discovered.append(t)
      cursor = data.get("cursor")
      pages += 1
      if not cursor:
        break

    game_like = [t for t in discovered if self._series_is_game_like(t)]
    # Prefer pinned moneyline series, then other GAME/MATCH, fill with non-junk
    seen: set[str] = set()
    tickers: list[str] = []
    for t in preferred + game_like:
      if t in seen:
        continue
      if self._series_is_skipped(t) and t not in preferred:
        continue
      seen.add(t)
      tickers.append(t)
      if len(tickers) >= int(self.scfg.get("max_series", 40)):
        break

    if len(tickers) < int(self.scfg.get("max_series", 40)):
      for t in discovered:
        if t in seen or self._series_is_skipped(t):
          continue
        seen.add(t)
        tickers.append(t)
        if len(tickers) >= int(self.scfg.get("max_series", 40)):
          break

    log.info(
      "sports series discovery: pool=%s game_like=%s selected=%s sample=%s",
      len(discovered),
      len(game_like),
      len(tickers),
      tickers[:12],
    )
    self._series_cache = (tickers, time.monotonic())
    return tickers

  def _fetch_open_events(self, series: str) -> list[dict[str, Any]]:
    cache_sec = float(self.scfg.get("discovery_cache_sec", 90))
    hit = self._events_cache.get(series)
    if hit and (time.monotonic() - hit[1]) < cache_sec:
      return list(hit[0])

    out: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while pages < 5:
      params: dict[str, Any] = {
        "series_ticker": series,
        "status": "open",
        "limit": 200,
      }
      if cursor:
        params["cursor"] = cursor
      self._throttle()
      try:
        data = self.kalshi.get("/events", params=params)
      except Exception as exc:
        log.warning("sports events fetch failed for %s: %s", series, exc)
        break
      out.extend(data.get("events") or [])
      cursor = data.get("cursor")
      pages += 1
      if not cursor:
        break

    self._events_cache[series] = (out, time.monotonic())
    return out

  def _parse_market(self, row: dict[str, Any], *, series: str, event_ticker: str) -> SportsMarketQuote | None:
    ticker = str(row.get("ticker") or "").strip()
    if not ticker:
      return None
    status = str(row.get("status") or "").lower()
    if status and status not in ("open", "active"):
      return None
    close = self._safe_parse_ts(row.get("close_time") or row.get("expected_expiration_time"))
    return SportsMarketQuote(
      ticker=ticker,
      event_ticker=event_ticker,
      series_ticker=series,
      title=str(row.get("title") or row.get("yes_sub_title") or ticker),
      subtitle=str(row.get("subtitle") or row.get("yes_sub_title") or ""),
      yes_bid=self.kalshi._dollars(row.get("yes_bid_dollars") or row.get("yes_bid")),
      yes_ask=self.kalshi._dollars(row.get("yes_ask_dollars") or row.get("yes_ask")),
      no_bid=self.kalshi._dollars(row.get("no_bid_dollars") or row.get("no_bid")),
      no_ask=self.kalshi._dollars(row.get("no_ask_dollars") or row.get("no_ask")),
      close_time=close,
      status=status or "open",
    )

  def _fetch_event_markets(self, series: str, event_ticker: str) -> list[SportsMarketQuote]:
    out: list[SportsMarketQuote] = []
    cursor: str | None = None
    pages = 0
    limit = int(self.scfg.get("max_markets_per_event", 40))
    while pages < 5 and len(out) < limit:
      params: dict[str, Any] = {
        "event_ticker": event_ticker,
        "status": "open",
        "limit": 200,
      }
      if cursor:
        params["cursor"] = cursor
      self._throttle()
      try:
        data = self.kalshi.get("/markets", params=params)
      except Exception as exc:
        log.warning("sports markets fetch failed for %s: %s", event_ticker, exc)
        break
      for row in data.get("markets") or []:
        m = self._parse_market(row, series=series, event_ticker=event_ticker)
        if m:
          out.append(m)
        if len(out) >= limit:
          break
      cursor = data.get("cursor")
      pages += 1
      if not cursor:
        break
    return out[:limit]

  def fetch_open_event_books(self) -> list[SportsEventBook]:
    """Return open sports event books with market quotes.

    Quote/book results are cached briefly so a fast poll (10–15s) does not
    hammer Kalshi (429) on every cycle. If a refresh returns empty (often 429),
    keep the last good books for a few minutes so the scanner stays useful.
    """
    quote_cache = float(self.scfg.get("quote_cache_sec", 30))
    stale_keep = float(self.scfg.get("stale_books_keep_sec", 180))
    self._last_books_stale = False
    if self._books_cache and (time.monotonic() - self._books_cache[1]) < quote_cache:
      return list(self._books_cache[0])

    max_per_series = int(self.scfg.get("max_events_per_series", 12))
    max_total = int(self.scfg.get("max_total_events", 150))
    now = datetime.now(timezone.utc)
    # Collect candidates across series first, then take the soonest-closing
    # globally so one sport cannot monopolize the scan budget.
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for series in self.list_series_tickers():
      events = self._fetch_open_events(series)
      ranked: list[tuple[float, dict[str, Any]]] = []
      for ev in events:
        close = self._safe_parse_ts(ev.get("strike_date") or ev.get("close_time"))
        if close is None:
          ranked.append((9e9, ev))
          continue
        if close <= now:
          continue
        ranked.append(((close - now).total_seconds(), ev))
      ranked.sort(key=lambda x: x[0])
      for secs, ev in ranked[:max_per_series]:
        candidates.append((secs, series, ev))
    candidates.sort(key=lambda x: x[0])
    if max_total > 0:
      candidates = candidates[:max_total]

    books: list[SportsEventBook] = []
    for _, series, ev in candidates:
      event_ticker = str(ev.get("event_ticker") or "").strip()
      if not event_ticker:
        continue
      markets = self._fetch_event_markets(series, event_ticker)
      if not markets:
        continue
      close = markets[0].close_time or self._safe_parse_ts(
        ev.get("strike_date") or ev.get("close_time")
      )
      books.append(
        SportsEventBook(
          event_ticker=event_ticker,
          series_ticker=series,
          title=str(ev.get("title") or event_ticker),
          close_time=close,
          markets=markets,
        )
      )

    if books:
      prev = list(self._books_cache[0]) if self._books_cache else []
      if prev and len(books) < max(10, int(len(prev) * 0.6)):
        self._last_books_stale = True
        log.warning(
          "sports books refresh partial (%s<%s) — keeping stale cache age=%.0fs",
          len(books),
          len(prev),
          time.monotonic() - self._books_cache[1],
        )
        return prev
      self._books_cache = (books, time.monotonic())
      return books

    # Empty refresh (rate-limit / outage): serve last good books if still fresh enough
    if self._books_cache and (time.monotonic() - self._books_cache[1]) < stale_keep:
      self._last_books_stale = True
      log.warning(
        "sports books refresh empty — using stale cache age=%.0fs n=%s",
        time.monotonic() - self._books_cache[1],
        len(self._books_cache[0]),
      )
      return list(self._books_cache[0])

    self._books_cache = (books, time.monotonic())
    return books
