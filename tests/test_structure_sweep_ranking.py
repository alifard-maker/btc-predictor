from src.trading.structure_sweep_ranking import (
  best_structure_variant,
  full_horizon_struct_items,
  is_full_horizon_result,
)


def test_is_full_horizon_result():
  fair = {"hours_simulated": 26247}
  assert is_full_horizon_result({"hours_simulated": 26246}, fair=fair)
  assert not is_full_horizon_result({"hours_simulated": 326}, fair=fair)


def test_best_structure_variant_ignores_truncated():
  fair = {"hours_simulated": 26247, "total_pnl_usd": -100.0}
  results = {
    "fair_baseline_gates": fair,
    "struct_short": {"hours_simulated": 326, "total_pnl_usd": -10.0},
    "struct_full": {"hours_simulated": 26246, "total_pnl_usd": -50.0},
  }
  name, row = best_structure_variant(results, fair=fair)
  assert name == "struct_full"
  assert row["total_pnl_usd"] == -50.0
  assert len(full_horizon_struct_items(results, fair=fair)) == 1
