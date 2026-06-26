"""Isotonic calibration for model probabilities."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

log = logging.getLogger(__name__)


class ProbabilityCalibrator:
  def __init__(self):
    self._iso: IsotonicRegression | None = None
    self.fitted = False

  def fit(self, prob_up: pd.Series | np.ndarray, outcomes: pd.Series | np.ndarray) -> bool:
    p = np.asarray(prob_up, dtype=float)
    y = np.asarray(outcomes, dtype=int)
    mask = np.isfinite(p) & np.isfinite(y)
    p, y = p[mask], y[mask]
    if len(p) < 30 or len(np.unique(y)) < 2:
      log.warning("Not enough resolved samples to fit calibrator (%d)", len(p))
      return False
    self._iso = IsotonicRegression(out_of_bounds="clip", y_min=0.02, y_max=0.98)
    self._iso.fit(p, y)
    self.fitted = True
    return True

  def transform(self, prob_up: float) -> float:
    if not self.fitted or self._iso is None:
      return float(prob_up)
    return float(self._iso.predict([prob_up])[0])

  def to_dict(self) -> dict[str, Any]:
    return {"fitted": self.fitted, "iso": self._iso}

  @classmethod
  def from_dict(cls, data: dict[str, Any]) -> "ProbabilityCalibrator":
    obj = cls()
    obj._iso = data.get("iso")
    obj.fitted = bool(data.get("fitted")) and obj._iso is not None
    return obj
