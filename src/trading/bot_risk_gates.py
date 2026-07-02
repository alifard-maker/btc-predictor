"""Pre-trade gates: per-bot daily loss cap and Kalshi API circuit breaker."""

from __future__ import annotations

from typing import Any, Protocol

from src.trading.bot_risk_state import bot_risk_key, get_bot_risk_coordinator
from src.trading.kalshi_circuit import get_circuit_breaker


class BotStoreLike(Protocol):
  def get_settings(self) -> Any: ...
  def save_settings(self, settings: Any, *, source: str = "internal", cfg: dict | None = None) -> Any: ...


SKIP_DAILY_CAP = "daily_loss_cap"
SKIP_KALSHI_DEGRADED = "kalshi_api_degraded"
SKIP_KALSHI_PAUSED = "kalshi_api_paused"

_KALSHI_AUTO_STOP_REASONS = frozenset({SKIP_KALSHI_PAUSED, SKIP_KALSHI_DEGRADED})


def risk_gate_skip_reason(*, bot_key: str) -> str | None:
  coord = get_bot_risk_coordinator()
  if coord:
    coord.refresh_day()
    if coord.is_cap_active(bot_key):
      return SKIP_DAILY_CAP
  circuit = get_circuit_breaker()
  if circuit:
    if circuit.is_paused():
      return SKIP_KALSHI_PAUSED
  return None


def sync_auto_stop_for_risk(
  store: BotStoreLike,
  *,
  bot_key: str,
  cfg: dict | None = None,
) -> None:
  """Align auto_stopped with this bot's daily loss cap (Kalshi pause blocks entries only)."""
  reason = risk_gate_skip_reason(bot_key=bot_key)
  settings = store.get_settings()
  d = settings.to_dict()
  current_reason = str(d.get("auto_stop_reason") or "")

  if current_reason in _KALSHI_AUTO_STOP_REASONS:
    if d.get("auto_stopped"):
      d["auto_stopped"] = False
      d["auto_stop_reason"] = None
      store.save_settings(type(settings)(**d), source="internal", cfg=cfg)
    return

  if reason == SKIP_DAILY_CAP:
    if not d.get("auto_stopped") or current_reason != reason:
      d["auto_stopped"] = True
      d["auto_stop_reason"] = reason
      store.save_settings(type(settings)(**d), source="internal", cfg=cfg)
    return

  if current_reason == SKIP_DAILY_CAP:
    d["auto_stopped"] = False
    d["auto_stop_reason"] = None
    store.save_settings(type(settings)(**d), source="internal", cfg=cfg)


def override_daily_loss_cap(
  store: BotStoreLike,
  *,
  kind: str,
  asset: str,
  cfg: dict | None = None,
) -> dict[str, Any]:
  """Resume entries for this bot today after a daily loss cap trip."""
  key = bot_risk_key(kind, asset)
  coord = get_bot_risk_coordinator()
  if coord:
    coord.override_cap(key)
  sync_auto_stop_for_risk(store, bot_key=key, cfg=cfg)
  return coord.status_for_bot(key) if coord else {"bot_key": key, "cap_override": True}


def record_exit_and_maybe_cap(
  pnl_usd: float,
  *,
  kind: str,
  asset: str,
  store: BotStoreLike,
  cfg: dict | None = None,
) -> bool:
  """Record exit P&L for one bot; auto-stop that bot if its cap is hit."""
  key = bot_risk_key(kind, asset)
  coord = get_bot_risk_coordinator()
  if not coord:
    return False
  hit = coord.record_exit_pnl(key, float(pnl_usd))
  if hit:
    sync_auto_stop_for_risk(store, bot_key=key, cfg=cfg)
  return hit
