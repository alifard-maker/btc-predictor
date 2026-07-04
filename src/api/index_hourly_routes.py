"""API route registration for SPX/NDX index hourly endpoints."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, HTTPException, Query, Request

from src.assets import asset_cfg, asset_enabled


def register_index_hourly_routes(app, loop_getter, cfg_getter, session_dep, apply_settings_fn) -> None:
  """Mirror /api/eth/hourly/* for each index asset."""

  def _loop():
    loop = loop_getter()
    if loop is None:
      raise HTTPException(503, "Service starting")
    return loop

  def _cfg():
    return cfg_getter()

  for asset in ("spx", "ndx"):
    label = asset.upper()
    prefix = f"/api/{asset}/hourly"

    def _prediction(include_bot: bool = Query(default=True), *, _asset=asset):
      loop = _loop()
      if not asset_enabled(_cfg(), _asset):
        raise HTTPException(503, f"{_asset.upper()} hourly disabled")
      return loop.index_hourly_prediction(_asset, include_bot=include_bot)

    def _calibration(*, _asset=asset):
      loop = _loop()
      cal = loop._index_calibrations.get(_asset)
      if cal is None:
        raise HTTPException(503, f"{label} hourly disabled")
      return {"summary": cal.summary()}

    def _predictions(limit: int = Query(default=30, le=200), *, _asset=asset):
      loop = _loop()
      cal = loop._index_calibrations.get(_asset)
      if cal is None:
        return []
      df = cal.load_recent(limit)
      if df.empty:
        return []
      from src.api.main import _serialize_records

      return _serialize_records(df)

    def _bot_status(lightweight: bool = Query(default=True), _user=Depends(session_dep), *, _asset=asset):
      loop = _loop()
      tab = loop._hourly_tab_for_bot_status(_asset)
      return loop.hourly_bot_status(
        _asset,
        tab if tab and tab.get("ok") else None,
        lightweight=lightweight,
      )

    def _sync_fills(_user=Depends(session_dep), *, _asset=asset):
      loop = _loop()
      result = loop.sync_hourly_kalshi_fills(_asset, force=True)
      tab = loop._hourly_tab_for_bot_status(_asset)
      status = loop.hourly_bot_status(_asset, tab if tab and tab.get("ok") else None)
      return {"sync": result, "bot": status}

    async def _settings(request: Request, _user=Depends(session_dep), *, _asset=asset):
      loop = _loop()
      body = await request.json()
      store = loop.hourly_bot_store(_asset)
      acfg = loop._index_cfgs.get(_asset) or asset_cfg(_cfg(), _asset)
      apply_settings_fn(store, body, cfg=acfg)
      tab = loop.index_hourly_prediction(_asset)
      return loop.hourly_bot_status(_asset, tab if tab.get("ok") else None)

    def _trades(limit: int = Query(default=100, le=500), _user=Depends(session_dep), *, _asset=asset):
      loop = _loop()
      return loop.hourly_bot_store(_asset).list_trades(limit=limit)

    def _fresh_start(_user=Depends(session_dep), *, _asset=asset):
      from src.api.main import _hourly_bot_fresh_start

      loop = _loop()
      return _hourly_bot_fresh_start(
        loop.hourly_bot_store(_asset),
        lambda: loop.index_hourly_prediction(_asset),
        _asset,
      )

    def _clear_history(_user=Depends(session_dep), *, _asset=asset):
      from src.api.main import _hourly_bot_clear_history

      loop = _loop()
      return _hourly_bot_clear_history(
        loop.hourly_bot_store(_asset),
        lambda: loop.index_hourly_prediction(_asset),
        _asset,
      )

    app.get(f"{prefix}/prediction")(_prediction)
    app.get(f"{prefix}/calibration")(_calibration)
    app.get(f"{prefix}/predictions")(_predictions)
    app.get(f"{prefix}/bot")(_bot_status)
    app.post(f"{prefix}/bot/sync-kalshi-fills")(_sync_fills)
    app.post(f"{prefix}/bot/settings")(_settings)
    app.get(f"{prefix}/bot/trades")(_trades)
    app.post(f"{prefix}/bot/fresh-start")(_fresh_start)
    app.post(f"{prefix}/bot/clear-history")(_clear_history)

    trial_prefix = f"/api/{asset}/hourly-trial"

    def _trial_bot_status(_user=Depends(session_dep), *, _asset=asset):
      loop = _loop()
      tab = loop.index_hourly_prediction(_asset, include_bot=False)
      return loop.hourly_trial_bot_status(
        _asset,
        tab if tab and tab.get("ok") else None,
      )

    async def _trial_settings(request: Request, _user=Depends(session_dep), *, _asset=asset):
      loop = _loop()
      body = await request.json()
      store = loop.hourly_trial_bot_store(_asset)
      acfg = loop._index_cfgs.get(_asset) or asset_cfg(_cfg(), _asset)
      apply_settings_fn(store, body, cfg=acfg)
      tab = loop.index_hourly_prediction(_asset, include_bot=False)
      return loop.hourly_trial_bot_status(_asset, tab if tab and tab.get("ok") else None)

    def _trial_reset_bankroll(_user=Depends(session_dep), *, _asset=asset):
      loop = _loop()
      store = loop.hourly_trial_bot_store(_asset)
      settings = store.get_settings()
      if settings.mode != "paper":
        raise HTTPException(400, "Reset bankroll is only available in paper mode")
      store.reset_paper_bankroll(settings.max_spend_per_hour_usd)
      tab = loop.index_hourly_prediction(_asset, include_bot=False)
      return loop.hourly_trial_bot_status(_asset, tab if tab and tab.get("ok") else None)

    def _trial_fresh_start(_user=Depends(session_dep), *, _asset=asset):
      from src.api.main import _hourly_bot_fresh_start

      loop = _loop()
      return _hourly_bot_fresh_start(
        loop.hourly_trial_bot_store(_asset),
        lambda: loop.index_hourly_prediction(_asset),
        _asset,
        kind="hourly_trial",
      )

    def _trial_clear_history(_user=Depends(session_dep), *, _asset=asset):
      from src.api.main import _hourly_bot_clear_history

      loop = _loop()
      return _hourly_bot_clear_history(
        loop.hourly_trial_bot_store(_asset),
        lambda: loop.index_hourly_prediction(_asset),
        _asset,
        kind="hourly_trial",
      )

    def _trial_override_daily_cap(_user=Depends(session_dep), *, _asset=asset):
      from src.api.main import _override_daily_cap_hourly

      loop = _loop()
      return _override_daily_cap_hourly(_asset, kind="hourly_trial")

    def _trial_trades(
      limit: int = Query(default=100, le=200),
      event_ticker: str | None = Query(default=None),
      _user=Depends(session_dep),
      *,
      _asset=asset,
    ):
      loop = _loop()
      store = loop.hourly_trial_bot_store(_asset)
      trades = store.list_trades(limit=limit, event_ticker=event_ticker)
      out: dict[str, Any] = {"trades": trades}
      if event_ticker:
        out["hour_summary"] = store.hour_interval_summary(event_ticker)
      return out

    app.get(f"{trial_prefix}/bot")(_trial_bot_status)
    app.post(f"{trial_prefix}/bot/settings")(_trial_settings)
    app.post(f"{trial_prefix}/bot/reset-bankroll")(_trial_reset_bankroll)
    app.post(f"{trial_prefix}/bot/fresh-start")(_trial_fresh_start)
    app.post(f"{trial_prefix}/bot/clear-history")(_trial_clear_history)
    app.post(f"{trial_prefix}/bot/override-daily-cap")(_trial_override_daily_cap)
    app.get(f"{trial_prefix}/bot/trades")(_trial_trades)
