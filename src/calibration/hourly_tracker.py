"""Track hourly Kalshi contract predictions vs settlement."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src.calibration.epoch import read_hourly_stats_epoch, write_hourly_stats_epoch
from src.calibration.stats_archive import (
  CategoryAgg,
  combined_public,
  df_after_snapshot,
  epoch_aggs_from_df,
  latest_slot_iso,
  merge_category,
  to_utc_timestamp,
)
from src.db.hourly_store import HourlyPredictionStore, create_hourly_store


def _archive_path(cfg: dict[str, Any]) -> Path:
  return Path(cfg["paths"]["logs"]) / "hourly_stats_archive.json"


def read_hourly_archive(cfg: dict[str, Any]):
  from src.calibration.stats_archive import StatsArchive
  path = _archive_path(cfg)
  if not path.exists():
    return StatsArchive()
  try:
    return StatsArchive.from_dict(json.loads(path.read_text()))
  except Exception:
    return StatsArchive()


def write_hourly_archive(cfg: dict[str, Any], archive) -> None:
  path = _archive_path(cfg)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(archive.to_dict(), indent=2) + "\n")


def archive_hourly_epoch(
  cfg: dict[str, Any],
  aggs: tuple,
  *,
  epoch_df: pd.DataFrame,
  increment_epochs: bool = False,
  note: str = "",
):
  open_agg, _, _, _, late_events, _, _ = aggs
  archive = read_hourly_archive(cfg)
  archive.open = merge_category(archive.open, open_agg)
  archive.late_events.extend(late_events)
  if increment_epochs:
    archive.epochs_archived += 1
  archive.last_archived_at = datetime.now(timezone.utc).isoformat()
  if not epoch_df.empty:
    slot_end = latest_slot_iso(epoch_df)
    if slot_end:
      prev = to_utc_timestamp(archive.snapshot_through) if archive.snapshot_through else None
      nxt = to_utc_timestamp(slot_end)
      archive.snapshot_through = (max(prev, nxt) if prev is not None else nxt).isoformat()
  if note:
    archive.snapshot_note = note
  write_hourly_archive(cfg, archive)
  return archive


class HourlyCalibrationTracker:
  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    self.store: HourlyPredictionStore = create_hourly_store(cfg)

  def log_prediction(self, row: dict[str, Any]) -> int:
    return self.store.log_prediction(row)

  def get_pending(self) -> list[dict[str, Any]]:
    return self.store.get_pending()

  def resolve(self, event_ticker: str, resolution) -> bool:
    return self.store.resolve(event_ticker, resolution)

  def load_resolved(self) -> pd.DataFrame:
    return self.store.load_resolved()

  def load_recent(self, limit: int = 50) -> pd.DataFrame:
    return self.store.load_recent(limit)

  def _correct(self, df: pd.DataFrame) -> pd.Series:
    def _row(r: pd.Series) -> bool:
      sig = str(r.get("primary_signal") or "")
      outcome = r.get("outcome")
      if outcome is None or (isinstance(outcome, float) and pd.isna(outcome)):
        return False
      if sig == "LEAN YES":
        return bool(outcome)
      if sig == "LEAN NO":
        return not bool(outcome)
      prob = float(r.get("primary_model_prob") or 0.5)
      return (prob >= 0.5) == bool(outcome)
    return df.apply(_row, axis=1)

  def _rolling(self, df: pd.DataFrame, windows=None) -> dict[str, dict[str, Any]]:
    windows = windows or [1, 2, 4, 12, 24]
    empty = {f"{h}h": {"correct": 0, "total": 0, "accuracy": None} for h in windows}
    if df.empty:
      return empty
    now = pd.Timestamp.now(tz="UTC")
    df = df.copy()
    df["_t"] = pd.to_datetime(df["logged_at"], utc=True)
    correct = self._correct(df)
    out: dict[str, dict[str, Any]] = {}
    for h in windows:
      mask = df["_t"] >= (now - pd.Timedelta(hours=h))
      n = int(mask.sum())
      if n == 0:
        out[f"{h}h"] = {"correct": 0, "total": 0, "accuracy": None}
      else:
        c = int(correct[mask].sum())
        out[f"{h}h"] = {"correct": c, "total": n, "accuracy": float(c / n)}
    return out

  def _training_frame(self, df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
      return df
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["logged_at"], utc=True)
    out["prob_up"] = pd.to_numeric(out["primary_model_prob"], errors="coerce")
    out["signal"] = out["primary_signal"]
    return out

  def summary(self) -> dict[str, Any]:
    df = self.load_resolved()
    archive = read_hourly_archive(self.cfg)
    train_df = self._training_frame(df)
    epoch_df = train_df
    if archive.snapshot_through and not train_df.empty:
      cutoff = to_utc_timestamp(archive.snapshot_through)
      epoch_df = train_df[train_df["timestamp"] > cutoff].copy()

    if df.empty:
      return {
        "n_resolved": 0,
        "rolling_accuracy": self._rolling(df),
        "stats_epoch": read_hourly_stats_epoch(self.cfg),
        "all_time": {
          **combined_public(archive, CategoryAgg(), CategoryAgg(), CategoryAgg()),
          "rolling_accuracy": self._rolling(df),
        },
      }

    correct = self._correct(df)
    probs = pd.to_numeric(df["primary_model_prob"], errors="coerce")
    valid = probs.notna() & df["outcome"].notna()
    brier = float(((probs[valid] - df.loc[valid, "outcome"]) ** 2).mean()) if valid.any() else None

    leans = df[df["primary_signal"].isin(["LEAN YES", "LEAN NO"])]
    yes_rows = leans[leans["primary_signal"] == "LEAN YES"]
    no_rows = leans[leans["primary_signal"] == "LEAN NO"]

    open_agg, _, _, _, _, _, _ = epoch_aggs_from_df(
      epoch_df,
      open_correct_fn=self._correct,
      late_correct_fn=lambda d: pd.Series(dtype=bool),
      flip_correct_fn=lambda d: pd.Series(dtype=bool),
    )
    open_all = merge_category(archive.open, open_agg)
    all_time = {
      "epochs_archived": archive.epochs_archived,
      "snapshot_through": archive.snapshot_through,
      "snapshot_note": archive.snapshot_note,
      "open": open_all.to_public(),
      "rolling_accuracy": self._rolling(df),
    }

    return {
      "n_resolved": int(len(df)),
      "brier_score": brier,
      "accuracy": float(correct.mean()),
      "rolling_accuracy": self._rolling(df),
      "lean_yes_signals": int(len(yes_rows)),
      "lean_yes_accuracy": float(self._correct(yes_rows).mean()) if len(yes_rows) else None,
      "lean_no_signals": int(len(no_rows)),
      "lean_no_accuracy": float(self._correct(no_rows).mean()) if len(no_rows) else None,
      "mean_calibration_error": float(
        (probs[valid] - df.loc[valid, "outcome"]).abs().mean()
      ) if valid.any() else None,
      "stats_epoch": read_hourly_stats_epoch(self.cfg),
      "all_time": all_time,
    }

  def reset_stats(self, *, note: str = "") -> dict[str, Any]:
    df = self.load_resolved()
    archive = read_hourly_archive(self.cfg)
    train_df = self._training_frame(df)
    epoch_df = df_after_snapshot(train_df, archive.snapshot_through)
    if not epoch_df.empty:
      aggs = epoch_aggs_from_df(
        epoch_df,
        open_correct_fn=self._correct,
        late_correct_fn=lambda d: pd.Series(dtype=bool),
        flip_correct_fn=lambda d: pd.Series(dtype=bool),
      )
      archive_hourly_epoch(
        self.cfg, aggs, epoch_df=epoch_df, increment_epochs=True, note=note
      )
    deleted = self.store.clear_all()
    epoch = write_hourly_stats_epoch(self.cfg, note=note) if self.cfg else {}
    return {"deleted_hourly_predictions": deleted, **epoch}

  def snapshot_stats(self, *, note: str = "hourly snapshot") -> dict[str, Any]:
    df = self.load_resolved()
    archive = read_hourly_archive(self.cfg)
    train_df = self._training_frame(df)
    epoch_df = df_after_snapshot(train_df, archive.snapshot_through)
    if epoch_df.empty:
      return {"status": "noop", "snapshot_through": archive.snapshot_through}
    aggs = epoch_aggs_from_df(
      epoch_df,
      open_correct_fn=self._correct,
      late_correct_fn=lambda d: pd.Series(dtype=bool),
      flip_correct_fn=lambda d: pd.Series(dtype=bool),
    )
    archive_hourly_epoch(self.cfg, aggs, epoch_df=epoch_df, note=note)
    return {"status": "ok", "snapshotted": len(epoch_df)}

  def fit_calibrator(self, calibrator) -> bool:
    df = self.load_resolved()
    if df.empty or len(df) < int(self.cfg.get("hourly", {}).get("calibration_min_resolved", 30)):
      return False
    return calibrator.fit(df["primary_model_prob"], df["outcome"])
