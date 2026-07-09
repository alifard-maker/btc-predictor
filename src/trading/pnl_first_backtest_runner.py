"""Railway background backtest queue + live P&L audit for P&L-first manager."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_RUN_LOCK = threading.Lock()
_ACTIVE_PROC: subprocess.Popen[str] | None = None
_ACTIVE_JOB: dict[str, Any] | None = None

DEFAULT_JOBS: list[dict[str, Any]] = [
  {
    "id": "phase_a_1h_backfill",
    "script": "scripts/backfill_1h_candles_railway.py",
    "output": "data/logs/backfill_1h_btc_manifest.json",
    "milestone": "phase_a_1h_history_backfill",
    "depends_on": [],
  },
  {
    "id": "phase_a_structure_sweep_v3",
    "script": "scripts/backtest_structure_memory_sweep_v3.py",
    "output": "data/logs/backtest_structure_memory_sweep_v3.json",
    "milestone": "phase_a_fair_baseline_and_structure_tune_v3",
    "depends_on": ["phase_a_1h_backfill"],
  },
  {
    "id": "phase_b_walkforward_ml",
    "script": "scripts/backtest_pnl_first_walkforward.py",
    "output": "data/logs/backtest_pnl_first_walkforward.json",
    "milestone": "phase_b_walkforward_ml_baseline",
    "depends_on": ["phase_a_1h_backfill", "phase_a_structure_sweep_v3"],
  },
  {
    "id": "phase_a_midhour_exits",
    "script": "scripts/backtest_pnl_first_midhour_exits.py",
    "output": "data/logs/backtest_pnl_first_midhour_exits.json",
    "milestone": "phase_a_midhour_entry_and_defer_exits",
    "depends_on": ["phase_a_1h_backfill"],
  },
  {
    "id": "phase2_eth_1h_backfill",
    "script": "scripts/backfill_1h_candles_railway.py --asset eth",
    "output": "data/logs/backfill_1h_eth_manifest.json",
    "milestone": "phase2_eth_1h_history_backfill",
    "depends_on": [],
  },
]

_STALE_LOG_SECONDS = 45 * 60
_MIN_BACKFILL_BARS = 5000
_MIN_V3_SPAN_DAYS = 300.0


def backtest_log_dir(cfg: dict[str, Any] | None = None) -> Path:
  del cfg
  base = Path(os.getenv("DATA_DIR", "data"))
  d = base / "logs" / "pnl_first_backtests"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _project_root() -> Path:
  return Path(__file__).resolve().parents[2]


def _data_root() -> Path:
  return Path(os.getenv("DATA_DIR", str(_project_root() / "data")))


def _resolve_job_output(rel_path: str) -> Path:
  p = Path(rel_path)
  if p.parts[:1] == ("data",):
    return _data_root().joinpath(*p.parts[1:])
  return _project_root() / p


def _job_by_id(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
  return next((j for j in jobs if j.get("id") == job_id), None)


def _job_sort_key(job: dict[str, Any]) -> int:
  job_id = str(job.get("id") or "")
  for i, spec in enumerate(DEFAULT_JOBS):
    if spec["id"] == job_id:
      return i
  return len(DEFAULT_JOBS)


def job_dependencies_met(job: dict[str, Any], jobs: list[dict[str, Any]]) -> bool:
  """True when all depends_on jobs completed with valid deliverables."""
  for dep_id in job.get("depends_on") or []:
    dep = _job_by_id(jobs, str(dep_id))
    if not dep or dep.get("status") != "completed":
      return False
    ok, _ = validate_job_deliverable(str(dep_id), dep.get("result_preview"), dep.get("output"))
    if not ok:
      return False
  return True


def validate_job_deliverable(
  job_id: str,
  preview: dict[str, Any] | None,
  output_rel: str | None = None,
) -> tuple[bool, str]:
  """Return (ok, reason). Jobs must meet deliverable contracts — no silent skips."""
  preview = preview or {}
  if job_id == "phase_a_1h_backfill":
    _, manifest = _resolve_backfill_manifest()
    if not manifest:
      return False, "backfill_manifest_missing"
    bars = int(manifest.get("bars_after") or 0)
    span = float(manifest.get("span_days") or 0)
    if bars < _MIN_BACKFILL_BARS and span < _MIN_V3_SPAN_DAYS:
      return False, f"backfill_insufficient:bars={bars},span_days={span}"
    return True, ""

  if job_id == "phase2_eth_1h_backfill":
    out = _resolve_job_output(str(output_rel or "data/logs/backfill_1h_eth_manifest.json"))
    if not out.exists():
      return False, "eth_backfill_manifest_missing"
    return True, ""

  if job_id == "phase_b_walkforward_ml":
    wf = preview.get("v1_walk_forward_ml") or {}
    if wf.get("skipped"):
      return False, str(wf.get("reason") or "walk_forward_skipped")
    if not wf.get("metrics") and not wf.get("n_folds"):
      return False, "walk_forward_missing_metrics"
    return True, ""

  if job_id in ("phase_a_structure_sweep_v2", "phase_a_structure_sweep_v3"):
    span = float(preview.get("span_days") or 0)
    bars = int(preview.get("bars") or 0)
    if job_id == "phase_a_structure_sweep_v3" and span < _MIN_V3_SPAN_DAYS and bars < _MIN_BACKFILL_BARS:
      return False, f"structure_sweep_short_history:span_days={span},bars={bars}"
    if preview.get("fair_baseline_pnl_usd") is None and not preview.get("fair_baseline_gates"):
      return False, "structure_sweep_missing_fair_baseline"
    if not preview.get("best_structure"):
      return False, "structure_sweep_missing_best_structure"
    return True, ""

  return True, ""


def repair_false_completions(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Re-queue jobs marked completed but missing required deliverables."""
  now = datetime.now(timezone.utc).isoformat()
  for job in jobs:
    if job.get("status") != "completed":
      continue
    ok, reason = validate_job_deliverable(
      str(job.get("id") or ""),
      job.get("result_preview"),
      job.get("output"),
    )
    if ok:
      continue
    job["status"] = "pending"
    job["requeued_at"] = now
    job["requeued_reason"] = reason
    job.pop("finished_at", None)
    log.warning("backtest job %s requeued (false completion): %s", job.get("id"), reason)
  return jobs


_BACKFILL_MANIFESTS = (
  "data/logs/backfill_1h_btc_manifest.json",
  "data/logs/backfill_1h_manifest.json",
)


def _resolve_backfill_manifest() -> tuple[Path, dict[str, Any] | None]:
  for rel in _BACKFILL_MANIFESTS:
    out = _resolve_job_output(rel)
    if not out.exists():
      continue
    try:
      return out, json.loads(out.read_text(encoding="utf-8"))
    except Exception:
      continue
  return _resolve_job_output(_BACKFILL_MANIFESTS[0]), None


def _sync_completed_from_artifacts(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Mark jobs completed when output artifacts already exist (post-repair / deploy)."""
  now = datetime.now(timezone.utc).isoformat()
  for job in jobs:
    if job.get("status") not in ("pending", "running", "failed"):
      continue
    job_id = str(job.get("id") or "")
    preview = None
    if job_id == "phase_a_1h_backfill":
      _, preview = _resolve_backfill_manifest()
    else:
      out = _resolve_job_output(str(job.get("output") or ""))
      if out.exists():
        try:
          preview = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
          preview = None
    ok, _ = validate_job_deliverable(job_id, preview, job.get("output"))
    if ok:
      job["status"] = "completed"
      job["finished_at"] = job.get("finished_at") or now
      if preview:
        job["result_preview"] = preview
      log.info("backtest job %s marked completed from existing artifact", job_id)
  return jobs


def ensure_backtest_queue(cfg: dict[str, Any] | None) -> list[dict[str, Any]]:
  from src.trading.pnl_first_railway_manager import load_manager_state, save_manager_state

  state = load_manager_state(cfg)
  jobs = state.get("backtest_jobs")
  now = datetime.now(timezone.utc).isoformat()

  if not isinstance(jobs, list) or not jobs:
    queued = [{**j, "status": "pending", "queued_at": now} for j in DEFAULT_JOBS]
    state["backtest_jobs"] = queued
    state["backtest_queue_initialized_at"] = now
    save_manager_state(state, cfg)
    log.info("pnl_first backtest queue initialized (%d jobs)", len(queued))
    return repair_false_completions(_requeue_stale_running(cfg, queued))

  existing_ids = {str(j.get("id")) for j in jobs if j.get("id")}
  for spec in DEFAULT_JOBS:
    if spec["id"] not in existing_ids:
      jobs.append({**spec, "status": "pending", "queued_at": now})
    else:
      for job in jobs:
        if job.get("id") == spec["id"]:
          for key in ("depends_on", "output", "script", "milestone"):
            if key in spec:
              if key == "depends_on":
                job[key] = list(spec["depends_on"])
              elif key == "output" and job.get("output") != spec["output"]:
                job[key] = spec[key]
              elif key not in job:
                job[key] = spec[key]

  jobs = repair_false_completions(jobs)
  jobs = _sync_completed_from_artifacts(jobs)
  state["backtest_jobs"] = jobs
  state["backtest_queue_updated_at"] = now
  save_manager_state(state, cfg)
  return _requeue_stale_running(cfg, jobs)


def _requeue_stale_running(cfg: dict[str, Any] | None, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Reset orphaned running jobs (deploy kill or log heartbeat timeout)."""
  from src.trading.pnl_first_railway_manager import load_manager_state, save_manager_state

  with _RUN_LOCK:
    if _ACTIVE_PROC is not None:
      return jobs

  now = datetime.now(timezone.utc)
  now_iso = now.isoformat()
  stale: list[dict[str, Any]] = []

  for j in jobs:
    if j.get("status") != "running":
      continue
    job_id = str(j.get("id") or "")
    log_path = _job_log_path(job_id)
    reason = "orphaned_no_active_proc"
    if log_path.exists():
      age = now.timestamp() - log_path.stat().st_mtime
      if age >= _STALE_LOG_SECONDS:
        reason = f"log_stale_{int(age)}s"
    stale.append(j)
    j["status"] = "pending"
    j["requeued_at"] = now_iso
    j["requeued_reason"] = reason
    j.pop("started_at", None)

  if not stale:
    return jobs

  state = load_manager_state(cfg)
  state["backtest_jobs"] = jobs
  save_manager_state(state, cfg)
  log.warning(
    "pnl_first backtest requeued stale running job(s): %s",
    [(s.get("id"), s.get("requeued_reason")) for s in stale],
  )
  return jobs


def _persist_jobs(cfg: dict[str, Any] | None, jobs: list[dict[str, Any]]) -> None:
  from src.trading.pnl_first_railway_manager import load_manager_state, save_manager_state

  state = load_manager_state(cfg)
  state["backtest_jobs"] = jobs
  save_manager_state(state, cfg)


def _job_log_path(job_id: str) -> Path:
  return backtest_log_dir() / f"{job_id}.log"


def tick_backtest_runner(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
  """Start or poll one background backtest job (non-blocking)."""
  global _ACTIVE_PROC, _ACTIVE_JOB
  jobs = ensure_backtest_queue(cfg)

  with _RUN_LOCK:
    if _ACTIVE_PROC is not None:
      rc = _ACTIVE_PROC.poll()
      if rc is None:
        return {"action": "backtest_running", "job": _ACTIVE_JOB}
      log_path = _job_log_path(str(_ACTIVE_JOB.get("id")))
      finished = datetime.now(timezone.utc).isoformat()
      for j in jobs:
        if j.get("id") == _ACTIVE_JOB.get("id"):
          preview: dict[str, Any] | None = None
          out = _resolve_job_output(str(j.get("output", "")))
          if out.exists():
            j["output_path"] = str(out)
            try:
              preview = json.loads(out.read_text(encoding="utf-8"))
              j["result_preview"] = preview
            except Exception:
              preview = None
          deliverable_ok, deliverable_reason = validate_job_deliverable(
            str(j.get("id") or ""),
            preview,
            j.get("output"),
          )
          if rc != 0:
            j["status"] = "failed"
            j["error"] = f"exit_code_{rc}"
            j["finished_at"] = finished
          elif not deliverable_ok:
            j["status"] = "pending"
            j["requeued_at"] = finished
            j["requeued_reason"] = deliverable_reason
            j.pop("finished_at", None)
            log.warning(
              "backtest job %s invalid deliverable — requeued: %s",
              j.get("id"),
              deliverable_reason,
            )
          else:
            j["status"] = "completed"
            j["finished_at"] = finished
          j["exit_code"] = rc
          j["log_path"] = str(log_path)
      _persist_jobs(cfg, jobs)
      _ACTIVE_PROC = None
      job_id = _ACTIVE_JOB.get("id")
      _ACTIVE_JOB = None
      log.info("pnl_first backtest job %s finished rc=%s", job_id, rc)
      return {"action": "backtest_finished", "job_id": job_id, "exit_code": rc}

    next_job = next(
      (
        j
        for j in sorted(jobs, key=_job_sort_key)
        if j.get("status") == "pending" and job_dependencies_met(j, jobs)
      ),
      None,
    )
    if not next_job:
      return {"action": "backtest_queue_idle", "completed": sum(1 for j in jobs if j.get("status") == "completed")}

    root = _project_root()
    script_tokens = str(next_job["script"]).split()
    script = root / script_tokens[0]
    script_args = script_tokens[1:]
    if not script.exists():
      next_job["status"] = "failed"
      next_job["error"] = f"missing script {script}"
      _persist_jobs(cfg, jobs)
      return {"action": "backtest_failed", "job": next_job}

    log_path = _job_log_path(str(next_job["id"]))
    next_job["status"] = "running"
    next_job["started_at"] = datetime.now(timezone.utc).isoformat()
    _persist_jobs(cfg, jobs)

    out_f = log_path.open("w", encoding="utf-8")
    _ACTIVE_PROC = subprocess.Popen(
      [sys.executable, "-u", str(script), *script_args],
      cwd=root,
      stdout=out_f,
      stderr=subprocess.STDOUT,
      env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    _ACTIVE_JOB = next_job
    log.info("pnl_first backtest started: %s", next_job["id"])
    return {"action": "backtest_started", "job": next_job}


def run_live_pnl_audit(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  """Kalshi vs bot hour P&L + periodic fill sync (runs on Railway manager)."""
  from src.trading.pnl_first_railway_manager import _stats_epoch

  audit: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat(), "issues": []}
  tab = loop.daily_prediction()
  event = (tab.get("event") or {}).get("event_ticker") if tab.get("ok") else None
  audit["event_ticker"] = event

  store = loop.hourly_bot_store("btc")
  settings = store.get_settings()
  audit["btc_live"] = settings.mode == "live" and settings.enabled

  if event:
    tab_ok = tab if tab.get("ok") else None
    try:
      status = loop.hourly_bot_status("btc", tab_ok)
      hs = status.get("hour_summary") or {}
    except Exception:
      hs = {}
    bot_hr = float(hs.get("realized_pnl_usd") or 0)
    open_legs = len(store.open_positions(event) or [])
    audit["bot_hour_realized_usd"] = bot_hr
    audit["open_legs"] = open_legs
    audit["hour_partial"] = open_legs > 0

    kalshi = loop.kalshi
    if kalshi and getattr(kalshi, "authenticated", False):
      from src.trading.kalshi_fill_sync import summarize_kalshi_experiment_fills

      since = _stats_epoch(cfg)
      sm = summarize_kalshi_experiment_fills(
        kalshi, since=since, asset="btc", event_ticker=str(event),
      )
      kalshi_hr = float(sm.get("total_pnl_usd") or 0)
      audit["kalshi_hour_pnl_usd"] = kalshi_hr
      audit["kalshi_hour_closed"] = sm.get("closed_trades")
      if not audit["hour_partial"] and abs(bot_hr - kalshi_hr) > 0.12:
        audit["issues"].append({
          "code": "kalshi_hour_pnl_drift",
          "bot_usd": bot_hr,
          "kalshi_usd": kalshi_hr,
          "delta_usd": round(bot_hr - kalshi_hr, 2),
        })

  log_dir = backtest_log_dir(cfg)
  issues_path = log_dir / "live_audit_issues.jsonl"
  if audit.get("issues"):
    with issues_path.open("a", encoding="utf-8") as f:
      f.write(json.dumps(audit, default=str) + "\n")

  audit_path = log_dir / "live_audit_latest.json"
  audit_path.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")

  try:
    from src.trading.exit_mark_fill_audit import run_exit_mark_fill_audit

    audit["exit_mark_fill"] = run_exit_mark_fill_audit(loop, cfg)
  except Exception as exc:
    log.warning("exit_mark_fill audit failed: %s", exc)
    audit["exit_mark_fill"] = {"error": str(exc)}

  audit_path.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
  return audit


def maybe_sync_kalshi_periodic(loop: Any, cycle: int, *, every: int = 15) -> dict[str, Any] | None:
  if cycle % every != 0:
    return None
  try:
    return loop.sync_hourly_kalshi_fills("btc", force=True)
  except Exception as exc:
    log.warning("periodic kalshi sync failed: %s", exc)
    return {"ok": False, "error": str(exc)}


def _v3_progress_snapshot() -> dict[str, Any] | None:
  """Partial v3 sweep from checkpoint (available before final JSON)."""
  from src.trading.structure_sweep_ranking import best_structure_variant, full_horizon_struct_items

  for rel in (
    "data/logs/backtest_structure_memory_sweep_v3.json",
    "data/logs/backtest_structure_memory_sweep_v3.progress.json",
  ):
    path = _resolve_job_output(rel)
    if not path.exists():
      continue
    try:
      ckpt = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
      continue
    results = ckpt.get("results") or {}
    fair = results.get("fair_baseline_gates") or {}
    fair_pnl = fair.get("total_pnl_usd")
    full_items = full_horizon_struct_items(results, fair=fair)
    best_pair = best_structure_variant(results, fair=fair)
    top_rows = []
    for name, row in full_items[:5]:
      top_rows.append({
        "name": name,
        "total_pnl_usd": row.get("total_pnl_usd"),
        "filled_enters": row.get("filled_enters"),
        "expectancy_per_fill_usd": row.get("expectancy_per_fill_usd"),
        "win_rate": row.get("win_rate"),
        "hours_with_fills": row.get("hours_with_fills"),
        "hours_simulated": row.get("hours_simulated"),
      })
    best_row = None
    delta = None
    if best_pair:
      best_name, best_result = best_pair
      best_row = {"name": best_name, "total_pnl_usd": best_result.get("total_pnl_usd"), "hours_simulated": best_result.get("hours_simulated")}
      if fair_pnl is not None:
        delta = round(float(best_result.get("total_pnl_usd") or 0) - float(fair_pnl), 2)
    struct_done = sum(1 for k in results if str(k).startswith("struct_"))
    return {
      "updated_at": ckpt.get("updated_at") or ckpt.get("generated_at"),
      "bars": ckpt.get("bars"),
      "span_days": ckpt.get("span_days"),
      "struct_variants_done": struct_done,
      "struct_variants_total": 233,
      "full_horizon_done": len(full_items),
      "fair_baseline_pnl_usd": fair_pnl,
      "best_structure_so_far": best_row,
      "delta_vs_fair_usd": delta,
      "top_structures": top_rows,
      "full_horizon_only": True,
      "checkpoint_path": str(path),
    }
  return None


def backtest_status(cfg: dict[str, Any] | None) -> dict[str, Any]:
  jobs = ensure_backtest_queue(cfg)
  with _RUN_LOCK:
    running = _ACTIVE_JOB
  out: dict[str, Any] = {
    "jobs": jobs,
    "running": running,
    "log_dir": str(backtest_log_dir(cfg)),
  }
  v3_progress = _v3_progress_snapshot()
  if v3_progress:
    out["v3_progress"] = v3_progress
  return out
