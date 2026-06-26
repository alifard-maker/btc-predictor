"""2nd Chance — reassess every slot at t+4min using open prediction + intra-slot state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.features.second_chance_labels import second_chance_feature_columns
from src.models.prob_calibration import ProbabilityCalibrator
from src.trading.edge import Signal
from src.trading.late_entry import LateEntryAdvisor, SlotPathStats

log = logging.getLogger(__name__)

SIGNAL_LONG = "2ND LONG"
SIGNAL_SHORT = "2ND SHORT"
SIGNAL_NO_TRADE = "2ND NO TRADE"


@dataclass(frozen=True)
class SecondChanceDecision:
  signal: str
  prob_up: float
  confidence: float
  expected_move_pct: float
  summary: str
  reasons: list[str]
  method: str  # ml | blend | path


class SecondChanceAdvisor:
  """Re-evaluate at minute 4 for all slots with an opening prediction."""

  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    self.scfg = cfg.get("second_chance", {})
    self.enabled = bool(self.scfg.get("enabled", True))
    self.elapsed_minutes = float(self.scfg.get("elapsed_minutes", 4))
    self.min_confidence = float(self.scfg.get("min_confidence", cfg.get("min_edge_confidence", 0.57)))
    self.ml_weight = float(self.scfg.get("blend", {}).get("ml_weight", 0.55))
    self.open_weight = float(self.scfg.get("blend", {}).get("open_weight", 0.25))
    self.path_weight = float(self.scfg.get("blend", {}).get("path_weight", 0.20))
    self.late = LateEntryAdvisor(cfg)
    self.model = None
    self.feature_names: list[str] = second_chance_feature_columns()
    self.calibrator = ProbabilityCalibrator()
    self._load_model()

  def _model_path(self) -> Path:
    return Path(self.cfg["paths"]["models"]) / "model_second_chance.joblib"

  def _load_model(self) -> None:
    path = self._model_path()
    if not path.exists():
      return
    try:
      from src.models.second_chance_trainer import SecondChanceTrainer
      trainer = SecondChanceTrainer(self.cfg)
      trainer.load(path)
      self.model = trainer.model
      self.feature_names = trainer.feature_names
      self.calibrator = trainer.calibrator
      log.info("Loaded 2nd Chance model (%d features)", len(self.feature_names))
    except Exception as e:
      log.warning("2nd Chance model load failed: %s", e)

  def _feature_row(
    self,
    *,
    open_prob_up: float,
    open_signal: str,
    stats: SlotPathStats,
    seconds_remaining: int,
  ) -> pd.DataFrame:
    elapsed = self.elapsed_minutes
    mins_left = max(0.1, seconds_remaining / 60.0)
    row = {
      "open_prob_up": open_prob_up,
      "open_signal_long": int(open_signal == Signal.LONG.value),
      "open_signal_short": int(open_signal == Signal.SHORT.value),
      "gap_pct": stats.gap_pct,
      "pct_time_above_ref": stats.pct_time_above_ref,
      "ref_crossings": stats.ref_crossings,
      "slot_mom_pct": stats.slot_mom_pct,
      "recent_mom_pct": stats.recent_mom_pct,
      "recent_above_ref_pct": stats.recent_above_ref_pct,
      "elapsed_minutes": elapsed,
      "minutes_remaining": mins_left,
    }
    return pd.DataFrame([row])

  def _ml_prob(self, features: pd.DataFrame) -> float | None:
    if self.model is None:
      return None
    cols = [c for c in self.feature_names if c in features.columns]
    row = features.iloc[0]
    if any(pd.isna(row.get(c)) for c in cols):
      return None
    X = pd.DataFrame([{c: row[c] for c in cols}])
    prob = float(self.model.predict_proba(X)[:, 1][0])
    if self.calibrator.fitted:
      prob = float(self.calibrator.transform(prob))
    return prob

  def _signal_from_prob(self, prob_up: float) -> str:
    if prob_up >= self.min_confidence:
      return SIGNAL_LONG
    if prob_up <= (1.0 - self.min_confidence):
      return SIGNAL_SHORT
    return SIGNAL_NO_TRADE

  def evaluate(
    self,
    *,
    open_prob_up: float,
    open_signal: str,
    reference_price: float,
    current_price: float,
    df_1m: pd.DataFrame | None,
    slot_start: pd.Timestamp,
    seconds_remaining: int,
  ) -> SecondChanceDecision:
    if not self.enabled:
      return SecondChanceDecision(
        signal=SIGNAL_NO_TRADE,
        prob_up=open_prob_up,
        confidence=0.0,
        expected_move_pct=0.0,
        summary="2nd Chance disabled.",
        reasons=[],
        method="disabled",
      )

    stats = LateEntryAdvisor.slot_path_stats(
      df_1m,
      slot_start,
      reference_price,
      momentum_bars=4,
      recovery_bars=4,
    )
    path_prob = self.late.reassess_prob_up_at_close(
      reference_price=reference_price,
      current_price=current_price,
      seconds_remaining=seconds_remaining,
      stats=stats,
      original_prob_up=open_prob_up,
    )
    features = self._feature_row(
      open_prob_up=open_prob_up,
      open_signal=open_signal,
      stats=stats,
      seconds_remaining=seconds_remaining,
    )
    ml_prob = self._ml_prob(features)

    weights = []
    probs = []
    if ml_prob is not None:
      weights.append(self.ml_weight)
      probs.append(ml_prob)
    weights.append(self.open_weight)
    probs.append(open_prob_up)
    weights.append(self.path_weight)
    probs.append(path_prob)
    w_sum = sum(weights) or 1.0
    prob_up = float(np.clip(sum(p * w for p, w in zip(probs, weights)) / w_sum, 0.05, 0.95))

    if ml_prob is not None:
      method = "blend"
    else:
      method = "path"

    signal = self._signal_from_prob(prob_up)
    confidence = abs(prob_up - 0.5) * 2.0
    expected_move_pct = stats.gap_pct

    mins, secs = divmod(max(0, seconds_remaining), 60)
    summary = (
      f"2nd Chance ({mins}m {secs:02d}s left): {prob_up * 100:.0f}% UP at close vs "
      f"${reference_price:,.2f} ref · open was {open_prob_up * 100:.0f}%"
    )
    reasons = [
      f"Move vs t=0: {stats.gap_pct:+.2f}% after {self.elapsed_minutes:.0f} min.",
      f"Held above ref ~{stats.pct_time_above_ref * 100:.0f}% of slot so far.",
      f"Open signal: {open_signal}.",
    ]
    if ml_prob is not None:
      reasons.append(f"ML @ t+4: {ml_prob * 100:.0f}% UP.")

    return SecondChanceDecision(
      signal=signal,
      prob_up=prob_up,
      confidence=confidence,
      expected_move_pct=expected_move_pct,
      summary=summary,
      reasons=reasons,
      method=method,
    )
