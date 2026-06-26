"""Flip-to-opposite — exit a losing open LONG/SHORT and recommend the other side."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.trading.late_entry import LateEntryAdvisor, SlotPathStats


@dataclass(frozen=True)
class FlipDecision:
  action: str  # FLIP LONG | FLIP SHORT
  prob_up: float
  summary: str
  reasons: list[str]


class FlipAdvisor:
  """Recommend flipping once per slot when open bet is losing and tape turned."""

  def __init__(self, cfg: dict[str, Any]):
    fcfg = cfg.get("flip", {})
    self.enabled = bool(fcfg.get("enabled", True))
    self.min_elapsed_min = float(fcfg.get("min_elapsed_minutes", 3))
    self.min_remaining_min = float(fcfg.get("min_remaining_minutes", 5))
    self.min_confidence = float(fcfg.get("min_confidence", 0.63))
    self.min_loss_pct = float(fcfg.get("min_loss_pct", 0.15))
    self.min_move_pct = float(fcfg.get("min_move_pct", 0.06))
    self.min_side_time_pct = float(fcfg.get("min_side_time_pct", 0.55))
    self.min_momentum_pct = float(fcfg.get("min_momentum_pct", 0.02))
    self.max_flips_per_slot = int(fcfg.get("max_flips_per_slot", 1))
    self.late = LateEntryAdvisor(cfg)

  def evaluate(
    self,
    *,
    signal_at_open: str,
    open_pnl_pct: float,
    elapsed_minutes: float,
    seconds_remaining: int,
    stats: SlotPathStats,
    reassessed_prob_up: float,
    existing_flip: str = "",
    flip_count: int = 0,
  ) -> FlipDecision | None:
    if not self.enabled or self.max_flips_per_slot <= 0:
      return None
    if flip_count >= self.max_flips_per_slot or existing_flip:
      return None
    if signal_at_open not in ("LONG", "SHORT"):
      return None

    minutes_left = seconds_remaining / 60
    if elapsed_minutes < self.min_elapsed_min:
      return None
    if minutes_left < self.min_remaining_min:
      return None
    if open_pnl_pct > -self.min_loss_pct:
      return None
    if stats.bars < 3:
      return None

    chop_ok, chop_note = self.late._chop_recovery_ok(stats)
    if not chop_ok:
      return None

    mins, secs = divmod(max(0, seconds_remaining), 60)
    time_left = f"{mins}m {secs:02d}s"
    outlook = (
      f"Outlook ({time_left} left): {reassessed_prob_up * 100:.0f}% UP at close"
    )

    if signal_at_open == "LONG":
      short_ok = (
        reassessed_prob_up <= (1.0 - self.min_confidence)
        and stats.gap_pct < 0
        and abs(stats.gap_pct) >= self.min_move_pct
        and stats.pct_time_above_ref <= (1.0 - self.min_side_time_pct)
        and stats.recent_mom_pct <= -self.min_momentum_pct
        and stats.slot_mom_pct <= 0
      )
      if not short_ok:
        return None
      reasons = [
        f"Open LONG down {abs(open_pnl_pct):.2f}% — past {self.min_loss_pct:.2f}% loss floor.",
        outlook,
        f"Held below t=0 ~{(1 - stats.pct_time_above_ref) * 100:.0f}% of slot; drift {stats.slot_mom_pct:+.2f}%.",
        f"Recent 1m flow {stats.recent_mom_pct:+.2f}% supports DOWN finish.",
      ]
      if chop_note:
        reasons.insert(1, chop_note)
      return FlipDecision(
        action="FLIP SHORT",
        prob_up=reassessed_prob_up,
        summary=f"FLIP SHORT — exit LONG, bet DOWN ({outlook})",
        reasons=reasons,
      )

    long_ok = (
      reassessed_prob_up >= self.min_confidence
      and stats.gap_pct > 0
      and abs(stats.gap_pct) >= self.min_move_pct
      and stats.pct_time_above_ref >= self.min_side_time_pct
      and stats.recent_mom_pct >= self.min_momentum_pct
      and stats.slot_mom_pct >= 0
    )
    if not long_ok:
      return None
    reasons = [
      f"Open SHORT down {abs(open_pnl_pct):.2f}% — past {self.min_loss_pct:.2f}% loss floor.",
      outlook,
      f"Held above t=0 ~{stats.pct_time_above_ref * 100:.0f}% of slot; drift {stats.slot_mom_pct:+.2f}%.",
      f"Recent 1m flow {stats.recent_mom_pct:+.2f}% supports UP finish.",
    ]
    if chop_note:
      reasons.insert(1, chop_note)
    return FlipDecision(
      action="FLIP LONG",
      prob_up=reassessed_prob_up,
      summary=f"FLIP LONG — exit SHORT, bet UP ({outlook})",
      reasons=reasons,
    )
