"""ETH 15m auto-bet bot — re-exports shared implementation."""

from src.trading.slot15_bot import (
  Slot15Bot as EthSlot15Bot,
  bet_qualifies as eth_slot15_bet_qualifies,
  enrich_open_positions_live as enrich_eth_slot15_open_positions_live,
)
from src.trading.slot15_bot_store import Slot15BotSettings as EthSlot15BotSettings
from src.trading.slot15_bot_store import Slot15BotStore as EthSlot15BotStore

__all__ = [
  "EthSlot15Bot",
  "EthSlot15BotSettings",
  "EthSlot15BotStore",
  "eth_slot15_bet_qualifies",
  "enrich_eth_slot15_open_positions_live",
]
