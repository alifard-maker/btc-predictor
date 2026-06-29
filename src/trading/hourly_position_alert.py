"""Position exit / hold guidance for hourly Kalshi contracts at :05 lock and :45 late call.

:05 (locked) — advise whether to enter or hold the frozen primary pick:
  - Regime-blocked or weak setup with an actionable signal → CUT LOSSES (do not enter).
  - Strong actionable lock → HOLD (enter/hold through settle).
  - No actionable signal → HOLD with neutral tone (no position guidance).

:45 (late call) — 15 minutes before settle; assumes entry followed the :05 lock:
  - Signal flipped, regime blocked, edge collapsed, or live drift against position → CUT LOSSES.
  - Favorable move with edge largely captured → TAKE PROFIT.
  - Still aligned with moderate edge → HOLD.
"""

from __future__ import annotations

from typing import Any, Literal

from src.trading.contract_signals import is_actionable_buy, is_buy_no, is_buy_yes
from src.trading.hourly_bet_assessment import assess_hourly_bet_from_late_call_row, assess_hourly_bet_from_row

AlertKind = Literal["CUT LOSSES", "TAKE PROFIT", "HOLD"]
ToneKind = Literal["danger", "success", "neutral"]


def _result(alert: AlertKind, tone: ToneKind, detail: str) -> dict[str, Any]:
  return {
    "alert": alert,
    "alert_tone": tone,
    "headline": alert,
    "detail": detail,
  }


def _min_edge(cfg: dict[str, Any] | None) -> float:
  hcfg = (cfg or {}).get("hourly", {}).get("regime", {})
  return float(hcfg.get("min_edge", 0.05))


def _signals_conflict(a: str | None, b: str | None) -> bool:
  if not is_actionable_buy(a) or not is_actionable_buy(b):
    return False
  return is_buy_yes(a) != is_buy_yes(b)


def _drift_pct(locked_ref: float | None, current_ref: float | None) -> float | None:
  if locked_ref is None or current_ref is None or float(locked_ref) <= 0:
    return None
  return (float(current_ref) - float(locked_ref)) / float(locked_ref) * 100


def _drift_favors_signal(signal: str | None, drift_pct: float | None, *, threshold: float = 0.03) -> bool | None:
  if drift_pct is None or not is_actionable_buy(signal):
    return None
  if abs(drift_pct) < threshold:
    return None
  if is_buy_yes(signal):
    return drift_pct >= threshold
  if is_buy_no(signal):
    return drift_pct <= -threshold
  return None


def _mu_shift_favors_signal(signal: str | None, locked_mu: float | None, current_mu: float | None) -> bool | None:
  if locked_mu is None or current_mu is None or not is_actionable_buy(signal):
    return None
  shift = float(current_mu) - float(locked_mu)
  if is_buy_yes(signal):
    return shift > 0
  if is_buy_no(signal):
    return shift < 0
  return None


def _spot_favors_held_side(
  *,
  side: str,
  live_price: float,
  pick: dict[str, Any],
) -> bool | None:
  """Whether live index spot structurally favors the held YES/NO leg."""
  floor = pick.get("floor_strike")
  cap = pick.get("cap_strike")
  strike_type = pick.get("strike_type")
  ctype = pick.get("contract_type", "threshold")
  held_yes = side == "yes"

  if ctype == "range" or strike_type == "between":
    if floor is not None and cap is not None:
      lo, hi = float(floor), float(cap)
      inside = lo <= live_price <= hi
      return inside if held_yes else not inside

  if strike_type == "greater" and floor is not None:
    above = live_price >= float(floor)
    return above if held_yes else not above
  if strike_type == "less" and cap is not None:
    below = live_price < float(cap)
    return below if held_yes else not below
  return None


def _signal_favors_held_side(signal: str | None, side: str) -> bool | None:
  if not is_actionable_buy(signal):
    return None
  held_yes = side == "yes"
  return is_buy_yes(signal) if held_yes else is_buy_no(signal)


def assess_held_hourly_position_alert(
  *,
  pos: dict[str, Any],
  pick: dict[str, Any],
  live_price: float | None,
  regime_allow_trade: bool,
  regime_reasons: list[str] | None = None,
  unrealized_pnl_usd: float | None = None,
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Position alert for an open bot leg — uses spot vs band/strike, not index drift alone."""
  side = str(pos.get("side") or "yes")
  entry_signal = pos.get("signal")
  current_signal = pick.get("signal")
  edge_f = float(pick["edge"]) if pick.get("edge") is not None else None
  min_edge = _min_edge(cfg)
  reasons = list(regime_reasons or [])

  spot_ok: bool | None = None
  if live_price is not None:
    try:
      spot_ok = _spot_favors_held_side(side=side, live_price=float(live_price), pick=pick)
    except (TypeError, ValueError):
      spot_ok = None

  sig_ok = _signal_favors_held_side(current_signal, side)
  sig_flipped = (
    is_actionable_buy(entry_signal)
    and is_actionable_buy(current_signal)
    and is_buy_yes(entry_signal) != is_buy_yes(current_signal)
  )

  if unrealized_pnl_usd is not None and unrealized_pnl_usd >= 0.50:
    if edge_f is not None and edge_f < min_edge:
      return _result(
        "TAKE PROFIT",
        "success",
        "Marked gain with edge mostly priced in — consider taking profit.",
      )
    return _result("HOLD", "success", "Position ahead on mark — hold unless signal flips.")

  if spot_ok is True and (unrealized_pnl_usd is None or unrealized_pnl_usd >= -0.05):
    return _result(
      "HOLD",
      "success",
      f"Spot supports your {side.upper()} leg on this contract — hold.",
    )

  if unrealized_pnl_usd is not None and unrealized_pnl_usd < -0.05:
    if sig_flipped:
      return _result(
        "CUT LOSSES",
        "danger",
        f"Signal flipped ({entry_signal} → {current_signal}) with loss on mark — cut.",
      )
    if spot_ok is False:
      return _result(
        "CUT LOSSES",
        "danger",
        "Spot moved against your band/strike leg with loss on mark — cut.",
      )
    if not regime_allow_trade and sig_ok is False:
      detail = "Regime blocked and signal no longer supports your leg"
      if reasons:
        detail += f" ({reasons[0]})"
      return _result("CUT LOSSES", "danger", detail + " — cut losses.")

  if not regime_allow_trade and spot_ok is False and unrealized_pnl_usd is not None and unrealized_pnl_usd < 0:
    detail = "Regime blocked with spot against your leg"
    if reasons:
      detail += f" ({reasons[0]})"
    return _result("CUT LOSSES", "danger", detail + " — cut losses.")

  if sig_flipped and spot_ok is not True:
    return _result(
      "HOLD",
      "neutral",
      f"Signal flipped ({entry_signal} → {current_signal}) but spot still OK — hold with tight risk.",
    )

  if sig_ok and unrealized_pnl_usd is not None and unrealized_pnl_usd >= 0:
    return _result("HOLD", "success", "Signal and mark still favor your leg — hold.")

  return _result("HOLD", "neutral", "Hold — position aligned or flat on mark.")


def assess_hourly_position_alert(
  *,
  snapshot_kind: Literal["locked", "late_call"],
  signal: str | None,
  edge: float | None,
  regime_allow_trade: bool,
  regime_reasons: list[str] | None = None,
  expected_move_pct: float | None = None,
  bet_assessment: dict[str, Any] | None = None,
  locked_signal: str | None = None,
  locked_edge: float | None = None,
  locked_regime_allow_trade: bool | None = None,
  locked_reference_price: float | None = None,
  reference_price: float | None = None,
  locked_terminal_mu: float | None = None,
  terminal_mu: float | None = None,
  live_price: float | None = None,
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Return CUT LOSSES / TAKE PROFIT / HOLD for an hourly snapshot."""
  min_edge = _min_edge(cfg)
  edge_f = float(edge) if edge is not None else None
  locked_edge_f = float(locked_edge) if locked_edge is not None else None
  has_signal = is_actionable_buy(signal)
  reasons = list(regime_reasons or [])

  if snapshot_kind == "locked":
    if not has_signal:
      return _result("HOLD", "neutral", "No BUY YES/NO on locked pick — no position guidance.")

    if not regime_allow_trade:
      detail = "Regime blocked at :05 lock"
      if reasons:
        detail += f" ({reasons[0]})"
      detail += " — do not enter; cut if you held from hour open."
      return _result("CUT LOSSES", "danger", detail)

    assessment = bet_assessment or {}
    if assessment.get("actionable_bet") and assessment.get("hour_quality") == "STRONG":
      return _result(
        "HOLD",
        "success",
        "Strong actionable lock — hold through settle if you entered at :05.",
      )

    if assessment.get("actionable_bet"):
      return _result(
        "HOLD",
        "neutral",
        "Actionable :05 lock — hold if entered; size for hourly settle risk.",
      )

    weak = assessment.get("hour_quality") == "WEAK"
    if weak:
      return _result(
        "HOLD",
        "neutral",
        "Weak hour quality at lock — hold only with tight risk; prefer skipping new entry.",
      )

    if edge_f is not None and edge_f < min_edge:
      return _result(
        "CUT LOSSES",
        "danger",
        f"Edge {edge_f * 100:.1f}¢ below {min_edge * 100:.0f}¢ minimum — do not enter at lock.",
      )

    return _result("HOLD", "neutral", "Locked pick — hold if already in; no strong entry edge.")

  # --- late_call (:45) ---
  entry_signal = locked_signal if is_actionable_buy(locked_signal) else signal
  if not is_actionable_buy(entry_signal):
    if has_signal:
      return _result(
        "HOLD",
        "neutral",
        "Late-call pick only — no :05 entry signal to manage; use late call for new decisions.",
      )
    return _result("HOLD", "neutral", "No actionable hourly position — nothing to exit at :45.")

  price_ref = live_price if live_price is not None else reference_price
  drift = _drift_pct(locked_reference_price, price_ref)
  drift_ok = _drift_favors_signal(entry_signal, drift)
  drift_against = drift is not None and drift_ok is False
  mu_ok = _mu_shift_favors_signal(entry_signal, locked_terminal_mu, terminal_mu)
  flipped = _signals_conflict(locked_signal, signal)
  regime_worse = bool(locked_regime_allow_trade) and not regime_allow_trade
  regime_blocked = not regime_allow_trade

  edge_captured = (
    locked_edge_f is not None
    and edge_f is not None
    and locked_edge_f >= min_edge
    and edge_f < min_edge
  )
  edge_collapsed = (
    locked_edge_f is not None
    and edge_f is not None
    and locked_edge_f >= min_edge
    and edge_f < locked_edge_f * 0.45
  )

  if flipped:
    return _result(
      "CUT LOSSES",
      "danger",
      f":45 signal flipped vs :05 lock ({locked_signal} → {signal}) — exit before settle.",
    )

  if regime_worse or (regime_blocked and is_actionable_buy(locked_signal)):
    detail = "Regime blocked at :45"
    if reasons:
      detail += f" ({reasons[0]})"
    detail += " — cut losses on the :05 position."
    return _result("CUT LOSSES", "danger", detail)

  if drift_against and (edge_collapsed or (edge_f is not None and edge_f < min_edge)):
    drift_s = f"{drift:+.2f}%" if drift is not None else "against you"
    return _result(
      "CUT LOSSES",
      "danger",
      f"Live drift {drift_s} vs :05 ref with weak edge — cut losses with 15 min left.",
    )

  if mu_ok is False and edge_collapsed:
    return _result(
      "CUT LOSSES",
      "danger",
      "Forecast shifted against your :05 leg and edge collapsed — cut losses.",
    )

  favorable_drift = drift_ok is True and drift is not None and abs(drift) >= 0.06
  if favorable_drift and (edge_captured or edge_collapsed):
    drift_s = f"{drift:+.2f}%" if drift is not None else "in your favor"
    return _result(
      "TAKE PROFIT",
      "success",
      f"Move {drift_s} captured vs :05 ref; edge mostly priced in — take profit before settle.",
    )

  if edge_captured and drift_ok is not False:
    return _result(
      "TAKE PROFIT",
      "success",
      "Kalshi caught up to your :05 edge — lock gains with 15 min to settle.",
    )

  if drift_ok and edge_f is not None and edge_f >= min_edge:
    return _result(
      "HOLD",
      "success",
      "Still aligned at :45 with edge and drift in your favor — hold through settle.",
    )

  if drift_against:
    return _result(
      "HOLD",
      "neutral",
      "Drift slightly against :05 entry but signal still aligned — hold with tight risk.",
    )

  return _result(
    "HOLD",
    "neutral",
    "Position still aligned with :05 lock — hold unless regime or drift worsens.",
  )


def assess_locked_position_alert_from_row(row: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
  """Position alert for the :05 locked snapshot."""
  bet = assess_hourly_bet_from_row(row, cfg)
  regime_reasons = [s for s in str(row.get("regime_notes") or "").split("; ") if s]
  return assess_hourly_position_alert(
    snapshot_kind="locked",
    signal=row.get("primary_signal"),
    edge=row.get("primary_edge"),
    regime_allow_trade=not bool(row.get("regime_blocked")),
    regime_reasons=regime_reasons,
    expected_move_pct=row.get("expected_move_pct"),
    bet_assessment=bet,
    cfg=cfg,
  )


def assess_late_call_position_alert_from_row(
  row: dict[str, Any],
  cfg: dict[str, Any] | None = None,
  *,
  live_price: float | None = None,
) -> dict[str, Any]:
  """Position alert for the :45 late-call snapshot (uses :05 lock columns on same row)."""
  bet = assess_hourly_bet_from_late_call_row(row, cfg)
  regime_reasons = [s for s in str(row.get("late_call_regime_notes") or "").split("; ") if s]
  return assess_hourly_position_alert(
    snapshot_kind="late_call",
    signal=row.get("late_call_primary_signal"),
    edge=row.get("late_call_primary_edge"),
    regime_allow_trade=not bool(row.get("late_call_regime_blocked")),
    regime_reasons=regime_reasons,
    expected_move_pct=row.get("late_call_expected_move_pct"),
    bet_assessment=bet,
    locked_signal=row.get("primary_signal"),
    locked_edge=row.get("primary_edge"),
    locked_regime_allow_trade=not bool(row.get("regime_blocked")),
    locked_reference_price=row.get("reference_price"),
    reference_price=row.get("late_call_reference_price"),
    locked_terminal_mu=row.get("terminal_mu") or row.get("blended_mu"),
    terminal_mu=row.get("late_call_terminal_mu") or row.get("late_call_blended_mu"),
    live_price=live_price,
    cfg=cfg,
  )
