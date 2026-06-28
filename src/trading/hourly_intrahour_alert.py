"""Live intrahour opportunity highlighting — shock + model recovery thesis."""

from __future__ import annotations

from typing import Any, Literal

from src.trading.contract_signals import is_actionable_buy, is_buy_no, is_buy_yes
from src.trading.hourly_bet_assessment import assess_contract_bet, assess_hourly_bet

TriggerKind = Literal["price_shock_recovery", "edge_spike", "mu_recovery"]


def _intrahour_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  h = (cfg or {}).get("hourly", {})
  icfg = h.get("intrahour") or {}
  rcfg = h.get("regime", {})
  return {
    "enabled": bool(icfg.get("enabled", True)),
    "min_shock_pct": float(icfg.get("min_shock_pct", 0.8)),
    "min_edge_for_highlight": float(icfg.get("min_edge_for_highlight", 0.08)),
    "min_edge_override_regime": float(icfg.get("min_edge_override_regime", 0.10)),
    "min_edge": float(rcfg.get("min_edge", 0.05)),
    "min_expected_move_pct": float(rcfg.get("min_expected_move_pct", 0.12)),
  }


def _move_pct(ref: float | None, current: float | None) -> float | None:
  if ref is None or current is None or float(ref) <= 0:
    return None
  return (float(current) - float(ref)) / float(ref) * 100


def _pick_contract(live: dict[str, Any]) -> dict[str, Any] | None:
  primary = live.get("primary_pick")
  if primary and is_actionable_buy(primary.get("signal")):
    return primary
  be_t = (live.get("strategy_threshold") or {}).get("best_edge")
  be_r = (live.get("strategy_range") or {}).get("best_edge")
  candidates = [
    c for c in (be_t, be_r) if c and c.get("edge") is not None and is_actionable_buy(c.get("signal"))
  ]
  if not candidates:
    return primary if primary else None
  return max(candidates, key=lambda c: abs(float(c["edge"])))


def _recovery_thesis(
  *,
  signal: str | None,
  move_pct: float | None,
  expected_move_pct: float | None,
  terminal_mu: float | None,
  current_price: float | None,
) -> bool:
  if move_pct is None:
    return False
  if move_pct < 0:
    if expected_move_pct is not None and expected_move_pct > 0:
      return True
    if terminal_mu is not None and current_price is not None and float(terminal_mu) > float(current_price):
      return True
    return is_buy_yes(signal)
  if move_pct > 0:
    if expected_move_pct is not None and expected_move_pct < 0:
      return True
    if terminal_mu is not None and current_price is not None and float(terminal_mu) < float(current_price):
      return True
    return is_buy_no(signal)
  return False


def _effective_bet_assessment(
  *,
  signal: str | None,
  edge: float | None,
  live: dict[str, Any],
  locked: dict[str, Any] | None,
  cfg: dict[str, Any],
) -> dict[str, Any]:
  bet = assess_contract_bet(
    signal=signal,
    edge=edge,
    live=live,
    locked=locked,
    use_live_regime=True,
    cfg={"hourly": {"regime": {"min_edge": cfg["min_edge"], "min_expected_move_pct": cfg["min_expected_move_pct"]}}},
  )
  edge_f = float(edge) if edge is not None else None
  regime = live.get("regime") or {}
  if not bet.get("actionable_bet") and is_actionable_buy(signal):
    if edge_f is not None and abs(edge_f) >= cfg["min_edge_override_regime"]:
      override = assess_hourly_bet(
        signal=signal,
        edge=edge,
        regime_allow_trade=True,
        regime_reasons=list(regime.get("reasons") or []),
        expected_move_pct=live.get("expected_move_pct"),
        min_edge=cfg["min_edge"],
        min_expected_move_pct=cfg["min_expected_move_pct"],
      )
      if override.get("actionable_bet"):
        bet = {**override, "regime_overridden": True}
  return bet


def _is_high_actionable(bet: dict[str, Any], min_edge: float) -> bool:
  tone = bet.get("actionable_tone")
  if tone == "strong":
    return True
  if bet.get("actionable_bet") and bet.get("hour_quality") in ("STRONG", "MODERATE"):
    return True
  return False


def assess_intrahour_opportunity(
  *,
  live: dict[str, Any],
  locked: dict[str, Any] | None = None,
  hour_open: dict[str, Any] | None = None,
  current_price: float | None = None,
  index_label: str = "ERTI",
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
  """Return intrahour highlight payload when live shock meets recovery + edge thesis."""
  icfg = _intrahour_cfg(cfg)
  if not icfg["enabled"]:
    return None

  ref_src = locked or hour_open
  if ref_src is None:
    return None

  ref_price = ref_src.get("reference_price")
  price = current_price if current_price is not None else live.get("current_price") or live.get("brti_live")
  move_pct = _move_pct(ref_price, price)
  if move_pct is None:
    return None

  pick = _pick_contract(live)
  if not pick:
    return {"highlight": False}

  signal = pick.get("signal")
  edge = pick.get("edge")
  if not is_actionable_buy(signal):
    return {"highlight": False}

  bet = _effective_bet_assessment(
    signal=signal,
    edge=edge,
    live=live,
    locked=locked,
    cfg=icfg,
  )
  edge_f = float(edge) if edge is not None else None
  edge_ok = edge_f is not None and abs(edge_f) >= icfg["min_edge_for_highlight"]
  high_actionable = _is_high_actionable(bet, icfg["min_edge_for_highlight"])

  terminal_mu = live.get("terminal_mu") or live.get("blended_mu")
  expected_move = live.get("expected_move_pct")
  if expected_move is None and price and terminal_mu:
    expected_move = (float(terminal_mu) - float(price)) / float(price) * 100

  shock = abs(move_pct) >= icfg["min_shock_pct"]
  recovery = _recovery_thesis(
    signal=signal,
    move_pct=move_pct,
    expected_move_pct=expected_move,
    terminal_mu=float(terminal_mu) if terminal_mu is not None else None,
    current_price=float(price) if price is not None else None,
  )

  trigger: TriggerKind | None = None
  severity: Literal["high", "moderate"] | None = None
  headline = "INTRAHOUR OPPORTUNITY"

  if shock and recovery and edge_ok and high_actionable:
    trigger = "price_shock_recovery"
    severity = "high" if bet.get("actionable_tone") == "strong" else "moderate"
    headline = "CRASH + RECOVERY BET" if move_pct < 0 else "SPIKE + FADE BET"
  elif shock and recovery and edge_ok and bet.get("actionable_bet"):
    trigger = "price_shock_recovery"
    severity = "moderate"
    headline = "CRASH + RECOVERY BET" if move_pct < 0 else "SPIKE + FADE BET"
  elif not shock and edge_ok and high_actionable and bet.get("actionable_bet"):
    locked_pick = (locked or {}).get("primary_pick") or {}
    locked_edge = locked_pick.get("edge")
    edge_spike = False
    if locked_edge is not None and edge_f is not None:
      locked_edge_f = float(locked_edge)
      edge_spike = edge_f >= locked_edge_f + 0.03 or edge_f >= locked_edge_f * 1.4
    if edge_spike:
      trigger = "edge_spike"
      severity = "moderate"
      headline = "INTRAHOUR OPPORTUNITY"
    else:
      return {"highlight": False, "move_pct_since_lock": round(move_pct, 3)}
  else:
    return {"highlight": False, "move_pct_since_lock": round(move_pct, 3)}

  ref_label = "lock" if locked else "hour open"
  move_s = f"{move_pct:+.2f}%"
  contract = pick.get("label") or pick.get("ticker") or "primary pick"
  detail = (
    f"{index_label} moved {move_s} since {ref_label}; model still favors {signal} on {contract}"
  )
  if recovery and shock:
    detail += " — recovery into settle window"
  elif trigger == "edge_spike":
    detail += f" — elevated edge {edge_f * 100:.1f}¢ vs Kalshi"
  if bet.get("regime_overridden"):
    detail += " (large edge overrides regime caution)"

  return {
    "highlight": True,
    "severity": severity,
    "headline": headline,
    "actionable_headline": bet.get("actionable_headline"),
    "detail": detail,
    "trigger": trigger,
    "move_pct_since_lock": round(move_pct, 3),
    "reference_price": ref_price,
    "current_price": price,
    "primary_pick": pick,
    "bet_assessment": bet,
  }
