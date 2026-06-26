"""Post-slot review — what happened vs what we predicted."""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.features.slots import floor_to_15m


def build_postmortem(
  row: dict[str, Any] | pd.Series,
  *,
  tz_name: str = "America/New_York",
) -> dict[str, Any]:
  """Analyze a resolved prediction row."""
  r = dict(row)
  ts = pd.Timestamp(r["timestamp"], tz="UTC")
  slot = floor_to_15m(ts, tz_name)
  ref = float(r.get("price") or 0)
  exit_p = r.get("exit_price")
  exit_p = float(exit_p) if exit_p is not None else None
  prob = float(r.get("prob_up", 0.5))
  signal = str(r.get("signal", "NO TRADE"))
  outcome = r.get("outcome")
  actual_up = bool(outcome) if outcome is not None else None

  move_pct = None
  move_usd = None
  if exit_p is not None and ref > 0:
    move_usd = exit_p - ref
    move_pct = move_usd / ref * 100

  late_sig = str(r.get("late_entry_signal") or "")
  late_prob = r.get("late_entry_prob_up")
  late_prob = float(late_prob) if late_prob is not None and late_prob == late_prob else None
  flip_sig = str(r.get("flip_signal") or "")
  flip_prob = r.get("flip_prob_up")
  flip_prob = float(flip_prob) if flip_prob is not None and flip_prob == flip_prob else None

  pred_up = prob >= 0.5
  direction_correct = actual_up == pred_up if actual_up is not None else None
  traded = signal in ("LONG", "SHORT")
  trade_correct = None
  if traded and actual_up is not None:
    trade_correct = (signal == "LONG" and actual_up) or (signal == "SHORT" and not actual_up)

  late_correct = None
  if late_sig and actual_up is not None:
    late_correct = (late_sig == "LATE LONG" and actual_up) or (late_sig == "LATE SHORT" and not actual_up)

  flip_correct = None
  if flip_sig and actual_up is not None:
    flip_correct = (flip_sig == "FLIP LONG" and actual_up) or (flip_sig == "FLIP SHORT" and not actual_up)

  lessons: list[str] = []
  conf = abs(prob - 0.5) * 2

  if late_sig and late_correct is False:
    lessons.append(
      f"{late_sig} at close miss — said {late_prob*100:.0f}% UP" if late_prob is not None else f"{late_sig} at close miss"
    )
  if late_sig and late_correct is True:
    lessons.append(f"{late_sig} would have won ({move_pct:+.2f}% BRTI move)" if move_pct is not None else f"{late_sig} would have won")
  if flip_sig and flip_correct is False:
    lessons.append(
      f"{flip_sig} miss — said {flip_prob*100:.0f}% UP" if flip_prob is not None else f"{flip_sig} miss"
    )
  if flip_sig and flip_correct is True:
    lessons.append(f"{flip_sig} won ({move_pct:+.2f}% BRTI move)" if move_pct is not None else f"{flip_sig} won")
  if not traded and not late_sig and not flip_sig and move_pct is not None and abs(move_pct) >= 0.15:
    lessons.append(
      f"NO TRADE but slot moved {move_pct:+.2f}% — model conviction only {conf*100:.0f}%"
    )
  if traded and trade_correct is False:
    lessons.append(f"{signal} lost; BRTI finished {'UP' if actual_up else 'DOWN'} vs t=0")
  if traded and trade_correct is True:
    lessons.append(f"{signal} aligned with {move_pct:+.2f}% BRTI move")
  if direction_correct is False and conf < 0.25:
    lessons.append("Low-confidence lean pointed wrong way — regime filter may help")
  if direction_correct is True and not traded and conf >= 0.35:
    lessons.append("Direction right but below trade threshold — consider calibration")

  return {
    "slot_start": slot.isoformat(),
    "signal": signal,
    "late_entry_signal": late_sig or None,
    "late_entry_prob_up": round(late_prob, 4) if late_prob is not None else None,
    "flip_signal": flip_sig or None,
    "flip_prob_up": round(flip_prob, 4) if flip_prob is not None else None,
    "prob_up": round(prob, 4),
    "confidence": round(conf, 3),
    "reference_price": ref,
    "exit_price": exit_p,
    "move_pct": round(move_pct, 4) if move_pct is not None else None,
    "move_usd": round(move_usd, 2) if move_usd is not None else None,
    "actual_up": actual_up,
    "direction_correct": direction_correct,
    "trade_correct": trade_correct,
    "lessons": lessons,
    "summary": lessons[0] if lessons else "In line with expectation",
  }
