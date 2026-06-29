"""Shared default bot dashboard settings (startup + fresh-start baseline)."""

from __future__ import annotations

from typing import Any


# Matches dashboard “starting reset” for paper bots:
# Auto-bet OFF, Paper ON, Live OFF, $25 cap, STRONG/ACTIONABLE OFF,
# Use profits OFF, Auto-refill ON.
BOT_DASHBOARD_DEFAULTS: dict[str, Any] = {
  "enabled": False,
  "mode": "paper",
  "allow_strong": False,
  "allow_actionable": False,
  "use_accumulated_profit": False,
  "profit_use_pct": 100.0,
  "paper_auto_refill": True,
  "auto_stopped": False,
}
