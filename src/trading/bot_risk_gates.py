"""Pre-trade gates: daily loss cap and Kalshi API circuit breaker."""

from __future__ import annotations

from typing import Any, Protocol

from src.trading.bot_risk_state import get_bot_risk_coordinator, get_registered_bot_stores
from src.trading.kalshi_circuit import get_circuit_breaker


class BotStoreLike(Protocol):
  def get_settings(self) -> Any: ...
  def save_settings(self, settings: Any, *, source: str = "internal", cfg: dict | None = None) -> Any: ...


SKIP_DAILY_CAP = "daily_loss_cap"
SKIP_KALSHI_PAUSED = "kalshi_api_paused"


def risk_gate_skip_reason() -> str | None:
  coord = get_bot_risk_coordinator()
  if coord:
    coord.refresh_day()
    if coord.is_cap_active():
      return SKIP_DAILY_CAP
  circuit = get_circuit_breaker()
  if circuit and circuit.is_paused():
    return SKIP_KALSHI_PAUSED
  return None


def sync_auto_stop_for_risk(store: BotStoreLike, *, cfg: dict | None = None) -> None:
  """Align auto_stopped with shared risk gates (daily cap / API pause)."""
  reason = risk_gate_skip_reason()
  settings = store.get_settings()
  d = settings.to_dict()
  current_reason = str(d.get("auto_stop_reason") or "")

  if reason in (SKIP_DAILY_CAP, SKIP_KALSHI_PAUSED):
    if not d.get("auto_stopped") or current_reason != reason:
      d["auto_stopped"] = True
      d["auto_stop_reason"] = reason
      store.save_settings(type(settings)(**d), source="internal", cfg=cfg)
    return

  if current_reason in (SKIP_DAILY_CAP, SKIP_KALSHI_PAUSED):
    d["auto_stopped"] = False
    d["auto_stop_reason"] = None
    store.save_settings(type(settings)(**d), source="internal", cfg=cfg)


def apply_daily_loss_cap_to_stores(stores: list[BotStoreLike], *, cfg: dict | None = None) -> None:
  for store in stores:
    sync_auto_stop_for_risk(store, cfg=cfg)


def record_exit_and_maybe_cap(
  pnl_usd: float,
  stores: list[BotStoreLike] | None = None,
  *,
  cfg: dict | None = None,
) -> bool:
  """Record exit P&L; if cap hit, auto-stop all stores. Returns True if cap hit."""
  coord = get_bot_risk_coordinator()
  if not coord:
    return False
  hit = coord.record_exit_pnl(float(pnl_usd))
  peer = stores or get_registered_bot_stores()
  if hit and peer:
    apply_daily_loss_cap_to_stores(peer, cfg=cfg)
  return hit
