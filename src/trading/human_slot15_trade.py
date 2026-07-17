"""Dashboard manual 15m (slot) trades — enter/exit/settle for KXBTC15M / KXETH15M."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from src.trading.human_hourly_trade import (
  apply_human_settings_body,
  human_trade_cfg,
  settings_from_cfg,
)
from src.trading.human_trade_store import HumanTradeStore
from src.trading.paper_execution import (
  entry_quote_log_fields,
  format_entry_book_detail,
  leg_pnl_usd,
  paper_entry_fill,
  paper_exit_fill,
  unrealized_leg_pnl_usd,
)
from src.trading.slot15_settlement import (
  resolve_slot15_rollover_exit_cents,
  should_rollover_close_slot15_leg,
)

log = logging.getLogger(__name__)

_ACTIONABLE_LONG = frozenset({"LONG", "LATE LONG", "FLIP LONG"})
_ACTIONABLE_SHORT = frozenset({"SHORT", "LATE SHORT", "FLIP SHORT"})


def is_actionable_long(signal: str | None) -> bool:
  return str(signal or "") in _ACTIONABLE_LONG


def is_actionable_short(signal: str | None) -> bool:
  return str(signal or "") in _ACTIONABLE_SHORT


def side_from_slot15_signal(signal: str | None) -> str | None:
  if is_actionable_long(signal):
    return "yes"
  if is_actionable_short(signal):
    return "no"
  return None


def slot_key_from_tab(tab: dict[str, Any] | None) -> str | None:
  if not tab:
    return None
  key = tab.get("slot_key") or (tab.get("monitor") or {}).get("slot_start")
  return str(key) if key else None


def slot15_pick_from_tab(
  tab: dict[str, Any] | None,
  market_ticker: str | None = None,
) -> dict[str, Any] | None:
  """Build a paper/live pick from the live 15m Kalshi market on the tab."""
  if not tab or not tab.get("ok"):
    return None
  kalshi = tab.get("kalshi") or {}
  ticker = str(kalshi.get("market_ticker") or "").strip()
  if not ticker:
    return None
  if market_ticker and str(market_ticker).upper() != ticker.upper():
    return None
  pred = tab.get("prediction") or {}
  monitor = tab.get("monitor") or {}
  signal = (
    monitor.get("late_entry_action")
    or monitor.get("flip_action")
    or pred.get("signal")
  )
  return {
    "ticker": ticker,
    "label": kalshi.get("title") or ticker,
    "signal": signal,
    "yes_bid": kalshi.get("yes_bid"),
    "yes_ask": kalshi.get("yes_ask"),
    "yes_mid": kalshi.get("yes_mid"),
    "kalshi_mid": kalshi.get("yes_mid"),
  }


def market_summary_from_tab(tab: dict[str, Any] | None) -> dict[str, Any] | None:
  pick = slot15_pick_from_tab(tab)
  if not pick:
    return None
  return {
    "market_ticker": pick["ticker"],
    "title": pick.get("label"),
    "yes_bid": pick.get("yes_bid"),
    "yes_ask": pick.get("yes_ask"),
    "yes_mid": pick.get("yes_mid"),
    "signal": pick.get("signal"),
    "suggested_side": side_from_slot15_signal(pick.get("signal")),
  }


def _contracts_for_stake(stake_usd: float, ask_cents: int | None) -> int:
  if ask_cents is None or ask_cents <= 0:
    return 0
  return max(0, int(stake_usd * 100 / ask_cents))


def _side_quotes(pick: dict[str, Any], side: str) -> tuple[int | None, int | None]:
  from src.trading.paper_execution import _side_quotes_cents

  return _side_quotes_cents(pick, side)


def build_slot15_feature_snapshot(
  *,
  tab: dict[str, Any] | None,
  pick: dict[str, Any],
  side: str,
) -> dict[str, Any]:
  pred = (tab or {}).get("prediction") or {}
  monitor = (tab or {}).get("monitor") or {}
  return {
    "market_ticker": pick.get("ticker"),
    "label": pick.get("label"),
    "side": side,
    "signal": pick.get("signal"),
    "yes_bid": pick.get("yes_bid"),
    "yes_ask": pick.get("yes_ask"),
    "yes_mid": pick.get("yes_mid"),
    "slot_key": slot_key_from_tab(tab),
    "prob_up": pred.get("prob_up"),
    "reference_price": pred.get("reference_price") or pred.get("price"),
    "current_price": monitor.get("current_price") or (tab or {}).get("brti_live"),
    "seconds_remaining": monitor.get("seconds_remaining"),
  }


def enrich_slot15_human_marks(
  open_positions: list[dict[str, Any]],
  tab: dict[str, Any] | None,
  *,
  quote_overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
  """Attach mark bid + unrealized P&L for open manual 15m legs."""
  overrides = {str(k).upper(): v for k, v in (quote_overrides or {}).items()}
  tab_pick = slot15_pick_from_tab(tab)
  out: list[dict[str, Any]] = []
  for pos in open_positions:
    row = dict(pos)
    ticker = str(pos.get("market_ticker") or "")
    pick = None
    if tab_pick and str(tab_pick.get("ticker") or "").upper() == ticker.upper():
      pick = dict(tab_pick)
    else:
      pick = {
        "ticker": ticker,
        "label": pos.get("label"),
        "signal": pos.get("signal"),
        "yes_bid": None,
        "yes_ask": None,
        "yes_mid": None,
      }
    ov = overrides.get(ticker.upper())
    if ov:
      pick = dict(pick)
      for key in ("yes_bid", "yes_ask", "kalshi_mid", "yes_mid", "no_bid", "no_ask"):
        if ov.get(key) is not None:
          pick[key] = ov[key]
      row["quote_source"] = "kalshi_live"
    else:
      row["quote_source"] = "slot15_tab"
    side = str(pos.get("side") or "yes").lower()
    mark = None
    fill = paper_exit_fill(pick=pick, side=side)
    if fill.get("ok") and fill.get("price_cents") is not None:
      mark = int(fill["price_cents"])
    row["mark_bid_cents"] = fill.get("bid_cents")
    row["mark_ask_cents"] = fill.get("ask_cents")
    entry_c = int(pos.get("entry_price_cents") or 0)
    contracts = int(pos.get("contracts") or 0)
    ur = unrealized_leg_pnl_usd(
      side=side,
      entry_price_cents=entry_c,
      mark_price_cents=mark,
      contracts=contracts,
    )
    row["mark_price_cents"] = mark
    row["unrealized_pnl_usd"] = ur
    row["current_signal"] = pick.get("signal")
    out.append(row)
  return out


def enrich_slot15_human_fast_marks(
  open_positions: list[dict[str, Any]],
  *,
  kalshi: Any,
  tab: dict[str, Any] | None,
) -> list[dict[str, Any]]:
  from src.trading.hourly_bot import _pick_from_kalshi_market
  from src.trading.human_hourly_trade import fetch_kalshi_quote_overrides

  overrides = fetch_kalshi_quote_overrides(open_positions, kalshi)
  # Also refresh the active tab market when present.
  if tab and tab.get("ok"):
    mkt = str((tab.get("kalshi") or {}).get("market_ticker") or "")
    if mkt and mkt.upper() not in overrides:
      fresh = _pick_from_kalshi_market(kalshi, mkt)
      if fresh:
        overrides[mkt.upper()] = fresh
  return enrich_slot15_human_marks(open_positions, tab, quote_overrides=overrides)


def preview_slot15_manual_entry(
  *,
  store: HumanTradeStore,
  tab: dict[str, Any] | None,
  market_ticker: str,
  side: str,
  mode: str,
  cfg: dict[str, Any] | None,
  asset: str,
) -> dict[str, Any]:
  del asset  # reserved for future asset-specific gates
  hcfg = human_trade_cfg(cfg)
  if not hcfg["enabled"]:
    return {"ok": False, "error": "human_trading_disabled"}
  settings = settings_from_cfg(cfg, store)
  pick = slot15_pick_from_tab(tab, market_ticker)
  if not pick:
    return {"ok": False, "error": "contract_not_in_live_book"}
  side_l = str(side).lower()
  if side_l not in ("yes", "no"):
    return {"ok": False, "error": "invalid_side"}
  slot_key = slot_key_from_tab(tab)
  if not slot_key:
    return {"ok": False, "error": "no_active_slot"}

  features = build_slot15_feature_snapshot(tab=tab, pick=pick, side=side_l)
  if mode == "paper":
    paper = store.get_paper_state_dict(settings.paper_bankroll_initial_usd)
    remaining = float(paper.get("paper_bankroll_usd") or 0)
    max_spread = int((tab or {}).get("paper_max_spread_cents") or 40)
    fill_preview = paper_entry_fill(
      pick=pick,
      side=side_l,
      remaining_budget_usd=min(remaining, settings.max_stake_per_entry_usd),
      max_spread_cents=max_spread,
    )
  else:
    bid, ask = _side_quotes(pick, side_l)
    est_contracts = _contracts_for_stake(settings.max_stake_per_entry_usd, ask)
    fill_preview = {
      "ok": ask is not None and est_contracts > 0,
      "price_cents": ask,
      "contracts": est_contracts,
      "bid_cents": bid,
      "ask_cents": ask,
      "skip_reason": None if ask else "no_liquidity",
    }

  return {
    "ok": True,
    "event_ticker": slot_key,
    "slot_key": slot_key,
    "pick": pick,
    "side": side_l,
    "mode": mode,
    "features": features,
    "fill_preview": fill_preview,
    "max_stake_usd": settings.max_stake_per_entry_usd,
  }


def execute_slot15_manual_enter(
  *,
  store: HumanTradeStore,
  tab: dict[str, Any] | None,
  market_ticker: str,
  side: str,
  mode: str,
  cfg: dict[str, Any] | None,
  asset: str,
  kalshi: Any | None = None,
) -> dict[str, Any]:
  preview = preview_slot15_manual_entry(
    store=store,
    tab=tab,
    market_ticker=market_ticker,
    side=side,
    mode=mode,
    cfg=cfg,
    asset=asset,
  )
  if not preview.get("ok"):
    return preview

  settings = settings_from_cfg(cfg, store)
  event_ticker = preview["event_ticker"]
  pick = preview["pick"]
  side_l = preview["side"]
  fill = preview.get("fill_preview") or {}
  if not fill.get("ok"):
    return {
      "ok": False,
      "error": fill.get("skip_reason") or "fill_failed",
      "preview": preview,
    }

  open_count = len(store.open_positions())
  if open_count >= settings.max_open_positions:
    return {"ok": False, "error": "max_open_positions", "preview": preview}

  price_cents = int(fill["price_cents"])
  contracts = int(fill["contracts"])
  if contracts <= 0:
    return {"ok": False, "error": "zero_contracts", "preview": preview}
  cost_usd = round(contracts * price_cents / 100.0, 2)
  pid = str(uuid.uuid4())
  entry_context = {"features": preview["features"], "product": "slot15"}

  if mode == "live":
    if not kalshi or not getattr(kalshi, "authenticated", False):
      return {"ok": False, "error": "kalshi_not_authenticated", "preview": preview}
    try:
      order = kalshi.create_order(
        ticker=str(pick["ticker"]),
        side=side_l,
        count=contracts,
        yes_price=price_cents if side_l == "yes" else None,
        no_price=price_cents if side_l == "no" else None,
        client_order_id=f"human15-{uuid.uuid4()}",
        time_in_force="immediate_or_cancel",
      )
      from src.data.kalshi import parse_v2_order_response

      parsed = parse_v2_order_response(order)
      filled = int(parsed.get("fill_count") or 0)
      if filled <= 0:
        return {
          "ok": False,
          "error": "live_fill_zero",
          "kalshi_order_id": parsed.get("order_id"),
          "preview": preview,
        }
      contracts = filled
      cost_usd = round(contracts * price_cents / 100.0, 2)
      kalshi_order_id = parsed.get("order_id")
      status = "filled"
      detail = (
        f"Manual LIVE 15m enter YES@{price_cents}¢"
        if side_l == "yes"
        else f"Manual LIVE 15m enter NO@{price_cents}¢"
      )
    except Exception as e:
      return {"ok": False, "error": f"kalshi_order_failed:{e}", "preview": preview}
  else:
    if not store.debit_paper_for_entry(cost_usd, settings.paper_bankroll_initial_usd):
      return {"ok": False, "error": "paper_bankroll_insufficient", "preview": preview}
    kalshi_order_id = None
    status = "filled"
    detail = f"Manual PAPER 15m enter · {format_entry_book_detail(fill).strip()}"

  pos = store.open_position({
    "id": pid,
    "event_ticker": event_ticker,
    "market_ticker": pick["ticker"],
    "side": side_l,
    "contracts": contracts,
    "entry_price_cents": price_cents,
    "cost_usd": cost_usd,
    "signal": pick.get("signal"),
    "label": pick.get("label"),
    "mode": mode,
  })
  quote_fields = entry_quote_log_fields(fill)
  trade = store.log_trade({
    "event_ticker": event_ticker,
    "action": "enter",
    "mode": mode,
    "market_ticker": pick.get("ticker"),
    "side": side_l,
    "contracts": contracts,
    "price_cents": price_cents,
    "entry_price_cents": price_cents,
    "cost_usd": cost_usd,
    "signal": pick.get("signal"),
    "label": pick.get("label"),
    "status": status,
    "detail": detail,
    "kalshi_order_id": kalshi_order_id,
    "position_id": pid,
    "entry_context": entry_context,
    **quote_fields,
  })
  return {
    "ok": True,
    "trade": trade,
    "position": pos,
    "preview": preview,
    "status": store.status(event_ticker),
  }


def execute_slot15_manual_exit(
  *,
  store: HumanTradeStore,
  tab: dict[str, Any] | None,
  position_id: str,
  cfg: dict[str, Any] | None,
  kalshi: Any | None = None,
) -> dict[str, Any]:
  hcfg = human_trade_cfg(cfg)
  if not hcfg["enabled"]:
    return {"ok": False, "error": "human_trading_disabled"}

  open_pos = next((p for p in store.open_positions() if p.get("id") == position_id), None)
  if not open_pos:
    return {"ok": False, "error": "position_not_open"}

  ticker = str(open_pos["market_ticker"])
  pick = slot15_pick_from_tab(tab, ticker) or {
    "ticker": ticker,
    "label": open_pos.get("label"),
    "signal": open_pos.get("signal"),
    "yes_bid": None,
    "yes_ask": None,
    "yes_mid": None,
  }

  side_l = str(open_pos["side"]).lower()
  mode = str(open_pos.get("mode") or "paper")
  contracts = int(open_pos["contracts"])
  entry_c = int(open_pos["entry_price_cents"])
  event_ticker = str(open_pos["event_ticker"])

  if kalshi:
    from src.trading.hourly_bot import _pick_from_kalshi_market

    fresh = _pick_from_kalshi_market(kalshi, ticker)
    if fresh and (fresh.get("yes_bid") is not None or fresh.get("yes_ask") is not None):
      pick = dict(pick)
      for key in ("yes_bid", "yes_ask", "kalshi_mid", "no_bid", "no_ask"):
        if fresh.get(key) is not None:
          pick[key] = fresh[key]

  fill = paper_exit_fill(pick=pick, side=side_l)
  if not fill.get("ok"):
    return {"ok": False, "error": fill.get("skip_reason") or "no_liquidity"}

  if mode == "live":
    if not kalshi or not getattr(kalshi, "authenticated", False):
      return {"ok": False, "error": "kalshi_not_authenticated"}
    from src.trading.live_bracket_orders import (
      aggressive_exit_limit_cents,
      live_exit_haircut_cents,
      place_live_exit_sell,
    )
    from src.trading.live_position_sync import kalshi_sellable_contracts

    sellable = kalshi_sellable_contracts(kalshi, ticker, side_l, critical=True)
    if sellable is None or sellable < 0.05:
      return {"ok": False, "error": "no_kalshi_inventory"}
    sell_ct = min(contracts, int(sellable))
    bid = int(fill["price_cents"])
    haircut = live_exit_haircut_cents(cfg)
    sell_cents = aggressive_exit_limit_cents(bid, haircut=haircut)
    try:
      order = place_live_exit_sell(
        kalshi,
        market_ticker=ticker,
        side=side_l,
        contracts=sell_ct,
        limit_cents=sell_cents,
      )
      filled = int(order.get("fill_count") or 0)
      if filled <= 0:
        return {"ok": False, "error": "live_exit_zero", "kalshi_order_id": order.get("order_id")}
      exit_cents = sell_cents
      contracts = filled
      kalshi_order_id = order.get("order_id")
    except Exception as e:
      return {"ok": False, "error": f"kalshi_exit_failed:{e}"}
  else:
    exit_cents = int(fill["price_cents"])
    kalshi_order_id = None

  pnl = float(
    leg_pnl_usd(
      entry_price_cents=entry_c,
      mark_or_exit_cents=exit_cents,
      contracts=contracts,
    )
    or 0.0,
  )
  if mode == "paper":
    settings = settings_from_cfg(cfg, store)
    cost_usd = float(open_pos.get("cost_usd") or (contracts * entry_c / 100.0))
    store.apply_paper_exit_settlement(
      cost_usd,
      pnl,
      settings.paper_bankroll_initial_usd,
    )

  store.close_position(position_id)
  features = build_slot15_feature_snapshot(tab=tab, pick=pick, side=side_l) if tab else {}
  trade = store.log_trade({
    "event_ticker": event_ticker,
    "action": "exit",
    "mode": mode,
    "market_ticker": open_pos.get("market_ticker"),
    "side": side_l,
    "contracts": contracts,
    "price_cents": exit_cents,
    "entry_price_cents": entry_c,
    "exit_price_cents": exit_cents,
    "cost_usd": open_pos.get("cost_usd"),
    "pnl_usd": round(pnl, 2),
    "signal": open_pos.get("signal"),
    "label": open_pos.get("label"),
    "status": "filled",
    "detail": f"Manual {mode.upper()} 15m exit @ {exit_cents}¢ · P&L {pnl:+.2f}",
    "kalshi_order_id": kalshi_order_id,
    "position_id": position_id,
    "entry_context": {
      "features": features,
      "exit_reason": "manual_dashboard",
      "mark_price_cents": exit_cents,
      "realized_pnl_usd": round(pnl, 2),
      "product": "slot15",
    },
  })
  return {
    "ok": True,
    "trade": trade,
    "status": store.status(slot_key_from_tab(tab)),
  }


def _credit_slot15_settlement_exit(
  store: HumanTradeStore,
  *,
  pos: dict[str, Any],
  exit_cents: int,
  note: str,
  settle_price: float | None,
  index_id: str,
  cfg: dict[str, Any] | None,
) -> dict[str, Any]:
  side_l = str(pos.get("side") or "yes").lower()
  entry_c = int(pos["entry_price_cents"])
  contracts = int(pos["contracts"])
  mode = str(pos.get("mode") or "paper").lower()
  ticker = str(pos.get("market_ticker") or "")
  canon = str(pos.get("event_ticker") or "")
  settings = settings_from_cfg(cfg, store)

  pnl = float(
    leg_pnl_usd(
      entry_price_cents=entry_c,
      mark_or_exit_cents=int(exit_cents),
      contracts=contracts,
    )
    or 0.0,
  )
  cost_usd = float(pos.get("cost_usd") or (contracts * entry_c / 100.0))
  if mode == "paper":
    store.apply_paper_exit_settlement(
      cost_usd,
      pnl,
      settings.paper_bankroll_initial_usd,
    )
  if str(pos.get("status") or "open") == "open":
    store.close_position(str(pos["id"]))

  settle_line = ""
  if settle_price is not None:
    try:
      settle_line = f" · {index_id} ${float(settle_price):,.2f}"
    except (TypeError, ValueError):
      pass
  detail = (
    f"{mode.upper()} EXIT (15m SLOT SETTLEMENT): {side_l.upper()} ×{contracts} "
    f"@ {exit_cents}¢ (entry {entry_c}¢) — {note}{settle_line}"
  )
  trade = store.log_trade({
    "event_ticker": canon,
    "action": "exit",
    "mode": mode,
    "market_ticker": ticker,
    "side": side_l,
    "contracts": contracts,
    "price_cents": exit_cents,
    "entry_price_cents": entry_c,
    "exit_price_cents": exit_cents,
    "cost_usd": cost_usd,
    "pnl_usd": round(pnl, 2),
    "signal": pos.get("signal"),
    "label": pos.get("label"),
    "status": "filled",
    "detail": detail,
    "position_id": pos["id"],
    "entry_context": {
      "exit_reason": "slot15_settlement",
      "settlement_note": note,
      "settle_price": settle_price,
      "index_id": index_id,
      "realized_pnl_usd": round(pnl, 2),
      "product": "slot15",
    },
  })
  log.info("Human 15m settlement: %s", detail)
  return trade


def settle_expired_slot15_human_positions(
  store: HumanTradeStore,
  *,
  current_slot_key: str | None,
  tab: dict[str, Any] | None = None,
  cfg: dict[str, Any] | None = None,
  kalshi: Any | None = None,
  index_id: str = "BRTI",
  settle_price: float | None = None,
) -> list[dict[str, Any]]:
  """Cash out open human 15m legs after their slot has settled."""
  from src.trading.slot15_bot import _price_cents_for_side, _yes_mid_cents

  settled_rows: list[dict[str, Any]] = []
  existing_exit_pids = {
    str(t.get("position_id") or "")
    for t in store.list_trades(limit=5000)
    if t.get("action") == "exit" and t.get("position_id")
  }
  kalshi_quote = (tab or {}).get("kalshi") or {}
  current_mkt = str(kalshi_quote.get("market_ticker") or "") or None
  yes_mid = _yes_mid_cents(kalshi_quote)

  for pos in list(store.open_positions()):
    slot_key = str(pos.get("event_ticker") or "")
    if not slot_key:
      continue
    pid = str(pos.get("id") or "")
    if pid and pid in existing_exit_pids:
      try:
        store.close_position(pid)
      except Exception:
        pass
      continue
    if current_slot_key and slot_key == str(current_slot_key):
      continue
    if not should_rollover_close_slot15_leg(pos, slot_key, kalshi=kalshi):
      continue

    exit_cents, note = resolve_slot15_rollover_exit_cents(
      pos,
      kalshi=kalshi,
      slot_key=slot_key,
      market_ticker=str(pos.get("market_ticker") or ""),
      current_market_ticker=current_mkt,
      quote=kalshi_quote,
      yes_mid_cents=yes_mid,
      price_for_side=_price_cents_for_side,
    )
    try:
      trade = _credit_slot15_settlement_exit(
        store,
        pos=pos,
        exit_cents=int(exit_cents),
        note=note,
        settle_price=settle_price,
        index_id=index_id,
        cfg=cfg,
      )
      settled_rows.append(trade)
      if pid:
        existing_exit_pids.add(pid)
    except Exception as e:
      log.exception(
        "Human 15m settlement failed for %s: %s",
        pos.get("market_ticker"),
        e,
      )
  return settled_rows


# Re-export settings helper for routes
__all__ = [
  "apply_human_settings_body",
  "enrich_slot15_human_fast_marks",
  "enrich_slot15_human_marks",
  "execute_slot15_manual_enter",
  "execute_slot15_manual_exit",
  "is_actionable_long",
  "is_actionable_short",
  "market_summary_from_tab",
  "preview_slot15_manual_entry",
  "settle_expired_slot15_human_positions",
  "side_from_slot15_signal",
  "slot15_pick_from_tab",
  "slot_key_from_tab",
]
