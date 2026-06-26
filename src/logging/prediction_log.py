from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.calibration.tracker import CalibrationTracker
from src.models.predictor import Prediction


class PredictionLogger:
  def __init__(self, cfg: dict[str, Any]):
    self.tracker = CalibrationTracker(cfg)
    logs_dir = Path(cfg["paths"]["logs"])
    logs_dir.mkdir(parents=True, exist_ok=True)
    self.jsonl_path = logs_dir / "predictions.jsonl"

  def log(self, pred: Prediction) -> int:
    ts = pred.timestamp.isoformat() if hasattr(pred.timestamp, "isoformat") else str(pred.timestamp)
    row_id = self.tracker.log_prediction(
      timestamp=ts,
      price=pred.price,
      prob_up=pred.prob_up,
      prob_down=pred.prob_down,
      confidence=pred.confidence,
      signal=pred.signal.value,
      expected_move=pred.expected_move,
    )

    record = {
      "timestamp": ts,
      "price": pred.price,
      "prob_up": pred.prob_up,
      "prob_down": pred.prob_down,
      "confidence": pred.confidence,
      "signal": pred.signal.value,
      "slot_label": pred.slot_label,
      "expected_move": pred.expected_move,
      "features": pred.features_snapshot,
      "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(self.jsonl_path, "a") as f:
      f.write(json.dumps(record) + "\n")

    return row_id
