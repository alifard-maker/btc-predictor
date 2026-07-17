from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response

from src.api.auth import (
  add_session_middleware,
  auth_enabled,
  auth_middleware,
  require_session,
)

from src import __version__ as APP_VERSION
from src.config import load_config
from src.data.storage import CandleStorage, HistoricalCollector
from src.models.predictor import Prediction
from src.scheduler.loop import PredictionLoop
from src.trading.live_mode_auth import live_bet_password, require_live_password
from src.trading.slot15_bet_assessment import assess_slot15_from_prediction

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

_cfg = load_config()
_loop: PredictionLoop | None = None
_scheduler = None


def _prediction_to_dict(pred: Prediction, *, asset: str = "btc") -> dict[str, Any]:
  acfg = _cfg if asset == "btc" else (_loop._eth_cfg if _loop and _loop._eth_cfg else _cfg)
  kalshi = _loop._kalshi_for(asset) if _loop else None
  predictor = _loop._predictor_for(asset) if _loop else None
  out = {
    "timestamp": pred.timestamp.isoformat() if hasattr(pred.timestamp, "isoformat") else str(pred.timestamp),
    "slot_start": pred.slot_start.isoformat() if pred.slot_start is not None else None,
    "slot_end": pred.slot_end.isoformat() if pred.slot_end is not None else None,
    "slot_label": pred.slot_label,
    "horizon_minutes": acfg.get("prediction_horizon_minutes", 15),
    "timezone": acfg.get("timezone", "America/New_York"),
    "reference_price": pred.reference_price or pred.price,
    "reference_source": pred.reference_source,
    "current_price": pred.current_price,
    "current_price_as_of": pred.current_price_as_of,
    "price": pred.reference_price or pred.price,
    "prob_up": round(pred.prob_up, 4),
    "prob_down": round(pred.prob_down, 4),
    "confidence": round(pred.confidence, 4),
    "expected_move": round(pred.expected_move, 2),
    "signal": pred.signal.value,
    "raw_prob_up": round(pred.raw_prob_up, 4) if pred.raw_prob_up is not None else None,
    "regime_notes": pred.regime_notes or [],
    "model_signal": pred.model_signal,
    "indicators": pred.indicators,
    "formatted": predictor.format_output(pred) if predictor else "",
    "asset": asset,
  }
  if _loop is not None:
    quote = _loop.live_price_quote(fresh=False, asset=asset)
    if quote is not None:
      out["current_price"] = round(quote.price, 2)
      out["current_price_source"] = quote.source
      if kalshi:
        out["price_feed"] = kalshi.price_feed_label()
        out["settlement_reference"] = kalshi.settlement_reference_label()
      if quote.trade_time is not None:
        out["current_price_as_of"] = quote.trade_time.isoformat()
      if quote.age_sec is not None:
        out["live_price_age_sec"] = round(quote.age_sec, 1)
  out["bet_assessment"] = assess_slot15_from_prediction(pred, acfg)
  return out


def _verify_admin(x_api_key: str | None = Header(default=None)) -> None:
  key = _cfg.get("admin_api_key", "")
  if not key:
    raise HTTPException(503, "ADMIN_API_KEY not configured")
  if x_api_key != key:
    raise HTTPException(401, "Invalid API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
  global _loop, _scheduler
  try:
    _loop = PredictionLoop(_cfg)
    app.state.loop = _loop
    try:
      from src.calibration.backfill_late import backfill_late_entries
      bf = backfill_late_entries(_cfg, dry_run=False, force=False, replay=True)
      if bf.get("updated"):
        log.info("Late-entry backfill on startup: %s", bf)
    except Exception as e:
      log.warning("Late-entry backfill skipped: %s", e)
    try:
      from src.trading.bot_pnl_backfill import backfill_all_bot_dbs

      data_dir = Path(_cfg["paths"]["logs"]).parent
      pnl_bf = backfill_all_bot_dbs(data_dir, dry_run=False, cfg=_cfg)
      if pnl_bf.get("fixed_count"):
        log.info("NO exit P&L backfill on startup: %s", pnl_bf)
    except Exception as e:
      log.warning("NO exit P&L backfill skipped: %s", e)
    try:
      from src.trading.bot_rollover_settlement_backfill import backfill_all_hourly_rollover_dbs

      rollover_bf = backfill_all_hourly_rollover_dbs(data_dir, dry_run=False, cfg=_cfg)
      if rollover_bf.get("fixed_count"):
        log.info("Hourly rollover settlement backfill on startup: %s", rollover_bf)
    except Exception as e:
      log.warning("Hourly rollover settlement backfill skipped: %s", e)
    try:
      from src.trading.bot_phantom_settlement_cleanup import cleanup_all_phantom_settlement_dbs

      phantom_bf = cleanup_all_phantom_settlement_dbs(data_dir, dry_run=False, cfg=_cfg)
      if phantom_bf.get("voided_count"):
        log.info("Phantom period-settlement cleanup on startup: %s", phantom_bf)
    except Exception as e:
      log.warning("Phantom period-settlement cleanup skipped: %s", e)
    try:
      from src.trading.bot_pnl_backfill import sync_daily_risk_from_trade_logs

      risk_sync = sync_daily_risk_from_trade_logs(data_dir, cfg=_cfg)
      if risk_sync.get("bots_adjusted"):
        log.info("Daily risk reconciled from trade logs: %s", risk_sync)
    except Exception as e:
      log.warning("Daily risk trade-log sync skipped: %s", e)
    try:
      snap = _loop.calibration.snapshot_stats(note="auto bootstrap")
      if snap.get("status") == "ok":
        log.info("Stats snapshot on startup: %s", snap)
    except Exception as e:
      log.warning("Stats snapshot skipped: %s", e)
  except Exception as e:
    log.exception("PredictionLoop init failed: %s", e)
    _loop = None

  def _boot_scheduler() -> None:
    global _scheduler
    if _loop is None:
      return
    try:
      if _cfg.get("enable_scheduler", True):
        _scheduler = _loop.start_background()
        app.state.scheduler = _scheduler
    except Exception:
      log.exception("Scheduler failed to start")

  threading.Thread(target=_boot_scheduler, daemon=True).start()
  try:
    from src.backup.logs_backup import volume_is_persistent

    data_dir = os.getenv("DATA_DIR", "/data")
    if not volume_is_persistent(data_dir):
      log.warning(
        "NO PERSISTENT VOLUME at /data — redeploys will wipe bot bankroll, trades, and backups. "
        "Attach a Railway volume at /data (see RAILWAY.md)."
      )
  except Exception:
    pass
  log.info("BTC Predictor API ready on port %s", os.getenv("PORT", "8080"))
  if auth_enabled(_cfg):
    log.info("Dashboard password protection enabled")
  elif os.getenv("APP_PASSWORD"):
    log.warning("APP_PASSWORD set but empty after load — check config")
  else:
    log.warning("APP_PASSWORD not set — dashboard is open without login")
  yield

  if _scheduler:
    _scheduler.shutdown(wait=False)
    log.info("Scheduler shut down")


app = FastAPI(
  title="BTC Predictor",
  description="Probabilistic BTC direction assistant — Railway backend",
  version=APP_VERSION,
  lifespan=lifespan,
)

app.add_middleware(
  CORSMiddleware,
  allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
  allow_credentials=True,
  allow_methods=["*"],
  allow_headers=["*"],
)


@app.middleware("http")
async def require_login(request: Request, call_next):
  return await auth_middleware(request, call_next, _cfg)


# Added last so it runs first and populates request.session before auth middleware.
add_session_middleware(app, _cfg)


def _session_user(request: Request) -> None:
  require_session(request, _cfg)


def _slot15_bot_payload(asset: str, reference_override: float | None = None) -> dict[str, Any]:
  if _loop is None:
    return {"ok": False, "error": "Service starting"}
  tab = _loop._slot15_tab(asset, reference_override)
  return _loop.slot15_bot_status(asset, tab if tab.get("ok") else None)


def _sanitize_json(obj: Any) -> Any:
  """Recursively make nested summary/bin payloads JSON-safe."""
  if obj is None:
    return None
  if isinstance(obj, dict):
    return {k: _sanitize_json(v) for k, v in obj.items()}
  if isinstance(obj, (list, tuple)):
    return [_sanitize_json(v) for v in obj]
  try:
    if pd.isna(obj):
      return None
  except (TypeError, ValueError):
    pass
  if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
    return None
  if hasattr(obj, "isoformat"):
    return obj.isoformat()
  if isinstance(obj, (np.integer, np.floating, np.bool_)):
    return obj.item()
  return obj


def _serialize_value(v: Any) -> Any:
  """Make DB/pandas values JSON-safe."""
  return _sanitize_json(v)


def _serialize_records(df: pd.DataFrame) -> list[dict[str, Any]]:
  records = df.to_dict(orient="records")
  for r in records:
    for k in list(r.keys()):
      r[k] = _serialize_value(r[k])
  return records


_DASHBOARD_HTML = Path(__file__).parent / "static" / "dashboard.html"
_LOGIN_HTML = Path(__file__).parent / "static" / "login.html"
_BOT_SETTINGS_UI_JS = Path(__file__).parent / "static" / "bot_settings_ui.js"


@app.get("/")
def root():
  return RedirectResponse(url="/dashboard")


@app.get("/login")
def login_page():
  return FileResponse(_LOGIN_HTML, media_type="text/html")


@app.post("/api/auth/login")
async def auth_login(request: Request, password: str = Form(...)):
  expected = _cfg.get("app_password", "")
  if expected and password != expected:
    return RedirectResponse(url="/login?error=1", status_code=303)
  request.session["authed"] = True
  return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
  request.session.clear()
  return RedirectResponse(url="/login", status_code=303)


@app.get("/dashboard")
def dashboard(request: Request, _: None = Depends(_session_user)):
  return FileResponse(_DASHBOARD_HTML, media_type="text/html")


@app.get("/static/bot_settings_ui.js")
def bot_settings_ui_js(_: None = Depends(_session_user)):
  return FileResponse(_BOT_SETTINGS_UI_JS, media_type="application/javascript")


def _volume_health_fields() -> dict[str, Any]:
  from src.backup.logs_backup import volume_is_persistent

  mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
  data_dir = os.getenv("DATA_DIR", "/data")
  persistent = volume_is_persistent(data_dir)
  return {
    "railway_volume_mount_path": mount,
    "volume_mounted_at_data": persistent,
    "data_persistence_warning": (
      None
      if persistent
      else "Redeploys wipe bot bankroll and trade logs until a Railway volume is mounted at /data"
    ),
  }


@app.get("/health")
def health(lite: bool = Query(default=False)):
  """Always return 200 so Railway healthchecks pass."""
  base = {
    "status": "starting",
    "service": "btc-predictor",
    "version": APP_VERSION,
    **_volume_health_fields(),
  }
  if _loop is None:
    return base
  if lite:
    st = _loop.lite_dashboard_health()
    kalshi = st.get("kalshi") or {}
    return {
      "status": "ok",
      "service": "btc-predictor",
      "version": APP_VERSION,
      **_volume_health_fields(),
      "scheduler_running": st.get("scheduler_running"),
      "data_dir": st.get("data_dir"),
      "volume_mounted_at_data": st.get("volume_mounted_at_data"),
      "kalshi": {
        "authenticated": bool(kalshi.get("authenticated")),
        "balance_usd": kalshi.get("balance_usd"),
        "balance_cents": kalshi.get("balance_cents"),
      },
      "bots_paused": st.get("bots_paused"),
    }
  status = _loop.status()
  out = {
    "status": "ok",
    "service": "btc-predictor",
    "version": APP_VERSION,
    **_volume_health_fields(),
    **status,
  }
  if _loop.eth_calibration is not None:
    out["eth_15m"] = _loop.eth_status()
  try:
    out["bot_risk"] = _loop.bot_risk_status()
  except Exception:
    pass
  return out


@app.get("/api/status")
def api_status():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.status()


def _apply_ref_override_fields(out: dict[str, Any], monitor: dict[str, Any]) -> None:
  if monitor.get("using_override"):
    out["reference_price"] = monitor["reference_price"]
    out["reference_price_api"] = monitor.get("reference_price_api")
    out["using_override"] = True


@app.get("/api/kalshi/status")
def kalshi_status():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.kalshi.status()


@app.get("/api/kalshi/portfolio-pnl")
def kalshi_portfolio_pnl(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.kalshi_portfolio_pnl import (
    build_kalshi_portfolio_pnl_report_cached,
    kalshi_portfolio_pnl_store,
  )

  store = kalshi_portfolio_pnl_store(_cfg)
  return build_kalshi_portfolio_pnl_report_cached(_loop.kalshi, _cfg, store=store)


@app.post("/api/kalshi/portfolio-pnl/clean-sheet")
def kalshi_portfolio_pnl_clean_sheet(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.kalshi_portfolio_pnl import (
    clean_sheet_kalshi_portfolio_pnl,
    kalshi_portfolio_pnl_store,
  )

  store = kalshi_portfolio_pnl_store(_cfg)
  return clean_sheet_kalshi_portfolio_pnl(store, _loop.kalshi, cfg=_cfg)


@app.get("/api/slot/monitor")
def slot_monitor(reference_override: float | None = Query(default=None, gt=0)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  monitor = _loop.slot_monitor(reference_override).to_dict()
  monitor["price_feed"] = _loop.kalshi.price_feed_label()
  monitor["settlement_reference"] = _loop.kalshi.settlement_reference_label()
  monitor["kalshi_authenticated"] = _loop.kalshi.authenticated
  return monitor


@app.get("/api/price/live")
def live_price():
  """Lightweight live price tick for fast dashboard polling."""
  if _loop is None:
    raise HTTPException(503, "Service starting")
  quote = _loop.live_price_quote(fresh=True)
  if quote is None:
    raise HTTPException(503, "Live price unavailable")
  return {
    "price": round(quote.price, 2),
    "source": quote.source,
    "as_of": quote.trade_time.isoformat() if quote.trade_time else None,
    "age_sec": round(quote.age_sec, 1) if quote.age_sec is not None else None,
    "kalshi_authenticated": _loop.kalshi.authenticated,
  }


@app.get("/api/prediction/latest")
def latest_prediction(reference_override: float | None = Query(default=None, gt=0)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  monitor = _loop.slot_monitor(reference_override).to_dict()
  if _loop.latest_prediction:
    out = _prediction_to_dict(_loop.latest_prediction)
    out["slot_monitor"] = monitor
    _apply_ref_override_fields(out, monitor)
    out["bot"] = _slot15_bot_payload("btc", reference_override)
    return out
  row = _loop.calibration.latest()
  if row:
    row["slot_monitor"] = monitor
    _apply_ref_override_fields(row, monitor)
    row["bot"] = _slot15_bot_payload("btc", reference_override)
    return row
  raise HTTPException(404, "No predictions yet")


@app.get("/api/predictions")
def list_predictions(limit: int = Query(default=50, le=500)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  try:
    df = _loop.calibration.load_recent(limit)
    if df.empty:
      return []
    return _serialize_records(df)
  except Exception as e:
    log.exception("Failed to load predictions: %s", e)
    raise HTTPException(500, str(e)) from e


@app.get("/api/calibration")
def calibration():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  try:
    tracker = _loop.calibration
    summary = tracker.summary()
    report = tracker.calibration_report()
    bins = report.to_dict(orient="records") if not report.empty else []
    return _sanitize_json({"summary": summary, "bins": bins})
  except Exception as e:
    log.exception("Calibration summary failed: %s", e)
    raise HTTPException(500, f"Calibration failed: {e}") from e


@app.get("/api/eth/15m/status")
def eth_15m_status():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  return _loop.eth_status()


@app.get("/api/eth/15m/slot/monitor")
def eth_slot_monitor(reference_override: float | None = Query(default=None, gt=0)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  kalshi = _loop._kalshi_for("eth")
  monitor = _loop.eth_slot_monitor(reference_override).to_dict()
  monitor["price_feed"] = kalshi.price_feed_label()
  monitor["settlement_reference"] = kalshi.settlement_reference_label()
  monitor["kalshi_authenticated"] = kalshi.authenticated
  monitor["asset"] = "eth"
  return monitor


@app.get("/api/eth/15m/prediction/latest")
def eth_latest_prediction(reference_override: float | None = Query(default=None, gt=0)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  monitor = _loop.eth_slot_monitor(reference_override).to_dict()
  if _loop.latest_eth_prediction:
    out = _prediction_to_dict(_loop.latest_eth_prediction, asset="eth")
    out["slot_monitor"] = monitor
    _apply_ref_override_fields(out, monitor)
    out["bot"] = _slot15_bot_payload("eth", reference_override)
    return out
  row = _loop.eth_calibration.latest()
  if row:
    row["slot_monitor"] = monitor
    row["asset"] = "eth"
    _apply_ref_override_fields(row, monitor)
    row["bot"] = _slot15_bot_payload("eth", reference_override)
    return row
  raise HTTPException(404, "No ETH 15m predictions yet")


@app.get("/api/eth/15m/predictions")
def eth_list_predictions(limit: int = Query(default=50, le=500)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    return []
  try:
    df = _loop.eth_calibration.load_recent(limit)
    if df.empty:
      return []
    return _serialize_records(df)
  except Exception as e:
    log.exception("Failed to load ETH 15m predictions: %s", e)
    raise HTTPException(500, str(e)) from e


@app.get("/api/eth/15m/calibration")
def eth_15m_calibration():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  try:
    tracker = _loop.eth_calibration
    summary = tracker.summary()
    report = tracker.calibration_report()
    bins = report.to_dict(orient="records") if not report.empty else []
    return _sanitize_json({"summary": summary, "bins": bins})
  except Exception as e:
    log.exception("ETH 15m calibration summary failed: %s", e)
    raise HTTPException(500, f"ETH 15m calibration failed: {e}") from e


@app.post("/api/admin/eth/15m/predict-now")
def eth_15m_predict_now(_: None = Depends(_verify_admin)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  pred = _loop.run_eth_prediction()
  if pred is None:
    raise HTTPException(500, _loop.eth_last_error or "ETH 15m prediction failed")
  return _prediction_to_dict(pred, asset="eth")


@app.get("/api/daily/prediction")
def daily_prediction(include_bot: bool = Query(default=True)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.daily_prediction(include_bot=include_bot)


@app.get("/api/eth/hourly/prediction")
def eth_hourly_prediction(include_bot: bool = Query(default=True)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.eth_hourly_prediction(include_bot=include_bot)


@app.get("/api/hourly/calibration")
def hourly_calibration():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  try:
    return _sanitize_json({"summary": _loop.hourly_calibration.summary()})
  except Exception as e:
    log.exception("Hourly calibration failed: %s", e)
    raise HTTPException(500, f"Hourly calibration failed: {e}") from e


@app.get("/api/eth/hourly/calibration")
def eth_hourly_calibration():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_hourly_calibration is None:
    raise HTTPException(503, "ETH hourly disabled")
  try:
    return _sanitize_json({"summary": _loop.eth_hourly_calibration.summary()})
  except Exception as e:
    log.exception("ETH hourly calibration failed: %s", e)
    raise HTTPException(500, f"ETH hourly calibration failed: {e}") from e


@app.get("/api/hourly/predictions")
def hourly_predictions(limit: int = Query(default=30, le=200)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  df = _loop.hourly_calibration.load_recent(limit)
  if df.empty:
    return []
  return _serialize_records(df)


@app.get("/api/eth/hourly/predictions")
def eth_hourly_predictions(limit: int = Query(default=30, le=200)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_hourly_calibration is None:
    return []
  df = _loop.eth_hourly_calibration.load_recent(limit)
  if df.empty:
    return []
  return _serialize_records(df)


@app.get("/api/hourly-v2/prediction")
def hourly_v2_prediction(include_bot: bool = Query(default=True)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.hourly_v2_prediction(include_bot=include_bot)


@app.get("/api/eth/hourly-v2/prediction")
def eth_hourly_v2_prediction(include_bot: bool = Query(default=True)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.eth_hourly_v2_prediction(include_bot=include_bot)


@app.get("/api/hourly-v2/calibration")
def hourly_v2_calibration():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.btc_hourly_v2_calibration is None:
    raise HTTPException(503, "BTC hourly v2 disabled")
  return _sanitize_json({"summary": _loop.btc_hourly_v2_calibration.summary()})


@app.get("/api/eth/hourly-v2/calibration")
def eth_hourly_v2_calibration():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_hourly_v2_calibration is None:
    raise HTTPException(503, "ETH hourly v2 disabled")
  return _sanitize_json({"summary": _loop.eth_hourly_v2_calibration.summary()})


@app.get("/api/hourly-v2/predictions")
def hourly_v2_predictions(limit: int = Query(default=30, le=200)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.btc_hourly_v2_calibration is None:
    return []
  df = _loop.btc_hourly_v2_calibration.load_recent(limit)
  if df.empty:
    return []
  return _serialize_records(df)


@app.get("/api/eth/hourly-v2/predictions")
def eth_hourly_v2_predictions(limit: int = Query(default=30, le=200)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_hourly_v2_calibration is None:
    return []
  df = _loop.eth_hourly_v2_calibration.load_recent(limit)
  if df.empty:
    return []
  return _serialize_records(df)


@app.post("/api/admin/hourly/predict-now")
def hourly_predict_now(
  force: bool = Query(default=False),
  _: None = Depends(_verify_admin),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  out = _loop.run_hourly_prediction(force=force)
  if not out or not out.get("ok"):
    raise HTTPException(500, out.get("error") if out else "Hourly prediction failed")
  return out


@app.post("/api/admin/hourly/open-now")
def hourly_open_now(_: None = Depends(_verify_admin)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  out = _loop.run_hourly_open_snapshot()
  if not out or not out.get("ok"):
    raise HTTPException(500, out.get("error") if out else "Hourly hour-open snapshot failed")
  return out


@app.post("/api/admin/hourly/late-call-now")
def hourly_late_call_now(
  force: bool = Query(default=False),
  _: None = Depends(_verify_admin),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  out = _loop.run_hourly_late_call(force=force)
  if not out or not out.get("ok"):
    raise HTTPException(500, out.get("error") if out else "Hourly late call failed")
  return out


@app.post("/api/admin/eth/hourly/predict-now")
def eth_hourly_predict_now(
  force: bool = Query(default=False),
  _: None = Depends(_verify_admin),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  out = _loop.run_eth_hourly_prediction(force=force)
  if not out or not out.get("ok"):
    raise HTTPException(500, out.get("error") if out else "ETH hourly prediction failed")
  return out


@app.post("/api/admin/eth/hourly/open-now")
def eth_hourly_open_now(_: None = Depends(_verify_admin)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  out = _loop.run_eth_hourly_open_snapshot()
  if not out or not out.get("ok"):
    raise HTTPException(500, out.get("error") if out else "ETH hourly hour-open snapshot failed")
  return out


@app.post("/api/admin/eth/hourly/late-call-now")
def eth_hourly_late_call_now(
  force: bool = Query(default=False),
  _: None = Depends(_verify_admin),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  out = _loop.run_eth_hourly_late_call(force=force)
  if not out or not out.get("ok"):
    raise HTTPException(500, out.get("error") if out else "ETH hourly late call failed")
  return out


def _maybe_close_paper_positions_on_live_switch(
  store,
  *,
  current_mode: str,
  new_mode: str,
  body: dict[str, Any],
) -> int:
  if current_mode != "paper" or new_mode != "live":
    return 0
  if not body.get("close_paper_positions", True):
    return 0
  event_ticker = body.get("event_ticker") or getattr(store, "_last_period_key", None)
  if not event_ticker:
    return 0
  from src.trading.bot_period_rollover import close_paper_positions_for_period

  return len(close_paper_positions_for_period(store, event_ticker))


def _apply_hourly_bot_settings(
  store,
  body: dict[str, Any],
  *,
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
  from src.trading.hourly_bot_store import HourlyBotSettings

  current = store.get_settings()
  mode = str(body.get("mode", current.mode))
  if mode not in ("paper", "live"):
    raise HTTPException(400, "mode must be paper or live")
  require_live_password(
    current_mode=current.mode,
    new_mode=mode,
    body=body,
    password=live_bet_password(_cfg),
  )
  new_enabled = bool(body.get("enabled", current.enabled))
  if new_enabled:
    auto_stopped = False
  elif "enabled" in body and not body["enabled"]:
    auto_stopped = False
  else:
    auto_stopped = current.auto_stopped
  settings = HourlyBotSettings.from_dict({
    **current.to_dict(),
    "enabled": new_enabled,
    "mode": mode,
    "max_spend_per_hour_usd": float(body.get("max_spend_per_hour_usd", current.max_spend_per_hour_usd)),
    "allow_strong": bool(body.get("allow_strong", current.allow_strong)),
    "allow_actionable": bool(body.get("allow_actionable", current.allow_actionable)),
    "take_profit_enabled": bool(body.get("take_profit_enabled", current.take_profit_enabled)),
    "take_profit_mode": str(body.get("take_profit_mode", current.take_profit_mode)),
    "take_profit_pct": float(body.get("take_profit_pct", current.take_profit_pct)),
    "take_profit_usd": float(body.get("take_profit_usd", current.take_profit_usd)),
    "trail_arm_profit_pct": float(body.get("trail_arm_profit_pct", current.trail_arm_profit_pct)),
    "trail_giveback_pct": float(body.get("trail_giveback_pct", current.trail_giveback_pct)),
    "trail_arm_profit_usd": float(body.get("trail_arm_profit_usd", current.trail_arm_profit_usd)),
    "trail_giveback_usd": float(body.get("trail_giveback_usd", current.trail_giveback_usd)),
    "min_take_profit_pct": float(body.get("min_take_profit_pct", current.min_take_profit_pct)),
    "max_take_profit_pct": float(body.get("max_take_profit_pct", current.max_take_profit_pct)),
    "min_hold_seconds": int(body.get("min_hold_seconds", current.min_hold_seconds)),
    "profit_exit_cooldown_seconds": int(
      body.get("profit_exit_cooldown_seconds", current.profit_exit_cooldown_seconds)
    ),
    "reentry_cooldown_seconds": int(body.get("reentry_cooldown_seconds", current.reentry_cooldown_seconds)),
    "auto_stop_on_budget_exhausted": bool(
      body.get("auto_stop_on_budget_exhausted", current.auto_stop_on_budget_exhausted)
    ),
    "use_accumulated_profit": bool(body.get("use_accumulated_profit", current.use_accumulated_profit)),
    "profit_use_pct": float(body.get("profit_use_pct", current.profit_use_pct)),
    "paper_auto_refill": bool(body.get("paper_auto_refill", current.paper_auto_refill)),
    "live_auto_refill_hour_budget": bool(
      body.get("live_auto_refill_hour_budget", current.live_auto_refill_hour_budget)
    ),
    "aggressive_entries": bool(body.get("aggressive_entries", current.aggressive_entries)),
    "auto_stopped": auto_stopped,
  })
  if settings.max_spend_per_hour_usd < 0:
    raise HTTPException(400, "max_spend_per_hour_usd must be >= 0")
  if not 0 <= settings.profit_use_pct <= 100:
    raise HTTPException(400, "profit_use_pct must be between 0 and 100")
  closed_paper = _maybe_close_paper_positions_on_live_switch(
    store,
    current_mode=current.mode,
    new_mode=mode,
    body=body,
  )
  old_cap = current.max_spend_per_hour_usd
  store.save_settings(settings, source="dashboard", cfg=cfg or _cfg)
  if settings.max_spend_per_hour_usd > old_cap:
    store.sync_paper_cap_on_max_increase(old_cap, settings.max_spend_per_hour_usd)
  out = settings.to_dict()
  if closed_paper:
    out["paper_positions_closed"] = closed_paper
  return out


def _apply_slot15_bot_settings(
  store,
  body: dict[str, Any],
  *,
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
  from src.trading.slot15_bot_store import Slot15BotSettings

  current = store.get_settings()
  mode = str(body.get("mode", current.mode))
  if mode not in ("paper", "live"):
    raise HTTPException(400, "mode must be paper or live")
  require_live_password(
    current_mode=current.mode,
    new_mode=mode,
    body=body,
    password=live_bet_password(_cfg),
  )
  new_enabled = bool(body.get("enabled", current.enabled))
  if new_enabled:
    auto_stopped = False
  elif "enabled" in body and not body["enabled"]:
    auto_stopped = False
  else:
    auto_stopped = current.auto_stopped
  settings = Slot15BotSettings.from_dict({
    **current.to_dict(),
    "enabled": new_enabled,
    "mode": mode,
    "max_spend_per_slot_usd": float(body.get("max_spend_per_slot_usd", current.max_spend_per_slot_usd)),
    "allow_strong": bool(body.get("allow_strong", current.allow_strong)),
    "allow_actionable": bool(body.get("allow_actionable", current.allow_actionable)),
    "take_profit_enabled": bool(body.get("take_profit_enabled", current.take_profit_enabled)),
    "take_profit_mode": str(body.get("take_profit_mode", current.take_profit_mode)),
    "take_profit_pct": float(body.get("take_profit_pct", current.take_profit_pct)),
    "take_profit_usd": float(body.get("take_profit_usd", current.take_profit_usd)),
    "trail_arm_profit_pct": float(body.get("trail_arm_profit_pct", current.trail_arm_profit_pct)),
    "trail_giveback_pct": float(body.get("trail_giveback_pct", current.trail_giveback_pct)),
    "trail_arm_profit_usd": float(body.get("trail_arm_profit_usd", current.trail_arm_profit_usd)),
    "trail_giveback_usd": float(body.get("trail_giveback_usd", current.trail_giveback_usd)),
    "min_take_profit_pct": float(body.get("min_take_profit_pct", current.min_take_profit_pct)),
    "max_take_profit_pct": float(body.get("max_take_profit_pct", current.max_take_profit_pct)),
    "min_hold_seconds": int(body.get("min_hold_seconds", current.min_hold_seconds)),
    "profit_exit_cooldown_seconds": int(
      body.get("profit_exit_cooldown_seconds", current.profit_exit_cooldown_seconds)
    ),
    "reentry_cooldown_seconds": int(body.get("reentry_cooldown_seconds", current.reentry_cooldown_seconds)),
    "auto_stop_on_budget_exhausted": bool(
      body.get("auto_stop_on_budget_exhausted", current.auto_stop_on_budget_exhausted)
    ),
    "use_accumulated_profit": bool(body.get("use_accumulated_profit", current.use_accumulated_profit)),
    "profit_use_pct": float(body.get("profit_use_pct", current.profit_use_pct)),
    "paper_auto_refill": bool(body.get("paper_auto_refill", current.paper_auto_refill)),
    "aggressive_entries": bool(body.get("aggressive_entries", current.aggressive_entries)),
    "auto_stopped": auto_stopped,
  })
  if settings.max_spend_per_slot_usd < 0:
    raise HTTPException(400, "max_spend_per_slot_usd must be >= 0")
  if not 0 <= settings.profit_use_pct <= 100:
    raise HTTPException(400, "profit_use_pct must be between 0 and 100")
  closed_paper = _maybe_close_paper_positions_on_live_switch(
    store,
    current_mode=current.mode,
    new_mode=mode,
    body=body,
  )
  old_cap = current.max_spend_per_slot_usd
  store.save_settings(settings, source="dashboard", cfg=cfg or _cfg)
  if settings.max_spend_per_slot_usd > old_cap:
    store.sync_paper_cap_on_max_increase(old_cap, settings.max_spend_per_slot_usd)
  out = settings.to_dict()
  if closed_paper:
    out["paper_positions_closed"] = closed_paper
  return out


@app.post("/api/hourly/bot/sync-kalshi-fills")
def hourly_bot_sync_kalshi_fills(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  try:
    result = _loop.sync_hourly_kalshi_fills("btc", force=True)
    tab = _loop._hourly_tab_for_bot_status("btc")
    status = _loop.hourly_bot_status("btc", tab if tab and tab.get("ok") else None)
    return {"sync": result, "bot": status}
  except Exception as e:
    log.exception("hourly bot sync-kalshi-fills failed: %s", e)
    raise HTTPException(500, f"Kalshi sync failed: {e}") from e


def _hourly_kalshi_fill_summary_response(
  asset: str,
  since: str | None,
  *,
  event_ticker: str | None = None,
) -> dict[str, Any]:
  from datetime import datetime, timezone

  from src.trading.bot_runtime import stats_epoch_at
  from src.trading.kalshi_fill_sync import summarize_kalshi_experiment_fills

  kalshi = _loop._kalshi_for(asset)
  if not kalshi or not getattr(kalshi, "authenticated", False):
    raise HTTPException(503, "Kalshi not authenticated")
  store = _loop.hourly_bot_store(asset, kind="hourly")
  since_dt: datetime | None = None
  if since:
    try:
      since_norm = str(since).replace("Z", "+00:00").replace(" ", "+")
      since_dt = datetime.fromisoformat(since_norm)
      if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    except ValueError as exc:
      raise HTTPException(400, f"Invalid since: {since}") from exc
  else:
    with store._connect() as conn:
      raw = stats_epoch_at(conn)
    if raw:
      since_dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
      if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
  if since_dt is None:
    raise HTTPException(400, "Provide since= or set stats_epoch_at on the bot store")
  return summarize_kalshi_experiment_fills(
    kalshi,
    since=since_dt,
    critical=True,
    asset=asset,
    event_ticker=event_ticker,
  )


@app.get("/api/hourly/bot/kalshi-fill-summary")
def hourly_bot_kalshi_fill_summary(
  since: str | None = Query(default=None, description="ISO-8601 UTC lower bound (defaults to stats_epoch_at)"),
  event_ticker: str | None = Query(default=None, description="Limit to one hourly event (Kalshi ground truth)"),
  _: None = Depends(_session_user),
):
  """Realized P&L from Kalshi hourly fill history (exchange source of truth)."""
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_kalshi_fill_summary_response("btc", since, event_ticker=event_ticker)


@app.get("/api/hourly/bot")
def hourly_bot_status(
  lightweight: bool = Query(default=True),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop._hourly_tab_for_bot_status("btc")
  return _loop.hourly_bot_status(
    "btc",
    tab if tab and tab.get("ok") else None,
    lightweight=lightweight,
  )


@app.get("/api/hourly/bot/live-reconcile")
def hourly_bot_live_reconcile(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.hourly_live_reconcile("btc")


@app.get("/api/hourly-trial/bot/live-reconcile")
def hourly_trial_bot_live_reconcile(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.hourly_live_reconcile("btc", kind="hourly_trial")


@app.post("/api/hourly/bot/settings")
async def hourly_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.hourly_bot_store("btc")
  _apply_hourly_bot_settings(store, body, cfg=_cfg)
  tab = _loop.daily_prediction()
  return _loop.hourly_bot_status("btc", tab if tab.get("ok") else None)


def _hourly_bot_clear_history(store, tab_fn, asset: str, *, kind: str = "hourly"):
  from src.trading.bot_risk_state import bot_risk_key, get_bot_risk_coordinator

  settings = store.get_settings()
  cap = float(settings.max_spend_per_hour_usd)
  store.clear_history(cap, mode=str(settings.mode or "paper"))
  coord = get_bot_risk_coordinator()
  if coord:
    coord.reset_bot_daily_pnl(bot_risk_key(kind, asset))
  tab = tab_fn()
  return _loop.hourly_bot_status(asset, tab if tab.get("ok") else None, kind=kind)


def _hourly_bot_fresh_start(store, tab_fn, asset: str, *, kind: str = "hourly"):
  return _hourly_bot_clear_history(store, tab_fn, asset, kind=kind)


def _slot15_bot_clear_history(store, tab_fn, asset: str, *, kind: str = "slot15", status_fn=None):
  from src.trading.bot_risk_state import bot_risk_key, get_bot_risk_coordinator

  settings = store.get_settings()
  cap = float(settings.max_spend_per_slot_usd)
  store.clear_history(cap, mode=str(settings.mode or "paper"))
  coord = get_bot_risk_coordinator()
  if coord:
    coord.reset_bot_daily_pnl(bot_risk_key(kind, asset))
  tab = tab_fn()
  if status_fn is None:
    return _loop.slot15_bot_status(asset, tab if tab.get("ok") else None)
  return status_fn(asset, tab if tab.get("ok") else None)


def _slot15_bot_fresh_start(store, tab_fn, asset: str, *, kind: str = "slot15", status_fn=None):
  return _slot15_bot_clear_history(store, tab_fn, asset, kind=kind, status_fn=status_fn)


def _apply_slot15_trial_bot_settings(store, body: dict[str, Any], *, cfg: dict[str, Any] | None = None):
  body = {**body, "mode": "paper"}
  return _apply_slot15_bot_settings(store, body, cfg=cfg)


def _override_daily_cap_hourly(asset: str, *, kind: str = "hourly") -> dict[str, Any]:
  from src.assets import asset_cfg
  from src.trading.bot_risk_gates import override_daily_loss_cap

  store = _loop.hourly_bot_store(asset, kind=kind)
  acfg = _cfg if asset == "btc" else (_loop._eth_cfg or asset_cfg(_cfg, asset))
  daily = override_daily_loss_cap(store, kind=kind, asset=asset, cfg=acfg)
  tab = _loop.daily_prediction() if asset == "btc" else _loop.eth_hourly_prediction()
  status = _loop.hourly_bot_status(asset, tab if tab.get("ok") else None, kind=kind)
  status["daily_loss"] = daily
  return status


def _override_daily_cap_slot15(
  asset: str,
  *,
  kind: str = "slot15",
  store=None,
  status_fn=None,
) -> dict[str, Any]:
  from src.assets import asset_cfg
  from src.trading.bot_risk_gates import override_daily_loss_cap

  store = store or _loop.slot15_bot_store(asset)
  acfg = _loop._acfg_15m(asset) if asset == "btc" else (_loop._eth_cfg or asset_cfg(_cfg, asset))
  daily = override_daily_loss_cap(store, kind=kind, asset=asset, cfg=acfg)
  tab = _loop._slot15_tab(asset)
  if status_fn is None:
    status = _loop.slot15_bot_status(asset, tab if tab.get("ok") else None)
  else:
    status = status_fn(asset, tab if tab.get("ok") else None)
  status["daily_loss"] = daily
  return status


@app.post("/api/hourly/bot/reset-bankroll")
def hourly_bot_reset_bankroll(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_bot_store("btc")
  settings = store.get_settings()
  if settings.mode != "paper":
    raise HTTPException(400, "Reset bankroll is only available in paper mode")
  store.reset_paper_bankroll(settings.max_spend_per_hour_usd)
  tab = _loop.daily_prediction()
  return _loop.hourly_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly/bot/fresh-start")
def hourly_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_fresh_start(_loop.hourly_bot_store("btc"), _loop.daily_prediction, "btc")


@app.post("/api/hourly/bot/clear-history")
def hourly_bot_clear_history(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_clear_history(_loop.hourly_bot_store("btc"), _loop.daily_prediction, "btc")


@app.post("/api/hourly/bot/override-daily-cap")
def hourly_bot_override_daily_cap(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _override_daily_cap_hourly("btc")


@app.get("/api/hourly/bot/trades")
def hourly_bot_trades(
  limit: int = Query(default=100, le=200),
  event_ticker: str | None = Query(default=None),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_bot_store("btc")
  trades = store.list_trades(limit=limit, event_ticker=event_ticker)
  out: dict[str, Any] = {"trades": trades}
  if event_ticker:
    out["hour_summary"] = store.hour_interval_summary(event_ticker)
  return out


@app.get("/api/bots/performance-report")
def bots_performance_report(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.bot_performance_report import build_all_bots_performance_report

  try:
    return build_all_bots_performance_report(_loop)
  except sqlite3.OperationalError as exc:
    if "locked" in str(exc).lower():
      raise HTTPException(503, "Bot databases busy — retry in a few seconds") from exc
    raise


@app.get("/api/bots/risk-status")
def bots_risk_status(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.bot_risk_status()


@app.get("/api/pnl-first/manager")
def pnl_first_manager_status(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.pnl_first_railway_manager import (
    PnlFirstManagerConfig,
    compute_btc_live_trade_timing,
    compute_live_milestone,
    manager_status_snapshot,
    run_preflight,
  )

  from src.trading.pnl_first_backtest_runner import backtest_status, run_live_pnl_audit

  mgr = PnlFirstManagerConfig.from_cfg(_cfg)
  snap = manager_status_snapshot(_loop)

  def _safe(label: str, fn):
    try:
      return fn()
    except Exception as exc:
      return {"ok": False, "error": f"{label}:{type(exc).__name__}:{exc}"}

  return {
    "config": {
      "enabled": mgr.enabled,
      "phase": mgr.phase,
      "enforce_sleep": mgr.enforce_sleep,
      "trading_armed": mgr.trading_armed,
      "auto_wake_when_ready": mgr.auto_wake_when_ready,
      "live_cap_usd": mgr.live_cap_usd,
      "interval_seconds": mgr.interval_seconds,
    },
    "runtime": snap,
    "preflight_now": _safe("preflight", lambda: run_preflight(_loop, _cfg)),
    "milestone_now": _safe("milestone", lambda: compute_live_milestone(_loop, _cfg)),
    "backtest_queue": _safe("backtest_queue", lambda: backtest_status(_cfg)),
    "live_audit": _safe("live_audit", lambda: run_live_pnl_audit(_loop, _cfg)),
    "trade_timing": _safe("trade_timing", lambda: compute_btc_live_trade_timing(_loop, _cfg)),
  }


@app.get("/api/pnl-first/four-k-week-plan/revision")
def pnl_first_four_k_week_plan_revision(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.four_k_week_plan import four_k_week_plan_revision_cached

  try:
    return four_k_week_plan_revision_cached(_loop, _cfg)
  except sqlite3.OperationalError as exc:
    if "locked" in str(exc).lower():
      raise HTTPException(503, "Bot databases busy — retry in a few seconds") from exc
    raise


@app.get("/api/pnl-first/four-k-week-plan")
def pnl_first_four_k_week_plan(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.four_k_week_plan import build_four_k_week_plan_report_cached

  try:
    return build_four_k_week_plan_report_cached(_loop, _cfg)
  except sqlite3.OperationalError as exc:
    if "locked" in str(exc).lower():
      raise HTTPException(503, "Bot databases busy — retry in a few seconds") from exc
    raise


@app.get("/api/pnl-first/kalshi-live-report")
def pnl_first_kalshi_live_report(
  asset: str = Query(default="btc", pattern="^(btc|eth)$"),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.kalshi_live_report import build_kalshi_live_report

  return build_kalshi_live_report(_loop, _cfg, asset=asset)


@app.get("/api/pnl-first/regroup-milestones")
def pnl_first_regroup_milestones(_: None = Depends(_session_user)):
  from src.trading.pnl_first_health_watchdog import load_regroup_milestones

  return load_regroup_milestones(_cfg)


@app.get("/api/pnl-first/paper-ab")
def pnl_first_paper_ab(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.pnl_first_paper_ab import paper_ab_output_path, write_paper_ab_report

  try:
    return write_paper_ab_report(_loop, _cfg)
  except Exception as exc:
    path = paper_ab_output_path(_cfg)
    if path.exists():
      import json

      stale = json.loads(path.read_text(encoding="utf-8"))
      stale["stale_fallback"] = True
      stale["refresh_error"] = f"{type(exc).__name__}:{exc}"
      return stale
    raise HTTPException(500, f"paper_ab report failed: {exc}") from exc


@app.get("/api/pnl-first/health")
def pnl_first_health(_: None = Depends(_session_user)):
  import json
  from pathlib import Path

  path = Path(os.getenv("DATA_DIR", "data")) / "logs" / "pnl_first_manager" / "health_latest.json"
  if path.exists():
    return json.loads(path.read_text(encoding="utf-8"))
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.pnl_first_health_watchdog import run_health_watchdog

  return run_health_watchdog(_loop, _cfg)


@app.get("/api/pnl-first/epoch-reconcile")
def pnl_first_epoch_reconcile(
  asset: str = Query(default="btc", pattern="^(btc|eth)$"),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.epoch_reconcile import build_epoch_reconcile_report_cached

  return build_epoch_reconcile_report_cached(_loop, _cfg, asset=asset)


@app.get("/api/bots/hourly-live-trial-compare")
def bots_hourly_live_trial_compare(
  asset: str = Query(default="btc", pattern="^(btc|eth|spx|ndx)$"),
  limit_hours: int = Query(default=24, ge=1, le=72),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.hourly_live_trial_compare import build_hourly_live_trial_compare_cached
  from src.trading.hourly_live_trial_align import HourlyLiveTrialAlignConfig
  from src.trading.compare_paper_twins import compare_store_kinds
  from src.trading.probe_24h import effective_compare_stats_epoch_at
  from src.assets import asset_cfg

  live_kind, trial_kind = compare_store_kinds(asset)
  live_store = _loop.hourly_bot_store(asset, kind=live_kind)
  trial_store = _loop.hourly_bot_store(asset, kind=trial_kind)
  cfg = asset_cfg(_cfg, asset)
  align = HourlyLiveTrialAlignConfig.from_cfg(cfg, kind="hourly")
  stats_epoch_at = effective_compare_stats_epoch_at(live_store, _cfg)
  try:
    out = build_hourly_live_trial_compare_cached(
      live_store,
      trial_store,
      asset=asset,
      limit_hours=limit_hours,
      live_kind=live_kind,
      trial_kind=trial_kind,
      pair_window_seconds=align.compare_pair_window_seconds,
      stats_epoch_at=stats_epoch_at,
    )
    trial_settings = trial_store.get_settings()
    out["paper_twin"] = {
      "kind": trial_kind,
      "enabled": bool(trial_settings.enabled),
      "mode": trial_settings.mode,
      "continuous": bool(trial_settings.continuous),
      "max_spend_per_hour_usd": trial_settings.max_spend_per_hour_usd,
    }
    return out
  except sqlite3.OperationalError as exc:
    if "locked" in str(exc).lower():
      raise HTTPException(503, "Bot databases busy — retry in a few seconds") from exc
    raise


@app.get("/api/bots/slot15-live-trial-compare")
def bots_slot15_live_trial_compare(
  asset: str = Query(default="eth", pattern="^(btc|eth)$"),
  limit_slots: int = Query(default=24, ge=1, le=48),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if asset == "eth" and _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  from src.trading.hourly_live_trial_align import HourlyLiveTrialAlignConfig
  from src.trading.hourly_live_trial_compare import build_slot15_live_trial_compare
  from src.assets import asset_cfg

  live_store = _loop.slot15_bot_store(asset)
  trial_store = _loop.slot15_trial_bot_store(asset)
  acfg = _loop._acfg_15m(asset) if asset == "btc" else (_loop._eth_cfg or asset_cfg(_cfg, asset))
  align = HourlyLiveTrialAlignConfig.from_cfg(acfg, kind="slot15")
  try:
    return build_slot15_live_trial_compare(
      live_store,
      trial_store,
      asset=asset,
      limit_slots=limit_slots,
      pair_window_seconds=align.compare_pair_window_seconds,
    )
  except sqlite3.OperationalError as exc:
    if "locked" in str(exc).lower():
      raise HTTPException(503, "Bot databases busy — retry in a few seconds") from exc
    raise


@app.post("/api/admin/bots/auto-tune")
def admin_bots_auto_tune(_: None = Depends(_verify_admin)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.run_bot_auto_tuning()


@app.get("/api/eth/hourly/bot")
def eth_hourly_bot_status(
  lightweight: bool = Query(default=True),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop._hourly_tab_for_bot_status("eth")
  return _loop.hourly_bot_status(
    "eth",
    tab if tab and tab.get("ok") else None,
    lightweight=lightweight,
  )


@app.post("/api/eth/hourly/bot/sync-kalshi-fills")
def eth_hourly_bot_sync_kalshi_fills(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  result = _loop.sync_hourly_kalshi_fills("eth", force=True)
  tab = _loop._hourly_tab_for_bot_status("eth")
  status = _loop.hourly_bot_status("eth", tab if tab and tab.get("ok") else None)
  return {"sync": result, "bot": status}


@app.get("/api/eth/hourly/bot/live-reconcile")
def eth_hourly_bot_live_reconcile(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.hourly_live_reconcile("eth")


@app.get("/api/eth/hourly/bot/kalshi-fill-summary")
def eth_hourly_bot_kalshi_fill_summary(
  since: str | None = Query(default=None, description="ISO-8601 UTC lower bound (defaults to stats_epoch_at)"),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_kalshi_fill_summary_response("eth", since)


@app.post("/api/eth/hourly/bot/settings")
async def eth_hourly_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.hourly_bot_store("eth")
  from src.assets import asset_cfg

  eth_cfg = _loop._eth_cfg or asset_cfg(_cfg, "eth")
  _apply_hourly_bot_settings(store, body, cfg=eth_cfg)
  tab = _loop.eth_hourly_prediction()
  return _loop.hourly_bot_status("eth", tab if tab.get("ok") else None)


@app.post("/api/eth/hourly/bot/reset-bankroll")
def eth_hourly_bot_reset_bankroll(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_bot_store("eth")
  settings = store.get_settings()
  if settings.mode != "paper":
    raise HTTPException(400, "Reset bankroll is only available in paper mode")
  store.reset_paper_bankroll(settings.max_spend_per_hour_usd)
  tab = _loop.eth_hourly_prediction()
  return _loop.hourly_bot_status("eth", tab if tab.get("ok") else None)


@app.post("/api/eth/hourly/bot/fresh-start")
def eth_hourly_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_fresh_start(_loop.hourly_bot_store("eth"), _loop.eth_hourly_prediction, "eth")


@app.post("/api/eth/hourly/bot/clear-history")
def eth_hourly_bot_clear_history(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_clear_history(_loop.hourly_bot_store("eth"), _loop.eth_hourly_prediction, "eth")


@app.post("/api/eth/hourly/bot/override-daily-cap")
def eth_hourly_bot_override_daily_cap(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _override_daily_cap_hourly("eth")


@app.get("/api/eth/hourly/bot/trades")
def eth_hourly_bot_trades(
  limit: int = Query(default=100, le=200),
  event_ticker: str | None = Query(default=None),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_bot_store("eth")
  trades = store.list_trades(limit=limit, event_ticker=event_ticker)
  out: dict[str, Any] = {"trades": trades}
  if event_ticker:
    out["hour_summary"] = store.hour_interval_summary(event_ticker)
  return out


@app.get("/api/eth/hourly-live/bot")
def eth_hourly_live_bot_status(
  lightweight: bool = Query(default=True),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop._hourly_tab_for_bot_status("eth")
  return _loop.hourly_live_bot_status(
    "eth",
    tab if tab and tab.get("ok") else None,
    lightweight=lightweight,
  )


@app.get("/api/eth/hourly-live/bot/trades")
def eth_hourly_live_bot_trades(
  limit: int = Query(default=100, le=200),
  event_ticker: str | None = Query(default=None),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_live_bot_store("eth")
  trades = store.list_trades(limit=limit, event_ticker=event_ticker)
  out: dict[str, Any] = {"trades": trades}
  if event_ticker:
    out["hour_summary"] = store.hour_interval_summary(event_ticker)
  return out


@app.get("/api/eth/hourly-live/bot/live-reconcile")
def eth_hourly_live_bot_live_reconcile(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.hourly_live_reconcile("eth", kind="hourly_live")


@app.post("/api/eth/hourly-live/bot/sync-kalshi-fills")
def eth_hourly_live_bot_sync_kalshi_fills(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  result = _loop.sync_hourly_kalshi_fills("eth", kind="hourly_live", force=True)
  tab = _loop._hourly_tab_for_bot_status("eth")
  status = _loop.hourly_live_bot_status(
    "eth",
    tab if tab and tab.get("ok") else None,
  )
  return {"sync": result, "bot": status}


@app.get("/api/hourly-v2/bot")
def hourly_v2_bot_status(
  lightweight: bool = Query(default=True),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop.hourly_v2_prediction(include_bot=False)
  return _loop.hourly_bot_status(
    "btc",
    tab if tab.get("ok") else None,
    kind="hourly_v2",
    lightweight=lightweight,
  )


@app.post("/api/hourly-v2/bot/settings")
async def hourly_v2_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.hourly_bot_store("btc", kind="hourly_v2")
  from src.assets import asset_v2_runtime_cfg

  acfg = _loop._btc_v2_cfg or _cfg
  _apply_hourly_bot_settings(store, body, cfg=asset_v2_runtime_cfg(acfg))
  tab = _loop.hourly_v2_prediction(include_bot=False)
  return _loop.hourly_bot_status("btc", tab if tab.get("ok") else None, kind="hourly_v2")


@app.post("/api/hourly-v2/bot/reset-bankroll")
def hourly_v2_bot_reset_bankroll(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_bot_store("btc", kind="hourly_v2")
  store.reset_paper_bankroll()
  tab = _loop.hourly_v2_prediction(include_bot=False)
  return _loop.hourly_bot_status("btc", tab if tab.get("ok") else None, kind="hourly_v2")


@app.post("/api/hourly-v2/bot/fresh-start")
def hourly_v2_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_bot_store("btc", kind="hourly_v2")
  return _hourly_bot_fresh_start(store, _loop.hourly_v2_prediction, "btc", kind="hourly_v2")


@app.post("/api/hourly-v2/bot/clear-history")
def hourly_v2_bot_clear_history(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_bot_store("btc", kind="hourly_v2")
  return _hourly_bot_clear_history(store, _loop.hourly_v2_prediction, "btc", kind="hourly_v2")


@app.get("/api/eth/hourly-v2/bot")
def eth_hourly_v2_bot_status(
  lightweight: bool = Query(default=True),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop.eth_hourly_v2_prediction(include_bot=False)
  return _loop.hourly_bot_status(
    "eth",
    tab if tab.get("ok") else None,
    kind="hourly_v2",
    lightweight=lightweight,
  )


@app.post("/api/eth/hourly-v2/bot/settings")
async def eth_hourly_v2_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.hourly_bot_store("eth", kind="hourly_v2")
  from src.assets import asset_v2_runtime_cfg

  acfg = _loop._eth_v2_cfg or _cfg
  _apply_hourly_bot_settings(store, body, cfg=asset_v2_runtime_cfg(acfg))
  tab = _loop.eth_hourly_v2_prediction(include_bot=False)
  return _loop.hourly_bot_status("eth", tab if tab.get("ok") else None, kind="hourly_v2")


@app.post("/api/eth/hourly-v2/bot/fresh-start")
def eth_hourly_v2_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_bot_store("eth", kind="hourly_v2")
  return _hourly_bot_fresh_start(store, _loop.eth_hourly_v2_prediction, "eth", kind="hourly_v2")


@app.get("/api/hourly-trial/bot")
def hourly_trial_bot_status(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial/bot/settings")
async def hourly_trial_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.hourly_trial_bot_store("btc")
  _apply_hourly_bot_settings(store, body, cfg=_cfg)
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial/bot/reset-bankroll")
def hourly_trial_bot_reset_bankroll(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_trial_bot_store("btc")
  settings = store.get_settings()
  if settings.mode != "paper":
    raise HTTPException(400, "Reset bankroll is only available in paper mode")
  store.reset_paper_bankroll(settings.max_spend_per_hour_usd)
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial/bot/fresh-start")
def hourly_trial_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_fresh_start(
    _loop.hourly_trial_bot_store("btc"),
    _loop.daily_prediction,
    "btc",
    kind="hourly_trial",
  )


@app.post("/api/hourly-trial/bot/clear-history")
def hourly_trial_bot_clear_history(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_clear_history(
    _loop.hourly_trial_bot_store("btc"),
    _loop.daily_prediction,
    "btc",
    kind="hourly_trial",
  )


@app.post("/api/hourly-trial/bot/override-daily-cap")
def hourly_trial_bot_override_daily_cap(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _override_daily_cap_hourly("btc", kind="hourly_trial")


@app.get("/api/hourly-trial/bot/trades")
def hourly_trial_bot_trades(
  limit: int = Query(default=100, le=200),
  event_ticker: str | None = Query(default=None),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_trial_bot_store("btc")
  trades = store.list_trades(limit=limit, event_ticker=event_ticker)
  out: dict[str, Any] = {"trades": trades}
  if event_ticker:
    out["hour_summary"] = store.hour_interval_summary(event_ticker)
  return out


@app.get("/api/hourly-trial-rally/bot")
def hourly_trial_rally_bot_status(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_rally_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial-rally/bot/settings")
async def hourly_trial_rally_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.hourly_trial_rally_bot_store("btc")
  _apply_hourly_bot_settings(store, body, cfg=_cfg)
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_rally_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial-rally/bot/reset-bankroll")
def hourly_trial_rally_bot_reset_bankroll(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_trial_rally_bot_store("btc")
  settings = store.get_settings()
  if settings.mode != "paper":
    raise HTTPException(400, "Reset bankroll is only available in paper mode")
  store.reset_paper_bankroll(settings.max_spend_per_hour_usd)
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_rally_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial-rally/bot/fresh-start")
def hourly_trial_rally_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_fresh_start(
    _loop.hourly_trial_rally_bot_store("btc"),
    _loop.daily_prediction,
    "btc",
    kind="hourly_trial_rally",
  )


@app.post("/api/hourly-trial-rally/bot/clear-history")
def hourly_trial_rally_bot_clear_history(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_clear_history(
    _loop.hourly_trial_rally_bot_store("btc"),
    _loop.daily_prediction,
    "btc",
    kind="hourly_trial_rally",
  )


@app.post("/api/hourly-trial-rally/bot/override-daily-cap")
def hourly_trial_rally_bot_override_daily_cap(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _override_daily_cap_hourly("btc", kind="hourly_trial_rally")


@app.get("/api/hourly-trial-rally/bot/trades")
def hourly_trial_rally_bot_trades(
  limit: int = Query(default=100, le=200),
  event_ticker: str | None = Query(default=None),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_trial_rally_bot_store("btc")
  trades = store.list_trades(limit=limit, event_ticker=event_ticker)
  out: dict[str, Any] = {"trades": trades}
  if event_ticker:
    out["hour_summary"] = store.hour_interval_summary(event_ticker)
  return out


@app.get("/api/hourly-trial-soft/bot")
def hourly_trial_soft_bot_status(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_soft_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial-soft/bot/settings")
async def hourly_trial_soft_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.hourly_trial_soft_bot_store("btc")
  _apply_hourly_bot_settings(store, body, cfg=_cfg)
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_soft_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial-soft/bot/reset-bankroll")
def hourly_trial_soft_bot_reset_bankroll(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_trial_soft_bot_store("btc")
  settings = store.get_settings()
  if settings.mode != "paper":
    raise HTTPException(400, "Reset bankroll is only available in paper mode")
  store.reset_paper_bankroll(settings.max_spend_per_hour_usd)
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_soft_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial-soft/bot/fresh-start")
def hourly_trial_soft_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_fresh_start(
    _loop.hourly_trial_soft_bot_store("btc"),
    _loop.daily_prediction,
    "btc",
    kind="hourly_trial_soft",
  )


@app.post("/api/hourly-trial-soft/bot/clear-history")
def hourly_trial_soft_bot_clear_history(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_clear_history(
    _loop.hourly_trial_soft_bot_store("btc"),
    _loop.daily_prediction,
    "btc",
    kind="hourly_trial_soft",
  )


@app.post("/api/hourly-trial-soft/bot/override-daily-cap")
def hourly_trial_soft_bot_override_daily_cap(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _override_daily_cap_hourly("btc", kind="hourly_trial_soft")


@app.get("/api/hourly-trial-soft/bot/trades")
def hourly_trial_soft_bot_trades(
  limit: int = Query(default=100, le=200),
  event_ticker: str | None = Query(default=None),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_trial_soft_bot_store("btc")
  trades = store.list_trades(limit=limit, event_ticker=event_ticker)
  out: dict[str, Any] = {"trades": trades}
  if event_ticker:
    out["hour_summary"] = store.hour_interval_summary(event_ticker)
  return out


@app.get("/api/hourly-trial-mech/bot")
def hourly_trial_mech_bot_status(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_mech_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial-mech/bot/settings")
async def hourly_trial_mech_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.hourly_trial_mech_bot_store("btc")
  _apply_hourly_bot_settings(store, body, cfg=_cfg)
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_mech_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial-mech/bot/reset-bankroll")
def hourly_trial_mech_bot_reset_bankroll(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_trial_mech_bot_store("btc")
  settings = store.get_settings()
  if settings.mode != "paper":
    raise HTTPException(400, "Reset bankroll is only available in paper mode")
  store.reset_paper_bankroll(settings.max_spend_per_hour_usd)
  tab = _loop.daily_prediction()
  return _loop.hourly_trial_mech_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly-trial-mech/bot/fresh-start")
def hourly_trial_mech_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_fresh_start(
    _loop.hourly_trial_mech_bot_store("btc"),
    _loop.daily_prediction,
    "btc",
    kind="hourly_trial_mech",
  )


@app.post("/api/hourly-trial-mech/bot/clear-history")
def hourly_trial_mech_bot_clear_history(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_clear_history(
    _loop.hourly_trial_mech_bot_store("btc"),
    _loop.daily_prediction,
    "btc",
    kind="hourly_trial_mech",
  )


@app.post("/api/hourly-trial-mech/bot/override-daily-cap")
def hourly_trial_mech_bot_override_daily_cap(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _override_daily_cap_hourly("btc", kind="hourly_trial_mech")


@app.get("/api/hourly-trial-mech/bot/trades")
def hourly_trial_mech_bot_trades(
  limit: int = Query(default=100, le=200),
  event_ticker: str | None = Query(default=None),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_trial_mech_bot_store("btc")
  trades = store.list_trades(limit=limit, event_ticker=event_ticker)
  out: dict[str, Any] = {"trades": trades}
  if event_ticker:
    out["hour_summary"] = store.hour_interval_summary(event_ticker)
  return out


@app.get("/api/eth/hourly-trial/bot")
def eth_hourly_trial_bot_status(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop.eth_hourly_prediction()
  return _loop.eth_hourly_trial_bot_status(tab if tab.get("ok") else None)


@app.post("/api/eth/hourly-trial/bot/settings")
async def eth_hourly_trial_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.hourly_trial_bot_store("eth")
  from src.assets import asset_cfg

  eth_cfg = _loop._eth_cfg or asset_cfg(_cfg, "eth")
  _apply_hourly_bot_settings(store, body, cfg=eth_cfg)
  tab = _loop.eth_hourly_prediction()
  return _loop.eth_hourly_trial_bot_status(tab if tab.get("ok") else None)


@app.post("/api/eth/hourly-trial/bot/reset-bankroll")
def eth_hourly_trial_bot_reset_bankroll(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_trial_bot_store("eth")
  settings = store.get_settings()
  if settings.mode != "paper":
    raise HTTPException(400, "Reset bankroll is only available in paper mode")
  store.reset_paper_bankroll(settings.max_spend_per_hour_usd)
  tab = _loop.eth_hourly_prediction()
  return _loop.eth_hourly_trial_bot_status(tab if tab.get("ok") else None)


@app.post("/api/eth/hourly-trial/bot/fresh-start")
def eth_hourly_trial_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_fresh_start(
    _loop.hourly_trial_bot_store("eth"),
    _loop.eth_hourly_prediction,
    "eth",
    kind="hourly_trial",
  )


@app.post("/api/eth/hourly-trial/bot/clear-history")
def eth_hourly_trial_bot_clear_history(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _hourly_bot_clear_history(
    _loop.hourly_trial_bot_store("eth"),
    _loop.eth_hourly_prediction,
    "eth",
    kind="hourly_trial",
  )


@app.post("/api/eth/hourly-trial/bot/override-daily-cap")
def eth_hourly_trial_bot_override_daily_cap(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _override_daily_cap_hourly("eth", kind="hourly_trial")


@app.get("/api/eth/hourly-trial/bot/trades")
def eth_hourly_trial_bot_trades(
  limit: int = Query(default=100, le=200),
  event_ticker: str | None = Query(default=None),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.hourly_trial_bot_store("eth")
  trades = store.list_trades(limit=limit, event_ticker=event_ticker)
  out: dict[str, Any] = {"trades": trades}
  if event_ticker:
    out["hour_summary"] = store.hour_interval_summary(event_ticker)
  return out


@app.get("/api/slot15/bot")
def slot15_bot_status(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop._slot15_tab("btc")
  return _loop.slot15_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/slot15/bot/settings")
async def slot15_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.slot15_bot_store("btc")
  _apply_slot15_bot_settings(store, body, cfg=_cfg)
  tab = _loop._slot15_tab("btc")
  return _loop.slot15_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/slot15/bot/reset-bankroll")
def slot15_bot_reset_bankroll(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.slot15_bot_store("btc")
  settings = store.get_settings()
  if settings.mode != "paper":
    raise HTTPException(400, "Reset bankroll is only available in paper mode")
  store.reset_paper_bankroll(settings.max_spend_per_slot_usd)
  tab = _loop._slot15_tab("btc")
  return _loop.slot15_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/slot15/bot/fresh-start")
def slot15_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _slot15_bot_fresh_start(_loop.slot15_bot_store("btc"), lambda: _loop._slot15_tab("btc"), "btc")


@app.post("/api/slot15/bot/clear-history")
def slot15_bot_clear_history(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _slot15_bot_clear_history(_loop.slot15_bot_store("btc"), lambda: _loop._slot15_tab("btc"), "btc")


@app.post("/api/slot15/bot/override-daily-cap")
def slot15_bot_override_daily_cap(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _override_daily_cap_slot15("btc")


@app.get("/api/slot15/bot/trades")
def slot15_bot_trades(
  limit: int = Query(default=100, le=200),
  event_ticker: str | None = Query(default=None),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  store = _loop.slot15_bot_store("btc")
  trades = store.list_trades(limit=limit, event_ticker=event_ticker)
  out: dict[str, Any] = {"trades": trades}
  if event_ticker:
    out["slot_summary"] = store.slot_interval_summary(event_ticker)
  return out


@app.get("/api/eth/15m/bot")
def eth_slot15_bot_status(
  lightweight: bool = Query(default=True),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  tab = _loop._slot15_tab_cached("eth")
  return _loop.slot15_bot_status(
    "eth",
    tab if tab.get("ok") else None,
    lightweight=lightweight,
  )


@app.post("/api/eth/15m/bot/settings")
async def eth_slot15_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  body = await request.json()
  store = _loop.slot15_bot_store("eth")
  from src.assets import asset_cfg

  eth_cfg = _loop._eth_cfg or asset_cfg(_cfg, "eth")
  _apply_slot15_bot_settings(store, body, cfg=eth_cfg)
  tab = _loop._slot15_tab("eth")
  return _loop.slot15_bot_status("eth", tab if tab.get("ok") else None)


@app.post("/api/eth/15m/bot/reset-bankroll")
def eth_slot15_bot_reset_bankroll(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  store = _loop.slot15_bot_store("eth")
  settings = store.get_settings()
  if settings.mode != "paper":
    raise HTTPException(400, "Reset bankroll is only available in paper mode")
  store.reset_paper_bankroll(settings.max_spend_per_slot_usd)
  tab = _loop._slot15_tab("eth")
  return _loop.slot15_bot_status("eth", tab if tab.get("ok") else None)


@app.post("/api/eth/15m/bot/fresh-start")
def eth_slot15_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  return _slot15_bot_fresh_start(_loop.slot15_bot_store("eth"), lambda: _loop._slot15_tab("eth"), "eth")


@app.post("/api/eth/15m/bot/clear-history")
def eth_slot15_bot_clear_history(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  return _slot15_bot_clear_history(_loop.slot15_bot_store("eth"), lambda: _loop._slot15_tab("eth"), "eth")


@app.post("/api/eth/15m/bot/override-daily-cap")
def eth_slot15_bot_override_daily_cap(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  return _override_daily_cap_slot15("eth")


@app.get("/api/eth/15m/bot/trades")
def eth_slot15_bot_trades(
  limit: int = Query(default=100, le=200),
  event_ticker: str | None = Query(default=None),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  store = _loop.slot15_bot_store("eth")
  trades = store.list_trades(limit=limit, event_ticker=event_ticker)
  out: dict[str, Any] = {"trades": trades}
  if event_ticker:
    out["slot_summary"] = store.slot_interval_summary(event_ticker)
  return out


@app.get("/api/eth/15m-trial/bot")
def eth_slot15_trial_bot_status(
  lightweight: bool = Query(default=True),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  tab = _loop._slot15_tab_cached("eth")
  return _loop.slot15_trial_bot_status(
    "eth",
    tab if tab.get("ok") else None,
    lightweight=lightweight,
  )


@app.post("/api/eth/15m-trial/bot/settings")
async def eth_slot15_trial_bot_settings(request: Request, _: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  body = await request.json()
  store = _loop.slot15_trial_bot_store("eth")
  from src.assets import asset_cfg

  eth_cfg = _loop._eth_cfg or asset_cfg(_cfg, "eth")
  _apply_slot15_trial_bot_settings(store, body, cfg=eth_cfg)
  tab = _loop._slot15_tab("eth")
  return _loop.slot15_trial_bot_status("eth", tab if tab.get("ok") else None)


@app.post("/api/eth/15m-trial/bot/reset-bankroll")
def eth_slot15_trial_bot_reset_bankroll(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  store = _loop.slot15_trial_bot_store("eth")
  settings = store.get_settings()
  store.reset_paper_bankroll(settings.max_spend_per_slot_usd)
  tab = _loop._slot15_tab("eth")
  return _loop.slot15_trial_bot_status("eth", tab if tab.get("ok") else None)


@app.post("/api/eth/15m-trial/bot/fresh-start")
def eth_slot15_trial_bot_fresh_start(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  return _slot15_bot_fresh_start(
    _loop.slot15_trial_bot_store("eth"),
    lambda: _loop._slot15_tab("eth"),
    "eth",
    kind="slot15_trial",
    status_fn=_loop.slot15_trial_bot_status,
  )


@app.post("/api/eth/15m-trial/bot/clear-history")
def eth_slot15_trial_bot_clear_history(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  return _slot15_bot_clear_history(
    _loop.slot15_trial_bot_store("eth"),
    lambda: _loop._slot15_tab("eth"),
    "eth",
    kind="slot15_trial",
    status_fn=_loop.slot15_trial_bot_status,
  )


@app.post("/api/eth/15m-trial/bot/override-daily-cap")
def eth_slot15_trial_bot_override_daily_cap(_: None = Depends(_session_user)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  return _override_daily_cap_slot15(
    "eth",
    kind="slot15_trial",
    store=_loop.slot15_trial_bot_store("eth"),
    status_fn=_loop.slot15_trial_bot_status,
  )


@app.get("/api/eth/15m-trial/bot/trades")
def eth_slot15_trial_bot_trades(
  limit: int = Query(default=100, le=200),
  event_ticker: str | None = Query(default=None),
  _: None = Depends(_session_user),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.eth_calibration is None:
    raise HTTPException(503, "ETH 15m disabled")
  store = _loop.slot15_trial_bot_store("eth")
  trades = store.list_trades(limit=limit, event_ticker=event_ticker)
  out: dict[str, Any] = {"trades": trades}
  if event_ticker:
    out["slot_summary"] = store.slot_interval_summary(event_ticker)
  return out


@app.post("/api/admin/train-hourly")
def admin_train_hourly(
  min_samples: int = Query(default=500, ge=100),
  _: None = Depends(_verify_admin),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.hourly_train_status.get("state") == "running":
    return {"status": "running", **_loop.hourly_train_status}

  def _run():
    _loop.train_hourly_model(min_samples=min_samples)

  threading.Thread(target=_run, daemon=True).start()
  return {"status": "started", "message": "Hourly training in background. Poll /api/admin/train-hourly/status."}


@app.get("/api/admin/train-hourly/status")
def admin_train_hourly_status(_: None = Depends(_verify_admin)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.hourly_train_status


@app.post("/api/admin/reset-hourly-stats")
def admin_reset_hourly_stats(
  note: str = Query(default="hourly epoch reset"),
  _: None = Depends(_verify_admin),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return {"status": "ok", **_loop.hourly_calibration.reset_stats(note=note)}


@app.post("/api/admin/snapshot-hourly-stats")
def admin_snapshot_hourly_stats(
  note: str = Query(default="hourly snapshot"),
  _: None = Depends(_verify_admin),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return {"status": "ok", **_loop.hourly_calibration.snapshot_stats(note=note)}


@app.post("/api/predict/now")
def predict_now(_: None = Depends(_verify_admin)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  pred = _loop.run_prediction()
  if pred is None:
    raise HTTPException(500, _loop.last_error or "Prediction failed")
  return _prediction_to_dict(pred)


@app.post("/api/admin/second-chance-now")
def second_chance_now(_: None = Depends(_verify_admin)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  out = _loop.run_second_chance()
  if out is None:
    raise HTTPException(500, _loop.last_error or "2nd Chance not logged (no open prediction or already logged)")
  return {"status": "ok", **out}


@app.post("/api/admin/train-second-chance")
def admin_train_second_chance(
  min_samples: int | None = Query(default=None, ge=50),
  _: None = Depends(_verify_admin),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.second_chance_train_status.get("state") == "running":
    return {"status": "running", **_loop.second_chance_train_status}

  def _run():
    _loop.train_second_chance_model(min_samples=min_samples)

  threading.Thread(target=_run, daemon=True).start()
  return {"status": "started", "message": "2nd Chance training in background. Poll /api/admin/train-second-chance/status."}


@app.get("/api/admin/train-second-chance/status")
def admin_train_second_chance_status(_: None = Depends(_verify_admin)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.second_chance_train_status


@app.post("/api/admin/collect")
def collect_historical(
  years: int = Query(default=3, le=5),
  full: bool = Query(default=False, description="Ignore resume and backfill full history"),
  _: None = Depends(_verify_admin),
):
  """Kick off historical data collection in a background thread."""
  if _loop is None:
    raise HTTPException(503, "Service starting")

  def _run():
    collector = HistoricalCollector(_cfg)
    log.info("Starting historical collection (%d years, full=%s)...", years, full)
    results = collector.collect_all(force_full=full)
    aux = collector.collect_auxiliary()
    log.info("Collection done: candles=%s auxiliary=%s", results, aux)

  threading.Thread(target=_run, daemon=True).start()
  return {
    "status": "started",
    "years": years,
    "full": full,
    "message": "Collection running in background. Check /health for candle counts.",
  }


@app.get("/api/postmortems")
def list_postmortems(limit: int = Query(default=15, le=100)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.postmortems.load_recent(limit)


@app.post("/api/admin/train")
def admin_train(
  min_samples: int | None = Query(default=None, ge=50, le=10000),
  _: None = Depends(_verify_admin),
):
  """Train LightGBM on stored candles in a background thread."""
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.train_status.get("state") == "running":
    return {"status": "running", **_loop.train_status}

  def _run():
    _loop.train_model(min_samples=min_samples)

  threading.Thread(target=_run, daemon=True).start()
  return {"status": "started", "message": "Training in background. Poll /api/admin/train/status."}


@app.get("/api/admin/train/status")
def admin_train_status(_: None = Depends(_verify_admin)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.train_status


@app.post("/api/admin/snapshot-stats")
def admin_snapshot_stats(
  note: str = Query(default="epoch snapshot"),
  _: None = Depends(_verify_admin),
):
  """Fold current DB epoch into persistent all-time archive without clearing predictions."""
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return {"status": "ok", **_loop.calibration.snapshot_stats(note=note)}


@app.post("/api/admin/fresh-start-all-paper-bots")
def admin_fresh_start_all_paper_bots(_: None = Depends(_session_user)):
  """Clear trade logs for every bot; paper bots also reset bankroll (live bots: log wipe only)."""
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.bot_fresh_start_all import fresh_start_all_bot_stores

  results = fresh_start_all_bot_stores(_loop, _cfg)
  return {"status": "ok", "reset": results}


@app.post("/api/admin/set-stats-epoch")
def admin_set_stats_epoch(
  at: str = Query(
    default="2026-07-04T16:59:00+00:00",
    description="ISO-8601 instant; stats count from here forward (default Jul 4 2026 12:59 PM EDT)",
  ),
  _: None = Depends(_session_user),
):
  """Backdate stats window on all bot DBs without clearing trades."""
  if _loop is None:
    raise HTTPException(503, "Service starting")
  from src.trading.bot_fresh_start_all import set_stats_epoch_all_stores

  try:
    parsed = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
  except ValueError as exc:
    raise HTTPException(400, f"Invalid at timestamp: {at}") from exc
  at_iso = parsed.astimezone(timezone.utc).isoformat()
  results = set_stats_epoch_all_stores(_loop, at_iso)
  return {"status": "ok", "stats_epoch_at": at_iso, "stores": results}


@app.post("/api/admin/reset-stats")
def admin_reset_stats(
  note: str = Query(default="release baseline"),
  _: None = Depends(_verify_admin),
):
  """Archive current epoch aggregates, clear prediction history, start fresh epoch."""
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return {"status": "ok", **_loop.reset_calibration_stats(note=note)}


@app.post("/api/admin/backfill-late")
def backfill_late(_: None = Depends(_verify_admin)):
  """Backfill missed late-entry fields from post-mortems and 1m replay."""
  from src.calibration.backfill_late import backfill_late_entries

  stats = backfill_late_entries(_cfg, dry_run=False, force=False, replay=True)
  return {"status": "ok", **stats}


@app.post("/api/admin/backfill-bot-pnl")
def backfill_bot_pnl(_: None = Depends(_verify_admin)):
  """Recompute inverted historical NO exit P&L in bot trade databases."""
  from src.trading.bot_pnl_backfill import backfill_all_bot_dbs, sync_daily_risk_from_trade_logs

  data_dir = Path(_cfg["paths"]["logs"]).parent
  stats = backfill_all_bot_dbs(data_dir, dry_run=False, cfg=_cfg)
  risk_sync = sync_daily_risk_from_trade_logs(data_dir, cfg=_cfg)
  return {"status": "ok", **stats, "daily_risk_sync": risk_sync}


@app.post("/api/admin/sync-daily-risk")
def admin_sync_daily_risk(_: None = Depends(_verify_admin)):
  """Reconcile bot_daily_risk.json with today's live exit P&L from trade logs."""
  from src.trading.bot_pnl_backfill import sync_daily_risk_from_trade_logs

  data_dir = Path(_cfg["paths"]["logs"]).parent
  risk_sync = sync_daily_risk_from_trade_logs(data_dir, cfg=_cfg)
  return {"status": "ok", **risk_sync}


@app.post("/api/admin/backfill-rollover-settlement")
def backfill_rollover_settlement(_: None = Depends(_verify_admin)):
  """Correct hourly period-rollover exits that used market marks instead of settlement."""
  from src.trading.bot_rollover_settlement_backfill import backfill_all_hourly_rollover_dbs

  data_dir = Path(_cfg["paths"]["logs"]).parent
  stats = backfill_all_hourly_rollover_dbs(data_dir, dry_run=False, cfg=_cfg)
  return {"status": "ok", **stats}


@app.post("/api/admin/backfill-phantom-settlement")
def backfill_phantom_settlement(_: None = Depends(_verify_admin)):
  from pathlib import Path

  from src.trading.bot_phantom_settlement_cleanup import cleanup_all_phantom_settlement_dbs

  data_dir = Path(_cfg["paths"]["logs"]).parent
  stats = cleanup_all_phantom_settlement_dbs(data_dir, dry_run=False, cfg=_cfg)
  return stats


@app.post("/api/admin/backfill-kalshi")
def backfill_kalshi(_: None = Depends(_verify_admin)):
  """Re-resolve prediction history using Kalshi KXBTC15M BRTI settlement."""
  from src.calibration.backfill import backfill_kalshi_predictions

  stats = backfill_kalshi_predictions(_cfg, dry_run=False)
  return {"status": "ok", **stats}


@app.post("/api/admin/backup-logs")
def admin_backup_logs(_: None = Depends(_verify_admin)):
  """Run full log backup now (paper + live trade exports and DB snapshots)."""
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return {"status": "ok", **_loop.run_log_backup(reason="manual")}


@app.get("/api/admin/backup-status")
def admin_backup_status(_: None = Depends(_verify_admin)):
  """Infra + Kalshi tax export status (volume, creds, per-bot CSV row counts)."""
  from src.backup.logs_backup import backup_summary, tax_export_status, volume_is_persistent

  if _loop is None:
    raise HTTPException(503, "Service starting")
  kalshi = getattr(_loop, "kalshi", None)
  data_dir = os.getenv("DATA_DIR", "/data")
  return {
    "status": "ok",
    "data_dir": data_dir,
    "volume_mounted_at_data": volume_is_persistent(data_dir),
    "railway_volume_mount_path": os.getenv("RAILWAY_VOLUME_MOUNT_PATH"),
    "kalshi_authenticated": bool(kalshi and getattr(kalshi, "authenticated", False)),
    "log_backup": backup_summary(_cfg),
    "tax_export": tax_export_status(_cfg),
  }


@app.get("/api/admin/backup-archive")
def admin_backup_archive(
  mode: str = Query("live", pattern="^(paper|live)$"),
  _: None = Depends(_verify_admin),
):
  """Download paper/ or live/ backup folder as zip (tax CSVs live under live/)."""
  from src.backup.logs_backup import build_backup_archive

  try:
    payload = build_backup_archive(_cfg, mode)
  except FileNotFoundError as e:
    raise HTTPException(404, str(e)) from e
  stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  return Response(
    content=payload,
    media_type="application/zip",
    headers={"Content-Disposition": f'attachment; filename="btc-predictor-{mode}-backup-{stamp}.zip"'},
  )


from src.api.index_hourly_routes import register_index_hourly_routes

register_index_hourly_routes(
  app,
  lambda: _loop,
  lambda: _cfg,
  _session_user,
  _apply_hourly_bot_settings,
)

from src.api.sports_routes import register_sports_routes

register_sports_routes(
  app,
  lambda: _loop,
  lambda: _cfg,
  _session_user,
)

from src.api.human_trade_routes import register_human_trade_routes
from src.api.human_slot15_trade_routes import register_human_slot15_trade_routes

register_human_trade_routes(
  app,
  get_loop=lambda: _loop,
  get_cfg=lambda: _cfg,
  session_dep=_session_user,
)
register_human_slot15_trade_routes(
  app,
  get_loop=lambda: _loop,
  get_cfg=lambda: _cfg,
  session_dep=_session_user,
)
