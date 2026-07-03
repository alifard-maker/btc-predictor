"""Tests for anti-whipsaw entry guards."""

from __future__ import annotations

import sqlite3

from src.trading.bot_whipsaw_guard import (
  WhipsawGuardConfig,
  apply_whipsaw_momentum_contract_cap,
  count_quick_exit_cuts,
  migrate_whipsaw_signal_gates,
  record_spot_against_cut,
  signal_refresh_required,
  whipsaw_hour_entry_blocked,
  whipsaw_pick_entry_blocked,
)
from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.hour_momentum import HourMomentumPolicy, HourMomentumState
from src.trading.live_regime_adaptive import AdaptiveDecision


def _conn() -> sqlite3.Connection:
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.execute(
    """
    CREATE TABLE bot_trades (
      event_ticker TEXT,
      action TEXT,
      status TEXT,
      exit_context_json TEXT
    )
    """
  )
  migrate_whipsaw_signal_gates(conn)
  return conn


def test_count_quick_exit_cuts():
  conn = _conn()
  ctx = '{"quick_exit_applied": true, "exit_reason": "CUT LOSSES"}'
  conn.execute(
    "INSERT INTO bot_trades VALUES (?, 'exit', 'filled', ?)",
    ("EV1", ctx),
  )
  conn.execute(
    "INSERT INTO bot_trades VALUES (?, 'exit', 'filled', ?)",
    ("EV1", '{"quick_exit_applied": false, "exit_reason": "CUT LOSSES"}'),
  )
  assert count_quick_exit_cuts(conn, "EV1") == 1


def test_whipsaw_hour_blocks_regime_and_cut_streak():
  wcfg = WhipsawGuardConfig()
  defense = AdaptiveDecision("defense", ("regime_blocked",))
  assert whipsaw_hour_entry_blocked(wcfg=wcfg, quick_exit_cuts=0, adaptive=defense) == "whipsaw_regime_blocked"
  normal = AdaptiveDecision("defense", ("default_defense",))
  assert whipsaw_hour_entry_blocked(wcfg=wcfg, quick_exit_cuts=3, adaptive=normal) == "whipsaw_cut_streak:3"


def test_signal_refresh_blocks_until_signal_changes():
  conn = _conn()
  record_spot_against_cut(conn, event_ticker="EV1", side="yes", signal="BUY YES")
  assert signal_refresh_required(conn, event_ticker="EV1", side="yes", current_signal="BUY YES")
  assert not signal_refresh_required(conn, event_ticker="EV1", side="yes", current_signal="BUY NO")


def test_conservative_contract_cap():
  estrat = EntryStrategyConfig(max_contracts_per_entry=6)
  policy = HourMomentumPolicy(
    state=HourMomentumState.CONSERVATIVE,
    reasons=("losing",),
    max_entries_per_cycle=2,
    stake_mult=0.8,
    max_stake_per_entry_usd=None,
    late_entry_min_ask_edge_cents=18.0,
    block_late_entry=False,
  )
  out = apply_whipsaw_momentum_contract_cap(estrat, policy, WhipsawGuardConfig())
  assert out.max_contracts_per_entry == 2


def test_scale_in_blocked_when_regime_blocked():
  wcfg = WhipsawGuardConfig()
  defense = AdaptiveDecision("defense", ("regime_blocked",))
  reason = whipsaw_pick_entry_blocked(
    wcfg=wcfg,
    adaptive=defense,
    side="yes",
    signal="BUY YES",
    is_scale_in=True,
    signal_gate_active=False,
  )
  assert reason == "whipsaw_scale_in_regime_blocked"
