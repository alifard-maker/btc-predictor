"""Re-entry guardrail after CHEAP LEG CUT exits (event + label identity)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlite3

# Default: 5 minutes — longer than passive/aggressive re-entry presets (120s / 30s).
DEFAULT_CHEAP_LEG_CUT_COOLDOWN_SECONDS = 300
# Default: 10 minutes on event+label after CUT LOSSES (hourly); longer late in the hour.
DEFAULT_CUT_LOSS_LABEL_COOLDOWN_SECONDS = 600
DEFAULT_CUT_LOSS_LATE_HOUR_MIN_HOURS = 10.0 / 60.0

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


def is_label_cut_cooldown_reason(reason: str | None) -> bool:
  """Exits that block re-entry on the same event + label identity."""
  return is_cheap_leg_cut_reason(reason) or reason == "CUT LOSSES"


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


def _bot_cfg_for_kind(cfg: dict[str, Any] | None, kind: str) -> dict[str, Any]:
  if not cfg:
    return {}
  if kind == "hourly":
    return (cfg.get("hourly") or {}).get("bot") or {}
  return (cfg.get("intra_slot") or {}).get("bot") or {}


def cut_loss_label_cooldown_seconds(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  hours_to_settle: float | None = None,
) -> int:
  """Re-entry cooldown on event+label after CUT LOSSES; rest-of-hour when late."""
  bot_cfg = _bot_cfg_for_kind(cfg, kind)
  base = int(
    bot_cfg.get("cut_loss_label_cooldown_seconds", DEFAULT_CUT_LOSS_LABEL_COOLDOWN_SECONDS)
  )
  late_min = float(
    bot_cfg.get("cut_loss_late_hour_min_hours", DEFAULT_CUT_LOSS_LATE_HOUR_MIN_HOURS)
  )
  if hours_to_settle is not None and hours_to_settle < late_min:
    rest = int(max(0.0, hours_to_settle) * 3600.0) + 30
    return max(base, rest)
  return base


def resolve_label_reentry_cooldown_seconds(
  exit_reason: str | None,
  cfg: dict[str, Any] | None,
  *,
  bot_kind: str,
  hours_to_settle: float | None = None,
) -> int:
  """Cooldown stored on event+label after cheap-leg or CUT LOSSES exits."""
  cfg_kind = "slot15" if bot_kind == "slot15" else "hourly"
  if is_cheap_leg_cut_reason(exit_reason):
    return cheap_leg_cut_cooldown_seconds(cfg, kind=cfg_kind)
  if exit_reason == "CUT LOSSES":
    return cut_loss_label_cooldown_seconds(
      cfg, kind=cfg_kind, hours_to_settle=hours_to_settle,
    )
  return 0


def resolve_exit_cooldown_seconds(
  settings: Any,
  exit_reason: str | None,
  cfg: dict[str, Any] | None,
  *,
  bot_kind: str,
  hours_to_settle: float | None = None,
  mode: str = "paper",
) -> int:
  """Cooldown to persist on exit; cheap-leg cuts use a fixed longer window."""
  from src.trading.bot_profit_exit import is_profit_exit_reason

  if is_cheap_leg_cut_reason(exit_reason):
    cfg_kind = "slot15" if bot_kind == "slot15" else "hourly"
    return cheap_leg_cut_cooldown_seconds(cfg, kind=cfg_kind)
  if exit_reason == "CUT LOSSES":
    cfg_kind = "slot15" if bot_kind == "slot15" else "hourly"
    label_cd = cut_loss_label_cooldown_seconds(
      cfg, kind=cfg_kind, hours_to_settle=hours_to_settle,
    )
    return max(int(settings.reentry_cooldown_seconds), label_cd)
  if is_profit_exit_reason(exit_reason):
    from src.trading.bot_live_exit import live_profit_exit_cooldown_seconds

    base = int(settings.profit_exit_cooldown_seconds)
    if mode == "live" and cfg is not None:
      kind = "slot15" if bot_kind == "slot15" else "hourly"
      return live_profit_exit_cooldown_seconds(base, cfg, kind=kind)
    return base
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
