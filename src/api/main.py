from __future__ import annotations

import logging
import math
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

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
    quote = _loop.live_price_quote(fresh=True, asset=asset)
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


@app.get("/health")
def health():
  """Always return 200 so Railway healthchecks pass."""
  base = {"status": "starting", "service": "btc-predictor", "version": APP_VERSION}
  if _loop is None:
    return base
  status = _loop.status()
  out = {"status": "ok", "service": "btc-predictor", "version": APP_VERSION, **status}
  if _loop.eth_calibration is not None:
    out["eth_15m"] = _loop.eth_status()
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
    return out
  row = _loop.calibration.latest()
  if row:
    row["slot_monitor"] = monitor
    _apply_ref_override_fields(row, monitor)
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
    return out
  row = _loop.eth_calibration.latest()
  if row:
    row["slot_monitor"] = monitor
    row["asset"] = "eth"
    _apply_ref_override_fields(row, monitor)
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
def daily_prediction():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.daily_prediction()


@app.get("/api/eth/hourly/prediction")
def eth_hourly_prediction():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.eth_hourly_prediction()


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


def _apply_hourly_bot_settings(store, body: dict[str, Any]) -> dict[str, Any]:
  from src.trading.hourly_bot_store import HourlyBotSettings

  current = store.get_settings()
  mode = str(body.get("mode", current.mode))
  if mode not in ("paper", "live"):
    raise HTTPException(400, "mode must be paper or live")
  settings = HourlyBotSettings(
    enabled=bool(body.get("enabled", current.enabled)),
    mode=mode,
    max_spend_per_hour_usd=float(body.get("max_spend_per_hour_usd", current.max_spend_per_hour_usd)),
    allow_strong=bool(body.get("allow_strong", current.allow_strong)),
    allow_actionable=bool(body.get("allow_actionable", current.allow_actionable)),
  )
  if settings.max_spend_per_hour_usd < 0:
    raise HTTPException(400, "max_spend_per_hour_usd must be >= 0")
  if not settings.allow_strong and not settings.allow_actionable:
    raise HTTPException(400, "Enable at least one of STRONG or ACTIONABLE")
  store.save_settings(settings)
  return settings.to_dict()


@app.get("/api/hourly/bot")
def hourly_bot_status(_: None = Depends(require_session)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop.daily_prediction()
  return _loop.hourly_bot_status("btc", tab if tab.get("ok") else None)


@app.post("/api/hourly/bot/settings")
async def hourly_bot_settings(request: Request, _: None = Depends(require_session)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.hourly_bot_store("btc")
  _apply_hourly_bot_settings(store, body)
  tab = _loop.daily_prediction()
  return _loop.hourly_bot_status("btc", tab if tab.get("ok") else None)


@app.get("/api/hourly/bot/trades")
def hourly_bot_trades(
  limit: int = Query(default=30, le=100),
  _: None = Depends(require_session),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.hourly_bot_store("btc").list_trades(limit=limit)


@app.get("/api/eth/hourly/bot")
def eth_hourly_bot_status(_: None = Depends(require_session)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  tab = _loop.eth_hourly_prediction()
  return _loop.hourly_bot_status("eth", tab if tab.get("ok") else None)


@app.post("/api/eth/hourly/bot/settings")
async def eth_hourly_bot_settings(request: Request, _: None = Depends(require_session)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  body = await request.json()
  store = _loop.hourly_bot_store("eth")
  _apply_hourly_bot_settings(store, body)
  tab = _loop.eth_hourly_prediction()
  return _loop.hourly_bot_status("eth", tab if tab.get("ok") else None)


@app.get("/api/eth/hourly/bot/trades")
def eth_hourly_bot_trades(
  limit: int = Query(default=30, le=100),
  _: None = Depends(require_session),
):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.hourly_bot_store("eth").list_trades(limit=limit)


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


@app.post("/api/admin/backfill-kalshi")
def backfill_kalshi(_: None = Depends(_verify_admin)):
  """Re-resolve prediction history using Kalshi KXBTC15M BRTI settlement."""
  from src.calibration.backfill import backfill_kalshi_predictions

  stats = backfill_kalshi_predictions(_cfg, dry_run=False)
  return {"status": "ok", **stats}
