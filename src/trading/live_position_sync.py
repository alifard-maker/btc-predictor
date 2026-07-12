"""Kalshi inventory checks and live exit hygiene (no naked resting sells)."""

from __future__ import annotations

import logging
import time
from typing import Any

from src.trading.bot_position_mode import normalize_position_mode
from src.data.kalshi import kalshi_order_is_executed, position_net_from_row
from src.trading.live_bracket_orders import (
  aggressive_exit_limit_cents,
  cancel_resting_orders,
  cancel_resting_orders_for_ticker,
  place_live_exit_sell,
)

log = logging.getLogger(__name__)


def _position_contracts(pos: dict[str, Any]) -> float:
  fp = pos.get("contracts_fp")
  if fp is not None:
    try:
      return float(fp)
    except (TypeError, ValueError):
      pass
  return float(int(pos.get("contracts") or 0))


def kalshi_contracts_for_adoption(
  sellable: float | None,
  snap: dict[str, Any] | None,
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  adoption_source: str = "orphan",
  market_ticker: str | None = None,
) -> tuple[int, float, float]:
  """Kalshi sellable inventory at adoption (same source as exit refresh), then cap."""
  from src.trading.bot_live_exit import AdoptionSource, cap_adopted_contracts
  from src.trading.live_range_guards import is_range_market_ticker

  snap_ct = float((snap or {}).get("contracts") or 0)
  if sellable is not None and sellable >= 0.05:
    raw_fp = round(float(sellable), 2)
  elif snap_ct >= 0.05:
    raw_fp = round(snap_ct, 2)
  else:
    return 0, 0.0, 0.0
  if snap_ct >= 0.05 and abs(raw_fp - snap_ct) > 0.04:
    log.info(
      "Adoption contract sync: sellable=%s snap=%s — using sellable",
      raw_fp,
      snap_ct,
    )
  src: AdoptionSource = adoption_source  # type: ignore[assignment]
  if adoption_source not in ("resting_fill", "orphan", "failed_exit_restore"):
    src = "orphan"
  ticker = market_ticker or str((snap or {}).get("market_ticker") or "")
  contracts, contracts_fp = cap_adopted_contracts(
    raw_fp,
    cfg,
    kind=kind,
    adoption_source=src,
    is_range=is_range_market_ticker(ticker),
  )
  return contracts, contracts_fp, raw_fp


def refresh_live_leg_contracts_from_kalshi(
  pos: dict[str, Any],
  kalshi: Any,
  store: Any,
) -> dict[str, Any]:
  """Align bot leg size with Kalshi inventory before exit P&L / profit checks."""
  if normalize_position_mode(pos.get("mode")) != "live":
    return pos
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return pos
  ticker = str(pos.get("market_ticker") or "")
  side = str(pos.get("side") or "").lower()
  if not ticker or side not in ("yes", "no"):
    return pos
  sellable = kalshi_sellable_contracts(kalshi, ticker, side, critical=True)
  if sellable is None:
    return pos
  bot_ct = _position_contracts(pos)
  if sellable <= bot_ct + 0.04:
    return pos
  mismatch = {
    "bot_contracts": round(bot_ct, 2),
    "kalshi_contracts": round(float(sellable), 2),
    "delta": round(float(sellable) - bot_ct, 2),
    "ticker": ticker,
    "side": side,
  }
  log.warning(
    "Contract mismatch before exit refresh: %s %s bot=%s kalshi=%s — syncing up",
    ticker,
    side.upper(),
    bot_ct,
    sellable,
  )
  entry_c = int(pos.get("entry_price_cents") or 0)
  if entry_c <= 0:
    snap = kalshi_position_leg(kalshi, ticker, side, critical=True)
    if snap and snap.get("entry_price_cents"):
      entry_c = int(snap["entry_price_cents"])
  contracts_fp = round(float(sellable), 2)
  contracts = max(1, int(round(contracts_fp)))
  cost_usd = (
    round(contracts_fp * entry_c / 100.0, 2)
    if entry_c > 0
    else float(pos.get("cost_usd") or 0)
  )
  store.update_position_contracts(
    str(pos["id"]),
    contracts=contracts,
    contracts_fp=contracts_fp,
    cost_usd=cost_usd,
    entry_price_cents=entry_c if entry_c > 0 else None,
  )
  updated = dict(pos)
  updated["contracts"] = contracts
  updated["contracts_fp"] = contracts_fp
  updated["cost_usd"] = cost_usd
  updated["contract_mismatch"] = mismatch
  if entry_c > 0:
    updated["entry_price_cents"] = entry_c
  return updated


def kalshi_position_leg(
  kalshi: Any,
  market_ticker: str,
  side: str,
  *,
  critical: bool = False,
) -> dict[str, Any] | None:
  """Contracts, cost, and avg entry from Kalshi market_positions row."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return None
  side_l = str(side or "").lower()
  ticker = str(market_ticker)
  for row in kalshi.list_market_positions(critical=critical):
    if str(row.get("ticker") or "") != ticker:
      continue
    net = position_net_from_row(row)
    contracts = max(0.0, float(net)) if side_l == "yes" else max(0.0, -float(net))
    if contracts < 0.05:
      return None
    exposure = row.get("market_exposure_dollars")
    if exposure is None:
      exposure = row.get("market_exposure")
    try:
      cost_usd = abs(float(exposure or 0))
    except (TypeError, ValueError):
      cost_usd = 0.0
    entry_cents: int | None = None
    if cost_usd > 0 and contracts > 0:
      entry_cents = max(1, min(99, int(round(100.0 * cost_usd / contracts))))
    return {
      "contracts": round(contracts, 2),
      "cost_usd": round(cost_usd, 2) if cost_usd > 0 else None,
      "entry_price_cents": entry_cents,
    }
  sellable = kalshi_sellable_contracts(kalshi, ticker, side_l, critical=critical)
  if sellable is None or sellable < 0.05:
    return None
  return {"contracts": round(float(sellable), 2), "cost_usd": None, "entry_price_cents": None}


def kalshi_sellable_contracts(
  kalshi: Any,
  market_ticker: str,
  side: str,
  *,
  critical: bool = False,
) -> float | None:
  """Contracts held on Kalshi for this leg; None when the API is unavailable."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return None
  side_l = str(side or "").lower()
  ticker = str(market_ticker)
  for row in kalshi.list_market_positions(critical=critical):
    if str(row.get("ticker") or "") != ticker:
      continue
    net = position_net_from_row(row)
    if side_l == "yes":
      return max(0.0, float(net))
    return max(0.0, -float(net))
  net = kalshi.get_market_position(ticker, critical=critical)
  if net is None:
    return None
  if side_l == "yes":
    return max(0.0, float(net))
  return max(0.0, -float(net))


def resting_exit_order_id(store: Any, position_id: str) -> str | None:
  """Most recent resting live exit order for a bot position."""
  with store._connect() as conn:
    row = conn.execute(
      """
      SELECT kalshi_order_id FROM bot_trades
      WHERE position_id = ? AND action = 'exit' AND status = 'resting'
        AND kalshi_order_id IS NOT NULL AND kalshi_order_id != ''
      ORDER BY created_at DESC LIMIT 1
      """,
      (position_id,),
    ).fetchone()
  if not row or not row[0]:
    return None
  return str(row[0])


def order_still_resting(kalshi: Any, order_id: str) -> bool:
  if not kalshi or not order_id:
    return False
  for row in kalshi.list_resting_orders():
    if str(row.get("order_id") or "") == str(order_id):
      return True
  return False


def cancel_orphan_live_sell_orders(kalshi: Any, allowed_tickers: set[str]) -> int:
  """Cancel resting sells on markets where the bot has no open live leg."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return 0
  cancelled = 0
  allowed = {str(t) for t in allowed_tickers}
  for row in kalshi.list_resting_orders():
    if str(row.get("action") or "").lower() != "sell":
      continue
    ticker = str(row.get("ticker") or "")
    if not ticker or ticker in allowed:
      continue
    oid = row.get("order_id")
    if not oid:
      continue
    try:
      kalshi.cancel_order(str(oid))
      cancelled += 1
      log.info("Cancelled orphan live sell %s on %s", oid, ticker)
    except Exception as e:
      log.warning("Cancel orphan sell %s on %s failed: %s", oid, ticker, e)
  return cancelled


def live_open_tickers(store: Any, period_key: str) -> set[str]:
  tickers: set[str] = set()
  for pos in store.open_positions(period_key):
    if normalize_position_mode(pos.get("mode")) == "live":
      tickers.add(str(pos["market_ticker"]))
  return tickers


def hourly_event_market_tickers_from_tab(tab: dict[str, Any]) -> set[str]:
  """Market tickers for the current hourly event (from live prediction tab)."""
  live = tab.get("live") or tab
  tickers: set[str] = set()

  def add(pick: dict[str, Any] | None) -> None:
    if pick and pick.get("ticker"):
      tickers.add(str(pick["ticker"]))

  add(live.get("primary_pick"))
  for block_key in ("strategy_threshold", "strategy_range"):
    block = live.get(block_key) or {}
    add(block.get("best_edge"))
    add(block.get("most_likely"))
    for row in block.get("contracts") or []:
      add(row)
  return tickers


def _ticker_in_hourly_event(ticker: str, event_ticker: str, allowed_tickers: set[str]) -> bool:
  t = str(ticker)
  e = str(event_ticker)
  if t in allowed_tickers:
    return True
  return t == e or t.startswith(f"{e}-")


def reconcile_stale_resting_enters(store: Any, kalshi: Any) -> dict[str, Any]:
  """Mark resting enter rows cancelled when Kalshi order is gone and no inventory."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {"ok": True, "cancelled": 0}
  cancel_fn = getattr(store, "cancel_resting_enter_rows", None)
  if not callable(cancel_fn):
    return {"ok": True, "cancelled": 0}

  cancelled = 0
  with store._connect() as conn:
    rows = conn.execute(
      """
      SELECT id, kalshi_order_id, market_ticker, side
      FROM bot_trades
      WHERE action = 'enter' AND status = 'resting' AND mode = 'live'
      ORDER BY created_at DESC
      LIMIT 100
      """,
    ).fetchall()

  for raw in rows:
    trade = dict(raw)
    oid = str(trade.get("kalshi_order_id") or "")
    ticker = str(trade.get("market_ticker") or "")
    side = str(trade.get("side") or "").lower()
    if oid and order_still_resting(kalshi, oid):
      continue
    if ticker and side in ("yes", "no"):
      sellable = kalshi_sellable_contracts(kalshi, ticker, side)
      if sellable is not None and sellable >= 0.05:
        continue
    n = cancel_fn(
      kalshi_order_id=oid or None,
      market_ticker=ticker or None,
      mode="live",
      reason="order gone unfilled",
    )
    cancelled += n
  return {"ok": True, "cancelled": cancelled}


def cancel_resting_enter_orders_for_hourly_event(
  kalshi: Any,
  event_ticker: str,
  tab: dict[str, Any],
  store: Any | None = None,
) -> int:
  """Cancel unfilled resting BUY orders on the current hourly event only."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return 0
  allowed = hourly_event_market_tickers_from_tab(tab)
  cancelled = 0
  for row in kalshi.list_resting_orders():
    if str(row.get("action") or "").lower() != "buy":
      continue
    ticker = str(row.get("ticker") or "")
    if not ticker or not _ticker_in_hourly_event(ticker, event_ticker, allowed):
      continue
    oid = row.get("order_id")
    if not oid:
      continue
    try:
      kalshi.cancel_order(str(oid))
      cancelled += 1
      if store is not None and hasattr(store, "cancel_resting_enter_rows"):
        store.cancel_resting_enter_rows(
          event_ticker=event_ticker,
          market_ticker=ticker,
          kalshi_order_id=str(oid),
          mode="live",
          reason="cancelled on Kalshi",
        )
      log.info("Cancelled resting enter %s on %s (event %s)", oid, ticker, event_ticker)
    except Exception as e:
      log.warning("Cancel resting enter %s on %s failed: %s", oid, ticker, e)
  if cancelled:
    log.info("Cancelled %s resting enter order(s) for hourly event %s", cancelled, event_ticker)
  return cancelled


def resting_sell_contracts(kalshi: Any, market_ticker: str, side: str) -> float:
  """Contracts tied up in unfilled resting sells for one leg."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return 0.0
  total = 0.0
  side_l = str(side or "").lower()
  ticker = str(market_ticker)
  for row in kalshi.list_resting_orders():
    if str(row.get("action") or "").lower() != "sell":
      continue
    if str(row.get("ticker") or "") != ticker:
      continue
    if str(row.get("side") or "").lower() != side_l:
      continue
    rem = row.get("remaining_count")
    if rem is None:
      rem = row.get("count")
    try:
      total += max(0.0, float(rem or 0))
    except (TypeError, ValueError):
      continue
  return total


def effective_kalshi_inventory(kalshi: Any, market_ticker: str, side: str) -> float | None:
  """Sellable inventory plus contracts in resting exit sells (API may report 0 while sell rests)."""
  sellable = kalshi_sellable_contracts(kalshi, market_ticker, side)
  if sellable is None:
    return None
  resting = resting_sell_contracts(kalshi, market_ticker, side)
  return max(float(sellable), float(resting))


def has_pending_bot_exit(kalshi: Any, store: Any, position_id: str) -> bool:
  pending_oid = resting_exit_order_id(store, position_id)
  return bool(pending_oid and order_still_resting(kalshi, pending_oid))


def should_reconcile_close_live_leg(
  kalshi: Any,
  store: Any,
  pos: dict[str, Any],
  *,
  cfg: dict[str, Any] | None = None,
  kind: str = "hourly",
) -> bool:
  """Only reconcile-close when Kalshi is truly flat (no position and no resting exit)."""
  if has_pending_bot_exit(kalshi, store, str(pos["id"])):
    return False
  ticker = str(pos["market_ticker"])
  side = str(pos["side"])
  if resting_sell_contracts(kalshi, ticker, side) > 0.05:
    return False
  sellable = kalshi_sellable_contracts(kalshi, ticker, side)
  if sellable is None or sellable > 0:
    return False
  if cfg is not None:
    from src.trading.bot_live_exit import reconcile_close_blocked

    if reconcile_close_blocked(store, pos, cfg, kind=kind) is not None:
      return False
  return True


def _filled_enter_kalshi_order_id(store: Any, position_id: str) -> str | None:
  """Kalshi order id from the bot's filled enter for this position."""
  with store._connect() as conn:
    row = conn.execute(
      """
      SELECT kalshi_order_id FROM bot_trades
      WHERE position_id = ? AND action = 'enter' AND status = 'filled'
        AND kalshi_order_id IS NOT NULL AND kalshi_order_id != ''
      ORDER BY created_at DESC LIMIT 1
      """,
      (position_id,),
    ).fetchone()
  if not row or not row[0]:
    return None
  return str(row[0])


def _inferred_exit_price_cents(store: Any, position_id: str) -> int | None:
  """Best-effort exit price from a recent resting/filled exit row for this leg."""
  with store._connect() as conn:
    row = conn.execute(
      """
      SELECT exit_price_cents, price_cents FROM bot_trades
      WHERE position_id = ? AND action = 'exit'
        AND status IN ('resting', 'filled')
        AND COALESCE(exit_price_cents, price_cents) IS NOT NULL
      ORDER BY created_at DESC LIMIT 1
      """,
      (position_id,),
    ).fetchone()
  if not row:
    return None
  val = row[0] if row[0] is not None else row[1]
  try:
    return int(val)
  except (TypeError, ValueError):
    return None


def reconcile_close_stale_live_leg(
  *,
  store: Any,
  pos: dict[str, Any],
  period_key: str,
  pick: dict[str, Any] | None = None,
  exit_reason: str = "RECONCILED",
  extra_detail: str = "",
  kalshi: Any | None = None,
  cfg: dict[str, Any] | None = None,
  kind: str = "hourly",
) -> dict[str, Any]:
  """Close a bot leg when Kalshi has no inventory (settled, sold elsewhere, or already flat)."""
  from src.trading.bot_live_exit import (
    inferred_exit_from_recent_trade,
    live_exit_config,
    recent_exit_trade,
  )
  from src.trading.kalshi_leg_exit import (
    avg_sell_fill_cents,
    market_binary_exit_cents,
    sum_verified_kalshi_buy_contracts,
  )
  from src.trading.paper_execution import leg_pnl_usd

  ticker = str(pos["market_ticker"])
  side = str(pos["side"])
  contracts = int(pos.get("contracts") or 0)
  entry_c = int(pos.get("entry_price_cents") or 0)
  pos_mode = normalize_position_mode(pos.get("mode"))
  pick = pick or {}
  inferred = _inferred_exit_price_cents(store, str(pos["id"]))
  if inferred is None:
    live_exit = live_exit_config(cfg, kind=kind)
    recent = recent_exit_trade(
      store,
      event_ticker=period_key,
      market_ticker=ticker,
      side=side,
      position_id=str(pos.get("id") or ""),
      max_age_seconds=live_exit.reconcile_grace_after_exit_seconds,
    )
    inferred = inferred_exit_from_recent_trade(recent)

  sell_fill_c = (
    avg_sell_fill_cents(
      kalshi,
      market_ticker=ticker,
      side=side,
      max_contracts=contracts,
    )
    if kalshi
    else None
  )
  allow_pnl = False
  exit_c: int | None = None
  source_note = ""
  if sell_fill_c is not None:
    exit_c = sell_fill_c
    source_note = f"Kalshi sell fills avg @ {sell_fill_c}¢"
    allow_pnl = True
  else:
    enter_oid = _filled_enter_kalshi_order_id(store, str(pos["id"]))
    min_contracts = max(0.5, float(contracts) * 0.85)
    verified_ct = 0.0
    if kalshi:
      if enter_oid:
        verified_ct = sum_verified_kalshi_buy_contracts(
          kalshi,
          market_ticker=ticker,
          side=side,
          kalshi_order_id=enter_oid,
        )
      if verified_ct < min_contracts:
        verified_ct = sum_verified_kalshi_buy_contracts(
          kalshi,
          market_ticker=ticker,
          side=side,
        )
    verified_entry = verified_ct >= min_contracts
    settle_c, settle_note = (
      market_binary_exit_cents(kalshi, market_ticker=ticker, side=side, pos=pos)
      if kalshi
      else (None, "")
    )
    if settle_c is not None and verified_entry:
      exit_c = settle_c
      source_note = settle_note
      allow_pnl = True
    elif inferred is not None:
      exit_c = int(inferred)
      source_note = f"inferred exit @ {exit_c}¢"
      allow_pnl = True
    elif settle_c is not None:
      exit_c = settle_c
      source_note = f"{settle_note} (unverified Kalshi entry — P&L not booked)"
      allow_pnl = False

  pnl_rounded = 0.0
  if allow_pnl and exit_c is not None and entry_c and contracts:
    pnl_rounded = round(
      float(
        leg_pnl_usd(
          entry_price_cents=entry_c,
          mark_or_exit_cents=exit_c,
          contracts=contracts,
        )
        or 0.0,
      ),
      2,
    )
  store.close_position(str(pos["id"]))
  detail = "Live EXIT reconciled"
  if exit_c is not None:
    detail += f" @ {exit_c}¢"
    if allow_pnl:
      if pnl_rounded < -0.005:
        detail += f" (loss ${abs(pnl_rounded):.2f})"
      elif pnl_rounded > 0.005:
        detail += f" (profit ${pnl_rounded:.2f})"
    else:
      detail += " (P&L not booked — no verified Kalshi entry on this side)"
  elif source_note:
    detail += " (exit price unknown)"
  detail += (
    f" (no Kalshi inventory for {side.upper()} on {ticker}) — "
    f"closed bot leg"
  )
  if source_note:
    detail += f" · {source_note}"
  if extra_detail:
    detail += f" · {extra_detail}"
  log.info(
    "Reconciled closed stale live leg %s %s x%s on %s",
    side.upper(),
    ticker,
    contracts,
    period_key,
  )
  row = store.log_trade({
    "event_ticker": period_key,
    "trigger": "continuous",
    "action": "exit",
    "mode": pos_mode,
    "market_ticker": ticker,
    "side": side,
    "contracts": contracts,
    "price_cents": exit_c if exit_c is not None else entry_c,
    "entry_price_cents": entry_c,
    "exit_price_cents": exit_c,
    "cost_usd": 0,
    "pnl_usd": pnl_rounded,
    "signal": pick.get("signal"),
    "label": pos.get("label"),
    "status": "reconciled",
    "detail": detail,
    "position_id": pos["id"],
  })
  if allow_pnl and pos_mode == "live" and abs(pnl_rounded) >= 0.005:
    from src.trading.bot_risk_gates import record_exit_and_maybe_cap

    asset = str((cfg or {}).get("_asset") or "btc")
    record_exit_and_maybe_cap(
      pnl_rounded,
      kind=kind,
      asset=asset,
      store=store,
      cfg=cfg,
    )
  return row


def purge_foreign_asset_open_positions(
  store: Any,
  kalshi: Any,
  *,
  asset: str,
  cfg: dict[str, Any] | None = None,
  kind: str = "hourly",
) -> dict[str, Any]:
  """Close open live legs that belong to the other asset (e.g. BTC tickers in ETH store)."""
  from src.trading.hourly_event_time import hourly_asset_for_ticker, market_ticker_event_ticker

  if not hasattr(store, "all_open_live_positions"):
    return {"ok": True, "changes": []}
  target = str(asset).lower()
  changes: list[dict[str, Any]] = []
  for pos in store.all_open_live_positions():
    if normalize_position_mode(pos.get("mode")) != "live":
      continue
    ticker = str(pos.get("market_ticker") or "")
    leg_asset = hourly_asset_for_ticker(ticker)
    if leg_asset is None or leg_asset == target:
      continue
    period_key = str(pos.get("event_ticker") or market_ticker_event_ticker(ticker))
    reconcile_close_stale_live_leg(
      store=store,
      pos=pos,
      period_key=period_key,
      kalshi=kalshi,
      cfg=cfg,
      kind=kind,
      extra_detail=f"purged foreign-{leg_asset} leg from {target} bot store",
    )
    changes.append({
      "action": "purged_foreign_asset",
      "ticker": ticker,
      "foreign_asset": leg_asset,
      "position_id": str(pos.get("id") or ""),
    })
  if changes:
    log.info("Purged %s foreign-asset phantom leg(s) from %s hourly store", len(changes), target)
  return {"ok": True, "changes": changes}


def sync_live_positions_from_kalshi(
  store: Any,
  kalshi: Any,
  event_ticker: str,
  *,
  cfg: dict[str, Any] | None = None,
  kind: str = "hourly",
) -> dict[str, Any]:
  """Align open live bot legs with Kalshi inventory (contracts + merge duplicates)."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {"ok": True, "changes": []}

  if hasattr(store, "all_open_live_positions"):
    open_live = store.all_open_live_positions()
  else:
    open_live = store.open_positions(event_ticker)
  groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
  for pos in open_live:
    if normalize_position_mode(pos.get("mode")) != "live":
      continue
    key = (str(pos["market_ticker"]), str(pos.get("side") or "").lower())
    groups.setdefault(key, []).append(pos)

  changes: list[dict[str, Any]] = []
  for (ticker, side), legs in groups.items():
    snap = kalshi_position_leg(kalshi, ticker, side)
    if snap is None:
      sellable = effective_kalshi_inventory(kalshi, ticker, side)
      if sellable is None:
        continue
      target_fp = round(float(sellable), 2)
      cost_usd = None
      entry_cents = None
    else:
      target_fp = float(snap["contracts"])
      cost_usd = snap.get("cost_usd")
      entry_cents = snap.get("entry_price_cents")

    bot_total = sum(_position_contracts(p) for p in legs)
    if abs(target_fp - bot_total) < 0.05:
      primary = legs[0]
      needs_entry = (
        entry_cents is not None
        and int(primary.get("entry_price_cents") or 0) != int(entry_cents)
      )
      needs_cost = (
        cost_usd is not None
        and abs(float(primary.get("cost_usd") or 0) - float(cost_usd)) > 0.02
      )
      if not needs_entry and not needs_cost:
        continue

    if target_fp <= 0:
      for pos in legs:
        if not should_reconcile_close_live_leg(kalshi, store, pos, cfg=cfg, kind=kind):
          changes.append({
            "ticker": ticker,
            "side": side,
            "action": "inventory_pending_exit",
            "position_id": str(pos["id"]),
            "bot_contracts": int(pos.get("contracts") or 0),
          })
          continue
        reconcile_close_stale_live_leg(
          store=store,
          pos=pos,
          period_key=str(pos.get("event_ticker") or event_ticker),
          kalshi=kalshi,
          cfg=cfg,
          kind=kind,
        )
        changes.append({
          "ticker": ticker,
          "side": side,
          "action": "reconciled_closed",
          "position_id": str(pos["id"]),
          "bot_contracts": int(pos.get("contracts") or 0),
        })
      continue

    legs.sort(key=lambda p: str(p.get("opened_at") or ""))
    primary = legs[0]
    entry_c = int(entry_cents or primary.get("entry_price_cents") or 0)
    if cost_usd is not None and float(cost_usd) > 0:
      new_cost = round(float(cost_usd), 2)
    elif entry_c:
      new_cost = round(target_fp * entry_c / 100.0, 2)
    else:
      new_cost = float(primary.get("cost_usd") or 0)
    store.update_position_contracts(
      str(primary["id"]),
      contracts=max(1, int(round(target_fp))),
      contracts_fp=target_fp,
      cost_usd=new_cost,
      entry_price_cents=entry_c if entry_c else None,
    )
    changes.append({
      "ticker": ticker,
      "side": side,
      "action": "synced",
      "from_contracts": bot_total,
      "to_contracts": target_fp,
      "position_id": str(primary["id"]),
    })
    for extra in legs[1:]:
      store.close_position(str(extra["id"]))
      changes.append({
        "ticker": ticker,
        "side": side,
        "action": "merged_duplicate",
        "position_id": str(extra["id"]),
      })

  return {"ok": True, "changes": changes}


def _has_open_live_leg(store: Any, event_ticker: str, market_ticker: str, side: str) -> bool:
  side_l = str(side or "").lower()
  for pos in store.open_positions(event_ticker):
    if normalize_position_mode(pos.get("mode")) != "live":
      continue
    if str(pos.get("market_ticker") or "") != str(market_ticker):
      continue
    if str(pos.get("side") or "").lower() == side_l:
      return True
  return False


def _has_open_live_leg_for_ticker(store: Any, market_ticker: str, side: str) -> bool:
  """True when any open live leg exists for this market ticker (any hour)."""
  side_l = str(side or "").lower()
  with store._connect() as conn:
    row = conn.execute(
      """
      SELECT 1 FROM bot_positions
      WHERE market_ticker = ? AND lower(side) = ? AND status = 'open' AND mode = 'live'
      LIMIT 1
      """,
      (str(market_ticker), side_l),
    ).fetchone()
  return row is not None


def adopt_filled_resting_enters(
  store: Any,
  kalshi: Any,
  event_ticker: str,
  *,
  cfg: dict[str, Any] | None = None,
  kind: str = "hourly",
  critical: bool = False,
) -> dict[str, Any]:
  """Open bot legs when a resting live enter filled on Kalshi but was never recorded.

  Scans all resting live enters (not only the current hour) so fills that land
  after hour rollover are still adopted.
  """
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {"ok": True, "changes": []}

  from src.trading.hourly_event_time import market_ticker_event_ticker

  changes: list[dict[str, Any]] = []
  with store._connect() as conn:
    rows = conn.execute(
      """
      SELECT * FROM bot_trades
      WHERE action = 'enter' AND status = 'resting' AND mode = 'live'
      ORDER BY created_at DESC
      LIMIT 100
      """,
    ).fetchall()

  seen: set[tuple[str, str]] = set()
  for raw in rows:
    trade = dict(raw)
    ticker = str(trade.get("market_ticker") or "")
    side = str(trade.get("side") or "").lower()
    if not ticker or side not in ("yes", "no"):
      continue
    key = (ticker, side)
    if key in seen:
      continue
    seen.add(key)
    leg_event = market_ticker_event_ticker(ticker) or str(trade.get("event_ticker") or event_ticker)
    if _has_open_live_leg_for_ticker(store, ticker, side):
      continue
    sellable = kalshi_sellable_contracts(kalshi, ticker, side, critical=critical)
    if sellable is None or sellable < 0.05:
      continue
    snap = kalshi_position_leg(kalshi, ticker, side, critical=critical) or {
      "contracts": round(float(sellable), 2),
      "cost_usd": None,
      "entry_price_cents": int(trade.get("entry_price_cents") or trade.get("price_cents") or 0),
    }
    contracts, contracts_fp, _raw_fp = kalshi_contracts_for_adoption(
      sellable, snap, cfg, kind=kind, adoption_source="resting_fill", market_ticker=ticker,
    )
    from src.trading.live_range_guards import apply_range_adoption_hour_cap

    contracts, contracts_fp = apply_range_adoption_hour_cap(
      contracts,
      contracts_fp,
      store=store,
      event_ticker=leg_event,
      market_ticker=ticker,
      side=side,
      cfg=cfg,
      kind=kind,
    )
    if contracts_fp < 0.05:
      continue
    entry_c = int(snap.get("entry_price_cents") or trade.get("entry_price_cents") or trade.get("price_cents") or 0)
    if entry_c <= 0:
      continue
    import uuid

    pid = str(uuid.uuid4())
    cost_usd = round(contracts_fp * entry_c / 100.0, 2)
    detail = (
      f"Live ENTER adopted from resting fill on Kalshi "
      f"(order {trade.get('kalshi_order_id') or '?'}) — {contracts} contracts"
    )
    store.open_position({
      "id": pid,
      "event_ticker": leg_event,
      "market_ticker": ticker,
      "side": side,
      "contracts": contracts,
      "contracts_fp": contracts_fp,
      "entry_price_cents": entry_c,
      "cost_usd": cost_usd,
      "signal": trade.get("signal"),
      "label": trade.get("label"),
      "mode": "live",
      "entry_source": "adopted_resting",
    })
    trade_id = trade.get("id")
    if trade_id is not None and hasattr(store, "promote_resting_enter_to_filled"):
      store.promote_resting_enter_to_filled(
        trade_id,
        event_ticker=leg_event,
        contracts=contracts,
        cost_usd=cost_usd,
        entry_price_cents=entry_c,
        position_id=pid,
        detail=detail,
      )
    else:
      store.log_trade({
        "event_ticker": leg_event,
        "trigger": "continuous",
        "action": "enter",
        "mode": "live",
        "market_ticker": ticker,
        "side": side,
        "contracts": contracts,
        "price_cents": entry_c,
        "entry_price_cents": entry_c,
        "cost_usd": cost_usd,
        "signal": trade.get("signal"),
        "label": trade.get("label"),
        "status": "filled",
        "detail": detail,
        "position_id": pid,
        "kalshi_order_id": trade.get("kalshi_order_id"),
      })
    changes.append({
      "action": "adopted_resting_enter",
      "ticker": ticker,
      "side": side,
      "contracts": contracts,
      "position_id": pid,
      "event_ticker": leg_event,
    })
    log.info(
      "Adopted resting enter fill %s %s x%s on %s",
      side.upper(),
      ticker,
      contracts,
      leg_event,
    )

  return {"ok": True, "changes": changes}


def _hourly_event_time_suffix(event_ticker: str) -> str | None:
  from src.trading.hourly_event_time import hourly_event_time_suffix

  return hourly_event_time_suffix(event_ticker)


def _ticker_belongs_to_hourly_event(ticker: str, event_ticker: str) -> bool:
  from src.trading.hourly_event_time import ticker_belongs_to_hourly_event

  return ticker_belongs_to_hourly_event(ticker, event_ticker)


def _recent_enter_trade_meta(
  store: Any,
  event_ticker: str,
  market_ticker: str,
  side: str,
) -> dict[str, Any]:
  with store._connect() as conn:
    row = conn.execute(
      """
      SELECT label, signal, entry_price_cents, price_cents FROM bot_trades
      WHERE market_ticker = ? AND side = ?
        AND action = 'enter' AND mode = 'live'
      ORDER BY created_at DESC LIMIT 1
      """,
      (market_ticker, side),
    ).fetchone()
  if not row:
    return {}
  return {
    "label": row[0],
    "signal": row[1],
    "entry_price_cents": row[2],
    "price_cents": row[3],
  }


def adopt_kalshi_orphan_inventory(
  store: Any,
  kalshi: Any,
  event_ticker: str,
  *,
  ticker_belongs: Any | None = None,
  cfg: dict[str, Any] | None = None,
  kind: str = "hourly",
  critical: bool = False,
  asset: str | None = None,
) -> dict[str, Any]:
  """Open bot legs for Kalshi inventory with no matching bot position (kalshi-only reconcile)."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return {"ok": True, "changes": []}

  from src.trading.hourly_event_time import (
    hourly_fill_belongs_to_asset,
    is_kalshi_hourly_event,
    market_ticker_event_ticker,
  )

  changes: list[dict[str, Any]] = []
  for row in kalshi.list_market_positions(critical=critical):
    ticker = str(row.get("ticker") or "")
    if not ticker:
      continue
    if asset and not hourly_fill_belongs_to_asset(ticker, asset):
      continue
    leg_event = market_ticker_event_ticker(ticker)
    if not leg_event or not is_kalshi_hourly_event(leg_event):
      continue
    net = position_net_from_row(row)
    if abs(net) < 0.05:
      continue
    side = "yes" if net > 0 else "no"
    if _has_open_live_leg_for_ticker(store, ticker, side):
      continue
    snap = kalshi_position_leg(kalshi, ticker, side, critical=critical)
    if snap is None:
      continue
    sellable = kalshi_sellable_contracts(kalshi, ticker, side, critical=critical)
    contracts, contracts_fp, _raw_fp = kalshi_contracts_for_adoption(
      sellable, snap, cfg, kind=kind, adoption_source="orphan", market_ticker=ticker,
    )
    from src.trading.live_range_guards import apply_range_adoption_hour_cap

    contracts, contracts_fp = apply_range_adoption_hour_cap(
      contracts,
      contracts_fp,
      store=store,
      event_ticker=leg_event,
      market_ticker=ticker,
      side=side,
      cfg=cfg,
      kind=kind,
    )
    if contracts_fp < 0.05:
      continue
    meta = _recent_enter_trade_meta(store, leg_event, ticker, side)
    entry_c = int(
      snap.get("entry_price_cents")
      or meta.get("entry_price_cents")
      or meta.get("price_cents")
      or 0,
    )
    if entry_c <= 0:
      cost_usd = snap.get("cost_usd")
      if cost_usd and contracts_fp > 0:
        entry_c = max(1, min(99, int(round(100.0 * float(cost_usd) / contracts_fp))))
    if entry_c <= 0:
      bid_c = _kalshi_leg_bid_cents(kalshi, ticker, side)
      if bid_c is not None and bid_c > 0:
        entry_c = bid_c
    if entry_c <= 0:
      continue
    import uuid

    pid = str(uuid.uuid4())
    cost_usd = round(contracts_fp * entry_c / 100.0, 2)
    store.open_position({
      "id": pid,
      "event_ticker": leg_event,
      "market_ticker": ticker,
      "side": side,
      "contracts": contracts,
      "contracts_fp": contracts_fp,
      "entry_price_cents": entry_c,
      "cost_usd": cost_usd,
      "signal": meta.get("signal"),
      "label": meta.get("label"),
      "mode": "live",
      "entry_source": "adopted_orphan",
    })
    store.log_trade({
      "event_ticker": leg_event,
      "trigger": "continuous",
      "action": "enter",
      "mode": "live",
      "market_ticker": ticker,
      "side": side,
      "contracts": contracts,
      "price_cents": entry_c,
      "entry_price_cents": entry_c,
      "cost_usd": cost_usd,
      "signal": meta.get("signal"),
      "label": meta.get("label"),
      "status": "filled",
      "detail": (
        f"Live ENTER adopted from Kalshi inventory "
        f"(kalshi-only reconcile) — {contracts_fp} contracts"
      ),
      "position_id": pid,
    })
    changes.append({
      "action": "adopted_kalshi_orphan",
      "ticker": ticker,
      "side": side,
      "contracts": contracts_fp,
      "position_id": pid,
      "event_ticker": leg_event,
    })
    log.info(
      "Adopted kalshi-only inventory %s %s x%s on %s",
      side.upper(),
      ticker,
      contracts_fp,
      leg_event,
    )

  return {"ok": True, "changes": changes}


def run_live_position_hygiene(
  *,
  store: Any,
  kalshi: Any,
  event_ticker: str,
  tab: dict[str, Any],
  settings_enabled: bool,
  cfg: dict[str, Any] | None = None,
  kind: str = "hourly",
  critical: bool = True,
  force_fill_sync: bool = False,
  asset: str | None = None,
) -> dict[str, Any]:
  """Sync inventory, cancel orphans, and optionally cancel resting enters when auto-bet is off."""
  foreign_purge = (
    purge_foreign_asset_open_positions(
      store, kalshi, asset=asset, cfg=cfg, kind=kind,
    )
    if asset
    else {"ok": True, "changes": []}
  )
  adopted_resting = adopt_filled_resting_enters(
    store, kalshi, event_ticker, cfg=cfg, kind=kind, critical=critical,
  )
  stale_resting = reconcile_stale_resting_enters(store, kalshi)
  adopted_orphans = adopt_kalshi_orphan_inventory(
    store, kalshi, event_ticker, cfg=cfg, kind=kind, critical=critical, asset=asset,
  )
  from src.trading.kalshi_fill_sync import sync_kalshi_fills_to_store

  fill_sync = sync_kalshi_fills_to_store(
    store, kalshi, critical=critical, cfg=cfg, kind=kind, force=force_fill_sync, asset=asset,
  )
  sync = sync_live_positions_from_kalshi(
    store, kalshi, event_ticker, cfg=cfg, kind=kind,
  )
  orphans = cancel_orphan_live_sell_orders(
    kalshi, live_open_tickers(store, event_ticker),
  )
  resting_cancelled = 0
  if not settings_enabled:
    resting_cancelled = cancel_resting_enter_orders_for_hourly_event(
      kalshi, event_ticker, tab, store=store,
    )
  adopted_changes = (
    (foreign_purge.get("changes") or [])
    + (adopted_resting.get("changes") or [])
    + ([{"action": "stale_resting_cancelled", "count": stale_resting.get("cancelled", 0)}] if stale_resting.get("cancelled") else [])
    + (adopted_orphans.get("changes") or [])
    + (fill_sync.get("changes") or [])
  )
  return {
    **sync,
    "ok": sync.get("ok", True),
    "changes": (sync.get("changes") or []) + adopted_changes,
    "kalshi_fill_sync": fill_sync,
    "orphan_sells_cancelled": orphans,
    "resting_enters_cancelled": resting_cancelled,
  }


def cancel_resting_enter_orders_for_market_tickers(
  kalshi: Any,
  market_tickers: set[str],
) -> int:
  """Cancel unfilled resting BUY orders on specific market tickers only."""
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return 0
  allowed = {str(t) for t in market_tickers if t}
  if not allowed:
    return 0
  cancelled = 0
  for row in kalshi.list_resting_orders():
    if str(row.get("action") or "").lower() != "buy":
      continue
    ticker = str(row.get("ticker") or "")
    if ticker not in allowed:
      continue
    oid = row.get("order_id")
    if not oid:
      continue
    try:
      kalshi.cancel_order(str(oid))
      cancelled += 1
      log.info("Cancelled resting enter %s on %s", oid, ticker)
    except Exception as e:
      log.warning("Cancel resting enter %s on %s failed: %s", oid, ticker, e)
  return cancelled


def run_live_slot_hygiene(
  *,
  store: Any,
  kalshi: Any,
  period_key: str,
  market_ticker: str | None,
  settings_enabled: bool,
  cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Sync inventory, adopt orphans, and cancel resting orders for a 15m slot."""
  def _slot15_belongs(ticker: str, _slot: str) -> bool:
    return bool(market_ticker) and str(ticker) == str(market_ticker)

  adopted_resting = adopt_filled_resting_enters(
    store, kalshi, period_key, cfg=cfg, kind="slot15",
  )
  adopted_orphans = adopt_kalshi_orphan_inventory(
    store,
    kalshi,
    period_key,
    ticker_belongs=_slot15_belongs if market_ticker else None,
    cfg=cfg,
    kind="slot15",
  )
  sync = sync_live_positions_from_kalshi(
    store, kalshi, period_key, cfg=cfg, kind="slot15",
  )
  orphans = cancel_orphan_live_sell_orders(
    kalshi, live_open_tickers(store, period_key),
  )
  resting_cancelled = 0
  if not settings_enabled and market_ticker:
    resting_cancelled = cancel_resting_enter_orders_for_market_tickers(
      kalshi, {str(market_ticker)},
    )
  return {
    **sync,
    "ok": sync.get("ok", True),
    "changes": (sync.get("changes") or []) + (adopted_resting.get("changes") or []) + (adopted_orphans.get("changes") or []),
    "orphan_sells_cancelled": orphans,
    "resting_enters_cancelled": resting_cancelled,
  }


def verify_kalshi_exit_fill(
  *,
  sellable_before: float | None,
  sellable_after: float | None,
  claimed_fill: int,
) -> int:
  """Confirmed contracts sold on Kalshi; never trust API fill_count alone."""
  if sellable_before is not None and sellable_after is not None:
    sold = max(0.0, float(sellable_before) - float(sellable_after))
    if sold < 0.05:
      return 0
    return min(int(round(sold)), max(0, int(claimed_fill)))
  return 0


def confirm_kalshi_exit_fill(
  *,
  sellable_before: float | None,
  sellable_after: float | None,
  claimed_fill: int,
  order_status: str | None,
) -> int:
  """Require inventory drop; when API claims fills, also require executed order status."""
  inv_fill = verify_kalshi_exit_fill(
    sellable_before=sellable_before,
    sellable_after=sellable_after,
    claimed_fill=claimed_fill,
  )
  if inv_fill <= 0:
    return 0
  if claimed_fill <= 0:
    return inv_fill
  if order_status is None:
    return inv_fill
  if kalshi_order_is_executed(order_status):
    return inv_fill
  return 0


def _kalshi_order_status(kalshi: Any, order_id: str | None) -> str | None:
  if not kalshi or not order_id:
    return None
  get_order = getattr(kalshi, "get_order", None)
  if not callable(get_order):
    return None
  try:
    row = get_order(str(order_id), critical=True)
  except Exception as e:
    log.debug("Kalshi get_order %s failed: %s", order_id, e)
    return None
  if not row:
    return None
  return str(row.get("status") or "").lower() or None


def _kalshi_leg_bid_cents(kalshi: Any, market_ticker: str, side: str) -> int | None:
  """Best-effort bid for a held leg (used for marketable exit retries)."""
  if not kalshi:
    return None
  get_market = getattr(kalshi, "get_market_ticker", None)
  if not callable(get_market):
    return None
  try:
    row = get_market(str(market_ticker))
  except Exception:
    return None
  if not row:
    return None
  side_l = str(side or "").lower()
  if side_l == "yes":
    raw = row.get("yes_bid_dollars")
    if raw is None:
      raw = row.get("yes_bid")
  else:
    raw = row.get("no_bid_dollars")
    if raw is None:
      raw = row.get("no_bid")
    if raw is None:
      yes_ask = row.get("yes_ask_dollars") or row.get("yes_ask")
      if yes_ask is not None:
        try:
          return max(1, min(99, int(round(100.0 - float(yes_ask)))))
        except (TypeError, ValueError):
          pass
  if raw is None:
    return None
  try:
    val = float(raw)
    if val <= 1.0:
      return max(1, min(99, int(round(val * 100.0))))
    return max(1, min(99, int(round(val))))
  except (TypeError, ValueError):
    return None


def _position_still_open(store: Any, position_id: str) -> bool:
  with store._connect() as conn:
    row = conn.execute(
      "SELECT 1 FROM bot_positions WHERE id = ? AND status = 'open' LIMIT 1",
      (str(position_id),),
    ).fetchone()
  return row is not None


def _ensure_live_leg_after_failed_exit(
  store: Any,
  kalshi: Any,
  pos: dict[str, Any],
  *,
  period_key: str,
  cfg: dict[str, Any] | None = None,
  kind: str = "hourly",
) -> None:
  """Keep or restore an open bot leg when Kalshi still holds inventory after a failed exit."""
  ticker = str(pos["market_ticker"])
  side = str(pos["side"])
  pid = str(pos["id"])
  sellable = kalshi_sellable_contracts(kalshi, ticker, side, critical=True)
  if sellable is None or sellable < 0.05:
    return
  snap = kalshi_position_leg(kalshi, ticker, side, critical=True)
  contracts, contracts_fp, _raw_fp = kalshi_contracts_for_adoption(
    sellable, snap, cfg, kind=kind, adoption_source="failed_exit_restore", market_ticker=ticker,
  )
  if contracts_fp < 0.05:
    return
  entry_c = int(pos.get("entry_price_cents") or 0)
  if entry_c <= 0:
    snap = kalshi_position_leg(kalshi, ticker, side, critical=True)
    if snap and snap.get("entry_price_cents"):
      entry_c = int(snap["entry_price_cents"])
  if _position_still_open(store, pid):
    store.update_position_contracts(
      pid,
      contracts=contracts,
      contracts_fp=contracts_fp,
      cost_usd=round(contracts_fp * entry_c / 100.0, 2) if entry_c else float(pos.get("cost_usd") or 0),
    )
    return
  if entry_c <= 0:
    bid_c = _kalshi_leg_bid_cents(kalshi, ticker, side)
    entry_c = bid_c or 50
  cost_usd = round(contracts_fp * entry_c / 100.0, 2)
  with store._connect() as conn:
    closed = conn.execute(
      "SELECT id FROM bot_positions WHERE id = ? AND status = 'closed'",
      (pid,),
    ).fetchone()
    if closed:
      conn.execute(
        """
        UPDATE bot_positions
        SET status = 'open', event_ticker = ?, market_ticker = ?, side = ?,
            contracts = ?, contracts_fp = ?, entry_price_cents = ?, cost_usd = ?,
            mode = 'live', entry_source = ?
        WHERE id = ?
        """,
        (
          period_key,
          ticker,
          side,
          contracts,
          contracts_fp,
          entry_c,
          cost_usd,
          "restored_after_unverified_exit",
          pid,
        ),
      )
    else:
      store.open_position({
        "id": pid,
        "event_ticker": period_key,
        "market_ticker": ticker,
        "side": side,
        "contracts": contracts,
        "contracts_fp": contracts_fp,
        "entry_price_cents": entry_c,
        "cost_usd": cost_usd,
        "signal": pos.get("signal"),
        "label": pos.get("label"),
        "mode": "live",
        "entry_source": "restored_after_unverified_exit",
      })
  log.warning(
    "Restored live bot leg %s %s x%s on %s after unverified exit",
    side.upper(),
    ticker,
    contracts_fp,
    period_key,
  )


def _cancel_pending_resting_exit(kalshi: Any, store: Any, position_id: str) -> str | None:
  """Cancel a stale bot resting-exit order when inventory is still held."""
  pending_oid = resting_exit_order_id(store, position_id)
  if not pending_oid:
    return None
  if order_still_resting(kalshi, pending_oid):
    try:
      kalshi.cancel_order(str(pending_oid))
      log.info("Cancelled stale resting exit %s for position %s", pending_oid, position_id)
    except Exception as e:
      log.warning("Cancel stale resting exit %s failed: %s", pending_oid, e)
  return pending_oid


def _invalidate_kalshi_position_cache(kalshi: Any, *, ticker: str) -> None:
  invalidate = getattr(kalshi, "invalidate_position_cache", None)
  if callable(invalidate):
    invalidate(ticker=ticker)


def _sellable_after_exit_attempt(
  kalshi: Any,
  *,
  ticker: str,
  side: str,
  sellable_before: float | None,
  claimed_fill: int,
  order_id: str | None = None,
) -> tuple[float | None, str | None]:
  """Poll fresh Kalshi inventory and order status after an exit order."""
  sellable_after: float | None = None
  order_status: str | None = None
  polls = 6 if claimed_fill > 0 else 1
  for attempt in range(polls):
    _invalidate_kalshi_position_cache(kalshi, ticker=ticker)
    sellable_after = kalshi_sellable_contracts(kalshi, ticker, side, critical=True)
    if order_id and claimed_fill > 0:
      order_status = _kalshi_order_status(kalshi, order_id)
    if confirm_kalshi_exit_fill(
      sellable_before=sellable_before,
      sellable_after=sellable_after,
      claimed_fill=claimed_fill,
      order_status=order_status,
    ) > 0:
      return sellable_after, order_status
    if claimed_fill <= 0 or attempt >= polls - 1:
      return sellable_after, order_status
    time.sleep(0.35)
  return sellable_after, order_status


def _attempt_live_exit_sell(
  kalshi: Any,
  *,
  ticker: str,
  side: str,
  sell_int: int,
  sell_cents: int,
  time_in_force: str,
  sellable_before: float | None = None,
) -> dict[str, Any]:
  """Place one live exit sell and refresh sellable inventory for verification."""
  exit_result = place_live_exit_sell(
    kalshi,
    market_ticker=ticker,
    side=side,
    contracts=sell_int,
    limit_cents=sell_cents,
    time_in_force=time_in_force,
  )
  claimed_fill = int(exit_result.get("fill_count") or 0)
  order_id = exit_result.get("order_id")
  sellable_after, order_status = _sellable_after_exit_attempt(
    kalshi,
    ticker=ticker,
    side=side,
    sellable_before=sellable_before,
    claimed_fill=claimed_fill,
    order_id=str(order_id) if order_id else None,
  )
  return {
    **exit_result,
    "sellable_after": sellable_after,
    "order_status": order_status,
  }


def _log_unverified_live_exit(
  store: Any,
  *,
  period_key: str,
  pos: dict[str, Any],
  pos_mode: str,
  pick: dict[str, Any],
  exit_reason: str,
  detail_suffix: str,
  extra_detail: str,
  sell_count: float,
  sell_cents: int,
  entry_c: int,
  live_exit_oid: str | None,
  claimed_fill: int,
) -> dict[str, Any]:
  return store.log_trade({
    "event_ticker": period_key,
    "trigger": "continuous",
    "action": "exit",
    "mode": pos_mode,
    "market_ticker": str(pos["market_ticker"]),
    "side": str(pos["side"]),
    "contracts": sell_count,
    "price_cents": sell_cents,
    "entry_price_cents": entry_c,
    "exit_price_cents": sell_cents,
    "cost_usd": 0,
    "pnl_usd": 0,
    "signal": pick.get("signal"),
    "label": pos.get("label"),
    "status": "skipped",
    "detail": (
      f"Live EXIT unverified (API claimed {claimed_fill} fill(s) but Kalshi inventory unchanged) — "
      f"bot leg kept open · {exit_reason}: {detail_suffix}{extra_detail}"
    ),
    "position_id": pos["id"],
    "kalshi_order_id": live_exit_oid,
  })


def try_live_position_exit(
  *,
  kalshi: Any,
  store: Any,
  pos: dict[str, Any],
  period_key: str,
  exit_price: int,
  contracts: int,
  entry_c: int,
  pos_mode: str,
  pick: dict[str, Any],
  exit_reason: str,
  detail_suffix: str,
  extra_detail: str,
  cfg: dict[str, Any] | None = None,
  kind: str = "hourly",
) -> dict[str, Any] | None:
  """Place or skip a live Kalshi exit. Returns a trade row when logged."""
  cancel_resting_orders(kalshi, pos)

  ticker = str(pos["market_ticker"])
  side = str(pos["side"])
  sellable = kalshi_sellable_contracts(kalshi, ticker, side, critical=True)

  pending_oid = resting_exit_order_id(store, pos["id"])
  if pending_oid and order_still_resting(kalshi, pending_oid):
    if sellable is not None and sellable > 0.05:
      _cancel_pending_resting_exit(kalshi, store, str(pos["id"]))
    else:
      return None

  if sellable is not None and sellable <= 0:
    if resting_sell_contracts(kalshi, ticker, side) > 0.05:
      return None
    if not should_reconcile_close_live_leg(kalshi, store, pos, cfg=cfg, kind=kind):
      return None
    return reconcile_close_stale_live_leg(
      store=store,
      pos=pos,
      period_key=period_key,
      pick=pick,
      exit_reason=exit_reason,
      extra_detail=f"{exit_reason}: {detail_suffix}{extra_detail}",
      kalshi=kalshi,
      cfg=cfg,
      kind=kind,
    )

  sellable_before = sellable
  sell_count = float(contracts)
  if sellable is not None:
    if sellable > contracts + 0.05:
      entry_c = int(pos.get("entry_price_cents") or entry_c)
      synced = int(round(float(sellable)))
      store.update_position_contracts(
        str(pos["id"]),
        contracts=synced,
        cost_usd=round(synced * entry_c / 100.0, 2),
      )
      contracts = synced
    sell_count = min(float(contracts), float(sellable))
  if sell_count < 0.01:
    return None

  cancel_resting_orders_for_ticker(kalshi, ticker)
  from src.trading.live_bracket_orders import live_exit_haircut_cents

  haircut = live_exit_haircut_cents(cfg)
  sell_cents = aggressive_exit_limit_cents(int(exit_price), haircut=haircut)
  sell_int = max(1, int(sell_count)) if sell_count >= 0.99 else 0
  if sell_int <= 0:
    return None

  attempt = _attempt_live_exit_sell(
    kalshi,
    ticker=ticker,
    side=side,
    sell_int=sell_int,
    sell_cents=sell_cents,
    time_in_force="immediate_or_cancel",
    sellable_before=sellable_before,
  )
  live_exit_oid = attempt.get("order_id")
  claimed_fill = int(attempt.get("fill_count") or 0)
  fill_count = confirm_kalshi_exit_fill(
    sellable_before=sellable_before,
    sellable_after=attempt.get("sellable_after"),
    claimed_fill=claimed_fill,
    order_status=attempt.get("order_status"),
  )
  last_attempt = attempt

  if claimed_fill > 0 and fill_count <= 0:
    log.warning(
      "Live exit order %s claimed %s fills but Kalshi inventory unchanged on %s — retrying floor sell",
      live_exit_oid,
      claimed_fill,
      ticker,
    )
    if live_exit_oid:
      try:
        kalshi.cancel_order(str(live_exit_oid))
      except Exception as e:
        log.warning("Cancel unverified exit %s failed: %s", live_exit_oid, e)
    sellable_before = kalshi_sellable_contracts(kalshi, ticker, side, critical=True)
    if sellable_before is not None and sellable_before > 0.05:
      sell_int = max(1, int(round(min(float(sell_int), float(sellable_before)))))
      retry = _attempt_live_exit_sell(
        kalshi,
        ticker=ticker,
        side=side,
        sell_int=sell_int,
        sell_cents=1,
        time_in_force="fill_or_kill",
        sellable_before=sellable_before,
      )
      live_exit_oid = retry.get("order_id") or live_exit_oid
      claimed_fill = int(retry.get("fill_count") or 0)
      fill_count = confirm_kalshi_exit_fill(
        sellable_before=sellable_before,
        sellable_after=retry.get("sellable_after"),
        claimed_fill=claimed_fill,
        order_status=retry.get("order_status"),
      )
      last_attempt = retry

  if claimed_fill > 0 and fill_count <= 0:
    bid_cents = _kalshi_leg_bid_cents(kalshi, ticker, side)
    sellable_before = kalshi_sellable_contracts(kalshi, ticker, side, critical=True)
    if (
      bid_cents is not None
      and bid_cents > 0
      and sellable_before is not None
      and sellable_before > 0.05
    ):
      log.warning(
        "Live exit floor sell unverified on %s — retrying marketable sell at bid %s¢",
        ticker,
        bid_cents,
      )
      if live_exit_oid:
        try:
          kalshi.cancel_order(str(live_exit_oid))
        except Exception as e:
          log.warning("Cancel unverified exit %s failed: %s", live_exit_oid, e)
      sell_int = max(1, int(round(min(float(sell_int), float(sellable_before)))))
      bid_attempt = _attempt_live_exit_sell(
        kalshi,
        ticker=ticker,
        side=side,
        sell_int=sell_int,
        sell_cents=bid_cents,
        time_in_force="immediate_or_cancel",
        sellable_before=sellable_before,
      )
      live_exit_oid = bid_attempt.get("order_id") or live_exit_oid
      claimed_fill = int(bid_attempt.get("fill_count") or 0)
      fill_count = confirm_kalshi_exit_fill(
        sellable_before=sellable_before,
        sellable_after=bid_attempt.get("sellable_after"),
        claimed_fill=claimed_fill,
        order_status=bid_attempt.get("order_status"),
      )
      if fill_count > 0:
        sell_cents = bid_cents
      last_attempt = bid_attempt

  if claimed_fill > 0 and fill_count <= 0:
    log.warning(
      "Live exit order %s claimed %s fills but Kalshi inventory unchanged on %s",
      live_exit_oid,
      claimed_fill,
      ticker,
    )
    _ensure_live_leg_after_failed_exit(
      store,
      kalshi,
      pos,
      period_key=period_key,
      cfg=cfg,
      kind=kind,
    )
    return _log_unverified_live_exit(
      store,
      period_key=period_key,
      pos=pos,
      pos_mode=pos_mode,
      pick=pick,
      exit_reason=exit_reason,
      detail_suffix=detail_suffix,
      extra_detail=extra_detail,
      sell_count=sell_count,
      sell_cents=sell_cents,
      entry_c=entry_c,
      live_exit_oid=live_exit_oid,
      claimed_fill=claimed_fill,
    )

  if fill_count <= 0:
    remaining = int(last_attempt.get("remaining_count") or sell_count)
    if remaining > 0 and str(last_attempt.get("order_id") or ""):
      return store.log_trade({
        "event_ticker": period_key,
        "trigger": "continuous",
        "action": "exit",
        "mode": pos_mode,
        "market_ticker": ticker,
        "side": side,
        "contracts": sell_count,
        "price_cents": sell_cents,
        "entry_price_cents": entry_c,
        "exit_price_cents": sell_cents,
        "cost_usd": 0,
        "signal": pick.get("signal"),
        "label": pos.get("label"),
        "status": "resting",
        "detail": (
          f"Live EXIT order {live_exit_oid} (0 filled — resting on Kalshi; "
          f"{remaining} remaining) "
          f"@ {sell_cents}¢"
        ),
        "position_id": pos["id"],
        "kalshi_order_id": live_exit_oid,
      })
    return None

  return {
    "exit_result": last_attempt,
    "live_exit_oid": live_exit_oid,
    "fill_count": fill_count,
    "sell_cents": sell_cents,
    "sell_count": float(fill_count),
  }
