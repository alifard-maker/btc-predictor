from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.calibration.epoch import read_stats_epoch, write_stats_epoch
from src.calibration.sources import is_kalshi_consistent
from src.calibration.stats_archive import (
  CategoryAgg,
  archive_epoch,
  category_from_signal_df,
  combined_public,
  epoch_aggs_from_df,
  read_archive,
  rolling_from_events,
)
from src.db.store import PredictionResolution, PredictionStore, create_prediction_store
from src.features.slots import floor_to_15m


class CalibrationTracker:
  """Track whether predicted probabilities match actual outcomes."""

  def __init__(self, cfg: dict[str, Any] | str):
    if isinstance(cfg, dict):
      self.cfg = cfg
      self.store: PredictionStore = create_prediction_store(cfg)
      self.tz = cfg.get("timezone", "America/New_York")
      self.kalshi_only = bool(cfg.get("calibration", {}).get("kalshi_only", True))
    else:
      from src.db.store import SqlitePredictionStore
      self.cfg = {}
      self.store = SqlitePredictionStore(cfg)
      self.store.init()
      self.tz = "America/New_York"
      self.kalshi_only = True

  def log_prediction(
    self,
    timestamp: str,
    price: float,
    prob_up: float,
    prob_down: float,
    confidence: float,
    signal: str,
    expected_move: float,
    *,
    reference_source: str = "",
    kalshi_market_ticker: str = "",
  ) -> int:
    return self.store.log_prediction(
      timestamp,
      price,
      prob_up,
      prob_down,
      confidence,
      signal,
      expected_move,
      reference_source=reference_source,
      kalshi_market_ticker=kalshi_market_ticker,
    )

  def get_pending(self) -> list[tuple[int, str, float]]:
    return self.store.get_pending()

  def resolve_with_prices(
    self,
    price_lookup: dict[str, PredictionResolution],
    *,
    force: bool = False,
  ) -> int:
    return self.store.resolve_with_prices(price_lookup, force=force)

  def load_all(self) -> pd.DataFrame:
    return self.store.load_all()

  def load_resolved(self) -> pd.DataFrame:
    df = self.store.load_resolved()
    return self._filter_calibration_df(df)

  def load_recent(self, limit: int = 50) -> pd.DataFrame:
    return self.store.load_recent(limit)

  def latest(self) -> dict[str, Any] | None:
    return self.store.latest()

  def reset_stats(self, *, note: str = "") -> dict[str, Any]:
    """Archive current epoch aggregates, then clear predictions for a fresh epoch."""
    archived: dict[str, Any] = {}
    if self.cfg:
      df = self._dedupe_by_slot(self.load_resolved())
      aggs = epoch_aggs_from_df(
        df,
        open_correct_fn=self._prediction_correct,
        late_correct_fn=self._late_entry_correct,
        flip_correct_fn=self._flip_correct,
      )
      archive = archive_epoch(self.cfg, aggs)
      archived = {
        "epochs_archived": archive.epochs_archived,
        "archived_open_n": aggs[0].n_resolved,
        "archived_late_n": aggs[1].n_resolved,
      }
    deleted = self.store.clear_all()
    epoch = write_stats_epoch(self.cfg, note=note) if self.cfg else {}
    return {"deleted_predictions": deleted, **archived, **epoch}

  def record_late_entry(
    self,
    slot_timestamp: str,
    signal: str,
    prob_up: float,
    seconds_remaining: int,
  ) -> bool:
    return self.store.record_late_entry(slot_timestamp, signal, prob_up, seconds_remaining)

  def record_flip(
    self,
    slot_timestamp: str,
    signal: str,
    prob_up: float,
    seconds_remaining: int,
  ) -> bool:
    return self.store.record_flip(slot_timestamp, signal, prob_up, seconds_remaining)

  def stats_epoch(self) -> dict[str, Any] | None:
    if not self.cfg:
      return None
    return read_stats_epoch(self.cfg)

  def _filter_calibration_df(self, df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or not self.kalshi_only:
      return df
    if "reference_source" not in df.columns or "exit_source" not in df.columns:
      return df.iloc[0:0]
    mask = df.apply(
      lambda r: is_kalshi_consistent(r.get("reference_source"), r.get("exit_source")),
      axis=1,
    )
    return df[mask].copy()

  def _dedupe_by_slot(self, df: pd.DataFrame) -> pd.DataFrame:
    """One row per 15m slot — keep earliest log (closest to slot open)."""
    if df.empty:
      return df
    out = df.copy()
    out["_slot"] = pd.to_datetime(out["timestamp"], utc=True).apply(
      lambda t: floor_to_15m(t, self.tz)
    )
    sort_col = None
    for col in ("created_at", "resolved_at", "id"):
      if col in out.columns and out[col].notna().any():
        sort_col = col
        break
    if sort_col:
      if sort_col == "id":
        out = out.sort_values(sort_col)
      else:
        out["_sort"] = pd.to_datetime(out[sort_col], utc=True)
        out = out.sort_values("_sort")
    return out.drop_duplicates(subset=["_slot"], keep="first").drop(
      columns=["_slot", "_sort"], errors="ignore"
    )

  def _prediction_correct(self, df: pd.DataFrame) -> pd.Series:
    return (df["prob_up"] >= 0.5) == df["outcome"]

  def _late_entry_correct(self, df: pd.DataFrame) -> pd.Series:
    def _row(r: pd.Series) -> bool:
      sig = str(r.get("late_entry_signal") or "")
      if sig == "LATE LONG":
        return bool(r["outcome"])
      if sig == "LATE SHORT":
        return not bool(r["outcome"])
      return False

    return df.apply(_row, axis=1)

  def _late_entry_mask(self, df: pd.DataFrame) -> pd.Series:
    if "late_entry_signal" not in df.columns:
      return pd.Series(False, index=df.index)
    return df["late_entry_signal"].notna() & (df["late_entry_signal"].astype(str).str.len() > 0)

  def _flip_correct(self, df: pd.DataFrame) -> pd.Series:
    def _row(r: pd.Series) -> bool:
      sig = str(r.get("flip_signal") or "")
      if sig == "FLIP LONG":
        return bool(r["outcome"])
      if sig == "FLIP SHORT":
        return not bool(r["outcome"])
      return False

    return df.apply(_row, axis=1)

  def _flip_mask(self, df: pd.DataFrame) -> pd.Series:
    if "flip_signal" not in df.columns:
      return pd.Series(False, index=df.index)
    return df["flip_signal"].notna() & (df["flip_signal"].astype(str).str.len() > 0)

  def _flip_summary(self, df: pd.DataFrame) -> dict[str, Any]:
    empty = {
      "n_logged": 0,
      "n_resolved": 0,
      "accuracy": None,
      "brier_score": None,
      "flip_long_signals": 0,
      "flip_long_accuracy": None,
      "flip_short_signals": 0,
      "flip_short_accuracy": None,
      "avg_seconds_remaining": None,
    }
    if df.empty or "flip_signal" not in df.columns:
      return empty

    flipped = df[self._flip_mask(df)]
    if flipped.empty:
      return empty

    resolved = flipped[flipped["outcome"].notna()]
    out = {
      "n_logged": int(len(flipped)),
      "n_resolved": int(len(resolved)),
      "accuracy": None,
      "brier_score": None,
      "flip_long_signals": int((flipped["flip_signal"] == "FLIP LONG").sum()),
      "flip_long_accuracy": None,
      "flip_short_signals": int((flipped["flip_signal"] == "FLIP SHORT").sum()),
      "flip_short_accuracy": None,
      "avg_seconds_remaining": None,
    }
    if "flip_seconds_remaining" in flipped.columns:
      rem = pd.to_numeric(flipped["flip_seconds_remaining"], errors="coerce").dropna()
      if len(rem):
        out["avg_seconds_remaining"] = float(rem.mean())
    if len(resolved) == 0:
      return out

    correct = self._flip_correct(resolved)
    out["accuracy"] = float(correct.mean())
    if "flip_prob_up" in resolved.columns:
      probs = pd.to_numeric(resolved["flip_prob_up"], errors="coerce")
      valid = probs.notna()
      if valid.any():
        out["brier_score"] = float(((probs[valid] - resolved.loc[valid, "outcome"]) ** 2).mean())

    longs = resolved[resolved["flip_signal"] == "FLIP LONG"]
    shorts = resolved[resolved["flip_signal"] == "FLIP SHORT"]
    if len(longs):
      out["flip_long_accuracy"] = float(self._flip_correct(longs).mean())
    if len(shorts):
      out["flip_short_accuracy"] = float(self._flip_correct(shorts).mean())
    return out

  def _late_entry_summary(self, df: pd.DataFrame) -> dict[str, Any]:
    empty = {
      "n_logged": 0,
      "n_resolved": 0,
      "accuracy": None,
      "brier_score": None,
      "late_long_signals": 0,
      "late_long_accuracy": None,
      "late_short_signals": 0,
      "late_short_accuracy": None,
      "avg_seconds_remaining": None,
    }
    if df.empty or "late_entry_signal" not in df.columns:
      return empty

    late = df[self._late_entry_mask(df)]
    if late.empty:
      return empty

    resolved = late[late["outcome"].notna()]
    out = {
      "n_logged": int(len(late)),
      "n_resolved": int(len(resolved)),
      "accuracy": None,
      "brier_score": None,
      "rolling_accuracy": None,
      "late_long_signals": int((late["late_entry_signal"] == "LATE LONG").sum()),
      "late_long_accuracy": None,
      "late_short_signals": int((late["late_entry_signal"] == "LATE SHORT").sum()),
      "late_short_accuracy": None,
      "avg_seconds_remaining": None,
    }
    if "late_entry_seconds_remaining" in late.columns:
      rem = pd.to_numeric(late["late_entry_seconds_remaining"], errors="coerce").dropna()
      if len(rem):
        out["avg_seconds_remaining"] = float(rem.mean())

    if resolved.empty:
      return out

    correct = self._late_entry_correct(resolved)
    out["accuracy"] = float(correct.mean())
    if "late_entry_prob_up" in resolved.columns:
      probs = pd.to_numeric(resolved["late_entry_prob_up"], errors="coerce")
      valid = probs.notna()
      if valid.any():
        out["brier_score"] = float(((probs[valid] - resolved.loc[valid, "outcome"]) ** 2).mean())

    longs = resolved[resolved["late_entry_signal"] == "LATE LONG"]
    shorts = resolved[resolved["late_entry_signal"] == "LATE SHORT"]
    if len(longs):
      out["late_long_accuracy"] = float(self._late_entry_correct(longs).mean())
    if len(shorts):
      out["late_short_accuracy"] = float(self._late_entry_correct(shorts).mean())
    out["rolling_accuracy"] = self._late_rolling_accuracy(df)
    return out

  def _rolling_open_accuracy(
    self,
    df: pd.DataFrame,
    windows_hours: list[int] | None = None,
  ) -> dict[str, dict[str, Any]]:
    windows_hours = windows_hours or [1, 2, 4, 12]
    empty = {f"{h}h": {"correct": 0, "total": 0, "accuracy": None} for h in windows_hours}
    if df.empty:
      return empty

    now = pd.Timestamp.now(tz="UTC")
    df = df.copy()
    df["_slot"] = pd.to_datetime(df["timestamp"], utc=True).apply(
      lambda t: floor_to_15m(t, self.tz)
    )
    correct = self._prediction_correct(df)

    out: dict[str, dict[str, Any]] = {}
    for h in windows_hours:
      mask = df["_slot"] >= (now - pd.Timedelta(hours=h))
      n = int(mask.sum())
      if n == 0:
        out[f"{h}h"] = {"correct": 0, "total": 0, "accuracy": None}
      else:
        c = int(correct[mask].sum())
        out[f"{h}h"] = {"correct": c, "total": n, "accuracy": float(c / n)}
    return out

  def rolling_accuracy(self, windows_hours: list[int] | None = None) -> dict[str, dict[str, Any]]:
    """Correct/total counts for resolved open predictions in each rolling time window."""
    return self._rolling_open_accuracy(self._dedupe_by_slot(self.load_resolved()), windows_hours)

  def _late_rolling_accuracy(
    self,
    df: pd.DataFrame,
    *,
    archive_events: list[dict[str, Any]] | None = None,
    windows_hours: list[int] | None = None,
  ) -> dict[str, dict[str, Any]]:
    windows_hours = windows_hours or [1, 2, 4, 12]
    _, late_events = category_from_signal_df(
      df,
      signal_col="late_entry_signal",
      prob_col="late_entry_prob_up",
      correct_fn=self._late_entry_correct,
      long_label="LATE LONG",
      short_label="LATE SHORT",
    )
    events = list(archive_events or []) + late_events
    return rolling_from_events(events, windows_hours=windows_hours)

  def _epoch_aggs(self, df: pd.DataFrame) -> tuple[CategoryAgg, CategoryAgg, CategoryAgg]:
    open_agg, late_agg, flip_agg, _, _ = epoch_aggs_from_df(
      df,
      open_correct_fn=self._prediction_correct,
      late_correct_fn=self._late_entry_correct,
      flip_correct_fn=self._flip_correct,
    )
    return open_agg, late_agg, flip_agg

  def _all_time_summary(self, df: pd.DataFrame) -> dict[str, Any]:
    if not self.cfg:
      return {}
    archive = read_archive(self.cfg)
    epoch_open, epoch_late, epoch_flip = self._epoch_aggs(df)
    combined = combined_public(archive, epoch_open, epoch_late, epoch_flip)
    combined["late_entry"]["rolling_accuracy"] = self._late_rolling_accuracy(
      df,
      archive_events=archive.late_events,
    )
    combined["open"]["rolling_accuracy"] = self._rolling_open_accuracy(df)
    return combined

  def calibration_report(self, n_bins: int = 10) -> pd.DataFrame:
    df = self._dedupe_by_slot(self.load_resolved())
    if df.empty or len(df) < n_bins:
      return pd.DataFrame()

    df["bin"] = pd.cut(df["prob_up"], bins=n_bins, labels=False)
    report = df.groupby("bin", observed=True).agg(
      count=("outcome", "count"),
      mean_predicted=("prob_up", "mean"),
      mean_actual=("outcome", "mean"),
      accuracy=("outcome", lambda x: ((df.loc[x.index, "prob_up"] >= 0.5) == x).mean()),
    ).reset_index()

    report["calibration_error"] = (report["mean_predicted"] - report["mean_actual"]).abs()
    return report

  def summary(self) -> dict[str, Any]:
    df = self._dedupe_by_slot(self.load_resolved())
    all_resolved = self._dedupe_by_slot(self.store.load_resolved())
    kalshi_n = len(df)
    total_n = len(all_resolved)
    if df.empty:
      out = {
        "n_resolved": 0,
        "rolling_accuracy": self.rolling_accuracy(),
        "stats_epoch": self.stats_epoch(),
        "late_entry": self._late_entry_summary(df),
        "flip": self._flip_summary(df),
        "open_at_slot": {"n_resolved": 0, "long_signals": 0, "short_signals": 0},
        "all_time": self._all_time_summary(df),
      }
      if self.kalshi_only and total_n > kalshi_n:
        out["n_resolved_total"] = total_n
        out["n_excluded_non_kalshi"] = total_n - kalshi_n
      return out

    brier = ((df["prob_up"] - df["outcome"]) ** 2).mean()
    open_df = df[df["signal"].isin(["LONG", "SHORT"])]
    longs = open_df[open_df["signal"] == "LONG"]
    shorts = open_df[open_df["signal"] == "SHORT"]
    cal = self.calibration_report()

    return {
      "n_resolved": len(df),
      "n_resolved_total": total_n if self.kalshi_only else len(df),
      "n_excluded_non_kalshi": (total_n - kalshi_n) if self.kalshi_only else 0,
      "kalshi_only": self.kalshi_only,
      "stats_epoch": self.stats_epoch(),
      "brier_score": float(brier),
      "overall_accuracy": float(self._prediction_correct(df).mean()),
      "rolling_accuracy": self.rolling_accuracy(),
      "open_at_slot": {
        "n_resolved": int(len(open_df)),
        "long_signals": len(longs),
        "long_accuracy": float(longs["outcome"].mean()) if len(longs) else None,
        "short_signals": len(shorts),
        "short_accuracy": float(1 - shorts["outcome"].mean()) if len(shorts) else None,
        "brier_score": float(((open_df["prob_up"] - open_df["outcome"]) ** 2).mean()) if len(open_df) else None,
      },
      "late_entry": self._late_entry_summary(df),
      "flip": self._flip_summary(df),
      "long_signals": len(longs),
      "long_accuracy": float(longs["outcome"].mean()) if len(longs) else None,
      "short_signals": len(shorts),
      "short_accuracy": float(1 - shorts["outcome"].mean()) if len(shorts) else None,
      "mean_calibration_error": float(cal["calibration_error"].mean()) if not cal.empty else None,
      "all_time": self._all_time_summary(df),
    }
