"""Kalshi fee schedule for backtest P&L."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FeeSchedule:
  """Fee rates in basis points of notional (contract price × contracts)."""

  maker_bps: float = 10.0
  taker_bps: float = 10.0

  @classmethod
  def from_config(cls, cfg: dict[str, Any]) -> FeeSchedule:
    bt = cfg.get("backtest", {}).get("fees", {})
    top = cfg.get("fees", {})
    maker = bt.get("maker_bps", top.get("maker_pct", 0.10) * 10)
    taker = bt.get("taker_bps", top.get("taker_pct", 0.10) * 10)
    return cls(maker_bps=float(maker), taker_bps=float(taker))


class FeeModel:
  """Compute per-leg and round-trip fees on Kalshi binary contracts."""

  def __init__(self, schedule: FeeSchedule | None = None, cfg: dict[str, Any] | None = None):
    if schedule is not None:
      self.schedule = schedule
    elif cfg is not None:
      self.schedule = FeeSchedule.from_config(cfg)
    else:
      self.schedule = FeeSchedule()

  def notional_usd(self, price_cents: int, contracts: int) -> float:
    return contracts * price_cents / 100.0

  def leg_fee_usd(self, price_cents: int, contracts: int, *, is_maker: bool) -> float:
    bps = self.schedule.maker_bps if is_maker else self.schedule.taker_bps
    return round(self.notional_usd(price_cents, contracts) * bps / 10_000.0, 4)

  def round_trip_fee_usd(
    self,
    entry_price_cents: int,
    exit_price_cents: int,
    contracts: int,
    *,
    entry_maker: bool,
    exit_maker: bool,
  ) -> float:
    entry = self.leg_fee_usd(entry_price_cents, contracts, is_maker=entry_maker)
    exit_fee = self.leg_fee_usd(exit_price_cents, contracts, is_maker=exit_maker)
    return round(entry + exit_fee, 4)

  def settlement_pnl_usd(
    self,
    *,
    side: str,
    entry_price_cents: int,
    contracts: int,
    won: bool,
    entry_maker: bool,
  ) -> float:
    """P&L for a held-to-settlement leg (exit at 100¢ if win, 0¢ if loss)."""
    exit_cents = 100 if won else 0
    gross = contracts * (exit_cents - entry_price_cents) / 100.0
    fees = self.leg_fee_usd(entry_price_cents, contracts, is_maker=entry_maker)
    return round(gross - fees, 4)
