"""API routes for the sports arb module."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query, Request

from src.data.sports_markets import sports_enabled
from src.scheduler.sports_support import run_sports_arb_scan, sports_arb_store, sports_status
from src.trading.live_mode_auth import live_bet_password, require_live_password
from src.trading.sports_arb_store import SportsArbSettings


def register_sports_routes(app, loop_getter, cfg_getter, session_dep) -> None:
  def _loop():
    loop = loop_getter()
    if loop is None:
      raise HTTPException(503, "Service starting")
    return loop

  def _cfg():
    return cfg_getter()

  @app.get("/api/sports/status")
  def sports_module_status(_: None = Depends(session_dep)):
    if not sports_enabled(_cfg()):
      return {"ok": False, "enabled": False, "error": "sports_disabled"}
    return sports_status(_loop())

  @app.get("/api/sports/opportunities")
  def sports_opportunities(
    limit: int = Query(default=50, le=200),
    _: None = Depends(session_dep),
  ):
    if not sports_enabled(_cfg()):
      raise HTTPException(503, "Sports module disabled")
    store = sports_arb_store(_loop())
    if store is None:
      raise HTTPException(503, "Sports store unavailable")
    opps = store.list_opportunities(limit=limit)
    return {"ok": True, "opportunities": opps, "count": len(opps)}

  @app.get("/api/sports/bot")
  def sports_bot(_: None = Depends(session_dep)):
    if not sports_enabled(_cfg()):
      return {"ok": False, "enabled": False, "error": "sports_disabled"}
    return sports_status(_loop())

  @app.post("/api/sports/bot/settings")
  async def sports_bot_settings(request: Request, _: None = Depends(session_dep)):
    if not sports_enabled(_cfg()):
      raise HTTPException(503, "Sports module disabled")
    store = sports_arb_store(_loop())
    if store is None:
      raise HTTPException(503, "Sports store unavailable")
    body = await request.json()
    cur = store.get_settings()
    dutch_live = bool(body.get("dutch_live", cur.dutch_live))
    value_live = bool(body.get("value_live", cur.value_live))
    # Legacy mode field still accepted when strategy flags omitted
    if "mode" in body and "dutch_live" not in body and "value_live" not in body:
      mode = str(body.get("mode") or cur.mode).lower()
      if mode not in ("paper", "live"):
        raise HTTPException(400, "mode must be paper or live")
      dutch_live = mode == "live"
      value_live = mode == "live"
    if (dutch_live or value_live) and not bool(
      ((_cfg().get("sports") or {}).get("bot") or {}).get("allow_live", False)
    ):
      raise HTTPException(400, "Sports live disabled in config (sports.bot.allow_live)")
    if dutch_live != cur.dutch_live or value_live != cur.value_live:
      # Any strategy live toggle requires password (arm or disarm)
      require_live_password(
        current_mode="paper",
        new_mode="live",
        body=body,
        password=live_bet_password(_cfg()),
      )
    updated = SportsArbSettings.from_dict({
      **cur.to_dict(),
      "enabled": bool(body.get("enabled", cur.enabled)),
      "dutch_live": dutch_live,
      "value_live": value_live,
      "dutch_max_open_usd": float(body.get("dutch_max_open_usd", cur.dutch_max_open_usd)),
      "dutch_max_stake_usd": float(body.get("dutch_max_stake_usd", cur.dutch_max_stake_usd)),
      "value_max_open_usd": float(body.get("value_max_open_usd", cur.value_max_open_usd)),
      "value_max_stake_usd": float(body.get("value_max_stake_usd", cur.value_max_stake_usd)),
      "value_strong_bets_only": bool(body.get("value_strong_bets_only", cur.value_strong_bets_only)),
      "max_live_per_scan": int(body.get("max_live_per_scan", cur.max_live_per_scan)),
      "max_live_trades_per_day": int(body.get("max_live_trades_per_day", cur.max_live_trades_per_day)),
      "paper_bankroll_usd": float(body.get("paper_bankroll_usd", cur.paper_bankroll_usd)),
      "max_open_usd": float(body.get("dutch_max_open_usd", body.get("max_open_usd", cur.dutch_max_open_usd))),
      "max_stake_per_opp_usd": float(
        body.get("dutch_max_stake_usd", body.get("max_stake_per_opp_usd", cur.dutch_max_stake_usd))
      ),
    })
    store.save_settings(updated, source="api")
    return sports_status(_loop())

  @app.post("/api/sports/bot/scan")
  def sports_bot_scan(_: None = Depends(session_dep)):
    if not sports_enabled(_cfg()):
      raise HTTPException(503, "Sports module disabled")
    result = run_sports_arb_scan(_loop())
    status = sports_status(_loop())
    return {"scan": result, "bot": status}

  @app.post("/api/sports/bot/fresh-start")
  def sports_bot_fresh_start(_: None = Depends(session_dep)):
    if not sports_enabled(_cfg()):
      raise HTTPException(503, "Sports module disabled")
    store = sports_arb_store(_loop())
    if store is None:
      raise HTTPException(503, "Sports store unavailable")
    store.fresh_start()
    return sports_status(_loop())

  @app.get("/api/sports/bot/trades")
  def sports_bot_trades(
    limit: int = Query(default=100, le=200),
    _: None = Depends(session_dep),
  ):
    if not sports_enabled(_cfg()):
      raise HTTPException(503, "Sports module disabled")
    store = sports_arb_store(_loop())
    if store is None:
      raise HTTPException(503, "Sports store unavailable")
    return {"trades": store.list_trades(limit=limit, for_display=True)}
