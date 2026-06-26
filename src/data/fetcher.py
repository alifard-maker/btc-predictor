from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import ccxt
import pandas as pd
import requests
import yfinance as yf

log = logging.getLogger(__name__)

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

# Per-exchange symbol mapping when config symbol doesn't match
EXCHANGE_SYMBOLS = {
  "binance": "BTC/USDT",
  "kraken": "BTC/USD",
  "coinbase": "BTC/USD",
  "coinbaseexchange": "BTC/USD",
  "bitstamp": "BTC/USD",
}


class DataFetcher:
  """Fetch live and historical market data with multi-exchange fallback."""

  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    self.symbol = cfg["symbol"]
    self._exchange_id: str | None = None
    self.exchange: ccxt.Exchange | None = None
    self._last_connect_error: str | None = None

  def _ensure_exchange(self) -> ccxt.Exchange:
    if self.exchange is not None:
      return self.exchange
    self.exchange = self._connect()
    return self.exchange

  def _connect(self) -> ccxt.Exchange:
    candidates = [self.cfg.get("exchange", "kraken")] + self.cfg.get("exchange_fallbacks", [])
    last_err: Exception | None = None

    for ex_id in candidates:
      if not hasattr(ccxt, ex_id):
        continue
      try:
        ex = getattr(ccxt, ex_id)({"enableRateLimit": True})
        sym = EXCHANGE_SYMBOLS.get(ex_id, self.symbol)
        ex.fetch_ohlcv(sym, "1m", limit=1)
        self._exchange_id = ex_id
        self.symbol = sym
        log.info("Connected to %s (%s)", ex_id, sym)
        return ex
      except Exception as e:
        last_err = e
        log.warning("Exchange %s unavailable: %s", ex_id, e)

    raise RuntimeError(f"No exchange available. Last error: {last_err}")

  def is_connected(self) -> bool:
    return self.exchange is not None

  def fetch_ohlcv(
    self,
    interval: str,
    since_ms: int | None = None,
    limit: int = 1000,
  ) -> pd.DataFrame:
    ex = self._ensure_exchange()
    raw = ex.fetch_ohlcv(
      self.symbol, timeframe=interval, since=since_ms, limit=limit
    )
    df = pd.DataFrame(raw, columns=CANDLE_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df

  def fetch_ohlcv_range(
    self,
    interval: str,
    start: datetime,
    end: datetime | None = None,
  ) -> pd.DataFrame:
    end = end or datetime.now(timezone.utc)
    since_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    frames: list[pd.DataFrame] = []

    while since_ms < end_ms:
      batch = self.fetch_ohlcv(interval, since_ms=since_ms, limit=1000)
      if batch.empty:
        break
      frames.append(batch)
      last_ts = int(batch["timestamp"].iloc[-1].timestamp() * 1000)
      if last_ts <= since_ms:
        break
      since_ms = last_ts + 1
      time.sleep(ex.rateLimit / 1000)

    if not frames:
      return pd.DataFrame(columns=CANDLE_COLUMNS)

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    df = df[df["timestamp"] <= pd.Timestamp(end, tz="UTC")]
    return df.reset_index(drop=True)

  def fetch_latest_candles(self, interval: str, count: int = 500) -> pd.DataFrame:
    return self.fetch_ohlcv(interval, limit=count)

  def fetch_last_price(self) -> float:
    """Latest trade price from the connected exchange."""
    ex = self._ensure_exchange()
    ticker = ex.fetch_ticker(self.symbol)
    last = ticker.get("last") or ticker.get("close")
    if last is None:
      raise RuntimeError("Exchange ticker returned no price")
    return float(last)

  def fetch_funding_rate(self, limit: int = 100) -> pd.DataFrame:
    """Binance perpetual funding rate (skipped if geo-blocked)."""
    symbol = "BTCUSDT"
    url = "https://fapi.binance.com/fapi/v1/fundingRate"
    try:
      resp = requests.get(url, params={"symbol": symbol, "limit": limit}, timeout=30)
      if resp.status_code == 451:
        log.warning("Binance futures blocked — skipping funding rate")
        return pd.DataFrame()
      resp.raise_for_status()
      data = resp.json()
      df = pd.DataFrame(data)
      if df.empty:
        return df
      df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
      df["fundingRate"] = df["fundingRate"].astype(float)
      return df.rename(columns={"fundingTime": "timestamp"})
    except Exception as e:
      log.warning("Funding rate fetch failed: %s", e)
      return pd.DataFrame()

  def fetch_open_interest(self, period: str = "5m", limit: int = 500) -> pd.DataFrame:
    symbol = "BTCUSDT"
    url = "https://fapi.binance.com/futures/data/openInterestHist"
    try:
      resp = requests.get(url, params={"symbol": symbol, "period": period, "limit": limit}, timeout=30)
      if resp.status_code == 451:
        log.warning("Binance futures blocked — skipping open interest")
        return pd.DataFrame()
      resp.raise_for_status()
      data = resp.json()
      df = pd.DataFrame(data)
      if df.empty:
        return df
      df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
      df["sumOpenInterest"] = df["sumOpenInterest"].astype(float)
      df["sumOpenInterestValue"] = df["sumOpenInterestValue"].astype(float)
      return df
    except Exception as e:
      log.warning("Open interest fetch failed: %s", e)
      return pd.DataFrame()

  def fetch_liquidations(self, limit: int = 100) -> pd.DataFrame:
    symbol = "BTCUSDT"
    url = "https://fapi.binance.com/fapi/v1/allForceOrders"
    try:
      resp = requests.get(url, params={"symbol": symbol, "limit": limit}, timeout=30)
      if resp.status_code in (400, 451):
        return pd.DataFrame()
      resp.raise_for_status()
      data = resp.json()
      df = pd.DataFrame(data)
      if df.empty:
        return df
      df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
      df["price"] = df["price"].astype(float)
      df["qty"] = df["qty"].astype(float)
      return df.rename(columns={"time": "timestamp"})
    except Exception:
      return pd.DataFrame()

  def fetch_macro(self, ticker: str, period: str = "5d", interval: str = "5m") -> pd.DataFrame:
    t = yf.Ticker(ticker)
    hist = t.history(period=period, interval=interval)
    if hist.empty:
      return pd.DataFrame()
    hist = hist.reset_index()
    hist.columns = [c.lower().replace(" ", "_") for c in hist.columns]
    if "datetime" in hist.columns:
      hist = hist.rename(columns={"datetime": "timestamp"})
    elif "date" in hist.columns:
      hist = hist.rename(columns={"date": "timestamp"})
    if hist["timestamp"].dt.tz is None:
      hist["timestamp"] = hist["timestamp"].dt.tz_localize("UTC")
    else:
      hist["timestamp"] = hist["timestamp"].dt.tz_convert("UTC")
    return hist

  def fetch_nasdaq_futures(self, **kwargs) -> pd.DataFrame:
    return self.fetch_macro("NQ=F", **kwargs)

  def fetch_dxy(self, **kwargs) -> pd.DataFrame:
    return self.fetch_macro("DX-Y.NYB", **kwargs)
