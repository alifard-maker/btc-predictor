"""Phase 0–1 P&L-first live gates — S1-only, positive live EV, taker discipline."""

from __future__ import annotations

from typing import Any

from src.backtest.mechanics_profiles import live_mechanics_profile_for_cfg
from src.trading.entry_strategy import (
  ask_cents_for_side,
  expected_value_per_contract_usd,
)
from src.trading.live_range_guards import is_range_pick


def pnl_first_active(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
) -> bool:
  if kind not in ("hourly", "hourly_live") or str(mode).lower() != "live":
    return False
  return live_mechanics_profile_for_cfg(cfg) == "pnl_first"


def pnl_first_s1_only_active(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
  asset: str | None = None,
) -> bool:
  """True when S2 range picks must be dropped (BTC live pnl_first or ETH paper experiment)."""
  if pnl_first_active(cfg, kind=kind, mode=mode):
    return True
  if kind not in ("hourly", "hourly_live") or str(mode).lower() != "paper":
    return False
  label = str(asset or (cfg or {}).get("_asset") or "").lower()
  if label != "eth":
    return False
  bot = ((cfg or {}).get("hourly") or {}).get("bot") or {}
  if str(bot.get("live_mechanics_profile") or "") != "pnl_first":
    return False
  return bool(dict(bot.get("paper_experiment") or {}).get("enabled"))


def _pnl_first_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict((cfg or {}).get("pnl_first") or {})


def pnl_first_milestone_hours(cfg: dict[str, Any] | None) -> int:
  return int(_pnl_first_cfg(cfg).get("milestone_positive_hours", 20))


def pnl_first_live_ev_floor_usd(cfg: dict[str, Any] | None) -> float:
  """Minimum expected USD per contract after fee buffer."""
  return float(_pnl_first_cfg(cfg).get("live_ev_min_usd_per_contract", 0.02))


def _model_p_win(pick: dict[str, Any], side: str) -> float | None:
  prob = pick.get("model_prob")
  if prob is None:
    return None
  try:
    p_yes = float(prob)
  except (TypeError, ValueError):
    return None
  p_yes = max(0.01, min(0.99, p_yes))
  return p_yes if str(side).lower() == "yes" else 1.0 - p_yes


def pnl_first_live_ev_block_reason(
  pick: dict[str, Any],
  side: str,
  cfg: dict[str, Any] | None,
) -> str | None:
  """Block when fee-adjusted live EV per contract is not positive."""
  p_win = _model_p_win(pick, side)
  ask = ask_cents_for_side(pick, side)
  if p_win is None or ask is None:
    return None
  ev = expected_value_per_contract_usd(p_win, int(ask))
  fees = dict(((cfg or {}).get("fees") or {}))
  taker = float(fees.get("taker_pct", 10.0)) / 100.0
  fee_buffer = max(pnl_first_live_ev_floor_usd(cfg), taker * (1.0 - ask / 100.0))
  if ev <= fee_buffer:
    return f"pnl_first_live_ev_negative:{ev:.3f}<={fee_buffer:.3f}"
  return None


def pnl_first_entry_block_reason(
  pick: dict[str, Any],
  side: str,
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
  asset: str | None = None,
  resolved_execution: dict[str, Any] | None = None,
) -> str | None:
  if pnl_first_s1_only_active(cfg, kind=kind, mode=mode, asset=asset) and is_range_pick(pick):
    return "pnl_first_s2_blocked"
  if not pnl_first_active(cfg, kind=kind, mode=mode):
    return None
  ev_block = pnl_first_live_ev_block_reason(pick, side, cfg)
  if ev_block:
    return ev_block
  if resolved_execution is not None:
    mode_exec = str(resolved_execution.get("execution_mode") or "")
    if mode_exec == "passive_limit":
      return "pnl_first_taker_only"
    if resolved_execution.get("price_cents") is None:
      return "pnl_first_no_entry_price"
  return None


def _regime_block_hint(tab: dict[str, Any]) -> str | None:
  live = tab.get("live") or tab
  regime = live.get("regime") or tab.get("regime") or {}
  if regime.get("blocked") is True or regime.get("allow_trade") is False:
    reasons = list(regime.get("reasons") or regime.get("block_reasons") or [])
    hint = str(reasons[0])[:96] if reasons else "regime"
    return hint
  return None


def pnl_first_regime_block_reason(
  tab: dict[str, Any],
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
) -> str | None:
  """Enforce hourly regime even in FREE mode when P&L-first live is active."""
  if not pnl_first_active(cfg, kind=kind, mode=mode):
    return None
  hint = _regime_block_hint(tab)
  if hint:
    return f"pnl_first_regime_blocked:{hint}"
  return None


def trial_mech_pause_when_live_regime_blocked(
  tab: dict[str, Any],
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  asset: str,
) -> str | None:
  """Pause paper twins when live would be regime-blocked (BTC mech + ETH trial)."""
  from src.trading.probe_24h import trial_regime_sync_pause_when_live_blocked

  return trial_regime_sync_pause_when_live_blocked(
    tab, cfg, kind=kind, asset=asset,
  )


def filter_pnl_first_candidates(
  candidates: list[tuple[float, dict[str, Any], dict[str, Any]]],
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
  asset: str | None = None,
) -> list[tuple[float, dict[str, Any], dict[str, Any]]]:
  """Drop S2 range picks from the candidate pool."""
  if not pnl_first_s1_only_active(cfg, kind=kind, mode=mode, asset=asset):
    return candidates
  out: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
  for row in candidates:
    pick = row[1]
    if is_range_pick(pick):
      continue
    out.append(row)
  return out
