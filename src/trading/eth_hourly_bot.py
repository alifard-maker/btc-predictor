"""Backward-compatible aliases for ETH hourly bot."""

from src.trading.hourly_bot import (
  HourlyBot as EthHourlyBot,
  _contracts_for_budget,
  bet_qualifies,
)
from src.trading.hourly_bot_store import HourlyBotSettings as EthHourlyBotSettings
from src.trading.hourly_bot_store import HourlyBotStore as EthHourlyBotStore

__all__ = [
  "EthHourlyBot",
  "EthHourlyBotSettings",
  "EthHourlyBotStore",
  "bet_qualifies",
  "_contracts_for_budget",
]
