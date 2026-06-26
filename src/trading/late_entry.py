"""Conservative late-entry evaluation for slots that opened NO TRADE."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.trading.edge import Signal


@dataclass(frozen=True)
class SlotPathStats:
  bars: int
  pct_time_above_ref: float
  ref_crossings: int
  slot_mom_pct: float
  recent_mom_pct: float
  gap_pct: float


@dataclass(frozen=True)
class LateEntryDecision:
  action: str  # WATCH | LATE LONG | LATE SHORT | NO BET
  prob_up: float
  close_side: str
  summary: str
  reasons: list[str]
  outlook_ready: bool


class LateEntryAdvisor:
  """Reassess NO-BET slots using intra-slot path + time remaining."""

  def __init__(self, cfg: dict[str, Any]):
    lcfg = cfg.get("late_entry", {})
    self.enabled = bool(lcfg.get("enabled", True))
    self.min_elapsed_min = float(lcfg.get("min_elapsed_minutes", 4))
    self.min_remaining_min = float(lcfg.get("min_remaining_minutes", 5))
    self.outlook_after_min = float(lcfg.get("outlook_after_minutes", 2))
    self.min_confidence = float(lcfg.get("min_confidence", 0.62))
    self.min_move_pct = float(lcfg.get("min_move_pct", 0.04))
    self.min_side_time_pct = float(lcfg.get("min_side_time_pct", 0.58))
    self.max_ref_crossings = int(lcfg.get("max_ref_crossings", 1))
    self.momentum_bars = int(lcfg.get("momentum_bars", 4))
    self.min_momentum_pct = float(lcfg.get("min_momentum_pct", 0.02))
    self.fee_buffer_pct = float(
      lcfg.get("fee_buffer_pct", cfg.get("intra_slot", {}).get("fee_buffer_pct", 0.13))
    )

  @staticmethod
  def slot_path_stats(
    df_1m: pd.DataFrame | None,
    slot_start: pd.Timestamp,
    reference_price: float,
    *,
    momentum_bars: int = 4,
  ) -> SlotPathStats:
    if reference_price <= 0:
      return SlotPathStats(0, 0.5, 0, 0.0, 0.0, 0.0)

    if df_1m is None or df_1m.empty:
      return SlotPathStats(0, 0.5, 0, 0.0, 0.0, 0.0)

    df = df_1m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    in_slot = df[df["timestamp"] >= slot_start].sort_values("timestamp")
    if in_slot.empty:
      return SlotPathStats(0, 0.5, 0, 0.0, 0.0, 0.0)

    closes = in_slot["close"].astype(float)
    above = (closes >= reference_price).astype(int)
    pct_above = float(above.mean()) if len(above) else 0.5

    side = np.sign(closes - reference_price)
    side = side.replace(0, np.nan).ffill().fillna(0)
    crossings = int((side.diff().abs() > 0).sum())

    first = float(closes.iloc[0])
    last = float(closes.iloc[-1])
    slot_mom = (last - first) / first * 100 if first > 0 else 0.0

    tail = in_slot.tail(max(2, momentum_bars))
    rets = tail["close"].pct_change().dropna() * 100
    recent = float(rets.sum()) if len(rets) else 0.0

    gap = (last - reference_price) / reference_price * 100

    return SlotPathStats(
      bars=len(in_slot),
      pct_time_above_ref=pct_above,
      ref_crossings=crossings,
      slot_mom_pct=slot_mom,
      recent_mom_pct=recent,
      gap_pct=gap,
    )

  def reassess_prob_up_at_close(
    self,
    *,
    reference_price: float,
    current_price: float,
    seconds_remaining: int,
    stats: SlotPathStats,
    original_prob_up: float,
  ) -> float:
    """Outlook for BRTI finish vs t=0 — weights slot history more as time passes."""
    if reference_price <= 0:
      return 0.5

    minutes_left = max(0.1, seconds_remaining / 60)
    minutes_elapsed = max(0.1, 15.0 - minutes_left)
    time_pressure = 1.0 - min(1.0, minutes_left / 15.0)
    history_weight = min(0.85, 0.25 + 0.04 * minutes_elapsed)

    gap_pct = stats.gap_pct
    score = 0.5

    score += np.clip(gap_pct * 0.14 * (0.25 + 0.75 * time_pressure), -0.4, 0.4)
    score += np.clip(stats.recent_mom_pct * 0.16 * history_weight, -0.16, 0.16)
    score += np.clip(stats.slot_mom_pct * 0.06 * history_weight, -0.12, 0.12)

    # Time spent on each side of ref — persistence into the close
    side_bias = (stats.pct_time_above_ref - 0.5) * 0.22 * history_weight
    score += np.clip(side_bias, -0.14, 0.14)

    # Whipsaw penalty when plenty of time remains
    if stats.ref_crossings > 1 and minutes_left > 6:
      score -= np.sign(gap_pct or 1) * min(0.08, stats.ref_crossings * 0.025)

    open_weight = max(0.05, 0.28 * (1.0 - time_pressure) * (1.0 - history_weight))
    score = score * (1.0 - open_weight) + float(original_prob_up) * open_weight
    return float(np.clip(score, 0.05, 0.95))

  def evaluate(
    self,
    *,
    elapsed_minutes: float,
    seconds_remaining: int,
    reference_price: float,
    stats: SlotPathStats,
    original_prob_up: float,
    current_price: float,
  ) -> LateEntryDecision:
    if not self.enabled:
      return LateEntryDecision(
        action="NO BET",
        prob_up=original_prob_up,
        close_side="",
        summary="Late entry disabled.",
        reasons=[],
        outlook_ready=False,
      )

    minutes_left = seconds_remaining / 60
    reasons: list[str] = []
    outlook_ready = elapsed_minutes >= self.outlook_after_min

    prob_up = self.reassess_prob_up_at_close(
      reference_price=reference_price,
      current_price=current_price,
      seconds_remaining=seconds_remaining,
      stats=stats,
      original_prob_up=original_prob_up,
    )

    if prob_up >= 0.57:
      close_side = "UP"
    elif prob_up <= 0.43:
      close_side = "DOWN"
    else:
      close_side = "TOSS-UP"

    mins, secs = divmod(max(0, seconds_remaining), 60)
    time_left = f"{mins}m {secs:02d}s"

    if elapsed_minutes < self.outlook_after_min:
      return LateEntryDecision(
        action="WATCH",
        prob_up=prob_up,
        close_side=close_side,
        summary=f"Early slot ({time_left} left) — building picture before reassessment.",
        reasons=[
          f"Wait until {self.outlook_after_min:.0f} min elapsed ({self.min_elapsed_min:.0f} min before late entry).",
        ],
        outlook_ready=False,
      )

    outlook_line = (
      f"Outlook ({time_left} left): {prob_up * 100:.0f}% UP at close vs ${reference_price:,.2f} ref"
    )

    if elapsed_minutes < self.min_elapsed_min:
      return LateEntryDecision(
        action="WATCH",
        prob_up=prob_up,
        close_side=close_side,
        summary=outlook_line,
        reasons=[
          f"{elapsed_minutes:.0f} min elapsed — need {self.min_elapsed_min:.0f} min before late-entry consideration.",
          f"Move vs t=0: {stats.gap_pct:+.2f}%; slot drift {stats.slot_mom_pct:+.2f}%.",
        ],
        outlook_ready=True,
      )

    if minutes_left < self.min_remaining_min:
      return LateEntryDecision(
        action="WATCH",
        prob_up=prob_up,
        close_side=close_side,
        summary=outlook_line,
        reasons=[
          f"Only {time_left} left — too little time for a new entry (min {self.min_remaining_min:.0f} min).",
        ],
        outlook_ready=True,
      )

    if stats.bars < 3:
      return LateEntryDecision(
        action="WATCH",
        prob_up=prob_up,
        close_side=close_side,
        summary=outlook_line,
        reasons=["Not enough 1m bars in this slot yet — waiting for clearer tape."],
        outlook_ready=True,
      )

    if stats.ref_crossings > self.max_ref_crossings:
      return LateEntryDecision(
        action="WATCH",
        prob_up=prob_up,
        close_side=close_side,
        summary=outlook_line,
        reasons=[
          f"Price crossed t=0 {stats.ref_crossings}× — chop; not entering late.",
        ],
        outlook_ready=True,
      )

    if abs(stats.gap_pct) < self.min_move_pct:
      return LateEntryDecision(
        action="WATCH",
        prob_up=prob_up,
        close_side=close_side,
        summary=outlook_line,
        reasons=[
          f"Move vs t=0 only {stats.gap_pct:+.2f}% — need {self.min_move_pct:.2f}%+ to justify late entry.",
        ],
        outlook_ready=True,
      )

    long_ok = (
      prob_up >= self.min_confidence
      and stats.gap_pct > 0
      and stats.pct_time_above_ref >= self.min_side_time_pct
      and stats.recent_mom_pct >= self.min_momentum_pct
      and stats.slot_mom_pct >= 0
    )
    short_ok = (
      prob_up <= (1 - self.min_confidence)
      and stats.gap_pct < 0
      and stats.pct_time_above_ref <= (1 - self.min_side_time_pct)
      and stats.recent_mom_pct <= -self.min_momentum_pct
      and stats.slot_mom_pct <= 0
    )

    if long_ok and abs(stats.gap_pct) >= self.fee_buffer_pct:
      return LateEntryDecision(
        action="LATE LONG",
        prob_up=prob_up,
        close_side=close_side,
        summary=f"LATE LONG — {outlook_line}",
        reasons=[
          f"Held above t=0 ~{stats.pct_time_above_ref * 100:.0f}% of slot; drift {stats.slot_mom_pct:+.2f}%.",
          f"Recent 1m flow {stats.recent_mom_pct:+.2f}% supports continuation.",
        ],
        outlook_ready=True,
      )

    if short_ok and abs(stats.gap_pct) >= self.fee_buffer_pct:
      return LateEntryDecision(
        action="LATE SHORT",
        prob_up=prob_up,
        close_side=close_side,
        summary=f"LATE SHORT — {outlook_line}",
        reasons=[
          f"Held below t=0 ~{(1 - stats.pct_time_above_ref) * 100:.0f}% of slot; drift {stats.slot_mom_pct:+.2f}%.",
          f"Recent 1m flow {stats.recent_mom_pct:+.2f}% supports continuation.",
        ],
        outlook_ready=True,
      )

    reasons.append(outlook_line)
    if close_side == "TOSS-UP":
      reasons.append("Outlook still balanced — no late entry.")
    elif prob_up >= 0.5 and not long_ok:
      reasons.append("UP lean but persistence/momentum below late-entry bar.")
    elif prob_up < 0.5 and not short_ok:
      reasons.append("DOWN lean but persistence/momentum below late-entry bar.")
    else:
      reasons.append("Conditions not aligned for late entry.")

    return LateEntryDecision(
      action="WATCH",
      prob_up=prob_up,
      close_side=close_side,
      summary=outlook_line,
      reasons=reasons,
      outlook_ready=True,
    )

  @staticmethod
  def to_exit_action(action: str) -> str:
    if action in (Signal.LONG.value, "LATE LONG"):
      return "LATE LONG"
    if action in (Signal.SHORT.value, "LATE SHORT"):
      return "LATE SHORT"
    if action == "WATCH":
      return "WATCH"
    return "NO BET"
