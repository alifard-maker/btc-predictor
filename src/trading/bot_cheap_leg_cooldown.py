"""Re-entry guardrail after CHEAP LEG CUT exits (event + label identity)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlite3

# Default: 5 minutes — longer than passive/aggressive re-entry presets (120s / 30s).
DEFAULT_CHEAP_LEG_CUT_COOLDOWN_SECONDS = 300

_CHEAP_LEG_CUT_COOLDOWNS_DDL = """
CREATE TABLE IF NOT EXISTS cheap_leg_cut_cooldowns (
  event_ticker TEXT NOT NULL,
  label TEXT NOT NULL,
  exited_at TEXT NOT NULL,
  cooldown_seconds INTEGER,
  PRIMARY KEY (event_ticker, label)
);
"""


def is_cheap_leg_cut_reason(reason: str | None) -> bool:
  return reason == "CHEAP LEG CUT LOSS"


def market_identity_label(label: str | None, market_ticker: str) -> str:
  """Stable key for event+label cooldowns; fall back to market ticker."""
  text = str(label or "").strip()
  return text or str(market_ticker)


def cheap_leg_cut_cooldown_seconds(cfg: dict[str, Any] | None, *, kind: str) -> int:
  """Read cheap-leg cut re-entry cooldown from hourly.bot or intra_slot.bot config."""
  bot_cfg: dict[str, Any] = {}
  if cfg:
    if kind == "hourly":
      bot_cfg = (cfg.get("hourly") or {}).get("bot") or {}
    else:
      bot_cfg = (cfg.get("intra_slot") or {}).get("bot") or {}
  return int(
    bot_cfg.get("cheap_leg_cut_cooldown_seconds", DEFAULT_CHEAP_LEG_CUT_COOLDOWN_SECONDS)
  )


def resolve_exit_cooldown_seconds(
  settings: Any,
  exit_reason: str | None,
  cfg: dict[str, Any] | None,
  *,
  bot_kind: str,
) -> int:
  """Cooldown to persist on exit; cheap-leg cuts use a fixed longer window."""
  from src.trading.bot_profit_exit import is_profit_exit_reason

  if is_cheap_leg_cut_reason(exit_reason):
    cfg_kind = "slot15" if bot_kind == "slot15" else "hourly"
    return cheap_leg_cut_cooldown_seconds(cfg, kind=cfg_kind)
  if is_profit_exit_reason(exit_reason):
    return int(settings.profit_exit_cooldown_seconds)
  return int(settings.reentry_cooldown_seconds)


def migrate_cheap_leg_cut_cooldowns(conn: sqlite3.Connection) -> None:
  conn.executescript(_CHEAP_LEG_CUT_COOLDOWNS_DDL)


def clear_cheap_leg_cut_cooldowns(conn: sqlite3.Connection) -> None:
  conn.execute("DELETE FROM cheap_leg_cut_cooldowns")


def record_cheap_leg_cut_cooldown(
  conn: sqlite3.Connection,
  event_ticker: str,
  *,
  label: str | None,
  market_ticker: str,
  cooldown_seconds: int,
  exited_at: str | None = None,
) -> None:
  identity = market_identity_label(label, market_ticker)
  now = exited_at or datetime.now(timezone.utc).isoformat()
  conn.execute(
    """
    INSERT INTO cheap_leg_cut_cooldowns (event_ticker, label, exited_at, cooldown_seconds)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(event_ticker, label) DO UPDATE SET
      exited_at = excluded.exited_at,
      cooldown_seconds = excluded.cooldown_seconds
    """,
    (event_ticker, identity, now, cooldown_seconds),
  )


def is_in_cheap_leg_cut_cooldown(
  conn: sqlite3.Connection,
  event_ticker: str,
  *,
  label: str | None,
  market_ticker: str,
  cooldown_seconds: int,
) -> bool:
  identity = market_identity_label(label, market_ticker)
  row = conn.execute(
    "SELECT exited_at, cooldown_seconds FROM cheap_leg_cut_cooldowns "
    "WHERE event_ticker = ? AND label = ?",
    (event_ticker, identity),
  ).fetchone()
  if not row:
    return False
  effective = row["cooldown_seconds"]
  if effective is None:
    effective = cooldown_seconds
  if int(effective) <= 0:
    return False
  exited_at = datetime.fromisoformat(str(row["exited_at"]).replace("Z", "+00:00"))
  if exited_at.tzinfo is None:
    exited_at = exited_at.replace(tzinfo=timezone.utc)
  elapsed = (datetime.now(timezone.utc) - exited_at).total_seconds()
  return elapsed < float(effective)
