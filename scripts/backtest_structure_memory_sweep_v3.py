#!/usr/bin/env python3
"""Phase A v3: fair baselines + comprehensive staged structure-memory grid (3y)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.backtest.hourly_mechanics_backtest import MechanicsSimOptions, run_mechanics_backtest
from src.config import load_config
from src.data.storage import CandleStorage

DEFAULT_OUT = Path(os.getenv("DATA_DIR", str(ROOT / "data"))) / "logs" / "backtest_structure_memory_sweep_v3.json"

GATES = MechanicsSimOptions(rank_by_ask_edge=True, use_live_regime=True)

LOOKBACKS = [4, 6, 8, 12, 18, 24]
MU_PULLS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35]
UPPER_BOXES = [0.65, 0.70, 0.75, 0.80, 0.85]
RESISTANCES = [0.2, 0.4, 0.6]
SIGMA_INFLATES = [1.0, 1.25, 1.5]
BLOCK_YES = [True, False]


@dataclass(frozen=True)
class StructParams:
  lookback: int
  mu_pull: float
  upper_box: float
  resistance: float
  sigma_inflate: float
  block_yes: bool


DEFAULT = StructParams(12, 0.35, 0.70, 0.4, 1.25, True)


def _variant_name(p: StructParams, stage: str) -> str:
  blk = "T" if p.block_yes else "F"
  return (
    f"struct_lb{p.lookback}_pull{p.mu_pull:.2f}_ub{p.upper_box:.2f}"
    f"_rp{p.resistance:.1f}_si{p.sigma_inflate:.2f}_blk{blk}_{stage}"
  )


def build_grid(*, quick: bool = False) -> list[tuple[str, StructParams, str]]:
  """Staged grid: exhaustive on each param axis, <500 total runs after dedupe."""
  if quick:
    combos: list[tuple[str, StructParams, str]] = [
      (_variant_name(StructParams(6, 0.15, 0.75, 0.4, 1.25, True), "quick"), StructParams(6, 0.15, 0.75, 0.4, 1.25, True), "quick"),
      (_variant_name(StructParams(12, 0.25, 0.70, 0.6, 1.5, False), "quick"), StructParams(12, 0.25, 0.70, 0.6, 1.5, False), "quick"),
    ]
    return combos

  seen: set[tuple] = set()
  out: list[tuple[str, StructParams, str]] = []

  def add(p: StructParams, stage: str) -> None:
    key = (p.lookback, p.mu_pull, p.upper_box, p.resistance, p.sigma_inflate, p.block_yes)
    if key in seen:
      return
    seen.add(key)
    out.append((_variant_name(p, stage), p, stage))

  d = DEFAULT
  for lb in LOOKBACKS:
    for pull in MU_PULLS:
      for ubox in UPPER_BOXES:
        add(StructParams(lb, pull, ubox, d.resistance, d.sigma_inflate, d.block_yes), "s1_primary")

  for rp in RESISTANCES:
    for si in SIGMA_INFLATES:
      for blk in BLOCK_YES:
        add(StructParams(d.lookback, d.mu_pull, d.upper_box, rp, si, blk), "s2_secondary")

  for lb in (4, 24):
    for pull in (0.10, 0.35):
      for rp in RESISTANCES:
        for blk in BLOCK_YES:
          add(StructParams(lb, pull, d.upper_box, rp, d.sigma_inflate, blk), "s3_extreme_lb_pull")

  for lb in (4, 12, 24):
    for rp in RESISTANCES:
      for si in SIGMA_INFLATES:
        add(StructParams(lb, d.mu_pull, d.upper_box, rp, si, d.block_yes), "s4_lb_resist_sigma")

  for ubox in UPPER_BOXES:
    for blk in BLOCK_YES:
      add(StructParams(d.lookback, d.mu_pull, ubox, d.resistance, d.sigma_inflate, blk), "s5_upper_block")

  return out


def _row(name: str, r: dict) -> dict:
  return {
    "name": name,
    "total_pnl_usd": r["total_pnl_usd"],
    "filled_enters": r["filled_enters"],
    "expectancy_per_fill_usd": r["expectancy_per_fill_usd"],
    "win_rate": r["win_rate"],
    "hours_with_fills": r["hours_with_fills"],
    "cut_loss_pnl": (r.get("by_exit_type") or {}).get("CUT LOSSES", {}).get("pnl"),
  }


def _sim_options(p: StructParams) -> MechanicsSimOptions:
  return MechanicsSimOptions(
    structure_memory=True,
    structure_lookback_bars=p.lookback,
    structure_mu_pull_strength=p.mu_pull,
    structure_upper_box_fraction=p.upper_box,
    structure_resistance_penalty=p.resistance,
    structure_sigma_inflate_tight=p.sigma_inflate,
    structure_block_yes_above=p.block_yes,
    rank_by_ask_edge=True,
    use_live_regime=True,
  )


def main() -> int:
  import argparse

  parser = argparse.ArgumentParser(description="Structure-memory sweep v3 (staged comprehensive grid)")
  parser.add_argument("--years", type=float, default=3.0)
  parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
  parser.add_argument("--quick", action="store_true", help="Tiny grid for smoke test")
  parser.add_argument("--resume", action="store_true", default=True, help="Resume from checkpoint if present")
  parser.add_argument("--no-resume", action="store_false", dest="resume")
  args = parser.parse_args()

  progress_path = args.output.with_suffix(".progress.json")
  checkpoint_every = 1

  cfg = load_config()
  df = CandleStorage(cfg).load("1h").sort_values("timestamp").reset_index(drop=True)
  if df.empty:
    print("error: no 1h candles — run backfill_1h_candles_railway.py first", flush=True)
    return 1

  end = df["timestamp"].max()
  df = df[df["timestamp"] >= end - pd.Timedelta(days=int(args.years * 365.25))].reset_index(drop=True)
  span_days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).total_seconds() / 86400.0
  print(f"loaded {len(df):,} 1h bars spanning {span_days:.1f}d", flush=True)

  grid = build_grid(quick=args.quick)
  print(f"grid size: {len(grid)} structure variants (+2 baselines)", flush=True)

  results: dict[str, dict] = {}
  summary_rows: list[dict] = []
  grid_meta: list[dict] = []
  done_names: set[str] = set()

  if args.resume and progress_path.exists():
    try:
      ckpt = json.loads(progress_path.read_text(encoding="utf-8"))
      results = dict(ckpt.get("results") or {})
      summary_rows = list(ckpt.get("summary") or [])
      grid_meta = list(ckpt.get("grid") or [])
      done_names = set(results.keys())
      print(f"resuming from checkpoint: {len(done_names)} variants already done", flush=True)
    except Exception as exc:
      print(f"checkpoint load failed ({exc}), starting fresh", flush=True)

  def _save_checkpoint() -> None:
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(
      json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "summary": summary_rows,
        "grid": grid_meta,
        "bars": len(df),
        "span_days": round(span_days, 2),
      }, indent=2),
      encoding="utf-8",
    )

  if "baseline_frozen" not in results:
    print("baseline pnl_first (frozen)...", flush=True)
    results["baseline_frozen"] = run_mechanics_backtest(df, cfg, profile="pnl_first", max_spend=15.0)
    summary_rows.append(_row("baseline_frozen", results["baseline_frozen"]))
    _save_checkpoint()

  if "fair_baseline_gates" not in results:
    print("fair baseline ask_edge + live_regime...", flush=True)
    results["fair_baseline_gates"] = run_mechanics_backtest(
      df, cfg, profile="pnl_first", max_spend=15.0, sim_options=GATES,
    )
    summary_rows.append(_row("fair_baseline_gates", results["fair_baseline_gates"]))
    _save_checkpoint()

  pending = [(name, params, stage) for name, params, stage in grid if name not in done_names]
  for i, (name, params, stage) in enumerate(pending, 1):
    idx = len(done_names) + i
    print(f"[{idx}/{len(grid)}] running {name}...", flush=True)
    results[name] = run_mechanics_backtest(
      df, cfg, profile="pnl_first", max_spend=15.0, sim_options=_sim_options(params),
    )
    summary_rows.append(_row(name, results[name]))
    grid_meta.append({
      "name": name,
      "stage": stage,
      "lookback_bars": params.lookback,
      "mu_pull_strength": params.mu_pull,
      "upper_box_fraction": params.upper_box,
      "resistance_mu_penalty": params.resistance,
      "sigma_inflate_tight": params.sigma_inflate,
      "block_yes_above": params.block_yes,
    })
    if i % checkpoint_every == 0 or i == len(pending):
      _save_checkpoint()

  fair_pnl = float(results["fair_baseline_gates"]["total_pnl_usd"])
  struct_items = [(k, v) for k, v in results.items() if k.startswith("struct_")]
  best = max(struct_items, key=lambda kv: kv[1]["total_pnl_usd"])

  payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "years": args.years,
    "bars": len(df),
    "span_days": round(span_days, 2),
    "bar_start": df["timestamp"].iloc[0].isoformat(),
    "bar_end": df["timestamp"].iloc[-1].isoformat(),
    "grid_size": len(grid),
    "grid_stages": {
      stage: sum(1 for g in grid_meta if g["stage"] == stage)
      for stage in sorted({g["stage"] for g in grid_meta})
    },
    "param_axes": {
      "lookback_bars": LOOKBACKS,
      "mu_pull_strength": MU_PULLS,
      "upper_box_fraction": UPPER_BOXES,
      "resistance_mu_penalty": RESISTANCES,
      "sigma_inflate_tight": SIGMA_INFLATES,
      "block_yes_above": BLOCK_YES,
    },
    "fair_baseline_pnl_usd": fair_pnl,
    "best_structure": {"name": best[0], **_row(best[0], best[1])},
    "delta_vs_fair_usd": round(float(best[1]["total_pnl_usd"]) - fair_pnl, 2),
    "summary": sorted(summary_rows, key=lambda r: r["total_pnl_usd"], reverse=True),
    "grid": grid_meta,
    "results": results,
  }
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
  if progress_path.exists():
    progress_path.unlink()
  print(f"wrote {args.output}", flush=True)
  print(
    f"fair=${fair_pnl:,.2f} best={best[0]} ${best[1]['total_pnl_usd']:,.2f} "
    f"delta=${payload['delta_vs_fair_usd']:+,.2f}",
    flush=True,
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
