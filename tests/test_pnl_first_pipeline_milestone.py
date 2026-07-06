"""Tests for P&L-first pipeline milestone (gate-stack proof)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.trading.pnl_first_pipeline_milestone import (
  REQUIRED_SESSION_GATES,
  classify_skip_reason,
  compute_pipeline_milestone,
  finalize_pipeline_hour,
  note_pipeline_preflight,
  record_pipeline_cycle,
  sync_pipeline_hour_boundary,
)
from src.trading.pnl_first_railway_manager import load_manager_state, save_manager_state


@pytest.fixture
def cfg():
  return {
    "pnl_first": {"milestone_pipeline_hours": 3},
    "hourly": {"bot": {"live_mechanics_profile": "pnl_first"}},
  }


@pytest.fixture
def state_path(tmp_path, monkeypatch):
  log_dir = tmp_path / "logs" / "pnl_first_manager"
  log_dir.mkdir(parents=True)
  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  return log_dir / "manager_state.json"


def test_classify_skip_reason_regime_and_edge():
  assert "regime" in classify_skip_reason("pnl_first_regime_blocked:move")
  flags = classify_skip_reason("ask_edge_too_low:6c")
  assert "edge" in flags
  assert "regime_clear" in flags
  assert "entry_fill" in classify_skip_reason(None, entry_filled=True)


def test_pipeline_hour_streak_and_session_gates(cfg, state_path):
  save_manager_state({}, cfg)

  record_pipeline_cycle(
    cfg,
    event_ticker="KXBTCD-H1",
    skip_reason="pnl_first_regime_blocked:chop",
    mode="live",
    kind="hourly",
    asset="btc",
  )
  for _ in range(4):
    record_pipeline_cycle(
      cfg,
      event_ticker="KXBTCD-H1",
      skip_reason="pnl_first_regime_blocked:chop",
      mode="live",
      kind="hourly",
      asset="btc",
    )
  note_pipeline_preflight(cfg, ok=True)
  record_pipeline_cycle(
    cfg,
    event_ticker="KXBTCD-H1",
    skip_reason="ask_edge_too_low:8c",
    mode="live",
    kind="hourly",
    asset="btc",
  )
  record_pipeline_cycle(
    cfg,
    event_ticker="KXBTCD-H1",
    skip_reason="pnl_first_no_entry_price",
    mode="live",
    kind="hourly",
    asset="btc",
  )

  finalize_pipeline_hour(cfg, "KXBTCD-H1", live=True)
  out = compute_pipeline_milestone(cfg)
  assert out["consecutive_pipeline_hours"] == 1
  assert set(out["session_gate_coverage"]) >= {"regime", "edge", "taker", "preflight"}
  assert out["missing_session_gates"] == []
  assert out["milestone_mode"] == "pipeline"


def test_pipeline_streak_resets_on_bad_hour(cfg, state_path):
  save_manager_state({}, cfg)

  def _complete_hour(event: str, *, preflight: bool = True) -> None:
    record_pipeline_cycle(
      cfg,
      event_ticker=event,
      skip_reason="pnl_first_regime_blocked:chop",
      mode="live",
      kind="hourly",
      asset="btc",
    )
    if preflight:
      note_pipeline_preflight(cfg, ok=True)
    finalize_pipeline_hour(cfg, event, live=True)

  _complete_hour("KXBTCD-H1")
  _complete_hour("KXBTCD-H2")
  assert compute_pipeline_milestone(cfg)["consecutive_pipeline_hours"] == 2

  note_pipeline_preflight(cfg, ok=False)
  record_pipeline_cycle(
    cfg,
    event_ticker="KXBTCD-H3",
    skip_reason="pnl_first_regime_blocked:chop",
    mode="live",
    kind="hourly",
    asset="btc",
  )
  finalize_pipeline_hour(cfg, "KXBTCD-H3", live=True)
  assert compute_pipeline_milestone(cfg)["consecutive_pipeline_hours"] == 0


def test_sync_pipeline_hour_boundary(cfg, state_path):
  save_manager_state({}, cfg)
  record_pipeline_cycle(
    cfg,
    event_ticker="KXBTCD-H1",
    skip_reason="pnl_first_regime_blocked:chop",
    mode="live",
    kind="hourly",
    asset="btc",
  )
  note_pipeline_preflight(cfg, ok=True)
  sync_pipeline_hour_boundary(cfg, "KXBTCD-H2")
  out = compute_pipeline_milestone(cfg)
  assert out["consecutive_pipeline_hours"] == 1
  state = json.loads(state_path.read_text())
  assert state["pipeline_milestone"]["current_hour"] is None


def test_milestone_achieved_requires_hours_and_gate_coverage(cfg, state_path):
  cfg["pnl_first"]["milestone_pipeline_hours"] = 2
  save_manager_state({}, cfg)

  def _complete(event: str) -> None:
    record_pipeline_cycle(
      cfg,
      event_ticker=event,
      skip_reason="pnl_first_regime_blocked:chop",
      mode="live",
      kind="hourly",
      asset="btc",
    )
    note_pipeline_preflight(cfg, ok=True)
    record_pipeline_cycle(
      cfg,
      event_ticker=event,
      skip_reason="ask_edge_too_low:6c",
      mode="live",
      kind="hourly",
      asset="btc",
    )
    record_pipeline_cycle(
      cfg,
      event_ticker=event,
      skip_reason="pnl_first_no_entry_price",
      mode="live",
      kind="hourly",
      asset="btc",
    )
    finalize_pipeline_hour(cfg, event, live=True)

  _complete("KXBTCD-H1")
  out = compute_pipeline_milestone(cfg)
  assert out["milestone_achieved"] is False
  assert out["consecutive_pipeline_hours"] == 1

  _complete("KXBTCD-H2")
  out = compute_pipeline_milestone(cfg)
  assert out["milestone_achieved"] is True
  assert REQUIRED_SESSION_GATES.issubset(set(out["session_gate_coverage"]))
