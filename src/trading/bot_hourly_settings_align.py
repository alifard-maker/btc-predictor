"""Hourly bot settings are independent per asset (BTC vs ETH)."""

from __future__ import annotations

from typing import Any


def align_eth_hourly_settings_from_btc(loop: Any) -> dict[str, Any]:
  """No-op: each hourly bot keeps its own mode, Auto-bet, and max at-risk."""
  del loop
  return {"skipped": True, "reason": "independent_bot_settings"}
