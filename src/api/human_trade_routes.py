"""API routes for dashboard manual (human) hourly trading."""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import Depends, HTTPException, Query, Request

from src.assets import asset_cfg
from src.trading.compare_paper_twins import compare_store_kinds, human_compare_bot_kind
from src.trading.human_bot_compare import build_human_bot_compare, export_human_training_rows
from src.trading.human_hourly_trade import (
  apply_human_settings_body,
  enrich_open_positions_fast_marks,
  enrich_open_positions_marks,
  execute_manual_enter,
  execute_manual_exit,
  preview_manual_entry,
  settle_expired_human_positions,
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


def _cached_human_tab(loop: Any, asset: str) -> dict[str, Any] | None:
  """Latest prediction snapshot only — no rebuild (for fast open-leg marks)."""
  if asset == "btc":
    tab = getattr(loop, "latest_hourly_prediction", None)
  elif asset == "eth":
    tab = getattr(loop, "latest_eth_hourly_prediction", None)
  else:
    preds = getattr(loop, "_latest_hourly_predictions", None) or {}
    tab = preds.get(asset)
  if tab and tab.get("ok"):
    return tab
  return None


def _settle_price_from_tab(tab: dict[str, Any] | None) -> float | None:
  if not tab:
    return None
  live = tab.get("live") or {}
  raw = tab.get("brti_live") or tab.get("erti_live") or live.get("current_price")
  try:
    px = float(raw) if raw is not None else None
  except (TypeError, ValueError):
    return None
  return px if px is not None and px > 0 else None


def _run_human_hour_settlement(
  loop: Any,
  store: Any,
  asset: str,
  tab: dict[str, Any] | None,
  cfg: dict[str, Any],
) -> list[dict[str, Any]]:
  event_ticker = (tab.get("event") or {}).get("event_ticker") if tab and tab.get("ok") else None
  live = (tab or {}).get("live") or {}
  index_id = str(live.get("index_id") or live.get("settlement_reference") or ("ERTI" if asset == "eth" else "BRTI"))
  return settle_expired_human_positions(
    store,
    current_event_ticker=event_ticker,
    settle_price=_settle_price_from_tab(tab),
    cfg=cfg,
    kalshi=loop._kalshi_for(asset),
    index_id=index_id,
    asset=asset,
  )


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
      kind = bot_kind or human_compare_bot_kind(asset)
      bot_status = _bot_status_for_compare(loop, asset, tab, kind)
      acfg = asset_cfg(get_cfg(), asset) if asset != "btc" else get_cfg()
      settled = _run_human_hour_settlement(loop, store, asset, tab, acfg)
      status = store.status(event_ticker)
      if settled:
        status["hour_settlements"] = [
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
      # Prefer live Kalshi marks so /status doesn't regress the fast-mark poll.
      if open_pos:
        status["open_positions"] = enrich_open_positions_fast_marks(
          open_pos,
          kalshi=loop._kalshi_for(asset),
          tab=tab if tab and tab.get("ok") else None,
          cfg=acfg,
        )
      else:
        status["open_positions"] = enrich_open_positions_marks(
          open_pos,
          tab if tab and tab.get("ok") else None,
          cfg=acfg,
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
        "status": status,
        "bot_status": bot_status,
        "bot_kind": kind,
      }

    @app.get(f"{prefix}/human-trades/marks")
    def human_trades_marks(_: None = Depends(session_dep)):
      """Fast open-leg marks via Kalshi /markets only (no S1/S2 rebuild)."""
      from datetime import datetime, timezone

      loop = get_loop()
      if loop is None:
        raise HTTPException(503, "Service starting")
      store = loop.human_trade_store(asset)
      tab = _cached_human_tab(loop, asset)
      acfg = asset_cfg(get_cfg(), asset) if asset != "btc" else get_cfg()
      _run_human_hour_settlement(loop, store, asset, tab, acfg)
      event_ticker = (tab.get("event") or {}).get("event_ticker") if tab and tab.get("ok") else None
      open_positions = (
        list(store.open_positions(event_ticker))
        if event_ticker
        else list(store.open_positions())
      )
      if not open_positions:
        return {
          "ok": True,
          "asset": asset,
          "open_positions": [],
          "open_unrealized_pnl_usd": None,
          "marked_at": datetime.now(timezone.utc).isoformat(),
          "quote_source": "none",
        }
      kalshi = loop._kalshi_for(asset)
      enriched = enrich_open_positions_fast_marks(
        open_positions,
        kalshi=kalshi,
        tab=tab,
        cfg=acfg,
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
        "open_positions": enriched,
        "open_unrealized_pnl_usd": round(ur_sum, 2) if ur_n else None,
        "open_marked_legs": ur_n,
        "marked_at": datetime.now(timezone.utc).isoformat(),
        "quote_source": "kalshi_live",
        "spot_cached": bool(tab),
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
      kind = bot_kind or human_compare_bot_kind(asset)
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
      bot_kind = str(body.get("bot_kind") or human_compare_bot_kind(asset))
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
      bot_kind = str(body.get("bot_kind") or human_compare_bot_kind(asset))
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
      # Fresh Kalshi marks for paper + live exits (TAKE PROFIT re-check needs live bid).
      kalshi = loop._kalshi_for(asset)
      out = execute_manual_exit(
        store=store,
        tab=tab,
        position_id=pos_id,
        cfg=cfg,
        kalshi=kalshi,
        verify_take_profit=bool(body.get("verify_take_profit")),
      )
      if not out.get("ok"):
        raise HTTPException(400, out.get("message") or out.get("error") or "exit_failed")
      return out

  _mount("btc", "/api/hourly")
  _mount("eth", "/api/eth/hourly")
