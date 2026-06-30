"""Track how often live/paper enters hit max_stake_per_entry_usd vs actual sizing."""

from __future__ import annotations

from typing import Any

from src.trading.entry_strategy import EntryStrategyConfig, live_entry_stake_cap_usd

# Within 5% of cap counts as "at cap" (rounding / contract quantization).
_BINDING_RATIO = 0.95
# Suggest reviewing a raise when this share of enters sit at the cap.
_BINDING_PCT_THRESHOLD = 0.25


def _median(values: list[float]) -> float:
  ordered = sorted(values)
  mid = len(ordered) // 2
  if not ordered:
    return 0.0
  if len(ordered) % 2:
    return ordered[mid]
  return (ordered[mid - 1] + ordered[mid]) / 2.0


def _summary_line(
  *,
  filled: int,
  avg: float,
  max_stake: float,
  effective_cap: float,
  pct_at_max: float,
  binding: bool,
) -> str:
  if filled <= 0:
    return "No filled enters — stake cap not evaluated."
  avg_s = f"${avg:.2f}"
  max_s = f"${max_stake:.2f}"
  eff_s = f"${effective_cap:.2f}"
  pct_s = f"{pct_at_max * 100:.0f}%"
  if binding:
    return (
      f"Avg enter {avg_s} vs max_stake {max_s} (effective cap {eff_s}); "
      f"{pct_s} of enters at cap — consider raising max_stake_per_entry_usd."
    )
  return (
    f"Avg enter {avg_s} vs max_stake {max_s} (effective cap {eff_s}); "
    f"{pct_s} at cap — cap not binding, keep max_stake_per_entry_usd."
  )


def compute_stake_cap_utilization(
  trades: list[dict[str, Any]],
  *,
  estrat: EntryStrategyConfig,
  max_spend_usd: float,
  mode: str | None = None,
) -> dict[str, Any]:
  """
  Compare filled enter cost_usd to max_stake_per_entry_usd and live stake cap.

  Returns avg/median enter cost, pct at cap, binding flag, and a one-line summary.
  """
  enters = [
    t
    for t in trades
    if t.get("action") == "enter" and t.get("status") == "filled"
  ]
  if mode:
    enters = [t for t in enters if str(t.get("mode") or "") == mode]

  max_stake = max(0.0, float(estrat.max_stake_per_entry_usd))
  live_cap = live_entry_stake_cap_usd(
    max_spend_per_hour_usd=max(0.0, float(max_spend_usd)),
    estrat=estrat,
  )
  effective_cap = min(max_stake, live_cap) if max_stake > 0 else live_cap

  costs = [
    float(t.get("cost_usd") or 0)
    for t in enters
    if float(t.get("cost_usd") or 0) > 0
  ]

  if not costs:
    return {
      "filled_enters": 0,
      "max_stake_per_entry_usd": round(max_stake, 2),
      "live_entry_stake_cap_usd": round(live_cap, 2),
      "effective_cap_usd": round(effective_cap, 2),
      "avg_enter_cost_usd": None,
      "median_enter_cost_usd": None,
      "pct_at_max_stake": None,
      "pct_at_effective_cap": None,
      "cap_binding": False,
      "summary_line": "No filled enters — stake cap not evaluated.",
    }

  at_max = sum(1 for c in costs if max_stake > 0 and c >= max_stake * _BINDING_RATIO)
  at_eff = sum(1 for c in costs if effective_cap > 0 and c >= effective_cap * _BINDING_RATIO)
  pct_at_max = at_max / len(costs)
  pct_at_eff = at_eff / len(costs)
  avg = sum(costs) / len(costs)
  med = _median(costs)

  binding = pct_at_max >= _BINDING_PCT_THRESHOLD or (
    pct_at_eff >= _BINDING_PCT_THRESHOLD and avg >= effective_cap * 0.8
  )

  return {
    "filled_enters": len(costs),
    "max_stake_per_entry_usd": round(max_stake, 2),
    "live_entry_stake_cap_usd": round(live_cap, 2),
    "effective_cap_usd": round(effective_cap, 2),
    "avg_enter_cost_usd": round(avg, 2),
    "median_enter_cost_usd": round(med, 2),
    "pct_at_max_stake": round(pct_at_max, 3),
    "pct_at_effective_cap": round(pct_at_eff, 3),
    "cap_binding": binding,
    "summary_line": _summary_line(
      filled=len(costs),
      avg=avg,
      max_stake=max_stake,
      effective_cap=effective_cap,
      pct_at_max=pct_at_max,
      binding=binding,
    ),
  }
