"""Paper execution helpers with realistic bid/ask-based fills."""

from __future__ import annotations

from typing import Any

# Fallback slippage when only midpoint is available.
MID_BUY_HAIRCUT_CENTS = 1
MID_SELL_HAIRCUT_CENTS = 1

# Guardrails to avoid unrealistic penny-lottery sizing in paper mode.
DEFAULT_PRICE_FLOOR_CENTS = 5
DEFAULT_PRICE_CEILING_CENTS = 99
DEFAULT_MAX_SPREAD_CENTS = 15
DEFAULT_MAX_CONTRACTS = 500


def _to_cents(value: Any) -> int | None:
  if value is None:
    return None
  try:
    raw = float(value)
  except (TypeError, ValueError):
    return None
  if raw <= 0:
    return None
  if raw <= 1.0:
    cents = int(round(raw * 100))
  else:
    cents = int(round(raw))
  return max(1, min(99, cents))


def _yes_mid_cents(pick: dict[str, Any]) -> int | None:
  return _to_cents(pick.get("kalshi_mid") if pick.get("kalshi_mid") is not None else pick.get("yes_mid"))


def _yes_bid_ask_cents(pick: dict[str, Any]) -> tuple[int | None, int | None]:
  yes_bid = _to_cents(pick.get("yes_bid"))
  yes_ask = _to_cents(pick.get("yes_ask"))
  return yes_bid, yes_ask


def _side_quotes_cents(pick: dict[str, Any], side: str) -> tuple[int | None, int | None]:
  yes_bid, yes_ask = _yes_bid_ask_cents(pick)
  if side == "yes":
    bid = yes_bid
    ask = yes_ask
  else:
    explicit_no_bid = _to_cents(pick.get("no_bid"))
    explicit_no_ask = _to_cents(pick.get("no_ask"))
    bid = explicit_no_bid if explicit_no_bid is not None else (100 - yes_ask if yes_ask is not None else None)
    ask = explicit_no_ask if explicit_no_ask is not None else (100 - yes_bid if yes_bid is not None else None)

  if bid is not None and ask is not None:
    return bid, ask

  mid = _yes_mid_cents(pick)
  if mid is None:
    return bid, ask

  if side == "yes":
    return (
      bid if bid is not None else max(1, mid - MID_SELL_HAIRCUT_CENTS),
      ask if ask is not None else min(99, mid + MID_BUY_HAIRCUT_CENTS),
    )
  no_mid = max(1, min(99, 100 - mid))
  return (
    bid if bid is not None else max(1, no_mid - MID_SELL_HAIRCUT_CENTS),
    ask if ask is not None else min(99, no_mid + MID_BUY_HAIRCUT_CENTS),
  )


def paper_entry_fill(
  *,
  pick: dict[str, Any],
  side: str,
  remaining_budget_usd: float,
  price_floor_cents: int = DEFAULT_PRICE_FLOOR_CENTS,
  price_ceiling_cents: int = DEFAULT_PRICE_CEILING_CENTS,
  max_spread_cents: int = DEFAULT_MAX_SPREAD_CENTS,
  max_contracts: int = DEFAULT_MAX_CONTRACTS,
) -> dict[str, Any]:
  bid_cents, ask_cents = _side_quotes_cents(pick, side)
  if bid_cents is None or ask_cents is None or ask_cents < bid_cents:
    return {
      "ok": False,
      "price_cents": None,
      "contracts": 0,
      "skip_reason": "no_liquidity",
      "bid_cents": bid_cents,
      "ask_cents": ask_cents,
    }

  if ask_cents < price_floor_cents:
    return {
      "ok": False,
      "price_cents": ask_cents,
      "contracts": 0,
      "skip_reason": "price_floor",
      "bid_cents": bid_cents,
      "ask_cents": ask_cents,
    }
  if ask_cents > price_ceiling_cents:
    return {
      "ok": False,
      "price_cents": ask_cents,
      "contracts": 0,
      "skip_reason": "price_ceiling",
      "bid_cents": bid_cents,
      "ask_cents": ask_cents,
    }

  if (ask_cents - bid_cents) > max_spread_cents:
    return {
      "ok": False,
      "price_cents": ask_cents,
      "contracts": 0,
      "skip_reason": "spread_too_wide",
      "bid_cents": bid_cents,
      "ask_cents": ask_cents,
    }

  if remaining_budget_usd <= 0:
    return {
      "ok": False,
      "price_cents": ask_cents,
      "contracts": 0,
      "skip_reason": "hour_budget_exhausted",
      "bid_cents": bid_cents,
      "ask_cents": ask_cents,
    }

  cost_per_contract = ask_cents / 100.0
  budget_cents = int(round(remaining_budget_usd * 100))
  contracts = budget_cents // ask_cents if ask_cents > 0 else 0
  if contracts <= 0:
    return {
      "ok": False,
      "price_cents": ask_cents,
      "contracts": 0,
      "skip_reason": "budget_too_small_for_contract",
      "bid_cents": bid_cents,
      "ask_cents": ask_cents,
    }
  if contracts > max_contracts:
    return {
      "ok": False,
      "price_cents": ask_cents,
      "contracts": contracts,
      "skip_reason": "contract_cap_exceeded",
      "bid_cents": bid_cents,
      "ask_cents": ask_cents,
    }

  cost_usd = round(contracts * cost_per_contract, 2)
  if cost_usd > round(remaining_budget_usd, 2):
    return {
      "ok": False,
      "price_cents": ask_cents,
      "contracts": contracts,
      "skip_reason": "budget_exceeded",
      "bid_cents": bid_cents,
      "ask_cents": ask_cents,
    }
  return {
    "ok": True,
    "price_cents": ask_cents,
    "contracts": contracts,
    "skip_reason": None,
    "bid_cents": bid_cents,
    "ask_cents": ask_cents,
  }


def entry_quote_log_fields(entry_fill: dict[str, Any]) -> dict[str, int | None]:
  """Persist bid/ask/spread from a paper entry fill for trade-log transparency."""
  bid = entry_fill.get("bid_cents")
  ask = entry_fill.get("ask_cents")
  spread: int | None = None
  if bid is not None and ask is not None:
    spread = int(ask) - int(bid)
  return {
    "entry_bid_cents": int(bid) if bid is not None else None,
    "entry_ask_cents": int(ask) if ask is not None else None,
    "entry_spread_cents": spread,
  }


def format_entry_book_detail(entry_fill: dict[str, Any]) -> str:
  """Human-readable bid/ask/spread snippet for trade log detail lines."""
  fields = entry_quote_log_fields(entry_fill)
  bid = fields.get("entry_bid_cents")
  ask = fields.get("entry_ask_cents")
  spread = fields.get("entry_spread_cents")
  if bid is None and ask is None:
    return ""
  parts = []
  if bid is not None:
    parts.append(f"bid {bid}¢")
  if ask is not None:
    parts.append(f"ask {ask}¢")
  if spread is not None:
    parts.append(f"spread {spread}¢")
  return " · book: " + " / ".join(parts)


def paper_exit_fill(*, pick: dict[str, Any], side: str) -> dict[str, Any]:
  bid_cents, ask_cents = _side_quotes_cents(pick, side)
  if bid_cents is None:
    return {
      "ok": False,
      "price_cents": None,
      "contracts": 0,
      "skip_reason": "no_liquidity",
      "bid_cents": bid_cents,
      "ask_cents": ask_cents,
    }
  return {
    "ok": True,
    "price_cents": bid_cents,
    "contracts": 0,
    "skip_reason": None,
    "bid_cents": bid_cents,
    "ask_cents": ask_cents,
  }
