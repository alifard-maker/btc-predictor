"""Regime gates for hourly Kalshi threshold picks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.trading.contract_signals import is_actionable_buy
from src.trading.entry_strategy import passes_ask_edge_gate


@dataclass(frozen=True)
class HourlyRegimeDecision:
  allow_trade: bool
  reasons: list[str]


class HourlyRegimeFilter:
  def __init__(self, cfg: dict[str, Any]):
    hcfg = cfg.get("hourly", {}).get("regime", {})
    self.enabled = bool(hcfg.get("enabled", True))
    self.min_expected_move_pct = float(hcfg.get("min_expected_move_pct", 0.12))
    self.min_edge = float(hcfg.get("min_edge", cfg.get("hourly", {}).get("min_edge", 0.05)))
    self.min_hours_to_settle = float(hcfg.get("min_hours_to_settle", 0.25))
    self.max_sigma_pct = float(hcfg.get("max_sigma_pct", 2.5))
    self.min_reasons_to_block = int(hcfg.get("min_reasons_to_block", 2))

  def evaluate(
    self,
    *,
    expected_move_pct: float,
    hours_to_settle: float,
    sigma_pct: float,
    edge: float | None,
    compression: float | None = None,
  ) -> HourlyRegimeDecision:
    if not self.enabled:
      return HourlyRegimeDecision(True, [])

    reasons: list[str] = []
    if abs(expected_move_pct) < self.min_expected_move_pct:
      reasons.append(
        f"Expected move {expected_move_pct:.2f}% below {self.min_expected_move_pct:.2f}% floor"
      )
    if hours_to_settle < self.min_hours_to_settle:
      reasons.append(f"Only {hours_to_settle:.2f}h to settle — too late for new lean")
    if sigma_pct > self.max_sigma_pct:
      reasons.append(f"Terminal σ {sigma_pct:.2f}% — wide uncertainty")
    if edge is not None and abs(edge) < self.min_edge:
      reasons.append(f"Edge {edge * 100:.1f}¢ below minimum")
    if compression is not None and not pd.isna(compression) and float(compression) > 1.15:
      reasons.append(f"Range compressed ({float(compression):.2f}×)")

    block = len(reasons) >= self.min_reasons_to_block
    return HourlyRegimeDecision(allow_trade=not block, reasons=reasons)


def min_hours_to_settle_for_entry(cfg: dict[str, Any] | None) -> float:
  """Minimum time left before hourly bot may open new legs (always enforced)."""
  hourly = (cfg or {}).get("hourly") or {}
  bot = hourly.get("bot") or {}
  if "min_hours_to_settle_for_entry" in bot:
    return float(bot["min_hours_to_settle_for_entry"])
  regime = hourly.get("regime") or {}
  return float(regime.get("min_hours_to_settle", 0.25))


def max_hours_to_settle_for_entry(cfg: dict[str, Any] | None) -> float:
  """Maximum hours-to-settle for new entries (blocks far-future hourly events)."""
  hourly = (cfg or {}).get("hourly") or {}
  bot = hourly.get("bot") or {}
  if "max_hours_to_settle_for_entry" in bot:
    return float(bot["max_hours_to_settle_for_entry"])
  return 1.25


@dataclass(frozen=True)
class LateEntryConfig:
  enabled: bool = True
  min_hours: float = 0.08
  min_ask_edge_cents: float = 15.0
  max_stake_usd: float = 2.50


def late_entry_config(cfg: dict[str, Any] | None) -> LateEntryConfig:
  bot = ((cfg or {}).get("hourly") or {}).get("bot") or {}
  raw = bot.get("late_entry") or {}
  return LateEntryConfig(
    enabled=bool(raw.get("enabled", True)),
    min_hours=float(raw.get("min_hours", 0.08)),
    min_ask_edge_cents=float(raw.get("min_ask_edge_cents", 15)),
    max_stake_usd=float(raw.get("max_stake_usd", 2.50)),
  )


def late_entry_pick_allowed(
  hours_to_settle: float | None,
  pick: dict[str, Any],
  side: str,
  cfg: dict[str, Any] | None,
) -> bool:
  """True when a pick may enter in the late-hour window (strong ask-edge exception)."""
  if hours_to_settle is None:
    return False
  le = late_entry_config(cfg)
  if not le.enabled:
    return False
  h = float(hours_to_settle)
  min_h = min_hours_to_settle_for_entry(cfg)
  if h < le.min_hours or h >= min_h:
    return False
  if not is_actionable_buy(pick.get("signal")):
    return False
  ok, _ = passes_ask_edge_gate(pick, side, le.min_ask_edge_cents)
  return ok


def is_late_entry_path(
  hours_to_settle: float | None,
  pick: dict[str, Any],
  side: str,
  cfg: dict[str, Any] | None,
) -> bool:
  return late_entry_pick_allowed(hours_to_settle, pick, side, cfg)


def entry_too_close_to_settle_skip_reason(
  hours_to_settle: float | None,
  cfg: dict[str, Any] | None,
) -> str | None:
  """Cycle-level settle gate — blocks only below late-entry floor or normal min when disabled."""
  if hours_to_settle is None:
    return None
  h = float(hours_to_settle)
  min_h = min_hours_to_settle_for_entry(cfg)
  le = late_entry_config(cfg)
  floor_h = le.min_hours if le.enabled else min_h
  if h < floor_h:
    return "too_late_for_new_entries"
  if le.enabled and h < min_h:
    return None
  if h < min_h:
    return "too_late_for_new_entries"
  return None


def entry_pick_settle_skip_reason(
  hours_to_settle: float | None,
  cfg: dict[str, Any] | None,
  *,
  pick: dict[str, Any],
  side: str,
) -> str | None:
  """Per-pick settle gate — applies late-entry exception when configured."""
  if hours_to_settle is None:
    return None
  h = float(hours_to_settle)
  min_h = min_hours_to_settle_for_entry(cfg)
  le = late_entry_config(cfg)
  floor_h = le.min_hours if le.enabled else min_h
  if h < floor_h:
    return "too_late_for_new_entries"
  if h < min_h:
    if late_entry_pick_allowed(hours_to_settle, pick, side, cfg):
      return None
    return "too_late_for_new_entries"
  return None


def entry_too_far_from_settle_skip_reason(
  hours_to_settle: float | None,
  cfg: dict[str, Any] | None,
) -> str | None:
  if hours_to_settle is None:
    return None
  max_h = max_hours_to_settle_for_entry(cfg)
  if float(hours_to_settle) > max_h:
    return "too_far_for_new_entries"
  return None
