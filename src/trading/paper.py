from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from src.trading.edge import EdgeCalculator, Signal


@dataclass
class Trade:
  timestamp: datetime
  direction: str
  entry_price: float
  size_usd: float
  prob_up: float
  exit_price: float | None = None
  pnl: float | None = None
  closed: bool = False


@dataclass
class PaperAccount:
  bankroll: float
  initial_bankroll: float
  trades: list[Trade] = field(default_factory=list)
  consecutive_losses: int = 0
  daily_pnl: float = 0.0
  current_day: date | None = None
  halted: bool = False
  halt_reason: str = ""


class PaperTrader:
  """Stage 3 / Phase 5: simulated trading with risk limits."""

  def __init__(self, cfg: dict[str, Any]):
    paper = cfg.get("paper", {})
    self.bankroll = paper.get("bankroll", 100.0)
    self.risk_pct = paper.get("risk_per_trade_pct", 1.0) / 100
    self.max_consecutive_losses = paper.get("max_consecutive_losses", 3)
    self.max_daily_dd_pct = paper.get("max_daily_drawdown_pct", 5.0) / 100
    self.horizon = cfg.get("prediction_horizon_minutes", 5)
    self.edge = EdgeCalculator(cfg)

    self.account = PaperAccount(
      bankroll=self.bankroll,
      initial_bankroll=self.bankroll,
    )

  def _check_daily_reset(self) -> None:
    today = date.today()
    if self.account.current_day != today:
      self.account.current_day = today
      self.account.daily_pnl = 0.0
      if self.account.halted and "daily" in self.account.halt_reason.lower():
        self.account.halted = False
        self.account.halt_reason = ""

  def can_trade(self) -> tuple[bool, str]:
    self._check_daily_reset()
    if self.account.halted:
      return False, self.account.halt_reason
    if self.account.consecutive_losses >= self.max_consecutive_losses:
      return False, f"Stopped: {self.max_consecutive_losses} consecutive losses"
    if self.account.daily_pnl <= -self.max_daily_dd_pct * self.account.initial_bankroll:
      self.account.halted = True
      self.account.halt_reason = "Daily drawdown limit hit"
      return False, self.account.halt_reason
    return True, ""

  def open_trade(self, timestamp: datetime, price: float, prob_up: float) -> Trade | None:
    ok, reason = self.can_trade()
    if not ok:
      return None

    signal = self.edge.recommend(prob_up)
    if signal == Signal.NO_TRADE:
      return None

    size = self.account.bankroll * self.risk_pct
    trade = Trade(
      timestamp=timestamp,
      direction=signal.value,
      entry_price=price,
      size_usd=size,
      prob_up=prob_up,
    )
    self.account.trades.append(trade)
    return trade

  def close_trade(self, trade: Trade, exit_price: float) -> float:
    if trade.direction == "LONG":
      ret = (exit_price - trade.entry_price) / trade.entry_price
    else:
      ret = (trade.entry_price - exit_price) / trade.entry_price

    # Deduct round-trip fees
    fees = self.edge.round_trip_cost
    net_ret = ret - fees
    pnl = trade.size_usd * net_ret

    trade.exit_price = exit_price
    trade.pnl = pnl
    trade.closed = True

    self.account.bankroll += pnl
    self.account.daily_pnl += pnl

    if pnl < 0:
      self.account.consecutive_losses += 1
      if self.account.consecutive_losses >= self.max_consecutive_losses:
        self.account.halted = True
        self.account.halt_reason = f"Stopped: {self.max_consecutive_losses} consecutive losses"
    else:
      self.account.consecutive_losses = 0

    return pnl

  def summary(self) -> dict[str, Any]:
    closed = [t for t in self.account.trades if t.closed]
    wins = [t for t in closed if t.pnl and t.pnl > 0]
    return {
      "bankroll": self.account.bankroll,
      "total_pnl": self.account.bankroll - self.account.initial_bankroll,
      "n_trades": len(closed),
      "win_rate": len(wins) / len(closed) if closed else 0,
      "halted": self.account.halted,
      "halt_reason": self.account.halt_reason,
    }
