"""Kalshi hourly contract settlement for paper period rollover."""

from __future__ import annotations

import re
from typing import Any


def _parse_money(raw: str) -> float:
  return float(str(raw).replace(",", ""))


def contract_spec_from_label(label: str | None) -> dict[str, Any]:
  """Best-effort strike metadata from dashboard labels like 'NO · $1,610 to 1,629.99'."""
  if not label:
    return {}
  text = re.sub(r"^(YES|NO)\s*·\s*", "", str(label).strip(), flags=re.IGNORECASE)
  between = re.search(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(?:to|–|-)\s*\$?\s*([\d,]+(?:\.\d+)?)",
    text,
    flags=re.IGNORECASE,
  )
  if between:
    return {
      "contract_type": "range",
      "strike_type": "between",
      "floor_strike": _parse_money(between.group(1)),
      "cap_strike": _parse_money(between.group(2)),
    }
  above = re.search(
    r"(?:≥|>=)?\s*\$?\s*([\d,]+(?:\.\d+)?)\s+or above",
    text,
    flags=re.IGNORECASE,
  )
  if above:
    return {
      "contract_type": "threshold",
      "strike_type": "greater",
      "floor_strike": _parse_money(above.group(1)),
    }
  below = re.search(r"<\s*\$?\s*([\d,]+(?:\.\d+)?)", text, flags=re.IGNORECASE)
  if below:
    return {
      "contract_type": "threshold",
      "strike_type": "less",
      "cap_strike": _parse_money(below.group(1)),
    }
  return {}


def contract_spec_from_position(pos: dict[str, Any]) -> dict[str, Any]:
  floor = pos.get("floor_strike")
  cap = pos.get("cap_strike")
  if floor is not None or cap is not None:
    return {
      "contract_type": pos.get("contract_type") or "range",
      "strike_type": pos.get("strike_type"),
      "floor_strike": floor,
      "cap_strike": cap,
    }
  return contract_spec_from_label(pos.get("label"))


def contract_spec_from_pick(pick: dict[str, Any] | None) -> dict[str, Any]:
  if not pick:
    return {}
  return {
    "contract_type": pick.get("contract_type", "threshold"),
    "strike_type": pick.get("strike_type"),
    "floor_strike": pick.get("floor_strike"),
    "cap_strike": pick.get("cap_strike"),
  }


def yes_wins_at_settle(settle_price: float, spec: dict[str, Any]) -> bool | None:
  """True if YES pays $1 at settle, False if NO pays, None if spec incomplete."""
  floor = spec.get("floor_strike")
  cap = spec.get("cap_strike")
  strike_type = spec.get("strike_type")
  ctype = spec.get("contract_type", "threshold")

  if ctype == "range" or strike_type == "between":
    if floor is None or cap is None:
      return None
    lo, hi = float(floor), float(cap)
    return lo <= float(settle_price) <= hi

  if strike_type == "greater" and floor is not None:
    return float(settle_price) >= float(floor)
  if strike_type == "less" and cap is not None:
    return float(settle_price) < float(cap)
  return None


def settlement_exit_cents(
  *,
  side: str,
  settle_price: float,
  spec: dict[str, Any],
) -> int | None:
  """Binary settlement payout on the held leg (100 = win, 0 = loss)."""
  yes_wins = yes_wins_at_settle(settle_price, spec)
  if yes_wins is None:
    return None
  held_yes = str(side).lower() == "yes"
  held_wins = yes_wins if held_yes else not yes_wins
  return 100 if held_wins else 0


def resolve_hourly_rollover_exit_cents(
  pos: dict[str, Any],
  *,
  settle_price: float | None,
  pick: dict[str, Any] | None = None,
  market_exit_cents: int,
) -> tuple[int, str]:
  """
  Prefer Kalshi hourly settlement at period end; fall back to last market mark.

  Returns (exit_cents, note_suffix).
  """
  if settle_price is not None and float(settle_price) > 0:
    spec = contract_spec_from_pick(pick) or contract_spec_from_position(pos)
    settled = settlement_exit_cents(
      side=str(pos.get("side") or "yes"),
      settle_price=float(settle_price),
      spec=spec,
    )
    if settled is not None:
      idx = f"${float(settle_price):,.2f}"
      outcome = "won" if settled == 100 else "lost"
      return settled, f"settled @ {settled}¢ ({outcome} vs {idx})"
  return market_exit_cents, f"marked @ {market_exit_cents}¢"
