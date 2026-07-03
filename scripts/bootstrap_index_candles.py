#!/usr/bin/env python3
"""Bootstrap 1h candle parquet for index assets (SPX/NDX) from yfinance."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.assets import asset_cfg
from src.config import load_config
from src.data.storage import CandleStorage

ASSET_TICKERS = {
  "spx": "^GSPC",
  "ndx": "^NDX",
}


def bootstrap(asset: str, *, years: int = 3) -> Path:
  cfg = load_config()
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
  print(f"Saved {len(out)} 1h bars to {path}")
  return path


def main() -> None:
  parser = argparse.ArgumentParser(description="Bootstrap index 1h candles from yfinance")
  parser.add_argument("assets", nargs="*", default=["spx", "ndx"], choices=["spx", "ndx"])
  parser.add_argument("--years", type=int, default=3)
  args = parser.parse_args()
  for asset in args.assets:
    bootstrap(asset, years=args.years)


if __name__ == "__main__":
  main()
