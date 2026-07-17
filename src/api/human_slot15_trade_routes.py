"""API routes for dashboard manual (human) 15m slot trading."""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request

from src.trading.human_slot15_trade import (
  apply_human_settings_body,
  enrich_slot15_human_fast_marks,
  enrich_slot15_human_marks,
  execute_slot15_manual_enter,
  execute_slot15_manual_exit,
  market_summary_from_tab,
  preview_slot15_manual_entry,
  settle_expired_slot15_human_positions,
  slot_key_from_tab,
)
from src.trading.live_mode_auth import live_bet_password, require_live_password

log = logging.getLogger(__name__)


def _slot15_tab(loop: Any, asset: str) -> dict[str, Any] | None:
  try:
    tab = loop._slot15_tab(asset)
  except Exception as e:
    log.warning("slot15 tab failed for %s: %s", asset, e)
    return None
  return tab if tab and tab.get("ok") else tab


def _acfg_15m(loop: Any, asset: str, get_cfg: Callable[[], dict[str, Any]]) -> dict[str, Any]:
  if hasattr(loop, "_acfg_15m"):
    return loop._acfg_15m(asset)
  from src.assets import asset_cfg

  cfg = get_cfg()
  return cfg if asset == "btc" else asset_cfg(cfg, asset)


def _settle_price_from_tab(tab: dict[str, Any] | None) -> float | None:
  if not tab:
    return None
  monitor = tab.get("monitor") or {}
  raw = tab.get("brti_live") or monitor.get("current_price")
  try:
    px = float(raw) if raw is not None else None
  except (TypeError, ValueError):
    return None
  return px if px is not None and px > 0 else None


def _index_id(asset: str, tab: dict[str, Any] | None) -> str:
  if tab:
    label = (tab.get("index_label") or "").strip()
    if label:
      return label
  return "ERTI" if asset == "eth" else "BRTI"


def register_human_slot15_trade_routes(
  app: Any,
  *,
  get_loop: Callable[[], Any],
  get_cfg: Callable[[], dict[str, Any]],
  session_dep: Any,
) -> None:
  """Mount /api/15m/human-trades/* and /api/eth/15m/human-trades/*."""

  def _mount(asset: str, prefix: str) -> None:
    @app.get(f"{prefix}/human-trades/status")
    def human_slot15_trades_status(_: None = Depends(session_dep)):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      store = loop.human_slot15_trade_store(asset)
      tab = _slot15_tab(loop, asset)
      acfg = _acfg_15m(loop, asset, get_cfg)
      slot_key = slot_key_from_tab(tab) if tab and tab.get("ok") else None
      settled = settle_expired_slot15_human_positions(
        store,
        current_slot_key=slot_key,
        tab=tab if tab and tab.get("ok") else None,
        cfg=acfg,
        kalshi=loop._kalshi_for(asset),
        index_id=_index_id(asset, tab),
        settle_price=_settle_price_from_tab(tab),
      )
      status = store.status(slot_key)
      if settled:
        status["slot_settlements"] = [
          {
            "id": t.get("id"),
            "label": t.get("label"),
            "pnl_usd": t.get("pnl_usd"),
            "exit_price_cents": t.get("exit_price_cents"),
            "detail": t.get("detail"),
          }
          for t in settled
        ]
      open_pos = list(status.get("open_positions") or [])
      if open_pos:
        status["open_positions"] = enrich_slot15_human_fast_marks(
          open_pos,
          kalshi=loop._kalshi_for(asset),
          tab=tab if tab and tab.get("ok") else None,
        )
      else:
        status["open_positions"] = enrich_slot15_human_marks(
          open_pos,
          tab if tab and tab.get("ok") else None,
        )
      ur_sum = 0.0
      ur_n = 0
      for p in status["open_positions"]:
        if p.get("unrealized_pnl_usd") is not None:
          ur_sum += float(p["unrealized_pnl_usd"])
          ur_n += 1
      paper_pnl = dict(status.get("paper_pnl") or {})
      paper_pnl["open_unrealized_pnl_usd"] = round(ur_sum, 2) if ur_n else None
      paper_pnl["open_marked_legs"] = ur_n
      status["paper_pnl"] = paper_pnl
      return {
        "ok": True,
        "asset": asset,
        "product": "slot15",
        "slot_key": slot_key,
        "market": market_summary_from_tab(tab if tab and tab.get("ok") else None),
        "spot_price": _settle_price_from_tab(tab),
        "index_label": _index_id(asset, tab),
        "status": status,
      }

    @app.get(f"{prefix}/human-trades/marks")
    def human_slot15_trades_marks(_: None = Depends(session_dep)):
      from datetime import datetime, timezone

      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      store = loop.human_slot15_trade_store(asset)
      tab = _slot15_tab(loop, asset)
      acfg = _acfg_15m(loop, asset, get_cfg)
      slot_key = slot_key_from_tab(tab) if tab and tab.get("ok") else None
      settle_expired_slot15_human_positions(
        store,
        current_slot_key=slot_key,
        tab=tab if tab and tab.get("ok") else None,
        cfg=acfg,
        kalshi=loop._kalshi_for(asset),
        index_id=_index_id(asset, tab),
        settle_price=_settle_price_from_tab(tab),
      )
      open_positions = list(store.open_positions())
      if not open_positions:
        return {
          "ok": True,
          "asset": asset,
          "product": "slot15",
          "open_positions": [],
          "open_unrealized_pnl_usd": None,
          "marked_at": datetime.now(timezone.utc).isoformat(),
          "quote_source": "none",
        }
      enriched = enrich_slot15_human_fast_marks(
        open_positions,
        kalshi=loop._kalshi_for(asset),
        tab=tab if tab and tab.get("ok") else None,
      )
      ur_sum = 0.0
      ur_n = 0
      for p in enriched:
        if p.get("unrealized_pnl_usd") is not None:
          ur_sum += float(p["unrealized_pnl_usd"])
          ur_n += 1
      return {
        "ok": True,
        "asset": asset,
        "product": "slot15",
        "open_positions": enriched,
        "open_unrealized_pnl_usd": round(ur_sum, 2) if ur_n else None,
        "open_marked_legs": ur_n,
        "marked_at": datetime.now(timezone.utc).isoformat(),
        "quote_source": "kalshi_live",
      }

    @app.post(f"{prefix}/human-trades/settings")
    async def human_slot15_trades_settings(
      request: Request,
      _: None = Depends(session_dep),
    ):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      cfg = _acfg_15m(loop, asset, get_cfg)
      body = await request.json()
      store = loop.human_slot15_trade_store(asset)
      settings = store.get_settings()
      if "mode" in body:
        require_live_password(
          settings.mode,
          str(body.get("mode", settings.mode)),
          body,
          live_bet_password(cfg),
        )
      apply_human_settings_body(store, body, cfg=cfg)
      tab = _slot15_tab(loop, asset)
      slot_key = slot_key_from_tab(tab) if tab and tab.get("ok") else None
      return {"ok": True, "status": store.status(slot_key)}

    @app.post(f"{prefix}/human-trades/preview")
    async def human_slot15_trades_preview(
      request: Request,
      _: None = Depends(session_dep),
    ):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      cfg = _acfg_15m(loop, asset, get_cfg)
      body = await request.json()
      store = loop.human_slot15_trade_store(asset)
      settings = store.get_settings()
      mode = str(body.get("mode") or settings.mode).lower()
      tab = _slot15_tab(loop, asset)
      return preview_slot15_manual_entry(
        store=store,
        tab=tab,
        market_ticker=str(body.get("market_ticker") or ""),
        side=str(body.get("side") or ""),
        mode=mode,
        cfg=cfg,
        asset=asset,
      )

    @app.post(f"{prefix}/human-trades/enter")
    async def human_slot15_trades_enter(
      request: Request,
      _: None = Depends(session_dep),
    ):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      cfg = _acfg_15m(loop, asset, get_cfg)
      body = await request.json()
      store = loop.human_slot15_trade_store(asset)
      settings = store.get_settings()
      mode = str(body.get("mode") or settings.mode).lower()
      if mode == "live":
        require_live_password("paper", "live", body, live_bet_password(cfg))
      tab = _slot15_tab(loop, asset)
      kalshi = loop._kalshi_for(asset) if mode == "live" else None
      out = execute_slot15_manual_enter(
        store=store,
        tab=tab,
        market_ticker=str(body.get("market_ticker") or ""),
        side=str(body.get("side") or ""),
        mode=mode,
        cfg=cfg,
        asset=asset,
        kalshi=kalshi,
      )
      if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "enter_failed")
      return out

    @app.post(f"{prefix}/human-trades/exit")
    async def human_slot15_trades_exit(
      request: Request,
      _: None = Depends(session_dep),
    ):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      cfg = _acfg_15m(loop, asset, get_cfg)
      body = await request.json()
      store = loop.human_slot15_trade_store(asset)
      settings = store.get_settings()
      pos_id = str(body.get("position_id") or "")
      if not pos_id:
        raise HTTPException(400, "position_id required")
      open_pos = next((p for p in store.open_positions() if p.get("id") == pos_id), None)
      mode = str((open_pos or {}).get("mode") or settings.mode).lower()
      if mode == "live":
        require_live_password("paper", "live", body, live_bet_password(cfg))
      tab = _slot15_tab(loop, asset)
      kalshi = loop._kalshi_for(asset)
      out = execute_slot15_manual_exit(
        store=store,
        tab=tab,
        position_id=pos_id,
        cfg=cfg,
        kalshi=kalshi,
      )
      if not out.get("ok"):
        raise HTTPException(400, out.get("message") or out.get("error") or "exit_failed")
      return out

  _mount("btc", "/api/15m")
  _mount("eth", "/api/eth/15m")
