from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from src.features.slots import current_slot_start, slot_end, slot_label
from src.trading.edge import Signal


class ExitAction(str, Enum):
  WAIT = "WAIT"
  NO_BET = "NO BET"
  HOLD = "HOLD"
  TAKE_PROFIT = "TAKE PROFIT"
  CUT_LOSS = "CUT LOSS"


@dataclass
class SlotMonitor:
  active: bool
  slot_label: str
  slot_start: str
  slot_end: str
  seconds_remaining: int
  elapsed_pct: float
  bet_side: str  # UP, DOWN, NONE
  signal_at_open: str
  reference_price: float
  current_price: float
  unrealized_pct: float
  unrealized_usd: float = 0.0
  action: ExitAction
  urgency: str  # low, medium, high
  message: str
  reasons: list[str]
  reference_price_api: float | None = None
  using_override: bool = False
  reassessed_prob_up: float | None = None
  reassessed_prob_down: float | None = None
  reassessed_close_side: str = ""
  reassess_summary: str = ""
  reference_source: str = ""
  current_price_as_of: str | None = None
  live_price_age_sec: float | None = None
  kalshi: dict[str, Any] | None = None

  def to_dict(self) -> dict[str, Any]:
    out = {
      "active": self.active,
      "slot_label": self.slot_label,
      "slot_start": self.slot_start,
      "slot_end": self.slot_end,
      "seconds_remaining": self.seconds_remaining,
      "elapsed_pct": round(self.elapsed_pct, 1),
      "bet_side": self.bet_side,
      "signal_at_open": self.signal_at_open,
      "reference_price": round(self.reference_price, 2),
      "reference_price_api": round(self.reference_price_api, 2) if self.reference_price_api is not None else None,
      "using_override": self.using_override,
      "current_price": round(self.current_price, 2),
      "unrealized_pct": round(self.unrealized_pct, 4),
      "unrealized_usd": round(self.unrealized_usd, 2),
      "action": self.action.value,
      "urgency": self.urgency,
      "message": self.message,
      "reasons": self.reasons,
      "reference_source": self.reference_source,
    }
    if self.current_price_as_of:
      out["current_price_as_of"] = self.current_price_as_of
    if self.live_price_age_sec is not None:
      out["live_price_age_sec"] = self.live_price_age_sec
    if self.kalshi:
      out["kalshi"] = self.kalshi
    if self.reassessed_prob_up is not None:
      out["reassessed_prob_up"] = round(self.reassessed_prob_up, 4)
      out["reassessed_prob_down"] = round(self.reassessed_prob_down or (1 - self.reassessed_prob_up), 4)
      out["reassessed_close_side"] = self.reassessed_close_side
      out["reassess_summary"] = self.reassess_summary
    return out


class ExitAdvisor:
  """Guide whether to hold, take profit, or cut loss during an active 15m slot."""

  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    self.tz = cfg.get("timezone", "America/New_York")
    intra = cfg.get("intra_slot", {})
    fees = cfg.get("fees", {})
    round_trip = (fees.get("taker_pct", 0.10) * 2 + cfg.get("slippage_pct", 0.05) * 2)
    self.take_profit_pct = float(intra.get("take_profit_pct", 0.18))
    self.stop_loss_pct = float(intra.get("stop_loss_pct", 0.15))
    self.lock_profit_min_pct = float(intra.get("lock_profit_min_pct", 0.10))
    self.late_window_minutes = float(intra.get("late_window_minutes", 3))
    self.fee_buffer_pct = float(intra.get("fee_buffer_pct", round_trip / 2))

  def _slot_momentum(self, df_1m: pd.DataFrame | None, slot_start: pd.Timestamp) -> float:
    """1m return over the active slot (positive = price rising)."""
    if df_1m is None or df_1m.empty:
      return 0.0
    df = df_1m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    in_slot = df[df["timestamp"] >= slot_start]
    if len(in_slot) < 2:
      return 0.0
    first = float(in_slot.iloc[0]["close"])
    last = float(in_slot.iloc[-1]["close"])
    if first <= 0:
      return 0.0
    return (last - first) / first * 100

  def _recent_1m_bias(self, df_1m: pd.DataFrame | None, slot_start: pd.Timestamp, bars: int = 3) -> float:
    """Sum of last N 1m bar returns inside the slot (%)."""
    if df_1m is None or df_1m.empty:
      return 0.0
    df = df_1m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    in_slot = df[df["timestamp"] >= slot_start]
    if len(in_slot) < 2:
      return 0.0
    tail = in_slot.tail(bars)
    rets = tail["close"].pct_change().dropna() * 100
    return float(rets.sum()) if len(rets) else 0.0

  def _reassess_prob_up_at_close(
    self,
    *,
    reference_price: float,
    current_price: float,
    seconds_remaining: int,
    recent_1m_pct: float,
    slot_mom_pct: float,
    original_prob_up: float,
  ) -> float:
    """Probability slot close finishes ABOVE t=0 reference, given now and time left."""
    if reference_price <= 0:
      return 0.5

    gap_pct = (current_price - reference_price) / reference_price * 100
    minutes_left = max(0.1, seconds_remaining / 60)
    time_pressure = 1.0 - min(1.0, minutes_left / 15.0)

    score = 0.5
    # Current level vs ref — matters more as the slot runs out
    score += np.clip(gap_pct * 0.12 * (0.2 + 0.8 * time_pressure), -0.38, 0.38)
    # Recent 1m flow shapes the remaining window
    score += np.clip(recent_1m_pct * 0.14, -0.14, 0.14)
    # Broader intra-slot drift
    score += np.clip(slot_mom_pct * 0.05 * (1 - time_pressure * 0.4), -0.1, 0.1)
    # Fade extreme moves when plenty of time remains
    if abs(gap_pct) > 0.12 and minutes_left > 5:
      score -= np.sign(gap_pct) * min(0.07, abs(gap_pct) * 0.04)

    open_weight = max(0.0, 0.22 * (1.0 - time_pressure))
    score = score * (1.0 - open_weight) + float(original_prob_up) * open_weight
    return float(np.clip(score, 0.05, 0.95))

  def _reassess_summary(
    self,
    prob_up: float,
    bet_side: str,
    seconds_remaining: int,
    reference_price: float,
  ) -> tuple[str, str]:
    """Return (close_side label, human summary) for slot end vs ref."""
    up_pct = prob_up * 100
    down_pct = (1 - prob_up) * 100
    mins, secs = divmod(max(0, seconds_remaining), 60)
    time_left = f"{mins}m {secs:02d}s"

    if prob_up >= 0.57:
      close_side = "UP"
    elif prob_up <= 0.43:
      close_side = "DOWN"
    else:
      close_side = "TOSS-UP"

    if bet_side == "UP":
      if close_side == "UP":
        line = f"Reassessed ({time_left} left): {up_pct:.0f}% UP at close — LONG still favored"
      elif close_side == "DOWN":
        line = f"Reassessed ({time_left} left): {down_pct:.0f}% DOWN at close — LONG at risk"
      else:
        line = f"Reassessed ({time_left} left): {up_pct:.0f}% UP / {down_pct:.0f}% DOWN at close"
    elif bet_side == "DOWN":
      if close_side == "UP":
        line = f"Reassessed ({time_left} left): {up_pct:.0f}% UP at close — DOWN unlikely to recover"
      elif close_side == "DOWN":
        line = f"Reassessed ({time_left} left): {down_pct:.0f}% DOWN at close — SHORT still viable"
      else:
        line = f"Reassessed ({time_left} left): {up_pct:.0f}% UP / {down_pct:.0f}% DOWN at close"
    else:
      line = f"Reassessed ({time_left} left): {up_pct:.0f}% UP at close vs ${reference_price:,.2f} ref"

    return close_side, line

  def evaluate(
    self,
    *,
    now: pd.Timestamp | None = None,
    reference_price: float,
    current_price: float,
    signal_at_open: str,
    df_1m: pd.DataFrame | None = None,
    slot_start: pd.Timestamp | None = None,
    original_prob_up: float = 0.5,
  ) -> SlotMonitor:
    now = pd.Timestamp(now or pd.Timestamp.now(tz="UTC"))
    if now.tzinfo is None:
      now = now.tz_localize("UTC")

    slot_s = slot_start or current_slot_start(now, self.tz)
    slot_e = slot_end(slot_s, self.tz)
    label = slot_label(slot_s, self.tz)

    total_sec = 15 * 60
    if now < slot_s:
      return SlotMonitor(
        active=False,
        slot_label=label,
        slot_start=slot_s.isoformat(),
        slot_end=slot_e.isoformat(),
        seconds_remaining=int((slot_e - now).total_seconds()),
        elapsed_pct=0.0,
        bet_side="NONE",
        signal_at_open=signal_at_open,
        reference_price=reference_price,
        current_price=current_price,
        unrealized_pct=0.0,
        action=ExitAction.WAIT,
        urgency="low",
        message="Slot has not started yet.",
        reasons=[],
      )

    if now >= slot_e:
      return SlotMonitor(
        active=False,
        slot_label=label,
        slot_start=slot_s.isoformat(),
        slot_end=slot_e.isoformat(),
        seconds_remaining=0,
        elapsed_pct=100.0,
        bet_side="NONE",
        signal_at_open=signal_at_open,
        reference_price=reference_price,
        current_price=current_price,
        unrealized_pct=0.0,
        action=ExitAction.WAIT,
        urgency="low",
        message="Slot ended — wait for the next :00/:15/:30/:45 prediction.",
        reasons=[],
      )

    elapsed = (now - slot_s).total_seconds()
    remaining = max(0, int((slot_e - now).total_seconds()))
    elapsed_pct = min(100.0, elapsed / total_sec * 100)
    late = remaining <= self.late_window_minutes * 60

    raw_move_pct = (current_price - reference_price) / reference_price * 100
    slot_mom = self._slot_momentum(df_1m, slot_s)
    recent = self._recent_1m_bias(df_1m, slot_s)

    if signal_at_open == Signal.LONG.value:
      bet_side = "UP"
      pnl_pct = raw_move_pct
      fav_mom = slot_mom > 0
      against_mom = slot_mom < -0.03 or recent < -0.04
    elif signal_at_open == Signal.SHORT.value:
      bet_side = "DOWN"
      pnl_pct = -raw_move_pct
      fav_mom = slot_mom < 0
      against_mom = slot_mom > 0.03 or recent > 0.04
    else:
      return SlotMonitor(
        active=True,
        slot_label=label,
        slot_start=slot_s.isoformat(),
        slot_end=slot_e.isoformat(),
        seconds_remaining=remaining,
        elapsed_pct=elapsed_pct,
        bet_side="NONE",
        signal_at_open=signal_at_open,
        reference_price=reference_price,
        current_price=current_price,
        unrealized_pct=0.0,
        action=ExitAction.NO_BET,
        urgency="low",
        message="No trade was recommended at slot open — nothing to manage.",
        reasons=["Opening signal was NO TRADE."],
      )

    reasons: list[str] = []
    action = ExitAction.HOLD
    urgency = "low"
    message = "Stay in — position is on track."

    net_after_fees = pnl_pct - self.fee_buffer_pct

    # --- Cut loss ---
    if pnl_pct <= -self.stop_loss_pct:
      action = ExitAction.CUT_LOSS
      urgency = "high"
      message = "Exit now to cap the loss."
      reasons.append(f"Down {abs(pnl_pct):.2f}% — past {self.stop_loss_pct:.2f}% stop.")

    elif pnl_pct < 0 and against_mom and elapsed_pct >= 35:
      action = ExitAction.CUT_LOSS
      urgency = "high"
      message = "Momentum is against you — consider exiting."
      reasons.append(f"Losing {abs(pnl_pct):.2f}% with price moving the wrong way.")
      reasons.append(f"Recent 1m flow: {recent:+.2f}%.")

    elif pnl_pct < -0.04 and late:
      action = ExitAction.CUT_LOSS
      urgency = "medium"
      message = "Little time left and you're underwater — limit damage."
      reasons.append(f"{remaining // 60}m {remaining % 60}s left with a losing position.")

    # --- Take profit (only if not already cut loss) ---
    elif pnl_pct >= self.take_profit_pct and (against_mom or late or pnl_pct >= self.take_profit_pct * 1.6):
      action = ExitAction.TAKE_PROFIT
      urgency = "medium" if net_after_fees > 0 else "high"
      message = "Lock in gains — move has largely played out."
      reasons.append(f"Up {pnl_pct:.2f}% — at/above {self.take_profit_pct:.2f}% target.")
      if against_mom:
        reasons.append("Short-term momentum is fading.")
      if late:
        reasons.append("Final minutes — secure profit before reversal.")

    elif pnl_pct >= self.lock_profit_min_pct and against_mom and net_after_fees > 0:
      action = ExitAction.TAKE_PROFIT
      urgency = "medium"
      message = "Take a safe return before the move reverses."
      reasons.append(f"In profit {pnl_pct:.2f}% but 1m tape turning ({recent:+.2f}%).")
      reasons.append(f"Above ~{self.fee_buffer_pct:.2f}% fee buffer.")

    elif pnl_pct > self.fee_buffer_pct and late and net_after_fees > 0:
      action = ExitAction.TAKE_PROFIT
      urgency = "medium"
      message = "Winning with minutes left — bank the gain."
      reasons.append(f"+{pnl_pct:.2f}% with under {self.late_window_minutes:.0f} min remaining.")

    # --- Hold ---
    else:
      if pnl_pct > 0:
        message = "Hold — still winning with room to run."
        reasons.append(f"+{pnl_pct:.2f}% from reference (t=0).")
        if fav_mom:
          reasons.append("Intra-slot momentum still supports your side.")
      elif pnl_pct > -self.stop_loss_pct * 0.5:
        message = "Hold — small drawdown, time and momentum may recover."
        reasons.append(f"{pnl_pct:+.2f}% — within normal noise.")
        if fav_mom:
          reasons.append("Price action still leaning your way.")
      else:
        message = "Hold cautiously — watch for stop breach."
        urgency = "medium"
        reasons.append(f"{pnl_pct:+.2f}% — approaching stop at -{self.stop_loss_pct:.2f}%.")

      if late and action == ExitAction.HOLD:
        reasons.append(f"{remaining // 60}m {remaining % 60}s until slot close.")

    reassessed_prob_up: float | None = None
    reassessed_prob_down: float | None = None
    reassessed_close_side = ""
    reassess_summary = ""

    if bet_side in ("UP", "DOWN"):
      reassessed_prob_up = self._reassess_prob_up_at_close(
        reference_price=reference_price,
        current_price=current_price,
        seconds_remaining=remaining,
        recent_1m_pct=recent,
        slot_mom_pct=slot_mom,
        original_prob_up=original_prob_up,
      )
      reassessed_prob_down = 1.0 - reassessed_prob_up
      reassessed_close_side, reassess_summary = self._reassess_summary(
        reassessed_prob_up, bet_side, remaining, reference_price,
      )
      reasons.append(reassess_summary)

      # Strong reassessment against the open bet → reinforce exit signals
      bet_losing = pnl_pct < 0
      reassess_against = (
        (bet_side == "DOWN" and reassessed_prob_up >= 0.55)
        or (bet_side == "UP" and reassessed_prob_up <= 0.45)
      )
      reassess_supports = (
        (bet_side == "UP" and reassessed_prob_up >= 0.55)
        or (bet_side == "DOWN" and reassessed_prob_up <= 0.45)
      )

      if action == ExitAction.CUT_LOSS:
        message = f"CUT LOSS — {reassess_summary}"
      elif action == ExitAction.TAKE_PROFIT:
        message = f"TAKE PROFIT — {reassess_summary}"
      elif bet_losing and reassess_against and action == ExitAction.HOLD:
        action = ExitAction.CUT_LOSS
        urgency = "high"
        message = f"CUT LOSS — {reassess_summary}"
        reasons.append("Live reassessment turned against your position.")
      elif action == ExitAction.HOLD and reassess_supports and pnl_pct > 0:
        reasons.append("Reassessment supports holding to slot close.")

    return SlotMonitor(
      active=True,
      slot_label=label,
      slot_start=slot_s.isoformat(),
      slot_end=slot_e.isoformat(),
      seconds_remaining=remaining,
      elapsed_pct=elapsed_pct,
      bet_side=bet_side,
      signal_at_open=signal_at_open,
      reference_price=reference_price,
      current_price=current_price,
      unrealized_pct=pnl_pct,
      unrealized_usd=reference_price * (pnl_pct / 100),
      action=action,
      urgency=urgency,
      message=message,
      reasons=reasons,
      reassessed_prob_up=reassessed_prob_up,
      reassessed_prob_down=reassessed_prob_down,
      reassessed_close_side=reassessed_close_side,
      reassess_summary=reassess_summary,
    )
