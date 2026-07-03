"""Anti-whipsaw guards: cut streak pause, regime block, signal refresh after spot-against cuts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.hour_momentum import HourMomentumPolicy, HourMomentumState
from src.trading.live_regime_adaptive import AdaptiveDecision

_SIGNAL_GATE_DDL = """
CREATE TABLE IF NOT EXISTS whipsaw_signal_gates (
  event_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  signal_at_cut TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (event_ticker, side)
);
"""


@dataclass(frozen=True)
class WhipsawGuardConfig:
  enabled: bool = True
  max_quick_exit_cuts_per_hour: int = 3
  pause_all_entries_after_cut_streak: bool = True
  block_entries_when_regime_blocked: bool = True
  block_scale_in_when_regime_blocked: bool = True
  require_signal_refresh_after_spot_against_cut: bool = True
  conservative_max_contracts_per_entry: int = 2
  event_cut_cooldown_seconds: int = 600

  @classmethod
  def from_cfg(cls, cfg: dict[str, Any] | None, *, kind: str = "hourly") -> WhipsawGuardConfig:
    if not cfg:
      return cls(enabled=False)
    if kind == "slot15":
      bot = (cfg.get("intra_slot") or {}).get("bot") or {}
    else:
      bot = (cfg.get("hourly") or {}).get("bot") or {}
    raw = dict(bot.get("whipsaw_guard") or {})
    if not raw:
      return cls(enabled=False)
    kw: dict[str, Any] = {"enabled": bool(raw.get("enabled", True))}
    for field in (
      "max_quick_exit_cuts_per_hour",
      "pause_all_entries_after_cut_streak",
      "block_entries_when_regime_blocked",
      "block_scale_in_when_regime_blocked",
      "require_signal_refresh_after_spot_against_cut",
      "conservative_max_contracts_per_entry",
      "event_cut_cooldown_seconds",
    ):
      if field in raw:
        kw[field] = raw[field]
    return replace(cls(), **kw)


def migrate_whipsaw_signal_gates(conn: sqlite3.Connection) -> None:
  conn.executescript(_SIGNAL_GATE_DDL)


def count_quick_exit_cuts(conn: sqlite3.Connection, event_ticker: str) -> int:
  rows = conn.execute(
    """
    SELECT exit_context_json FROM bot_trades
    WHERE event_ticker = ? AND action = 'exit' AND status IN ('filled', 'reconciled')
    """,
    (event_ticker,),
  ).fetchall()
  count = 0
  for row in rows:
    raw = row["exit_context_json"] if hasattr(row, "keys") else row[0]
    if not raw:
      continue
    try:
      ctx = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
      continue
    if not ctx.get("quick_exit_applied"):
      continue
    if str(ctx.get("exit_reason") or "") != "CUT LOSSES":
      continue
    count += 1
  return count


def record_spot_against_cut(
  conn: sqlite3.Connection,
  *,
  event_ticker: str,
  side: str,
  signal: str | None,
) -> None:
  sig = str(signal or "").strip()
  if not sig:
    return
  now = datetime.now(timezone.utc).isoformat()
  conn.execute(
    """
    INSERT INTO whipsaw_signal_gates (event_ticker, side, signal_at_cut, created_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(event_ticker, side) DO UPDATE SET
      signal_at_cut = excluded.signal_at_cut,
      created_at = excluded.created_at
    """,
    (event_ticker, side.lower(), sig, now),
  )


def clear_signal_gate(conn: sqlite3.Connection, event_ticker: str, side: str) -> None:
  conn.execute(
    "DELETE FROM whipsaw_signal_gates WHERE event_ticker = ? AND side = ?",
    (event_ticker, side.lower()),
  )


def signal_refresh_required(
  conn: sqlite3.Connection,
  *,
  event_ticker: str,
  side: str,
  current_signal: str | None,
) -> bool:
  row = conn.execute(
    "SELECT signal_at_cut FROM whipsaw_signal_gates WHERE event_ticker = ? AND side = ?",
    (event_ticker, side.lower()),
  ).fetchone()
  if not row:
    return False
  blocked = row["signal_at_cut"] if hasattr(row, "keys") else row[0]
  cur = str(current_signal or "").strip()
  if not cur:
    return True
  if cur != str(blocked):
    clear_signal_gate(conn, event_ticker, side)
    return False
  return True


def whipsaw_hour_entry_blocked(
  *,
  wcfg: WhipsawGuardConfig,
  quick_exit_cuts: int,
  adaptive: AdaptiveDecision,
) -> str | None:
  if not wcfg.enabled:
    return None
  if (
    wcfg.pause_all_entries_after_cut_streak
    and quick_exit_cuts >= wcfg.max_quick_exit_cuts_per_hour
  ):
    return f"whipsaw_cut_streak:{quick_exit_cuts}"
  if wcfg.block_entries_when_regime_blocked and "regime_blocked" in adaptive.reasons:
    return "whipsaw_regime_blocked"
  return None


def whipsaw_pick_entry_blocked(
  *,
  wcfg: WhipsawGuardConfig,
  adaptive: AdaptiveDecision,
  side: str,
  signal: str | None,
  is_scale_in: bool,
  signal_gate_active: bool,
  block_scale_in_after_quick_exit_cut: bool = False,
  quick_exit_cuts: int = 0,
  mirror_trial_scale_in: bool = False,
) -> str | None:
  if not wcfg.enabled:
    return None
  if (
    is_scale_in
    and wcfg.block_scale_in_when_regime_blocked
    and "regime_blocked" in adaptive.reasons
    and not mirror_trial_scale_in
  ):
    return "whipsaw_scale_in_regime_blocked"
  if (
    block_scale_in_after_quick_exit_cut
    and is_scale_in
    and quick_exit_cuts > 0
  ):
    return "whipsaw_scale_in_after_quick_exit_cut"
  if (
    wcfg.require_signal_refresh_after_spot_against_cut
    and signal_gate_active
  ):
    return f"whipsaw_signal_refresh_required:{signal or 'unknown'}"
  return None


def apply_whipsaw_momentum_contract_cap(
  estrat: EntryStrategyConfig,
  policy: HourMomentumPolicy | None,
  wcfg: WhipsawGuardConfig,
) -> EntryStrategyConfig:
  if not wcfg.enabled or policy is None:
    return estrat
  if policy.state != HourMomentumState.CONSERVATIVE:
    return estrat
  cap = int(wcfg.conservative_max_contracts_per_entry)
  if cap <= 0:
    return estrat
  current = int(estrat.max_contracts_per_entry or 0)
  if current <= 0:
    return replace(estrat, max_contracts_per_entry=cap)
  return replace(estrat, max_contracts_per_entry=min(current, cap))
