#!/usr/bin/env python3
"""Walk-forward backtest CLI with simulated fills and fees."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import click
from rich.console import Console
from rich.table import Table

from src.backtest.fill_simulator import OrderStyle
from src.backtest.walk_forward import WalkForwardBacktest, WalkForwardConfig
from src.config import load_config
from src.data.storage import CandleStorage
from src.experiments.edge_test import compare_variants

console = Console()


def _summary_table(metrics, fold_summaries: list) -> Table:
  table = Table(title="Walk-Forward Backtest Summary")
  table.add_column("Metric")
  table.add_column("Value")
  rows = [
    ("Trades (signals)", str(metrics.n_trades)),
    ("Filled", str(metrics.n_filled)),
    ("Fill rate", f"{metrics.fill_rate:.2%}"),
    ("Win rate", f"{metrics.win_rate:.2%}"),
    ("Expectancy ($/trade)", f"${metrics.expectancy_usd:.4f}"),
    ("Expectancy 95% CI", f"[{metrics.expectancy_ci_lower}, {metrics.expectancy_ci_upper}]"),
    ("Total PnL ($)", f"${metrics.total_pnl_usd:.4f}"),
    ("Sharpe-like", f"{metrics.sharpe_like:.4f}"),
    ("Max drawdown ($)", f"${metrics.max_drawdown_usd:.4f}"),
    ("Folds", str(len(fold_summaries))),
  ]
  for k, v in rows:
    table.add_row(k, v)
  return table


@click.command()
@click.option("--horizon", default=None, type=click.Choice(["hourly", "15m"]), help="Backtest horizon")
@click.option("--train-window", default=None, type=int)
@click.option("--test-window", default=None, type=int)
@click.option("--step", default=None, type=int)
@click.option(
  "--order-style",
  default=None,
  type=click.Choice(["passive_limit", "cross_spread"]),
)
@click.option("--output", default=None, help="JSON output path")
@click.option("--compare-cross-spread", is_flag=True, help="Run edge test: passive vs cross-spread")
def main(
  horizon: str | None,
  train_window: int | None,
  test_window: int | None,
  step: int | None,
  order_style: str | None,
  output: str | None,
  compare_cross_spread: bool,
) -> None:
  cfg = load_config()
  wf_cfg = WalkForwardConfig.from_config(cfg)
  if horizon:
    wf_cfg.horizon = horizon
  if train_window:
    wf_cfg.train_window = train_window
  if test_window:
    wf_cfg.test_window = test_window
  if step:
    wf_cfg.step = step
  if order_style:
    wf_cfg.order_style = OrderStyle(order_style)

  storage = CandleStorage(cfg)
  if wf_cfg.horizon == "hourly":
    df_primary = storage.load("1h")
    df_context = storage.load("15m")
    label = "1h (hourly events)"
  else:
    df_primary = storage.load("15m")
    df_context = storage.load("1m")
    label = "15m (slot events)"

  if df_primary.empty:
    console.print(f"[red]No {label} candle data. Run collect_historical.py first.[/red]")
    sys.exit(1)

  console.print(f"Walk-forward backtest on {len(df_primary):,} {label} bars...")
  bt = WalkForwardBacktest(cfg, wf_cfg)
  trades, metrics, folds = bt.run(df_primary, df_context if not df_context.empty else None)

  console.print(_summary_table(metrics, folds))

  edge_test_result = None
  if compare_cross_spread:
    console.print("\n[yellow]Running variant comparison: passive_limit vs cross_spread[/yellow]")
    passive_cfg = WalkForwardConfig.from_config(cfg)
    passive_cfg.order_style = OrderStyle.PASSIVE_LIMIT
    passive_cfg.horizon = wf_cfg.horizon
    passive_cfg.train_window = wf_cfg.train_window
    passive_cfg.test_window = wf_cfg.test_window
    passive_cfg.step = wf_cfg.step

    cross_cfg = WalkForwardConfig.from_config(cfg)
    cross_cfg.order_style = OrderStyle.CROSS_SPREAD
    cross_cfg.horizon = wf_cfg.horizon
    cross_cfg.train_window = wf_cfg.train_window
    cross_cfg.test_window = wf_cfg.test_window
    cross_cfg.step = wf_cfg.step

    _, passive_metrics, _ = WalkForwardBacktest(cfg, passive_cfg).run(
      df_primary, df_context if not df_context.empty else None
    )
    cross_trades, cross_metrics, _ = WalkForwardBacktest(cfg, cross_cfg).run(
      df_primary, df_context if not df_context.empty else None
    )
    filled_passive = trades[trades["filled"]]["pnl_usd"]
    filled_cross = cross_trades[cross_trades["filled"]]["pnl_usd"]
    edge_test_result = compare_variants(
      filled_passive,
      filled_cross,
      name_a="passive_limit",
      name_b="cross_spread",
    ).to_dict()
    console.print(f"  Mean diff (passive - cross): ${edge_test_result['mean_diff_usd']:.4f}")
    console.print(f"  Permutation p-value: {edge_test_result['permutation_p_value']:.4f}")
    if edge_test_result.get("power_warning"):
      console.print(f"  [yellow]{edge_test_result['power_warning']}[/yellow]")

  payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "horizon": wf_cfg.horizon,
    "config": {
      "train_window": wf_cfg.train_window,
      "test_window": wf_cfg.test_window,
      "step": wf_cfg.step,
      "order_style": wf_cfg.order_style.value,
    },
    "metrics": metrics.to_dict(),
    "folds": folds,
    "n_trade_rows": len(trades),
    "edge_test": edge_test_result,
    "trades_sample": trades.head(20).to_dict(orient="records"),
  }

  out_path = output or str(
    Path(cfg["paths"]["logs"]) / f"walk_forward_backtest_{wf_cfg.horizon}.json"
  )
  Path(out_path).parent.mkdir(parents=True, exist_ok=True)
  with open(out_path, "w") as f:
    json.dump(payload, f, indent=2, default=str)
  console.print(f"\n[green]Results saved to {out_path}[/green]")


if __name__ == "__main__":
  main()
