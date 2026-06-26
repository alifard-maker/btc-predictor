"""Load persisted funding, OI, and macro series for feature engineering."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)


class AuxiliaryStore:
  def __init__(self, cfg: dict[str, Any]):
    self.base = Path(cfg["paths"]["candles"]) / "auxiliary"

  def load(self, name: str) -> pd.DataFrame:
    path = self.base / f"{name}.parquet"
    if not path.exists():
      return pd.DataFrame()
    try:
      df = pd.read_parquet(path)
      if "timestamp" not in df.columns:
        return pd.DataFrame()
      df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
      return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
      log.warning("Failed to load auxiliary %s: %s", name, e)
      return pd.DataFrame()

  def load_all(self) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for name in ("funding_rate", "open_interest", "nasdaq_futures", "dxy", "liquidations"):
      df = self.load(name)
      if not df.empty:
        out[name] = df
    return out
