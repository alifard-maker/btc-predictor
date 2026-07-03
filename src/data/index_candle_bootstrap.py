"""Bootstrap 1h candle parquet for index assets (SPX/NDX) from yfinance."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from src.assets import INDEX_ASSETS, asset_cfg, asset_enabled
from src.data.storage import CandleStorage

log = logging.getLogger(__name__)

ASSET_TICKERS = {
  "spx": "^GSPC",
  "ndx": "^NDX",
}

_BOOTSTRAP_STARTED: set[str] = set()
_BOOTSTRAP_LOCK = threading.Lock()


def index_candles_missing(cfg: dict[str, Any], asset: str) -> bool:
  acfg = asset_cfg(cfg, asset)
  return not CandleStorage(acfg).path_for("1h").exists()


def bootstrap_index_candles(asset: str, *, years: int = 3, cfg: dict[str, Any] | None = None) -> Path:
  from src.config import load_config

  cfg = cfg or load_config()
  acfg = asset_cfg(cfg, asset)
  ticker = ASSET_TICKERS[asset]
  storage = CandleStorage(acfg)
  period = f"{years}y"
  hist = yf.Ticker(ticker).history(period=period, interval="1h")
  if hist.empty:
    raise RuntimeError(f"No yfinance data for {ticker}")
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
  for col in ("open", "high", "low", "close", "volume"):
    if col not in hist.columns:
      hist[col] = 0.0 if col == "volume" else hist["close"]
  out = hist[["timestamp", "open", "high", "low", "close", "volume"]].copy()
  storage.save("1h", out)
  path = storage.path_for("1h")
  log.info("Bootstrapped %s 1h candles: %s rows → %s", asset.upper(), len(out), path)
  return path


def bootstrap_index_candles_if_missing(
  cfg: dict[str, Any],
  *,
  years: int = 3,
  background: bool = True,
) -> list[str]:
  """Bootstrap missing index candle parquets. Returns assets queued or bootstrapped."""
  queued: list[str] = []
  for asset in INDEX_ASSETS:
    if not asset_enabled(cfg, asset):
      continue
    if not index_candles_missing(cfg, asset):
      continue
    with _BOOTSTRAP_LOCK:
      if asset in _BOOTSTRAP_STARTED:
        continue
      _BOOTSTRAP_STARTED.add(asset)
    queued.append(asset)
    if background:
      threading.Thread(
        target=_bootstrap_worker,
        args=(asset, years, cfg),
        name=f"bootstrap-{asset}-candles",
        daemon=True,
      ).start()
    else:
      _bootstrap_worker(asset, years, cfg)
  return queued


def _bootstrap_worker(asset: str, years: int, cfg: dict[str, Any]) -> None:
  try:
    bootstrap_index_candles(asset, years=years, cfg=cfg)
  except Exception:
    log.exception("%s index candle bootstrap failed", asset.upper())
    with _BOOTSTRAP_LOCK:
      _BOOTSTRAP_STARTED.discard(asset)
