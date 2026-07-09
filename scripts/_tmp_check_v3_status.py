#!/usr/bin/env python3
"""Temporary one-shot status dump for Railway volume artifacts."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def mtime(p: Path) -> str:
  return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()


def main() -> None:
  p = Path("/data/logs/backtest_structure_memory_sweep_v3.progress.json")
  final = Path("/data/logs/backtest_structure_memory_sweep_v3.json")
  log = Path("/data/logs/pnl_first_backtests/phase_a_structure_sweep_v3.log")
  print("progress_exists", p.exists(), "final_exists", final.exists())
  if p.exists():
    print("progress_mtime", mtime(p))
    ck = json.loads(p.read_text())
    res = ck.get("results") or {}
    struct = sum(1 for k in res if str(k).startswith("struct_"))
    fair = (res.get("fair_baseline_gates") or {}).get("total_pnl_usd")
    rows = [r for r in (ck.get("summary") or []) if str(r.get("name", "")).startswith("struct_")]
    rows.sort(key=lambda r: float(r.get("total_pnl_usd") or 0), reverse=True)
    print("updated_at", ck.get("updated_at"))
    print("struct_done", struct, "bars", ck.get("bars"), "span", ck.get("span_days"))
    print("fair", fair)
    print("best", rows[0] if rows else None)
    print("top5", [(r.get("name"), r.get("total_pnl_usd")) for r in rows[:5]])
  if final.exists():
    print("final_mtime", mtime(final))
    f = json.loads(final.read_text())
    print("FINAL fair", f.get("fair_baseline_pnl_usd"))
    print("FINAL best", f.get("best_structure"))
    print("FINAL delta", f.get("delta_vs_fair_usd"))
  if log.exists():
    print("log_mtime", mtime(log), "size", log.stat().st_size)
    lines = log.read_text(errors="replace").splitlines()
    print("LOG_TAIL")
    for ln in lines[-20:]:
      print(ln)
  pb = Path("/data/logs/backtest_pnl_first_walkforward.json")
  print("phase_b_exists", pb.exists())
  if pb.exists():
    d = json.loads(pb.read_text())
    wf = d.get("v1_walk_forward_ml") or {}
    print("phase_b skipped", wf.get("skipped"), "n_folds", wf.get("n_folds"), "has_metrics", bool(wf.get("metrics")))
  for cand in [
    Path("/data/logs/pnl_first_manager/manager_state.json"),
    Path("/data/manager_state.json"),
    Path("/data/logs/manager_state.json"),
  ]:
    if cand.exists():
      print("STATE", cand)
      st = json.loads(cand.read_text())
      for j in st.get("backtest_jobs") or []:
        print(j.get("id"), "->", j.get("status"), j.get("requeued_reason", ""))
      break
  # candle health
  for pq in [
    Path("/data/candles/1h/candles.parquet"),
    Path("/data/candles/15m/candles.parquet"),
    Path("/data/candles/1m/candles.parquet"),
  ]:
    if pq.exists():
      print("parquet", pq, "size", pq.stat().st_size, "mtime", mtime(pq))
    else:
      print("parquet_missing", pq)


if __name__ == "__main__":
  main()
