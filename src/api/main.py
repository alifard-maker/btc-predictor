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
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from src.calibration.tracker import CalibrationTracker
from src.config import load_config
from src.data.storage import HistoricalCollector
from src.models.predictor import Prediction
from src.scheduler.loop import PredictionLoop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

_cfg = load_config()
_loop: PredictionLoop | None = None
_scheduler = None


def _prediction_to_dict(pred: Prediction) -> dict[str, Any]:
  return {
    "timestamp": pred.timestamp.isoformat() if hasattr(pred.timestamp, "isoformat") else str(pred.timestamp),
    "slot_start": pred.slot_start.isoformat() if pred.slot_start is not None else None,
    "slot_end": pred.slot_end.isoformat() if pred.slot_end is not None else None,
    "slot_label": pred.slot_label,
    "horizon_minutes": _cfg.get("prediction_horizon_minutes", 15),
    "price": pred.price,
    "prob_up": round(pred.prob_up, 4),
    "prob_down": round(pred.prob_down, 4),
    "confidence": round(pred.confidence, 4),
    "expected_move": round(pred.expected_move, 2),
    "signal": pred.signal.value,
    "formatted": _loop.predictor.format_output(pred) if _loop else "",
  }


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
  yield

  if _scheduler:
    _scheduler.shutdown(wait=False)
    log.info("Scheduler shut down")


app = FastAPI(
  title="BTC Predictor",
  description="Probabilistic BTC direction assistant — Railway backend",
  version="0.1.0",
  lifespan=lifespan,
)

app.add_middleware(
  CORSMiddleware,
  allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
  allow_credentials=True,
  allow_methods=["*"],
  allow_headers=["*"],
)


def _serialize_value(v: Any) -> Any:
  """Make DB/pandas values JSON-safe."""
  if v is None:
    return None
  try:
    if pd.isna(v):
      return None
  except (TypeError, ValueError):
    pass
  if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
    return None
  if hasattr(v, "isoformat"):
    return v.isoformat()
  if isinstance(v, (np.integer, np.floating, np.bool_)):
    return v.item()
  return v


def _serialize_records(df: pd.DataFrame) -> list[dict[str, Any]]:
  records = df.to_dict(orient="records")
  for r in records:
    for k in list(r.keys()):
      r[k] = _serialize_value(r[k])
  return records


_DASHBOARD_HTML = Path(__file__).parent / "static" / "dashboard.html"


@app.get("/")
def root():
  return RedirectResponse(url="/dashboard")


@app.get("/dashboard")
def dashboard():
  return FileResponse(_DASHBOARD_HTML, media_type="text/html")


@app.get("/health")
def health():
  """Always return 200 so Railway healthchecks pass."""
  if _loop is None:
    return {"status": "starting", "service": "btc-predictor"}
  status = _loop.status()
  return {"status": "ok", "service": "btc-predictor", **status}


@app.get("/api/status")
def api_status():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  return _loop.status()


@app.get("/api/prediction/latest")
def latest_prediction():
  if _loop is None:
    raise HTTPException(503, "Service starting")
  if _loop.latest_prediction:
    return _prediction_to_dict(_loop.latest_prediction)
  row = _loop.calibration.latest()
  if row:
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
  tracker = _loop.calibration
  summary = tracker.summary()
  report = tracker.calibration_report()
  bins = report.to_dict(orient="records") if not report.empty else []
  return {"summary": summary, "bins": bins}


@app.post("/api/predict/now")
def predict_now(_: None = Depends(_verify_admin)):
  if _loop is None:
    raise HTTPException(503, "Service starting")
  pred = _loop.run_prediction()
  if pred is None:
    raise HTTPException(500, _loop.last_error or "Prediction failed")
  return _prediction_to_dict(pred)


@app.post("/api/admin/collect")
def collect_historical(
  years: int = Query(default=3, le=5),
  _: None = Depends(_verify_admin),
):
  """Kick off historical data collection in a background thread."""
  if _loop is None:
    raise HTTPException(503, "Service starting")

  def _run():
    collector = HistoricalCollector(_cfg)
    log.info("Starting historical collection (%d years)...", years)
    results = collector.collect_all()
    log.info("Collection done: %s", results)

  threading.Thread(target=_run, daemon=True).start()
  return {"status": "started", "years": years, "message": "Collection running in background. Check /api/status for candle count."}
