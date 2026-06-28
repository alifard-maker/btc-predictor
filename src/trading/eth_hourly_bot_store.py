"""Backward-compatible aliases for ETH hourly bot store."""

from src.trading.hourly_bot_store import HourlyBotSettings as EthHourlyBotSettings
from src.trading.hourly_bot_store import HourlyBotStore as EthHourlyBotStore

__all__ = ["EthHourlyBotSettings", "EthHourlyBotStore"]
