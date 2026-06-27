"""Kalshi contract action labels (BUY YES / BUY NO) vs legacy LEAN YES/NO."""

from __future__ import annotations

BUY_YES = "BUY YES"
BUY_NO = "BUY NO"
VALUE_YES = "VALUE YES"
FADE_YES = "FADE YES"
NEUTRAL = "NEUTRAL"

# Logged rows before Beta 3.4.1 may still use LEAN YES/NO.
_LEGACY_BUY_YES = frozenset({BUY_YES, "LEAN YES"})
_LEGACY_BUY_NO = frozenset({BUY_NO, "LEAN NO"})
ACTIONABLE_BUY = _LEGACY_BUY_YES | _LEGACY_BUY_NO


def is_buy_yes(sig: str | None) -> bool:
  return str(sig or "") in _LEGACY_BUY_YES


def is_buy_no(sig: str | None) -> bool:
  return str(sig or "") in _LEGACY_BUY_NO


def is_actionable_buy(sig: str | None) -> bool:
  return str(sig or "") in ACTIONABLE_BUY


def favors_yes_leg(sig: str | None) -> bool:
  """Outcome is correct when the event happens (YES wins)."""
  return str(sig or "") in _LEGACY_BUY_YES | {VALUE_YES}


def favors_no_leg(sig: str | None) -> bool:
  """Outcome is correct when the event does not happen (NO wins)."""
  return str(sig or "") in _LEGACY_BUY_NO | {FADE_YES}


def signal_correct_for_outcome(
  signal: str | None,
  outcome: int,
  model_prob: float | None,
) -> bool:
  sig = str(signal or "")
  if favors_yes_leg(sig):
    return outcome == 1
  if favors_no_leg(sig):
    return outcome == 0
  prob = float(model_prob if model_prob is not None else 0.5)
  return (prob >= 0.5) == bool(outcome)


def primary_pick_correct(signal: str | None, outcome: int | bool | None, model_prob: float | None) -> bool:
  """Threshold primary pick vs binary outcome column."""
  if outcome is None:
    return False
  try:
    if outcome != outcome:  # NaN
      return False
  except TypeError:
    pass
  sig = str(signal or "")
  if is_buy_yes(sig):
    return bool(outcome)
  if is_buy_no(sig):
    return not bool(outcome)
  prob = float(model_prob if model_prob is not None else 0.5)
  return (prob >= 0.5) == bool(outcome)
