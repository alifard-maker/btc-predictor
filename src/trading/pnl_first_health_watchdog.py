"""Railway manager health checks — parquet, API, queue, milestones."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _data_root() -> Path:
  return Path(os.getenv("DATA_DIR", "data"))


def _candles_1h_path() -> Path:
  return _data_root() / "candles" / "1h" / "candles.parquet"


def check_candles_health() -> dict[str, Any]:
  path = _candles_1h_path()
  if not path.exists():
    return {"ok": False, "issue": "missing_1h_parquet", "path": str(path)}
  size = path.stat().st_size
  if size == 0:
    return {"ok": False, "issue": "zero_byte_1h_parquet", "path": str(path), "size": size}
  return {"ok": True, "path": str(path), "size": size}


def check_backtest_queue(jobs: list[dict[str, Any]] | None) -> dict[str, Any]:
  jobs = jobs or []
  running = [j for j in jobs if j.get("status") == "running"]
  failed = [j for j in jobs if j.get("status") == "failed"]
  stale_running = [
    j for j in running
    if j.get("requeued_reason") and "stale" in str(j.get("requeued_reason"))
  ]
  issues: list[str] = []
  if failed:
    issues.append(f"failed_jobs:{','.join(str(j.get('id')) for j in failed)}")
  if len(running) > 1:
    issues.append(f"multiple_running:{len(running)}")
  return {
    "ok": not issues,
    "running": [j.get("id") for j in running],
    "failed": [j.get("id") for j in failed],
    "stale_running": [j.get("id") for j in stale_running],
    "issues": issues,
  }


def load_regroup_milestones(cfg: dict[str, Any] | None) -> dict[str, Any]:
  from src.trading.pnl_first_railway_manager import manager_log_dir

  path = manager_log_dir(cfg) / "regroup_milestones.json"
  if not path.exists():
    return {}
  try:
    return json.loads(path.read_text(encoding="utf-8"))
  except Exception:
    return {}


def save_regroup_milestones(cfg: dict[str, Any] | None, milestones: dict[str, Any]) -> None:
  from src.trading.pnl_first_railway_manager import manager_log_dir

  path = manager_log_dir(cfg) / "regroup_milestones.json"
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(milestones, indent=2, default=str), encoding="utf-8")


def mark_regroup_milestone(cfg: dict[str, Any] | None, milestone_id: str, *, detail: dict[str, Any] | None = None) -> dict[str, Any]:
  milestones = load_regroup_milestones(cfg)
  milestones[milestone_id] = {
    "status": "completed",
    "completed_at": datetime.now(timezone.utc).isoformat(),
    "detail": detail or {},
  }
  save_regroup_milestones(cfg, milestones)
  log.info("regroup milestone completed: %s", milestone_id)
  return milestones[milestone_id]


def run_health_watchdog(
  loop: Any,
  cfg: dict[str, Any] | None,
  *,
  jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
  """Non-fatal health snapshot; manager tick uses this every cycle."""
  candles = check_candles_health()
  queue = check_backtest_queue(jobs)
  milestones = load_regroup_milestones(cfg)

  kalshi_report_ok = False
  kalshi_error: str | None = None
  try:
    from src.trading.kalshi_live_report import build_kalshi_live_report

    rep = build_kalshi_live_report(loop, cfg, asset="btc")
    kalshi_report_ok = bool(rep.get("ok"))
    if kalshi_report_ok:
      out = _data_root() / "logs" / "pnl_first_manager" / "kalshi_live_report_latest.json"
      out.parent.mkdir(parents=True, exist_ok=True)
      out.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    else:
      kalshi_error = str(rep.get("error") or "unknown")
  except Exception as exc:
    kalshi_error = f"{type(exc).__name__}:{exc}"

  issues: list[str] = []
  if not candles.get("ok"):
    issues.append(str(candles.get("issue")))
  issues.extend(queue.get("issues") or [])
  if not kalshi_report_ok and kalshi_error:
    issues.append(f"kalshi_report:{kalshi_error}")

  eth_paper: dict[str, Any] = {"ok": True, "skipped": True}
  try:
    from src.trading.eth_paper_experiment import check_eth_paper_harness

    eth_paper = check_eth_paper_harness(loop, cfg)
    if not eth_paper.get("skipped") and not eth_paper.get("ok"):
      issues.extend(eth_paper.get("issues") or [])
    eth_out = _data_root() / "logs" / "pnl_first_manager" / "eth_paper_health_latest.json"
    eth_out.write_text(json.dumps(eth_paper, indent=2, default=str), encoding="utf-8")
  except Exception as exc:
    eth_paper = {"ok": False, "issues": [f"eth_paper_health:{type(exc).__name__}:{exc}"]}
    issues.append(str(eth_paper["issues"][0]))

  health = {
    "ok": not issues,
    "ts": datetime.now(timezone.utc).isoformat(),
    "candles": candles,
    "backtest_queue": queue,
    "kalshi_report_ok": kalshi_report_ok,
    "kalshi_report_error": kalshi_error,
    "eth_paper_harness": eth_paper,
    "regroup_milestones": milestones,
    "issues": issues,
  }
  out = _data_root() / "logs" / "pnl_first_manager" / "health_latest.json"
  out.parent.mkdir(parents=True, exist_ok=True)
  out.write_text(json.dumps(health, indent=2, default=str), encoding="utf-8")
  if issues:
    log.warning("pnl_first health issues: %s", issues)
  return health
