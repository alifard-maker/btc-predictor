#!/usr/bin/env python3
"""Compare ETH paper (split-hold experiment) vs Kalshi live epoch P&L."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.trading.kalshi_live_report import build_kalshi_live_report
from src.trading.pnl_first_railway_manager import experiment_epoch_at
from src.trading.trade_timing_analytics import build_trade_timing_report


def main() -> int:
  cfg = load_config()
  from src.scheduler.loop import PredictionLoop

  loop = PredictionLoop(cfg)
  since = experiment_epoch_at(loop, cfg, asset="btc")
  base = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
  out = base / "logs" / "pnl_first_manager" / "paper_ab_latest.json"

  kalshi = build_kalshi_live_report(loop, cfg, asset="btc")

  # ETH paper experiment arm (pnl_first paper_profit_exit_hold applies in paper mode)
  eth_store = loop.hourly_bot_store("eth", kind="hourly")
  eth_trades = eth_store.list_trades(limit=5000)
  eth_timing = build_trade_timing_report(eth_trades, mode="paper", since=since)

  payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "epoch_start_at": since.isoformat(),
    "experiment": {
      "arm": "eth_hourly_paper",
      "hold_split": dict((cfg.get("pnl_first") or {}).get("paper_profit_exit_hold") or {}),
      "defer_profit_target_minutes": (cfg.get("pnl_first") or {}).get("defer_profit_target_minutes_to_settle"),
      "defer_leg_stop_minutes": (cfg.get("pnl_first") or {}).get("defer_leg_stop_minutes_to_settle"),
      "mid_hour_entry": dict((cfg.get("pnl_first") or {}).get("mid_hour_entry") or {}),
      "eth_max_hours_to_settle": (
        ((cfg.get("eth") or {}).get("hourly") or {}).get("bot") or {}
      ).get("max_hours_to_settle_for_entry"),
    },
    "control_reference": {
      "arm": "btc_kalshi_live_fills",
      "note": "Ground truth for same epoch window; not a paired A/B on identical signals",
    },
    "kalshi_live": {
      "closed_legs": kalshi.get("closed_legs"),
      "total_pnl_usd": kalshi.get("total_pnl_usd"),
      "by_exit_type": kalshi.get("by_exit_type"),
      "by_entry_timing": kalshi.get("by_entry_timing"),
    },
    "eth_paper": {
      "closed_legs": eth_timing.get("closed_legs"),
      "total_pnl_usd": eth_timing.get("total_pnl_usd"),
      "by_entry_timing": eth_timing.get("by_minutes_to_settle_at_entry"),
      "by_exit_timing": eth_timing.get("by_minutes_to_settle_at_exit"),
    },
    "delta_eth_paper_minus_kalshi_usd": round(
      float(eth_timing.get("total_pnl_usd") or 0) - float(kalshi.get("total_pnl_usd") or 0),
      2,
    ),
  }
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
  print(json.dumps(payload, indent=2), flush=True)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
