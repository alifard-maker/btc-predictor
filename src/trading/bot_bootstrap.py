"""Optional env bootstrap for 24/7 paper bots on fresh Railway deploys."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
  from src.scheduler.loop import PredictionLoop

log = logging.getLogger(__name__)

# PAPER_BOT_AUTO_ENABLE=btc,eth,slot15,eth-slot15,all
_TARGET_ALIASES: dict[str, tuple[str, str]] = {
  "btc": ("hourly", "btc"),
  "btc-hourly": ("hourly", "btc"),
  "eth": ("hourly", "eth"),
  "eth-hourly": ("hourly", "eth"),
  "slot15": ("slot15", "btc"),
  "btc-slot15": ("slot15", "btc"),
  "eth-slot15": ("slot15", "eth"),
  "spx": ("hourly", "spx"),
  "spx-hourly": ("hourly", "spx"),
  "ndx": ("hourly", "ndx"),
  "ndx-hourly": ("hourly", "ndx"),
}

_ALL_TARGETS: list[tuple[str, str]] = [
  ("hourly", "btc"),
  ("hourly", "eth"),
  ("hourly", "spx"),
  ("hourly", "ndx"),
  ("slot15", "btc"),
  ("slot15", "eth"),
]


def parse_auto_enable_tokens(raw: str) -> list[tuple[str, str]]:
  tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
  out: list[tuple[str, str]] = []
  seen: set[tuple[str, str]] = set()
  for token in tokens:
    if token == "all":
      for target in _ALL_TARGETS:
        if target not in seen:
          seen.add(target)
          out.append(target)
      continue
    target = _TARGET_ALIASES.get(token)
    if target and target not in seen:
      seen.add(target)
      out.append(target)
  return out


def _store_for(loop: PredictionLoop, kind: str, asset: str) -> Any:
  if kind == "hourly":
    return loop.hourly_bot_store(asset)
  return loop.slot15_bot_store(asset)


def _eth_available(loop: PredictionLoop, asset: str) -> bool:
  if asset != "eth":
    return True
  from src.assets import asset_enabled

  if not asset_enabled(loop.cfg, "eth"):
    return False
  if asset == "eth" and hasattr(loop, "_slot15m_enabled"):
    # slot15 eth may be disabled while hourly eth is on — checked per kind by caller
    return True
  return True


def _should_auto_enable(store: Any) -> bool:
  """Only on a fresh bot DB: disabled and no trades logged yet."""
  settings = store.get_settings()
  if settings.enabled:
    return False
  return len(store.list_trades(limit=1)) == 0


def bootstrap_paper_bots(loop: PredictionLoop) -> list[str]:
  raw = os.getenv("PAPER_BOT_AUTO_ENABLE", "").strip()
  if not raw:
    return []

  activated: list[str] = []
  for kind, asset in parse_auto_enable_tokens(raw):
    if asset == "eth" and kind == "slot15" and not loop._slot15m_enabled("eth"):
      continue
    if asset == "eth" and kind == "hourly" and not _eth_available(loop, asset):
      continue
    if asset in ("spx", "ndx") and kind == "hourly":
      from src.assets import asset_enabled

      if not asset_enabled(loop.cfg, asset):
        continue

    store = _store_for(loop, kind, asset)
    if not _should_auto_enable(store):
      continue

    settings = store.get_settings()
    settings_cls = type(settings)
    updated = settings_cls(**{**settings.to_dict(), "enabled": True, "mode": "paper"})
    store.save_settings(updated)
    label = f"{asset}-{kind}"
    activated.append(label)
    log.info("PAPER_BOT_AUTO_ENABLE: enabled paper auto-bet for %s", label)

  return activated
