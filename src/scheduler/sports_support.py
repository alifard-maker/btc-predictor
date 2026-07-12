"""Scheduler wiring for the sports arb paper scanner."""

from __future__ import annotations

import logging
from typing import Any

from src.data.sports_markets import sports_cfg, sports_enabled
from src.trading.sports_arb_bot import SportsArbBot, sports_db_path
from src.trading.sports_arb_store import SportsArbStore

log = logging.getLogger(__name__)


def init_sports(loop: Any) -> None:
  cfg = loop.cfg
  if not sports_enabled(cfg):
    loop._sports_arb_bot = None
    loop._sports_arb_store = None
    return
  store = SportsArbStore(sports_db_path(cfg))
  kalshi = getattr(loop, "kalshi", None)
  loop._sports_arb_store = store
  loop._sports_arb_bot = SportsArbBot(cfg, store, kalshi=kalshi)
  log.info("sports arb module initialized (db=%s)", sports_db_path(cfg))


def sports_arb_bot(loop: Any) -> SportsArbBot | None:
  return getattr(loop, "_sports_arb_bot", None)


def sports_arb_store(loop: Any) -> SportsArbStore | None:
  return getattr(loop, "_sports_arb_store", None)


def run_sports_arb_scan(loop: Any) -> dict[str, Any]:
  bot = sports_arb_bot(loop)
  if bot is None:
    if sports_enabled(loop.cfg):
      init_sports(loop)
      bot = sports_arb_bot(loop)
    if bot is None:
      return {"ok": False, "error": "sports_disabled"}
  return bot.run_scan_cycle()


def sports_status(loop: Any) -> dict[str, Any]:
  bot = sports_arb_bot(loop)
  if bot is None:
    if sports_enabled(loop.cfg):
      init_sports(loop)
      bot = sports_arb_bot(loop)
    if bot is None:
      return {"ok": False, "error": "sports_disabled", "enabled": False}
  return bot.status()


def schedule_sports_jobs(loop: Any, scheduler) -> None:
  if not sports_enabled(loop.cfg):
    return
  init_sports(loop)
  poll = int(sports_cfg(loop.cfg).get("poll_seconds", 30))
  scheduler.add_job(
    lambda: run_sports_arb_scan(loop),
    "interval",
    seconds=max(10, poll),
    id="sports_arb_scan",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=45,
    replace_existing=True,
  )
  log.info("scheduled sports_arb_scan every %ss", max(10, poll))
