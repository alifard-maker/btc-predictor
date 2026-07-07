"""Tests for P&L-first Railway backtest queue hardening."""

from __future__ import annotations

import json
from pathlib import Path

from src.trading import pnl_first_backtest_runner as runner


def test_validate_phase_b_fails_when_walk_forward_skipped():
  ok, reason = runner.validate_job_deliverable(
    "phase_b_walkforward_ml",
    {"v1_walk_forward_ml": {"skipped": True, "reason": "Need at least 550 rows"}},
  )
  assert ok is False
  assert "550" in reason


def test_validate_phase_b_passes_with_metrics():
  ok, reason = runner.validate_job_deliverable(
    "phase_b_walkforward_ml",
    {"v1_walk_forward_ml": {"metrics": {"total_pnl_usd": -10.0}, "n_folds": 3}},
  )
  assert ok is True
  assert reason == ""


def test_validate_v3_requires_span():
  ok, _ = runner.validate_job_deliverable(
    "phase_a_structure_sweep_v3",
    {"span_days": 100, "fair_baseline_pnl_usd": -1},
  )
  assert ok is False
  ok2, _ = runner.validate_job_deliverable(
    "phase_a_structure_sweep_v3",
    {"span_days": 900, "fair_baseline_pnl_usd": -1, "best_structure": {"name": "x"}},
  )
  assert ok2 is True


def test_job_dependencies_block_until_backfill_done(tmp_path, monkeypatch):
  manifest = tmp_path / "logs" / "backfill_1h_btc_manifest.json"
  manifest.parent.mkdir(parents=True)
  manifest.write_text(json.dumps({"bars_after": 26000, "span_days": 1000}), encoding="utf-8")

  monkeypatch.setattr(runner, "_data_root", lambda: tmp_path)

  jobs = [
    {"id": "phase_a_1h_backfill", "status": "completed", "output": "data/logs/backfill_1h_btc_manifest.json"},
    {"id": "phase_b_walkforward_ml", "status": "pending", "depends_on": ["phase_a_1h_backfill"]},
  ]
  assert runner.job_dependencies_met(jobs[1], jobs) is True

  jobs[0]["status"] = "pending"
  assert runner.job_dependencies_met(jobs[1], jobs) is False


def test_repair_false_completion_requeues_phase_b():
  jobs = [
    {
      "id": "phase_b_walkforward_ml",
      "status": "completed",
      "result_preview": {"v1_walk_forward_ml": {"skipped": True}},
    },
  ]
  repaired = runner.repair_false_completions(jobs)
  assert repaired[0]["status"] == "pending"
  assert repaired[0].get("requeued_reason")
