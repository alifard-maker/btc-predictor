"""Live entry limit pricing: cross the spread when ask-edge is high enough."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from src.trading.entry_strategy import (
  EntryStrategyConfig,
  ask_cents_for_side,
  ask_edge_cents_for_pick,
)
from src.trading.paper_execution import _side_quotes_cents

PassiveLimitAt = Literal["mid", "bid", "bid_plus_one"]


@dataclass(frozen=True)
class LiveEntryPricingConfig:
  cross_spread_enabled: bool = True
  cross_spread_min_edge_cents: float = 12.0
  passive_limit_at: PassiveLimitAt = "mid"
  taker_only: bool = False

  @classmethod
  def from_bot_cfg(cls, bot_cfg: dict[str, Any] | None) -> LiveEntryPricingConfig:
    raw = (bot_cfg or {}).get("live_entry") or {}
    passive = str(raw.get("passive_limit_at", "mid")).strip().lower()
    if passive not in ("mid", "bid", "bid_plus_one"):
      passive = "mid"
    return cls(
      cross_spread_enabled=bool(raw.get("cross_spread_enabled", True)),
      cross_spread_min_edge_cents=float(raw.get("cross_spread_min_edge_cents", 12.0)),
      passive_limit_at=passive,  # type: ignore[arg-type]
      taker_only=bool(raw.get("taker_only", False)),
    )


def live_entry_pricing_from_cfg(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  aggressive: bool = False,
) -> LiveEntryPricingConfig:
  """Resolve live-entry pricing for hourly or slot15 bot sections."""
  if not cfg:
    pricing = LiveEntryPricingConfig()
  elif kind == "slot15":
    pricing = LiveEntryPricingConfig.from_bot_cfg((cfg.get("intra_slot") or {}).get("bot"))
  else:
    from src.backtest.mechanics_profiles import entry_kind_for_bot

    if entry_kind_for_bot(kind) == "hourly":
      pricing = LiveEntryPricingConfig.from_bot_cfg((cfg.get("hourly") or {}).get("bot"))
    else:
      pricing = LiveEntryPricingConfig.from_bot_cfg(cfg.get("bot"))
  if aggressive:
    pricing = replace(pricing, cross_spread_min_edge_cents=10.0)
  else:
    # Passive preset: never cross the spread in live (backtest + live both worse).
    pricing = replace(pricing, cross_spread_enabled=False)
  return pricing


def _mid_cents_for_pick(pick: dict[str, Any], side: str) -> int | None:
  mid = pick.get("kalshi_mid")
  if mid is None:
    mid = pick.get("yes_mid")
  if mid is None:
    prob = pick.get("model_prob")
    if prob is not None:
      mid = float(prob) if side == "yes" else 1.0 - float(prob)
  if mid is None:
    return None
  yes_cents = max(1, min(99, int(round(float(mid) * 100))))
  if side == "yes":
    return yes_cents
  return max(1, min(99, 100 - yes_cents))


def _passive_limit_cents(
  pick: dict[str, Any],
  side: str,
  *,
  passive_limit_at: PassiveLimitAt,
) -> int | None:
  bid, ask = _side_quotes_cents(pick, side)
  if passive_limit_at == "bid":
    if bid is not None:
      return int(bid)
    return _mid_cents_for_pick(pick, side)
  if passive_limit_at == "bid_plus_one":
    if bid is not None:
      cap = int(ask) - 1 if ask is not None else 98
      return max(1, min(99, int(bid) + 1, cap))
    return _mid_cents_for_pick(pick, side)
  return _mid_cents_for_pick(pick, side)


def effective_cross_spread_min_edge_cents(
  pricing: LiveEntryPricingConfig,
  estrat: EntryStrategyConfig,
) -> float:
  """Cross only when ask-edge clears both configured floor and entry gate."""
  floor = float(pricing.cross_spread_min_edge_cents)
  gate = float(estrat.min_ask_edge_cents)
  return max(floor, gate)


def resolve_live_entry_price(
  pick: dict[str, Any],
  side: str,
  *,
  pricing: LiveEntryPricingConfig,
  estrat: EntryStrategyConfig,
) -> dict[str, Any]:
  """
  Choose live limit price for a buy.

  Returns price_cents, execution_mode (cross_spread | passive_limit), bid/ask/spread,
  and ask_edge_cents at decision time.
  """
  bid, ask = _side_quotes_cents(pick, side)
  ask_edge = ask_edge_cents_for_pick(pick, side)
  spread = int(ask) - int(bid) if bid is not None and ask is not None else None

  cross_threshold = effective_cross_spread_min_edge_cents(pricing, estrat)
  if (
    pricing.cross_spread_enabled
    and ask is not None
    and ask_edge is not None
    and ask_edge >= cross_threshold
  ):
    return {
      "price_cents": int(ask),
      "execution_mode": "cross_spread",
      "bid_cents": bid,
      "ask_cents": ask,
      "spread_cents": spread,
      "ask_edge_cents": ask_edge,
      "cross_spread_min_edge_cents": cross_threshold,
    }

  if pricing.taker_only:
    return {
      "price_cents": None,
      "execution_mode": "blocked_taker_only",
      "bid_cents": bid,
      "ask_cents": ask,
      "spread_cents": spread,
      "ask_edge_cents": ask_edge,
      "cross_spread_min_edge_cents": cross_threshold,
    }

  passive = _passive_limit_cents(pick, side, passive_limit_at=pricing.passive_limit_at)
  return {
    "price_cents": passive,
    "execution_mode": "passive_limit",
    "bid_cents": bid,
    "ask_cents": ask,
    "spread_cents": spread,
    "ask_edge_cents": ask_edge,
    "cross_spread_min_edge_cents": cross_threshold,
  }


def format_live_entry_execution_detail(resolved: dict[str, Any]) -> str:
  """Short snippet for trade log detail lines."""
  mode = str(resolved.get("execution_mode") or "passive_limit")
  bid = resolved.get("bid_cents")
  ask = resolved.get("ask_cents")
  spread = resolved.get("spread_cents")
  edge = resolved.get("ask_edge_cents")
  parts = [f"entry={mode}"]
  if bid is not None and ask is not None:
    parts.append(f"bid/ask={int(bid)}/{int(ask)}¢")
  if spread is not None:
    parts.append(f"spread={int(spread)}¢")
  if edge is not None:
    parts.append(f"ask_edge={float(edge):.0f}¢")
  thresh = resolved.get("cross_spread_min_edge_cents")
  if mode == "cross_spread" and thresh is not None:
    parts.append(f"cross≥{float(thresh):.0f}¢")
  return " · ".join(parts)
