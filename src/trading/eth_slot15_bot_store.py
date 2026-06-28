"""ETH 15m auto-bet bot store — re-exports shared implementation."""

from src.trading.slot15_bot_store import Slot15BotSettings as EthSlot15BotSettings
from src.trading.slot15_bot_store import Slot15BotStore as EthSlot15BotStore

__all__ = ["EthSlot15BotSettings", "EthSlot15BotStore"]
