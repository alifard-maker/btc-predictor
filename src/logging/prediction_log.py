from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.calibration.tracker import CalibrationTracker
from src.calibration.sources import KALSHI_REF_SOURCE
from src.models.predictor import Prediction


class PredictionLogger:
  def __init__(self, cfg: dict[str, Any]):
    self.tracker = CalibrationTracker(cfg)
    logs_dir = Path(cfg["paths"]["logs"])
    logs_dir.mkdir(parents=True, exist_ok=True)
    self.jsonl_path = logs_dir / "predictions.jsonl"

  def log(self, pred: Prediction, *, kalshi_market_ticker: str = "") -> int:
    ts = pred.timestamp.isoformat() if hasattr(pred.timestamp, "isoformat") else str(pred.timestamp)
    ref = pred.reference_price or pred.price
    ref_source = pred.reference_source or KALSHI_REF_SOURCE
    row_id = self.tracker.log_prediction(
      timestamp=ts,
      price=ref,
      prob_up=pred.prob_up,
      prob_down=pred.prob_down,
      confidence=pred.confidence,
      signal=pred.signal.value,
      expected_move=pred.expected_move,
      reference_source=ref_source,
      kalshi_market_ticker=kalshi_market_ticker,
    )

    record = {
      "timestamp": ts,
      "price": ref,
      "reference_price": ref,
      "reference_source": ref_source,
      "kalshi_market_ticker": kalshi_market_ticker,
      "current_price": pred.current_price,
      "slot_label": pred.slot_label,
      "prob_up": pred.prob_up,
      "prob_down": pred.prob_down,
      "confidence": pred.confidence,
      "signal": pred.signal.value,
      "expected_move": pred.expected_move,
      "model_signal": pred.model_signal,
      "regime_notes": pred.regime_notes or [],
      "raw_prob_up": pred.raw_prob_up,
      "features": pred.features_snapshot,
      "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(self.jsonl_path, "a") as f:
      f.write(json.dumps(record) + "\n")

    return row_id
