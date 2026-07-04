"""Resolve exit prices for bot legs from Kalshi fills and market settlement."""

from __future__ import annotations

import logging
from typing import Any

from src.data.kalshi_hourly import fetch_market_row, market_settled
from src.trading.hourly_settlement import (
  contract_spec_from_position,
  settlement_exit_cents,
)

log = logging.getLogger(__name__)


def _yes_price_cents(raw: Any) -> int | None:
  if raw is None or raw == "":
    return None
  try:
    val = float(raw)
  except (TypeError, ValueError):
    return None
  if 0 < val < 1:
    return max(1, min(99, int(round(val * 100))))
  return max(1, min(99, int(round(val))))


def _dollars_field_to_cents(raw: Any) -> int | None:
  if raw is None or raw == "":
    return None
  try:
    val = float(raw)
  except (TypeError, ValueError):
    return None
  if 0 < val <= 1:
    return max(1, min(99, int(round(val * 100))))
  return max(1, min(99, int(round(val))))


def leg_price_cents_from_fill(fill: dict[str, Any], *, held_side: str) -> int | None:
  """Held-leg price in cents from a Kalshi fill (V2 dollars or legacy cents)."""
  side_l = str(held_side or "").lower()
  yes_d = fill.get("yes_price_dollars") or fill.get("yes_price_fixed")
  no_d = fill.get("no_price_dollars") or fill.get("no_price_fixed")
  yes_c = _dollars_field_to_cents(yes_d) if yes_d not in (None, "") else _yes_price_cents(
    fill.get("yes_price") or fill.get("price"),
  )
  no_c = _dollars_field_to_cents(no_d) if no_d not in (None, "") else _yes_price_cents(fill.get("no_price"))
  if side_l == "yes":
    if yes_c is not None:
      return yes_c
    if no_c is not None:
      return max(1, min(99, 100 - no_c))
    return None
  if no_c is not None:
    return no_c
  if yes_c is not None:
    return max(1, min(99, 100 - yes_c))
  return None


def sum_verified_kalshi_buy_contracts(
  kalshi: Any,
  *,
  market_ticker: str,
  side: str,
  kalshi_order_id: str | None = None,
) -> float:
  """Buy fill contracts on Kalshi for ticker+side (optional single order_id)."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return 0.0
  from src.trading.kalshi_fill_sync import (
    _build_order_direction_cache,
    _fill_action_side,
    _fill_count,
  )

  ticker = str(market_ticker)
  side_l = str(side or "").lower()
  cache = _build_order_direction_cache(kalshi)
  total = 0.0
  for fill in kalshi.list_fills(ticker=ticker, limit=500):
    leg = _fill_action_side(fill, cache)
    if not leg:
      continue
    ft, action, fs = leg
    if ft != ticker or action != "buy" or fs != side_l:
      continue
    if kalshi_order_id and str(fill.get("order_id") or "") != str(kalshi_order_id):
      continue
    total += _fill_count(fill)
  return round(total, 2)


def avg_sell_fill_cents(
  kalshi: Any,
  *,
  market_ticker: str,
  side: str,
  max_contracts: int | None = None,
) -> int | None:
  """Volume-weighted average sell fill price on the held leg."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return None
  from src.trading.kalshi_fill_sync import (
    _build_order_direction_cache,
    _fill_action_side,
    _fill_count,
    _fill_market_ticker,
  )

  side_l = str(side or "").lower()
  ticker = str(market_ticker)
  fills = kalshi.list_fills(ticker=ticker, limit=200)
  cache = _build_order_direction_cache(kalshi)
  total_ct = 0.0
  total_px_ct = 0.0
  for fill in fills:
    row = fill if _fill_market_ticker(fill) else {**fill, "ticker": ticker}
    leg = _fill_action_side(row, cache)
    if not leg or leg[1] != "sell" or leg[2] != side_l:
      continue
    px = leg_price_cents_from_fill(fill, held_side=side_l)
    if px is None:
      continue
    ct = _fill_count(fill)
    if ct <= 0:
      continue
    if max_contracts is not None:
      ct = min(ct, max(0.0, float(max_contracts) - total_ct))
      if ct <= 0:
        break
    total_ct += ct
    total_px_ct += ct * float(px)
  if total_ct < 0.05:
    return None
  return max(1, min(99, int(round(total_px_ct / total_ct))))


def market_binary_exit_cents(
  kalshi: Any,
  *,
  market_ticker: str,
  side: str,
  pos: dict[str, Any] | None = None,
) -> tuple[int | None, str]:
  """Binary 0/100 payout when Kalshi has finalized the market."""
  if not kalshi:
    return None, ""
  row = fetch_market_row(kalshi, str(market_ticker))
  if not row or not market_settled(row):
    return None, ""
  exp = row.get("expiration_value")
  if exp in (None, ""):
    return None, ""
  try:
    settle_price = float(exp)
  except (TypeError, ValueError):
    return None, ""
  spec = contract_spec_from_position(pos or {})
  settled = settlement_exit_cents(
    side=str(side or "yes"),
    settle_price=settle_price,
    spec=spec,
  )
  if settled is None:
    return None, ""
  idx = f"${settle_price:,.2f}"
  outcome = "won" if settled == 100 else "lost"
  return int(settled), f"Kalshi settled @ {settled}¢ ({outcome} vs {idx})"


def slot15_binary_exit_cents(
  kalshi: Any,
  *,
  market_ticker: str,
  side: str,
  slot_key: str,
) -> tuple[int | None, str]:
  """15m up/down binary payout from Kalshi slot settlement."""
  if not kalshi:
    return None, ""
  import pandas as pd

  try:
    settlement = kalshi.slot_settlement(pd.Timestamp(slot_key))
  except Exception as e:
    log.warning("slot15 settlement lookup failed for %s: %s", slot_key, e)
    return None, ""
  if settlement is None or not settlement.settled:
    return None, ""
  if str(settlement.market_ticker) != str(market_ticker):
    row = kalshi.get_market_ticker(str(market_ticker))
    if row and market_settled(row):
      return market_binary_exit_cents(
        kalshi, market_ticker=market_ticker, side=side, pos=None,
      )
    return None, ""
  outcome_up = settlement.outcome_up
  if outcome_up is None:
    return None, ""
  held_yes = str(side or "").lower() == "yes"
  held_wins = bool(outcome_up) if held_yes else not bool(outcome_up)
  cents = 100 if held_wins else 0
  close_b = settlement.close_brti
  open_b = settlement.open_brti
  note = (
    f"Kalshi 15m settled @ {cents}¢ "
    f"({'up' if outcome_up else 'down'}; BRTI ${open_b:,.2f}→${close_b:,.2f})"
  )
  return cents, note


def resolve_kalshi_leg_exit_cents(
  kalshi: Any,
  *,
  market_ticker: str,
  side: str,
  contracts: int,
  pos: dict[str, Any] | None = None,
  period_key: str | None = None,
  inferred_cents: int | None = None,
) -> tuple[int | None, str]:
  """
  Best exit price for a flat Kalshi leg: sell fills → market settlement → inferred.

  Returns (exit_cents, source_note).
  """
  ticker = str(market_ticker)
  side_l = str(side or "").lower()

  fill_c = avg_sell_fill_cents(
    kalshi,
    market_ticker=ticker,
    side=side_l,
    max_contracts=int(contracts),
  )
  if fill_c is not None:
    return fill_c, f"Kalshi sell fills avg @ {fill_c}¢"

  settle_c, settle_note = market_binary_exit_cents(
    kalshi, market_ticker=ticker, side=side_l, pos=pos,
  )
  if settle_c is not None:
    return settle_c, settle_note

  if period_key and kalshi and hasattr(kalshi, "slot_settlement"):
    slot_c, slot_note = slot15_binary_exit_cents(
      kalshi,
      market_ticker=ticker,
      side=side_l,
      slot_key=str(period_key),
    )
    if slot_c is not None:
      return slot_c, slot_note

  if inferred_cents is not None:
    return int(inferred_cents), f"inferred exit @ {int(inferred_cents)}¢"

  return None, ""
