"""API routes for dashboard manual (human) hourly trading."""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import Depends, HTTPException, Query, Request

from src.assets import asset_cfg
from src.trading.compare_paper_twins import compare_store_kinds
from src.trading.human_bot_compare import build_human_bot_compare, export_human_training_rows
from src.trading.human_hourly_trade import (
  apply_human_settings_body,
  execute_manual_enter,
  execute_manual_exit,
  preview_manual_entry,
)
from src.trading.live_mode_auth import live_bet_password, require_live_password

log = logging.getLogger(__name__)


def _human_tab(loop: Any, asset: str) -> dict[str, Any] | None:
  if asset == "eth":
    return loop.eth_hourly_prediction(include_bot=False)
  if asset in ("spx", "ndx"):
    fn = getattr(loop, f"{asset}_hourly_prediction", None)
    if callable(fn):
      return fn(include_bot=False)
    return None
  return loop.daily_prediction(include_bot=False)


def _bot_status_for_compare(
  loop: Any,
  asset: str,
  tab: dict[str, Any] | None,
  bot_kind: str,
) -> dict[str, Any]:
  return loop.hourly_bot_status(
    asset,
    tab if tab and tab.get("ok") else None,
    kind=bot_kind,
    lightweight=True,
  )


def register_human_trade_routes(
  app: Any,
  *,
  get_loop: Callable[[], Any],
  get_cfg: Callable[[], dict[str, Any]],
  session_dep: Any,
) -> None:
  """Mount /api/hourly/human-trades/* and /api/eth/hourly/human-trades/*."""

  def _mount(asset: str, prefix: str) -> None:
    @app.get(f"{prefix}/human-trades/status")
    def human_trades_status(
      bot_kind: str | None = Query(default=None),
      _: None = Depends(session_dep),
    ):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      store = loop.human_trade_store(asset)
      tab = _human_tab(loop, asset)
      event_ticker = (tab.get("event") or {}).get("event_ticker") if tab and tab.get("ok") else None
      kind = bot_kind or compare_store_kinds(asset)[0]
      bot_status = _bot_status_for_compare(loop, asset, tab, kind)
      return {
        "ok": True,
        "asset": asset,
        "status": store.status(event_ticker),
        "bot_status": bot_status,
        "bot_kind": kind,
      }

    @app.get(f"{prefix}/human-trades/compare")
    def human_trades_compare(
      bot_kind: str | None = Query(default=None),
      pair_window_seconds: int = Query(default=180, ge=30, le=600),
      _: None = Depends(session_dep),
    ):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      kind = bot_kind or compare_store_kinds(asset)[0]
      return build_human_bot_compare(
        loop.human_trade_store(asset),
        loop.hourly_bot_store(asset, kind=kind),
        asset=asset,
        bot_kind=kind,
        pair_window_seconds=pair_window_seconds,
      )

    @app.get(f"{prefix}/human-trades/training-export")
    def human_trades_training_export(
      limit: int = Query(default=500, ge=1, le=2000),
      _: None = Depends(session_dep),
    ):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      store = loop.human_trade_store(asset)
      return {
        "ok": True,
        "asset": asset,
        "rows": export_human_training_rows(store, limit=limit),
      }

    @app.post(f"{prefix}/human-trades/settings")
    async def human_trades_settings(
      request: Request,
      _: None = Depends(session_dep),
    ):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      cfg = asset_cfg(get_cfg(), asset)
      body = await request.json()
      store = loop.human_trade_store(asset)
      settings = store.get_settings()
      if "mode" in body:
        require_live_password(
          settings.mode,
          str(body.get("mode", settings.mode)),
          body,
          live_bet_password(cfg),
        )
      apply_human_settings_body(store, body, cfg=cfg)
      tab = _human_tab(loop, asset)
      event_ticker = (tab.get("event") or {}).get("event_ticker") if tab and tab.get("ok") else None
      return {"ok": True, "status": store.status(event_ticker)}

    @app.post(f"{prefix}/human-trades/preview")
    async def human_trades_preview(
      request: Request,
      _: None = Depends(session_dep),
    ):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      cfg = asset_cfg(get_cfg(), asset)
      body = await request.json()
      store = loop.human_trade_store(asset)
      settings = store.get_settings()
      mode = str(body.get("mode") or settings.mode).lower()
      bot_kind = str(body.get("bot_kind") or compare_store_kinds(asset)[0])
      tab = _human_tab(loop, asset)
      bot_status = _bot_status_for_compare(loop, asset, tab, bot_kind)
      return preview_manual_entry(
        store=store,
        tab=tab,
        market_ticker=str(body.get("market_ticker") or ""),
        side=str(body.get("side") or ""),
        mode=mode,
        bot_status=bot_status,
        cfg=cfg,
        asset=asset,
      )

    @app.post(f"{prefix}/human-trades/enter")
    async def human_trades_enter(
      request: Request,
      _: None = Depends(session_dep),
    ):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      cfg = asset_cfg(get_cfg(), asset)
      body = await request.json()
      store = loop.human_trade_store(asset)
      settings = store.get_settings()
      mode = str(body.get("mode") or settings.mode).lower()
      if mode == "live":
        require_live_password("paper", "live", body, live_bet_password(cfg))
      bot_kind = str(body.get("bot_kind") or compare_store_kinds(asset)[0])
      tab = _human_tab(loop, asset)
      bot_status = _bot_status_for_compare(loop, asset, tab, bot_kind)
      kalshi = loop._kalshi_for(asset) if mode == "live" else None
      out = execute_manual_enter(
        store=store,
        tab=tab,
        market_ticker=str(body.get("market_ticker") or ""),
        side=str(body.get("side") or ""),
        mode=mode,
        bot_status=bot_status,
        cfg=cfg,
        asset=asset,
        kalshi=kalshi,
      )
      if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "enter_failed")
      out["bot_status"] = bot_status
      return out

    @app.post(f"{prefix}/human-trades/exit")
    async def human_trades_exit(
      request: Request,
      _: None = Depends(session_dep),
    ):
      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      cfg = asset_cfg(get_cfg(), asset)
      body = await request.json()
      store = loop.human_trade_store(asset)
      settings = store.get_settings()
      pos_id = str(body.get("position_id") or "")
      if not pos_id:
        raise HTTPException(400, "position_id required")
      open_pos = next((p for p in store.open_positions() if p.get("id") == pos_id), None)
      mode = str((open_pos or {}).get("mode") or settings.mode).lower()
      if mode == "live":
        require_live_password("paper", "live", body, live_bet_password(cfg))
      tab = _human_tab(loop, asset)
      kalshi = loop._kalshi_for(asset) if mode == "live" else None
      out = execute_manual_exit(
        store=store,
        tab=tab,
        position_id=pos_id,
        cfg=cfg,
        kalshi=kalshi,
      )
      if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "exit_failed")
      return out

  _mount("btc", "/api/hourly")
  _mount("eth", "/api/eth/hourly")
