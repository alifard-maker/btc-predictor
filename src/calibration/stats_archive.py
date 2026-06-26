"""Persistent all-time calibration aggregates — survives epoch resets."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd


def to_utc_timestamp(val: Any) -> pd.Timestamp:
  ts = pd.Timestamp(val)
  if ts.tzinfo is None:
    return ts.tz_localize("UTC")
  return ts.tz_convert("UTC")


@dataclass
class CategoryAgg:
  n_resolved: int = 0
  n_correct: int = 0
  brier_sum_sq: float = 0.0
  long_signals: int = 0
  long_correct: int = 0
  short_signals: int = 0
  short_correct: int = 0
  n_logged: int = 0

  def to_public(self) -> dict[str, Any]:
    out: dict[str, Any] = {
      "n_resolved": self.n_resolved,
      "n_logged": self.n_logged,
      "accuracy": (self.n_correct / self.n_resolved) if self.n_resolved else None,
      "brier_score": (self.brier_sum_sq / self.n_resolved) if self.n_resolved else None,
    }
    if self.long_signals:
      out["long_signals"] = self.long_signals
      out["long_accuracy"] = (self.long_correct / self.long_signals) if self.long_signals else None
    if self.short_signals:
      out["short_signals"] = self.short_signals
      out["short_accuracy"] = (self.short_correct / self.short_signals) if self.short_signals else None
    return out

  @classmethod
  def from_dict(cls, raw: dict[str, Any] | None) -> CategoryAgg:
    if not raw:
      return cls()
    return cls(
      n_resolved=int(raw.get("n_resolved") or 0),
      n_correct=int(raw.get("n_correct") or 0),
      brier_sum_sq=float(raw.get("brier_sum_sq") or 0.0),
      long_signals=int(raw.get("long_signals") or 0),
      long_correct=int(raw.get("long_correct") or 0),
      short_signals=int(raw.get("short_signals") or 0),
      short_correct=int(raw.get("short_correct") or 0),
      n_logged=int(raw.get("n_logged") or 0),
    )


@dataclass
class StatsArchive:
  version: int = 1
  epochs_archived: int = 0
  last_archived_at: str | None = None
  snapshot_through: str | None = None
  snapshot_note: str | None = None
  open: CategoryAgg = field(default_factory=CategoryAgg)
  late: CategoryAgg = field(default_factory=CategoryAgg)
  flip: CategoryAgg = field(default_factory=CategoryAgg)
  second_chance: CategoryAgg = field(default_factory=CategoryAgg)
  late_events: list[dict[str, Any]] = field(default_factory=list)
  flip_events: list[dict[str, Any]] = field(default_factory=list)
  second_chance_events: list[dict[str, Any]] = field(default_factory=list)

  @classmethod
  def from_dict(cls, raw: dict[str, Any] | None) -> StatsArchive:
    if not raw:
      return cls()
    return cls(
      version=int(raw.get("version") or 1),
      epochs_archived=int(raw.get("epochs_archived") or 0),
      last_archived_at=raw.get("last_archived_at"),
      snapshot_through=raw.get("snapshot_through"),
      snapshot_note=raw.get("snapshot_note"),
      open=CategoryAgg.from_dict(raw.get("open")),
      late=CategoryAgg.from_dict(raw.get("late")),
      flip=CategoryAgg.from_dict(raw.get("flip")),
      second_chance=CategoryAgg.from_dict(raw.get("second_chance")),
      late_events=list(raw.get("late_events") or []),
      flip_events=list(raw.get("flip_events") or []),
      second_chance_events=list(raw.get("second_chance_events") or []),
    )

  def to_dict(self) -> dict[str, Any]:
    return {
      "version": self.version,
      "epochs_archived": self.epochs_archived,
      "last_archived_at": self.last_archived_at,
      "snapshot_through": self.snapshot_through,
      "snapshot_note": self.snapshot_note,
      "open": asdict(self.open),
      "late": asdict(self.late),
      "flip": asdict(self.flip),
      "second_chance": asdict(self.second_chance),
      "late_events": self.late_events,
      "flip_events": self.flip_events,
      "second_chance_events": self.second_chance_events,
    }


def archive_path(cfg: dict[str, Any]) -> Path:
  return Path(cfg["paths"]["logs"]) / "stats_archive.json"


def read_archive(cfg: dict[str, Any]) -> StatsArchive:
  path = archive_path(cfg)
  if not path.exists():
    return StatsArchive()
  try:
    return StatsArchive.from_dict(json.loads(path.read_text()))
  except Exception:
    return StatsArchive()


def df_after_snapshot(df: pd.DataFrame, snapshot_through: str | None) -> pd.DataFrame:
  """Rows not yet folded into the persistent archive snapshot."""
  if df.empty or not snapshot_through:
    return df
  cutoff = to_utc_timestamp(snapshot_through)
  ts = pd.to_datetime(df["timestamp"], utc=True)
  return df[ts > cutoff].copy()


def latest_slot_iso(df: pd.DataFrame) -> str | None:
  if df.empty:
    return None
  return pd.to_datetime(df["timestamp"], utc=True).max().isoformat()


def merge_epoch_into_archive(
  cfg: dict[str, Any],
  epoch_aggs: tuple,
  *,
  epoch_df: pd.DataFrame,
  increment_epochs: bool = False,
  note: str = "",
) -> StatsArchive:
  """Merge slot aggregates into the archive file (snapshot or pre-reset)."""
  open_agg, late_agg, flip_agg, sc_agg, late_events, flip_events, sc_events = epoch_aggs
  archive = read_archive(cfg)
  archive.open = merge_category(archive.open, open_agg)
  archive.late = merge_category(archive.late, late_agg)
  archive.flip = merge_category(archive.flip, flip_agg)
  archive.second_chance = merge_category(archive.second_chance, sc_agg)
  archive.late_events.extend(late_events)
  archive.flip_events.extend(flip_events)
  archive.second_chance_events.extend(sc_events)
  if increment_epochs:
    archive.epochs_archived += 1
  archive.last_archived_at = datetime.now(timezone.utc).isoformat()
  slot_end = latest_slot_iso(epoch_df)
  if slot_end:
    prev = to_utc_timestamp(archive.snapshot_through) if archive.snapshot_through else None
    nxt = to_utc_timestamp(slot_end)
    archive.snapshot_through = (max(prev, nxt) if prev is not None else nxt).isoformat()
  if note:
    archive.snapshot_note = note
  write_archive(cfg, archive)
  return archive


def archive_epoch(cfg: dict[str, Any], epoch_aggs: tuple, *, epoch_df: pd.DataFrame) -> StatsArchive:
  """Merge current epoch aggregates into archive before a reset."""
  return merge_epoch_into_archive(cfg, epoch_aggs, epoch_df=epoch_df, increment_epochs=True)


def write_archive(cfg: dict[str, Any], archive: StatsArchive) -> None:
  path = archive_path(cfg)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(archive.to_dict(), indent=2) + "\n")


def merge_category(a: CategoryAgg, b: CategoryAgg) -> CategoryAgg:
  return CategoryAgg(
    n_resolved=a.n_resolved + b.n_resolved,
    n_correct=a.n_correct + b.n_correct,
    brier_sum_sq=a.brier_sum_sq + b.brier_sum_sq,
    long_signals=a.long_signals + b.long_signals,
    long_correct=a.long_correct + b.long_correct,
    short_signals=a.short_signals + b.short_signals,
    short_correct=a.short_correct + b.short_correct,
    n_logged=a.n_logged + b.n_logged,
  )


def category_from_open_df(df: pd.DataFrame, *, correct_fn: Callable[[pd.DataFrame], pd.Series]) -> CategoryAgg:
  if df.empty:
    return CategoryAgg()
  correct = correct_fn(df)
  probs = pd.to_numeric(df["prob_up"], errors="coerce")
  valid = probs.notna()
  brier_sum = float(((probs[valid] - df.loc[valid, "outcome"]) ** 2).sum()) if valid.any() else 0.0
  open_df = df[df["signal"].isin(["LONG", "SHORT"])] if "signal" in df.columns else df.iloc[0:0]
  longs = open_df[open_df["signal"] == "LONG"] if not open_df.empty else open_df
  shorts = open_df[open_df["signal"] == "SHORT"] if not open_df.empty else open_df
  long_correct = int(longs["outcome"].sum()) if len(longs) else 0
  short_correct = int((1 - shorts["outcome"]).sum()) if len(shorts) else 0
  return CategoryAgg(
    n_resolved=int(len(df)),
    n_correct=int(correct.sum()),
    brier_sum_sq=brier_sum,
    long_signals=int(len(longs)),
    long_correct=long_correct,
    short_signals=int(len(shorts)),
    short_correct=short_correct,
  )


def category_from_signal_df(
  df: pd.DataFrame,
  *,
  signal_col: str,
  prob_col: str,
  correct_fn: Callable[[pd.DataFrame], pd.Series],
  long_label: str,
  short_label: str,
) -> tuple[CategoryAgg, list[dict[str, Any]]]:
  if df.empty or signal_col not in df.columns:
    return CategoryAgg(), []
  mask = df[signal_col].notna() & (df[signal_col].astype(str).str.len() > 0)
  rows = df[mask]
  if rows.empty:
    return CategoryAgg(), []

  resolved = rows[rows["outcome"].notna()]
  events: list[dict[str, Any]] = []
  for _, r in resolved.iterrows():
    sig = str(r.get(signal_col) or "")
    prob = r.get(prob_col)
    prob_f = float(prob) if prob is not None and prob == prob else None
    outcome = int(r["outcome"])
    ok = bool(correct_fn(pd.DataFrame([r])).iloc[0])
    sec = r.get(
      {
        "late_entry_signal": "late_entry_seconds_remaining",
        "flip_signal": "flip_seconds_remaining",
        "second_chance_signal": "second_chance_seconds_remaining",
      }.get(signal_col)
    )
    events.append({
      "slot": to_utc_timestamp(r["timestamp"]).isoformat(),
      "signal": sig,
      "prob_up": prob_f,
      "outcome": outcome,
      "correct": ok,
      "seconds_remaining": int(sec) if sec is not None and sec == sec else None,
    })

  correct = correct_fn(resolved)
  probs = pd.to_numeric(resolved[prob_col], errors="coerce") if prob_col in resolved.columns else pd.Series(dtype=float)
  valid = probs.notna()
  brier_sum = float(((probs[valid] - resolved.loc[valid, "outcome"]) ** 2).sum()) if valid.any() else 0.0
  longs = resolved[resolved[signal_col] == long_label]
  shorts = resolved[resolved[signal_col] == short_label]
  long_correct = int(correct_fn(longs).sum()) if len(longs) else 0
  short_correct = int(correct_fn(shorts).sum()) if len(shorts) else 0
  return CategoryAgg(
    n_resolved=int(len(resolved)),
    n_correct=int(correct.sum()),
    brier_sum_sq=brier_sum,
    n_logged=int(len(rows)),
    long_signals=int(len(longs)),
    long_correct=long_correct,
    short_signals=int(len(shorts)),
    short_correct=short_correct,
  ), events


def epoch_aggs_from_df(
  df: pd.DataFrame,
  *,
  open_correct_fn: Callable[[pd.DataFrame], pd.Series],
  late_correct_fn: Callable[[pd.DataFrame], pd.Series],
  flip_correct_fn: Callable[[pd.DataFrame], pd.Series],
  second_chance_correct_fn: Callable[[pd.DataFrame], pd.Series] | None = None,
) -> tuple[CategoryAgg, CategoryAgg, CategoryAgg, CategoryAgg, list, list, list]:
  open_agg = category_from_open_df(df, correct_fn=open_correct_fn)
  late_agg, late_events = category_from_signal_df(
    df,
    signal_col="late_entry_signal",
    prob_col="late_entry_prob_up",
    correct_fn=late_correct_fn,
    long_label="LATE LONG",
    short_label="LATE SHORT",
  )
  flip_agg, flip_events = category_from_signal_df(
    df,
    signal_col="flip_signal",
    prob_col="flip_prob_up",
    correct_fn=flip_correct_fn,
    long_label="FLIP LONG",
    short_label="FLIP SHORT",
  )
  sc_fn = second_chance_correct_fn or (lambda d: pd.Series(dtype=bool))
  sc_agg, sc_events = category_from_signal_df(
    df,
    signal_col="second_chance_signal",
    prob_col="second_chance_prob_up",
    correct_fn=sc_fn,
    long_label="2ND LONG",
    short_label="2ND SHORT",
  )
  return open_agg, late_agg, flip_agg, sc_agg, late_events, flip_events, sc_events


def snapshot_epoch(
  cfg: dict[str, Any],
  epoch_aggs: tuple,
  *,
  epoch_df: pd.DataFrame,
  note: str = "",
) -> StatsArchive:
  """Fold current DB epoch into archive without clearing predictions."""
  return merge_epoch_into_archive(cfg, epoch_aggs, epoch_df=epoch_df, increment_epochs=False, note=note)


def archive_epoch(cfg: dict[str, Any], epoch_aggs: tuple, *, epoch_df: pd.DataFrame) -> StatsArchive:
  """Merge current epoch aggregates into archive before a reset."""
  return merge_epoch_into_archive(cfg, epoch_aggs, epoch_df=epoch_df, increment_epochs=True)


def combined_public(
  archive: StatsArchive,
  epoch_open: CategoryAgg,
  epoch_late: CategoryAgg,
  epoch_flip: CategoryAgg,
  epoch_second_chance: CategoryAgg | None = None,
) -> dict[str, Any]:
  open_all = merge_category(archive.open, epoch_open)
  late_all = merge_category(archive.late, epoch_late)
  flip_all = merge_category(archive.flip, epoch_flip)
  sc_all = merge_category(archive.second_chance, epoch_second_chance or CategoryAgg())

  def _signal_public(agg: CategoryAgg, *, long_key: str, short_key: str) -> dict[str, Any]:
    base = agg.to_public()
    if agg.long_signals:
      base[long_key] = agg.long_signals
      base[f"{long_key.replace('_signals', '_accuracy')}"] = (
        (agg.long_correct / agg.long_signals) if agg.long_signals else None
      )
    if agg.short_signals:
      base[short_key] = agg.short_signals
      base[f"{short_key.replace('_signals', '_accuracy')}"] = (
        (agg.short_correct / agg.short_signals) if agg.short_signals else None
      )
    return base

  return {
    "epochs_archived": archive.epochs_archived,
    "last_archived_at": archive.last_archived_at,
    "open": open_all.to_public(),
    "late_entry": _signal_public(late_all, long_key="late_long_signals", short_key="late_short_signals"),
    "flip": _signal_public(flip_all, long_key="flip_long_signals", short_key="flip_short_signals"),
    "second_chance": _signal_public(sc_all, long_key="sc_long_signals", short_key="sc_short_signals"),
  }


def rolling_from_events(
  events: list[dict[str, Any]],
  *,
  windows_hours: list[int] | None = None,
) -> dict[str, dict[str, Any]]:
  windows_hours = windows_hours or [1, 2, 4, 12]
  empty = {f"{h}h": {"correct": 0, "total": 0, "accuracy": None} for h in windows_hours}
  if not events:
    return empty

  df = pd.DataFrame(events)
  df["_slot"] = pd.to_datetime(df["slot"], utc=True)
  now = pd.Timestamp.now(tz="UTC")
  out: dict[str, dict[str, Any]] = {}
  for h in windows_hours:
    mask = df["_slot"] >= (now - pd.Timedelta(hours=h))
    n = int(mask.sum())
    if n == 0:
      out[f"{h}h"] = {"correct": 0, "total": 0, "accuracy": None}
    else:
      c = int(df.loc[mask, "correct"].sum())
      out[f"{h}h"] = {"correct": c, "total": n, "accuracy": float(c / n)}
  return out
