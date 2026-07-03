"""Live index prices via yfinance for SPX/NDX hourly bots."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

from src.data.kalshi import KalshiPriceQuote

log = logging.getLogger(__name__)

_CACHE: dict[str, tuple[KalshiPriceQuote | None, float]] = {}


def _cache_sec(cfg: dict[str, Any] | None) -> float:
  pf = (cfg or {}).get("price_feed") or {}
  return float(pf.get("cache_sec", 30))


def index_price_source(ticker: str) -> str:
  slug = str(ticker).lower().replace("^", "").replace("=", "_").replace("-", "_")
  return f"yfinance_{slug}_live"


def fetch_yfinance_quote(
  ticker: str,
  *,
  cfg: dict[str, Any] | None = None,
  fresh: bool = False,
) -> KalshiPriceQuote | None:
  """Fetch latest index price from yfinance."""
  ticker = str(ticker)
  now_mono = time.monotonic()
  cached = _CACHE.get(ticker)
  ttl = _cache_sec(cfg)
  if not fresh and cached and (now_mono - cached[1]) < ttl:
    return cached[0]
  quote: KalshiPriceQuote | None = None
  try:
    t = yf.Ticker(ticker)
    hist = t.history(period="1d", interval="1m")
    if hist.empty:
      info = t.fast_info
      price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
      if price is not None:
        quote = KalshiPriceQuote(
          price=float(price),
          source=index_price_source(ticker),
          trade_time=datetime.now(timezone.utc),
        )
    else:
      row = hist.iloc[-1]
      price = float(row["Close"])
      ts = hist.index[-1]
      if hasattr(ts, "to_pydatetime"):
        trade_time = ts.to_pydatetime()
        if trade_time.tzinfo is None:
          trade_time = trade_time.replace(tzinfo=timezone.utc)
        else:
          trade_time = trade_time.astimezone(timezone.utc)
      else:
        trade_time = datetime.now(timezone.utc)
      quote = KalshiPriceQuote(price=price, source=index_price_source(ticker), trade_time=trade_time)
  except Exception as e:
    log.warning("yfinance %s fetch failed: %s", ticker, e)
  _CACHE[ticker] = (quote, now_mono)
  return quote
