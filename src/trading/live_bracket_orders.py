"""Resting limit sells on Kalshi for live cheap-leg protection (software bracket)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.trading.bot_profit_exit import CheapLegExitConfig, cheap_leg_exit_config

log = logging.getLogger(__name__)


@dataclass
class LiveRestingExitConfig:
  enabled: bool = False
  cheap_leg_only: bool = True
  bracket_take_profit: bool = True


def live_resting_exit_config(cfg: dict[str, Any] | None) -> LiveRestingExitConfig:
  raw = (cfg or {}).get("live_resting_exits") or {}
  return LiveRestingExitConfig(
    enabled=bool(raw.get("enabled", False)),
    cheap_leg_only=bool(raw.get("cheap_leg_only", True)),
    bracket_take_profit=bool(raw.get("bracket_take_profit", True)),
  )


def aggressive_exit_limit_cents(bid_cents: int, *, haircut: int = 2) -> int:
  """Price live exit limits slightly through the bid to improve fill odds."""
  return max(1, min(99, int(bid_cents) - max(0, int(haircut))))


def live_exit_haircut_cents(cfg: dict[str, Any] | None = None) -> int:
  """IOC exit haircut (¢ through bid). Must match LEG STOP decision math on live."""
  default = 4
  if not isinstance(cfg, dict):
    return default
  candidates = (
    ((cfg.get("hourly") or {}).get("bot") or {}).get("live_exit"),
    (((cfg.get("eth") or {}).get("hourly") or {}).get("bot") or {}).get("live_exit"),
    (((cfg.get("eth") or {}).get("hourly_live") or {}).get("bot") or {}).get("live_exit"),
    ((cfg.get("intra_slot") or {}).get("bot") or {}).get("live_exit"),
  )
  for node in candidates:
    if isinstance(node, dict) and "aggressive_exit_haircut_cents" in node:
      return max(0, int(node.get("aggressive_exit_haircut_cents") or 0))
  return default


def bracket_take_profit_cents(
  entry_cents: int,
  *,
  take_profit_pct: float,
  min_take_profit_pct: float,
  max_take_profit_pct: float,
) -> int:
  pct = max(min_take_profit_pct, min(max_take_profit_pct, take_profit_pct))
  target = int(round(entry_cents * (1.0 + pct)))
  return max(entry_cents + 1, min(99, target))


def should_place_resting_exits(
  *,
  entry_cents: int,
  cheap_cfg: CheapLegExitConfig,
  resting_cfg: LiveRestingExitConfig,
) -> bool:
  if not resting_cfg.enabled:
    return False
  if resting_cfg.cheap_leg_only:
    return entry_cents > 0 and entry_cents <= cheap_cfg.max_entry_cents
  return True


def place_live_bracket_orders(
  kalshi: Any,
  *,
  market_ticker: str,
  side: str,
  contracts: int,
  entry_cents: int,
  cheap_cfg: CheapLegExitConfig,
  resting_cfg: LiveRestingExitConfig,
  take_profit_pct: float,
  min_take_profit_pct: float,
  max_take_profit_pct: float,
) -> dict[str, str | None]:
  """Place resting sell limits: stop floor at cut_loss_cents; optional TP limit.

  Kalshi has no native bracket orders — this is the Bot-for-Kalshi-style software
  bracket implemented as paired resting limit sells after entry fills.
  """
  out: dict[str, str | None] = {"stop_order_id": None, "take_profit_order_id": None}
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return out
  if not should_place_resting_exits(
    entry_cents=entry_cents,
    cheap_cfg=cheap_cfg,
    resting_cfg=resting_cfg,
  ):
    return out

  stop_cents = max(1, int(cheap_cfg.cut_loss_cents))
  try:
    stop_order = kalshi.create_order(
      ticker=market_ticker,
      side=side,
      count=contracts,
      action="sell",
      yes_price=stop_cents if side == "yes" else None,
      no_price=stop_cents if side == "no" else None,
    )
    out["stop_order_id"] = _order_id(stop_order)
    log.info(
      "Live resting stop sell %s ×%s @ %s¢ on %s (order %s)",
      side.upper(),
      contracts,
      stop_cents,
      market_ticker,
      out["stop_order_id"],
    )
  except Exception as e:
    log.warning("Live resting stop order failed: %s", e)

  if resting_cfg.bracket_take_profit:
    tp_cents = bracket_take_profit_cents(
      entry_cents,
      take_profit_pct=take_profit_pct,
      min_take_profit_pct=min_take_profit_pct,
      max_take_profit_pct=max_take_profit_pct,
    )
    if tp_cents > stop_cents:
      try:
        tp_order = kalshi.create_order(
          ticker=market_ticker,
          side=side,
          count=contracts,
          action="sell",
          yes_price=tp_cents if side == "yes" else None,
          no_price=tp_cents if side == "no" else None,
        )
        out["take_profit_order_id"] = _order_id(tp_order)
        log.info(
          "Live resting TP sell %s ×%s @ %s¢ on %s (order %s)",
          side.upper(),
          contracts,
          tp_cents,
          market_ticker,
          out["take_profit_order_id"],
        )
      except Exception as e:
        log.warning("Live resting take-profit order failed: %s", e)

  return out


def cancel_resting_orders(kalshi: Any, pos: dict[str, Any]) -> None:
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return
  for key in ("stop_order_id", "take_profit_order_id"):
    oid = pos.get(key)
    if not oid:
      continue
    try:
      kalshi.cancel_order(str(oid))
      log.info("Cancelled resting order %s", oid)
    except Exception as e:
      log.warning("Cancel resting order %s failed: %s", oid, e)


def cancel_resting_orders_for_ticker(kalshi: Any, market_ticker: str) -> int:
  """Cancel all Kalshi resting orders on a market (dedupe before enter/exit)."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return 0
  cancel_fn = getattr(kalshi, "cancel_resting_orders_for_ticker", None)
  if not callable(cancel_fn):
    return 0
  n = int(cancel_fn(str(market_ticker)) or 0)
  if n:
    log.info("Cancelled %s resting order(s) on %s", n, market_ticker)
  return n


def place_live_exit_sell(
  kalshi: Any,
  *,
  market_ticker: str,
  side: str,
  contracts: int,
  limit_cents: int,
  time_in_force: str = "immediate_or_cancel",
) -> dict[str, Any]:
  """Marketable limit sell when software exit fires in live mode."""
  empty: dict[str, Any] = {"order_id": None, "fill_count": 0, "remaining_count": 0}
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return empty
  price = max(1, min(99, int(limit_cents)))
  try:
    order = kalshi.create_order(
      ticker=market_ticker,
      side=side,
      count=contracts,
      action="sell",
      yes_price=price if side == "yes" else None,
      no_price=price if side == "no" else None,
      time_in_force=time_in_force,
    )
    from src.data.kalshi import parse_v2_order_response

    parsed = parse_v2_order_response(order)
    oid = parsed["order_id"]
    fill_count = int(parsed["fill_count"])
    log.info(
      "Live EXIT sell %s ×%s @ %s¢ order %s (%s filled)",
      side.upper(), contracts, price, oid, fill_count,
    )
    return {
      "order_id": oid,
      "fill_count": fill_count,
      "remaining_count": int(parsed["remaining_count"]),
    }
  except Exception as e:
    log.warning("Live exit sell failed: %s", e)
    return empty


def resting_config_for_kind(cfg: dict[str, Any] | None, *, kind: str) -> tuple[CheapLegExitConfig, LiveRestingExitConfig]:
  return cheap_leg_exit_config(cfg, kind=kind), live_resting_exit_config(cfg)


def _order_id(order: dict[str, Any]) -> str | None:
  return (order.get("order") or order).get("order_id") or order.get("order_id")
