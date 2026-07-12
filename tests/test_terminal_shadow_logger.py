"""Tests for Track B terminal shadow logger."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.trading.terminal_shadow_logger import (
  finalize_terminal_shadow_event,
  maybe_log_terminal_shadow,
  prob_above_strike,
  summarize_track_b_shadow,
  track_b_shadow_active,
)


@pytest.fixture
def shadow_cfg(tmp_path, monkeypatch):
  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  return {
    "pnl_first": {
      "track_b_shadow": {
        "enabled": True,
        "started_at": "2026-07-12T15:30:00+00:00",
        "stats_epoch_at": "2026-07-12T15:30:00+00:00",
        "assets": ["eth"],
        "cadence_seconds": 0,
        "max_hours_to_settle": 0.25,
        "taker_fee_rate": 0.07,
      },
    },
  }


def test_prob_above_strike_near_spot():
  p = prob_above_strike(3000.0, 2990.0, 30.0, 0.1)
  assert 0.5 < p < 0.99


def test_maybe_log_terminal_shadow_in_window(shadow_cfg):
  tab = {
    "ok": True,
    "event": {"event_ticker": "KXETH-26JUL1218"},
    "live": {
      "current_price": 3000.0,
      "terminal_sigma": 25.0,
      "hours_to_settle": 0.12,
      "strategy_threshold": {
        "best_edge": {
          "ticker": "KXETH-26JUL1218-T3000",
          "floor_strike": 3000.0,
          "yes_ask": 0.42,
          "kalshi_mid": 0.40,
          "strike_type": "greater",
        },
      },
    },
  }
  out = maybe_log_terminal_shadow(tab, shadow_cfg, asset="eth")
  assert out and out.get("logged") is True
  assert out.get("edge_cents") is not None


def test_maybe_log_skips_outside_window(shadow_cfg):
  tab = {
    "ok": True,
    "event": {"event_ticker": "KXETH-26JUL1218"},
    "live": {
      "current_price": 3000.0,
      "terminal_sigma": 25.0,
      "hours_to_settle": 0.5,
      "strategy_threshold": {"best_edge": {"floor_strike": 3000.0, "yes_ask": 0.42}},
    },
  }
  out = maybe_log_terminal_shadow(tab, shadow_cfg, asset="eth")
  assert out and out.get("skipped") is True


def test_finalize_and_summarize(shadow_cfg, monkeypatch):
  with tempfile.TemporaryDirectory() as tmp:
    monkeypatch.setenv("DATA_DIR", tmp)
    tab = {
      "ok": True,
      "event": {"event_ticker": "KXETH-26JUL1217"},
      "live": {
        "current_price": 3000.0,
        "terminal_sigma": 25.0,
        "hours_to_settle": 0.1,
        "strategy_threshold": {
          "best_edge": {
            "ticker": "T1",
            "floor_strike": 2990.0,
            "yes_ask": 0.45,
            "kalshi_mid": 0.43,
            "strike_type": "greater",
          },
        },
      },
    }
    maybe_log_terminal_shadow(tab, shadow_cfg, asset="eth")
    fin = finalize_terminal_shadow_event(
      "KXETH-26JUL1217",
      shadow_cfg,
      asset="eth",
      settle_spot=3005.0,
    )
    assert fin and fin.get("finalized") is True
    summary = summarize_track_b_shadow(shadow_cfg, asset="eth")
    assert summary["events_logged"] >= 1
    assert summary["median_edge_cents"] is not None


def test_track_b_inactive():
  assert track_b_shadow_active({}) is False
