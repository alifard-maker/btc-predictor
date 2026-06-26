from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src.data.fetcher import DataFetcher


class CandleStorage:
  """Persist candles as parquet files partitioned by interval."""

  def __init__(self, cfg: dict[str, Any]):
    self.base = Path(cfg["paths"]["candles"])

  def path_for(self, interval: str) -> Path:
    return self.base / interval / "candles.parquet"

  def save(self, interval: str, df: pd.DataFrame) -> None:
    path = self.path_for(interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
      existing = pd.read_parquet(path)
      combined = (
        pd.concat([existing, df], ignore_index=True)
        .drop_duplicates(subset=["timestamp"])
        .sort_values("timestamp")
      )
    else:
      combined = df.sort_values("timestamp")
    combined.to_parquet(path, index=False)

  def load(self, interval: str, start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
    path = self.path_for(interval)
    if not path.exists():
      return pd.DataFrame()
    df = pd.read_parquet(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    if start:
      df = df[df["timestamp"] >= pd.Timestamp(start, tz="UTC")]
    if end:
      df = df[df["timestamp"] <= pd.Timestamp(end, tz="UTC")]
    return df.reset_index(drop=True)

  def latest_timestamp(self, interval: str) -> datetime | None:
    df = self.load(interval)
    if df.empty:
      return None
    return df["timestamp"].iloc[-1].to_pydatetime()


class HistoricalCollector:
  """Phase 1: collect years of multi-timeframe BTC and auxiliary data."""

  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    self.fetcher = DataFetcher(cfg)
    self.storage = CandleStorage(cfg)

  def collect_candles(
    self,
    interval: str,
    years: int | None = None,
    *,
    force_full: bool = False,
  ) -> int:
    years = years or self.cfg.get("historical_years", 3)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * years)

    existing = len(self.storage.load(interval))
    min_rows = self.cfg.get("min_history_candles", {}).get(interval)
    if min_rows is None and interval == "15m":
      min_rows = int(self.cfg.get("model", {}).get("min_train_samples", 1500) * 1.5)

    last = self.storage.latest_timestamp(interval)
    if not force_full and existing >= (min_rows or 0) and last and last > start:
      start = last - timedelta(minutes=5)

    df = self.fetcher.fetch_ohlcv_range(interval, start, end)
    if not df.empty:
      self.storage.save(interval, df)
    return len(df)

  def collect_all(self, *, force_full: bool = False) -> dict[str, int]:
    results = {}
    # 15m first — enough for training while 1m backfill continues
    order = sorted(self.cfg["intervals"], key=lambda x: (0 if x == "15m" else 1, x))
    for interval in order:
      results[interval] = self.collect_candles(interval, force_full=force_full)
    return results

  def collect_auxiliary(self, out_dir: Path | None = None) -> dict[str, int]:
    """Funding, OI, liquidations, macro — saved separately."""
    out = out_dir or Path(self.cfg["paths"]["candles"]) / "auxiliary"
    out.mkdir(parents=True, exist_ok=True)
    counts = {}

    for name, fn in [
      ("funding_rate", self.fetcher.fetch_funding_rate),
      ("open_interest", lambda: self.fetcher.fetch_open_interest(period="5m", limit=500)),
      ("liquidations", self.fetcher.fetch_liquidations),
      ("nasdaq_futures", self.fetcher.fetch_nasdaq_futures),
      ("dxy", self.fetcher.fetch_dxy),
    ]:
      try:
        df = fn()
        if not df.empty:
          df.to_parquet(out / f"{name}.parquet", index=False)
          counts[name] = len(df)
        else:
          counts[name] = 0
      except Exception:
        counts[name] = -1
    return counts
