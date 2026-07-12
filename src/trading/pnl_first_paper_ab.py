"""ETH paper vs live mirror + BTC twin live scorecard for P&L-first manager."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.trading.btc_twin_live import (
  btc_twin_experiment_start_at,
  btc_twin_live_active,
  btc_twin_live_cfg,
)
from src.trading.eth_paper_experiment import (
  eth_bot_cfg,
  eth_experiment_start_at,
  eth_live_mirror_active,
  eth_live_mirror_cfg,
)
from src.trading.kalshi_live_report import build_kalshi_live_report
from src.trading.pnl_first_railway_manager import experiment_epoch_at
from src.trading.trade_timing_analytics import build_trade_timing_report


def paper_ab_output_path(cfg: dict[str, Any] | None = None) -> Path:
  del cfg
  base = Path(os.getenv("DATA_DIR", "data"))
  return base / "logs" / "pnl_first_manager" / "paper_ab_latest.json"


def _eth_timing(loop: Any, cfg: dict[str, Any] | None, *, kind: str, mode: str) -> dict[str, Any]:
  eth_since = eth_experiment_start_at(cfg) or experiment_epoch_at(loop, cfg, asset="eth")
  store = loop.hourly_bot_store("eth", kind=kind)
  trades = store.list_trades(limit=5000)
  return build_trade_timing_report(
    trades,
    mode=mode,
    since=eth_since,
    since_field="exit",
  )


def _btc_twin_timing(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  since = btc_twin_experiment_start_at(cfg) or experiment_epoch_at(loop, cfg, asset="btc")
  store = loop.hourly_bot_store("btc", kind="hourly")
  trades = store.list_trades(limit=5000)
  return build_trade_timing_report(
    trades,
    mode="live",
    since=since,
    since_field="exit",
  )


def build_paper_ab_report(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  """ETH paper vs live mirror + BTC twin live, plus historical Kalshi BTC reference."""
  eth_since = eth_experiment_start_at(cfg) or experiment_epoch_at(loop, cfg, asset="eth")
  kalshi_since = experiment_epoch_at(loop, cfg, asset="btc")
  twin_since = btc_twin_experiment_start_at(cfg)
  kalshi = build_kalshi_live_report(loop, cfg, asset="btc")

  eth_paper_timing = _eth_timing(loop, cfg, kind="hourly", mode="paper")
  eth_live_timing = (
    _eth_timing(loop, cfg, kind="hourly_live", mode="live")
    if eth_live_mirror_active(cfg)
    else {"closed_legs": 0, "total_pnl_usd": 0.0}
  )
  btc_twin_timing = (
    _btc_twin_timing(loop, cfg)
    if btc_twin_live_active(cfg)
    else {"closed_legs": 0, "total_pnl_usd": 0.0}
  )

  return {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "epoch_start_at": kalshi_since.isoformat(),
    "eth_epoch_start_at": eth_since.isoformat() if eth_since else None,
    "btc_twin_epoch_start_at": twin_since.isoformat() if twin_since else None,
    "experiment": {
      "arm": "btc_eth_twin_live_mid_hour",
      "hold_split": dict((cfg.get("pnl_first") or {}).get("paper_profit_exit_hold") or {}),
      "defer_profit_target_minutes": (cfg.get("pnl_first") or {}).get("defer_profit_target_minutes_to_settle"),
      "defer_leg_stop_minutes": (cfg.get("pnl_first") or {}).get("defer_leg_stop_minutes_to_settle"),
      "mid_hour_entry": dict((cfg.get("pnl_first") or {}).get("mid_hour_entry") or {}),
      "paper_experiment": dict(eth_bot_cfg(cfg).get("paper_experiment") or {}),
      "live_mirror": dict(eth_live_mirror_cfg(cfg)),
      "btc_twin_live": dict(btc_twin_live_cfg(cfg)),
      "guards_stripped": {
        "soft_rally": bool((eth_bot_cfg(cfg).get("soft_rally") or {}).get("enabled")),
        "whipsaw_regime_block": bool(
          (eth_bot_cfg(cfg).get("whipsaw_guard") or {}).get("block_entries_when_regime_blocked")
        ),
      },
      "eth_max_hours_to_settle": eth_bot_cfg(cfg).get("max_hours_to_settle_for_entry"),
    },
    "control_reference": {
      "arm": "btc_kalshi_live_fills_historical",
      "note": "Pre-twin Kalshi BTC fills; twin live uses btc_twin_live bot log below",
    },
    "kalshi_live": {
      "closed_legs": kalshi.get("closed_legs"),
      "total_pnl_usd": kalshi.get("total_pnl_usd"),
      "by_exit_type": kalshi.get("by_exit_type"),
      "by_entry_timing": kalshi.get("by_entry_timing"),
    },
    "btc_live": {
      "closed_legs": btc_twin_timing.get("closed_legs"),
      "total_pnl_usd": btc_twin_timing.get("total_pnl_usd"),
      "by_entry_timing": btc_twin_timing.get("by_minutes_to_settle_at_entry"),
      "by_exit_timing": btc_twin_timing.get("by_minutes_to_settle_at_exit"),
      "store_kind": "hourly",
      "enabled": btc_twin_live_active(cfg),
    },
    "eth_paper": {
      "closed_legs": eth_paper_timing.get("closed_legs"),
      "total_pnl_usd": eth_paper_timing.get("total_pnl_usd"),
      "by_entry_timing": eth_paper_timing.get("by_minutes_to_settle_at_entry"),
      "by_exit_timing": eth_paper_timing.get("by_minutes_to_settle_at_exit"),
      "store_kind": "hourly",
    },
    "eth_live": {
      "closed_legs": eth_live_timing.get("closed_legs"),
      "total_pnl_usd": eth_live_timing.get("total_pnl_usd"),
      "by_entry_timing": eth_live_timing.get("by_minutes_to_settle_at_entry"),
      "by_exit_timing": eth_live_timing.get("by_minutes_to_settle_at_exit"),
      "store_kind": "hourly_live",
      "enabled": eth_live_mirror_active(cfg),
    },
    "delta_eth_paper_minus_live_usd": round(
      float(eth_paper_timing.get("total_pnl_usd") or 0) - float(eth_live_timing.get("total_pnl_usd") or 0),
      2,
    ),
    "delta_btc_live_minus_eth_live_usd": round(
      float(btc_twin_timing.get("total_pnl_usd") or 0) - float(eth_live_timing.get("total_pnl_usd") or 0),
      2,
    ),
    "delta_eth_paper_minus_kalshi_usd": round(
      float(eth_paper_timing.get("total_pnl_usd") or 0) - float(kalshi.get("total_pnl_usd") or 0),
      2,
    ),
  }


def write_paper_ab_report(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  payload = build_paper_ab_report(loop, cfg)
  out = paper_ab_output_path(cfg)
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
  return payload
