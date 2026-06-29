"""Scale-in evaluation for adding to winning positions on the same ticker."""

from __future__ import annotations

from typing import Any

from src.trading.entry_strategy import EntryStrategyConfig, ask_cents_for_side, ask_implied_prob, win_prob_for_side
from src.trading.paper_execution import paper_exit_fill


def unrealized_pnl_usd(pos: dict[str, Any], mark_cents: int | None) -> float | None:
  if mark_cents is None:
    return None
  entry_c = int(pos["entry_price_cents"])
  contracts = int(pos["contracts"])
  if pos["side"] == "yes":
    return round(contracts * (mark_cents - entry_c) / 100.0, 2)
  return round(contracts * (entry_c - mark_cents) / 100.0, 2)


def mark_cents_for_pick(pick: dict[str, Any], side: str) -> int | None:
  fill = paper_exit_fill(pick=pick, side=side)
  if fill.get("ok"):
    return int(fill["price_cents"])
  return None


def _leg_entry_ask_edge_cents(pos: dict[str, Any], pick: dict[str, Any], side: str) -> float | None:
  p = win_prob_for_side(pick, side)
  if p is None:
    return None
  entry_ask = int(pos["entry_price_cents"])
  return round((p - ask_implied_prob(entry_ask)) * 100, 1)


def evaluate_scale_in(
  existing_positions: list[dict[str, Any]],
  pick: dict[str, Any],
  side: str,
  estrat: EntryStrategyConfig,
) -> tuple[bool, str | None]:
  ticker = str(pick.get("ticker") or existing_positions[0].get("market_ticker") or "")

  if not estrat.allow_scale_in:
    return False, f"already_open:{ticker}"

  if len(existing_positions) >= estrat.scale_in_max_legs_per_ticker:
    return False, "scale_in_max_legs"

  leg_sides = {str(p.get("side") or "") for p in existing_positions}
  if len(leg_sides) != 1 or side not in leg_sides:
    return False, "scale_in_side_mismatch"

  combined_upnl = 0.0
  for pos in existing_positions:
    mark = mark_cents_for_pick(pick, str(pos["side"]))
    upnl = unrealized_pnl_usd(pos, mark)
    if upnl is None:
      return False, "scale_in_no_mark"
    combined_upnl += upnl

  if combined_upnl < estrat.scale_in_min_unrealized_pnl_usd:
    return False, f"scale_in_not_winner:{combined_upnl:.2f}"

  if estrat.scale_in_min_ask_edge_improvement_cents > 0:
    leg_edges: list[float] = []
    for pos in existing_positions:
      edge = _leg_entry_ask_edge_cents(pos, pick, side)
      if edge is None:
        return False, "scale_in_no_edge"
      leg_edges.append(edge)
    min_leg_edge = min(leg_edges)
    new_ask = ask_cents_for_side(pick, side)
    p = win_prob_for_side(pick, side)
    if new_ask is None or p is None:
      return False, "scale_in_no_edge"
    new_edge = round((p - ask_implied_prob(new_ask)) * 100, 1)
    required = min_leg_edge + estrat.scale_in_min_ask_edge_improvement_cents
    if new_edge < required:
      return False, f"scale_in_edge_not_improved:{new_edge:.1f}"

  return True, None
