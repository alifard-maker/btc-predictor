#!/usr/bin/env python3
"""Walk-forward ML validation with feature stability and overfit detection."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import click
import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table
from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score

from src.backtest.walk_forward import WalkForwardConfig, generate_folds
from src.config import load_config
from src.data.storage import CandleStorage
from src.models.trainer import ModelTrainer, _make_model

console = Console()


def _feature_importance_stability(importances: list[dict[str, float]], top_k: int = 10) -> dict:
  if not importances:
    return {"top_k_overlap_mean": 0.0, "rank_correlation_mean": 0.0}
  names = sorted({k for imp in importances for k in imp})
  if not names:
    return {"top_k_overlap_mean": 0.0, "rank_correlation_mean": 0.0}

  ranks_per_fold: list[dict[str, int]] = []
  for imp in importances:
    sorted_feats = sorted(imp.items(), key=lambda x: -x[1])
    ranks = {feat: rank for rank, (feat, _) in enumerate(sorted_feats)}
    ranks_per_fold.append(ranks)

  overlaps = []
  rank_corrs = []
  for i in range(len(ranks_per_fold)):
    for j in range(i + 1, len(ranks_per_fold)):
      top_i = set(sorted(ranks_per_fold[i], key=lambda f: ranks_per_fold[i][f])[:top_k])
      top_j = set(sorted(ranks_per_fold[j], key=lambda f: ranks_per_fold[j][f])[:top_k])
      overlaps.append(len(top_i & top_j) / top_k)
      vec_i = [ranks_per_fold[i].get(f, len(names)) for f in names]
      vec_j = [ranks_per_fold[j].get(f, len(names)) for f in names]
      if np.std(vec_i) > 0 and np.std(vec_j) > 0:
        rank_corrs.append(float(np.corrcoef(vec_i, vec_j)[0, 1]))

  return {
    "top_k_overlap_mean": round(float(np.mean(overlaps)) if overlaps else 0.0, 4),
    "rank_correlation_mean": round(float(np.mean(rank_corrs)) if rank_corrs else 0.0, 4),
    "top_k": top_k,
  }


def run_walk_forward_ml_validation(
  cfg: dict,
  df_15m: pd.DataFrame,
  df_1m: pd.DataFrame | None,
  wf_cfg: WalkForwardConfig,
) -> dict:
  trainer = ModelTrainer(cfg)
  X, y = trainer.prepare_training_data(df_15m, df_1m)
  cols = trainer.feature_names
  clean = pd.concat([X, y.rename("label")], axis=1).dropna()
  X = clean[cols]
  y = clean["label"]

  min_needed = wf_cfg.train_window + wf_cfg.test_window
  if len(clean) < min_needed:
    raise ValueError(f"Need at least {min_needed} samples, got {len(clean)}")

  fold_results = []
  importances: list[dict[str, float]] = []
  model_type = cfg.get("model", {}).get("type", "lightgbm")

  for fold_i, (train_start, train_end, test_end) in enumerate(
    generate_folds(len(clean), wf_cfg.train_window, wf_cfg.test_window, wf_cfg.step)
  ):
    X_train = X.iloc[train_start:train_end]
    y_train = y.iloc[train_start:train_end]
    X_test = X.iloc[train_end:test_end]
    y_test = y.iloc[train_end:test_end]

    model = _make_model(model_type)
    model.fit(X_train, y_train)

    train_proba = model.predict_proba(X_train)[:, 1]
    test_proba = model.predict_proba(X_test)[:, 1]

    is_auc = float(roc_auc_score(y_train, train_proba)) if y_train.nunique() > 1 else 0.5
    oos_auc = float(roc_auc_score(y_test, test_proba)) if y_test.nunique() > 1 else 0.5
    is_acc = float(accuracy_score(y_train, (train_proba >= 0.5).astype(int)))
    oos_acc = float(accuracy_score(y_test, (test_proba >= 0.5).astype(int)))
    oos_brier = float(brier_score_loss(y_test, test_proba))

    auc_gap = is_auc - oos_auc
    acc_gap = is_acc - oos_acc
    overfit_flag = auc_gap > 0.08 or acc_gap > 0.10

    if hasattr(model, "feature_importances_"):
      imp = dict(zip(cols, model.feature_importances_.tolist()))
      importances.append(imp)

    fold_results.append({
      "fold": fold_i,
      "n_train": len(X_train),
      "n_test": len(X_test),
      "is_auc": round(is_auc, 4),
      "oos_auc": round(oos_auc, 4),
      "is_accuracy": round(is_acc, 4),
      "oos_accuracy": round(oos_acc, 4),
      "oos_brier": round(oos_brier, 4),
      "auc_gap": round(auc_gap, 4),
      "acc_gap": round(acc_gap, 4),
      "overfit_flag": overfit_flag,
    })

  stability = _feature_importance_stability(importances)
  mean_oos_auc = float(np.mean([f["oos_auc"] for f in fold_results]))
  mean_is_auc = float(np.mean([f["is_auc"] for f in fold_results]))
  mean_gap = float(np.mean([f["auc_gap"] for f in fold_results]))
  n_overfit = sum(1 for f in fold_results if f["overfit_flag"])

  daily_retrain_warning = None
  if mean_gap > 0.06:
    daily_retrain_warning = (
      f"In-sample AUC exceeds OOS by {mean_gap:.3f} on average — "
      "daily full retrain likely overfits; prefer walk-forward refits."
    )

  return {
    "folds": fold_results,
    "summary": {
      "n_folds": len(fold_results),
      "mean_is_auc": round(mean_is_auc, 4),
      "mean_oos_auc": round(mean_oos_auc, 4),
      "mean_auc_gap": round(mean_gap, 4),
      "folds_overfit_flagged": n_overfit,
      "feature_stability": stability,
      "daily_retrain_warning": daily_retrain_warning,
    },
  }


@click.command()
@click.option("--train-window", default=None, type=int)
@click.option("--test-window", default=None, type=int)
@click.option("--step", default=None, type=int)
@click.option("--output", default=None, help="JSON output path")
def main(train_window: int | None, test_window: int | None, step: int | None, output: str | None) -> None:
  cfg = load_config()
  wf_cfg = WalkForwardConfig.from_config(cfg)
  if train_window:
    wf_cfg.train_window = train_window
  if test_window:
    wf_cfg.test_window = test_window
  if step:
    wf_cfg.step = step

  storage = CandleStorage(cfg)
  df_15m = storage.load("15m")
  df_1m = storage.load("1m")
  if df_15m.empty:
    console.print("[red]No 15m data. Run collect_historical.py first.[/red]")
    sys.exit(1)

  console.print(f"ML walk-forward validation on {len(df_15m):,} 15m bars...")
  result = run_walk_forward_ml_validation(cfg, df_15m, df_1m if not df_1m.empty else None, wf_cfg)

  table = Table(title="ML Walk-Forward Validation")
  table.add_column("Metric")
  table.add_column("Value")
  s = result["summary"]
  for k, v in [
    ("Folds", s["n_folds"]),
    ("Mean IS AUC", s["mean_is_auc"]),
    ("Mean OOS AUC", s["mean_oos_auc"]),
    ("Mean AUC gap", s["mean_auc_gap"]),
    ("Overfit folds", s["folds_overfit_flagged"]),
    ("Top-K feature overlap", s["feature_stability"]["top_k_overlap_mean"]),
    ("Rank correlation", s["feature_stability"]["rank_correlation_mean"]),
  ]:
    table.add_row(k, str(v))
  console.print(table)

  if s.get("daily_retrain_warning"):
    console.print(f"\n[yellow]⚠ {s['daily_retrain_warning']}[/yellow]")

  out_path = output or str(Path(cfg["paths"]["logs"]) / "ml_walk_forward_validation.json")
  payload = {"generated_at": datetime.now(timezone.utc).isoformat(), **result}
  Path(out_path).parent.mkdir(parents=True, exist_ok=True)
  with open(out_path, "w") as f:
    json.dump(payload, f, indent=2)
  console.print(f"\n[green]Saved to {out_path}[/green]")


if __name__ == "__main__":
  main()
