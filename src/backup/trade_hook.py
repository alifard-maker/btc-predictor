"""Fire-and-forget trade backup hook from bot stores."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CFG: dict[str, Any] | None = None


def _cfg() -> dict[str, Any]:
  global _CFG
  if _CFG is None:
    from src.config import load_config

    _CFG = load_config()
  return _CFG


def _bot_meta(db_path: Path) -> tuple[str, str]:
  name = db_path.stem
  asset = "eth" if name.endswith("_eth") else "btc"
  kind = "slot15" if "slot15" in name else "hourly"
  return asset, kind


def notify_trade_logged(db_path: Path, *, trade: dict[str, Any]) -> None:
  try:
    from src.backup.logs_backup import on_trade_logged

    asset, kind = _bot_meta(db_path)
    on_trade_logged(_cfg(), kind=kind, asset=asset, trade=trade)
  except Exception as e:
    log.debug("Trade backup hook skipped: %s", e)
