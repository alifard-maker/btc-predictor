"""Dashboard manual hourly trades — enter/exit, snapshots, bot counterfactual."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from src.trading.contract_signals import is_buy_no, is_buy_yes
from src.trading.human_trade_store import HumanTradeSettings, HumanTradeStore
from src.trading.live_range_guards import (
  range_band_spot_entry_block_reason,
  threshold_spot_entry_block_reason,
  threshold_spot_entry_guard_shadow_only,
)
from src.trading.paper_execution import (
  entry_quote_log_fields,
  format_entry_book_detail,
  leg_pnl_usd,
  paper_entry_fill,
  paper_exit_fill,
  unrealized_leg_pnl_usd,
)

log = logging.getLogger(__name__)


def human_trade_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  raw = (cfg or {}).get("human_trading") or {}
  stake = raw.get("max_stake_per_entry_usd")
  if stake is None:
    pnl = (cfg or {}).get("pnl_first") or {}
    stake = pnl.get("max_stake_per_entry_usd", 2.50)
  return {
    "enabled": bool(raw.get("enabled", True)),
    "default_mode": str(raw.get("default_mode", "paper")),
    "max_stake_per_entry_usd": float(stake),
    "paper_bankroll_initial_usd": float(raw.get("paper_bankroll_initial_usd", 100.0)),
    "max_open_positions": int(raw.get("max_open_positions", 20)),
  }


def settings_from_cfg(cfg: dict[str, Any] | None, store: HumanTradeStore) -> HumanTradeSettings:
  saved = store.get_settings()
  hcfg = human_trade_cfg(cfg)
  return HumanTradeSettings(
    mode=saved.mode or hcfg["default_mode"],
    max_stake_per_entry_usd=float(
      saved.max_stake_per_entry_usd or hcfg["max_stake_per_entry_usd"],
    ),
    paper_bankroll_initial_usd=float(
      saved.paper_bankroll_initial_usd or hcfg["paper_bankroll_initial_usd"],
    ),
    max_open_positions=int(saved.max_open_positions or hcfg["max_open_positions"]),
  )


def enrich_open_positions_marks(
  open_positions: list[dict[str, Any]],
  tab: dict[str, Any] | None,
  cfg: dict[str, Any] | None = None,
  *,
  quote_overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
  """Attach mark bid, unrealized P&L, and bot exit signal for open manual legs."""
  from src.trading.hourly_position_alert import assess_held_hourly_position_alert

  live = (tab or {}).get("live") or {}
  try:
    live_price = float(
      (tab or {}).get("brti_live")
      or (tab or {}).get("erti_live")
      or live.get("current_price"),
    )
  except (TypeError, ValueError):
    live_price = None
  hours_left = live.get("hours_to_settle")
  try:
    hours_f = float(hours_left) if hours_left is not None else None
  except (TypeError, ValueError):
    hours_f = None
  regime = live.get("regime") or {}
  overrides = {str(k).upper(): v for k, v in (quote_overrides or {}).items()}
  out: list[dict[str, Any]] = []
  for pos in open_positions:
    row = dict(pos)
    ticker = str(pos.get("market_ticker") or "")
    pick = pick_from_tab(tab, ticker)
    if not pick:
      # Fall back to stored strike fields so spot-vs-strike exit advice still works.
      pick = {
        "ticker": pos.get("market_ticker"),
        "label": pos.get("label"),
        "signal": pos.get("signal"),
        "strike_type": pos.get("strike_type"),
        "contract_type": pos.get("contract_type"),
        "floor_strike": pos.get("floor_strike"),
        "cap_strike": pos.get("cap_strike"),
      }
    ov = overrides.get(ticker.upper())
    if ov:
      pick = dict(pick)
      for key in ("yes_bid", "yes_ask", "kalshi_mid", "no_bid", "no_ask"):
        if ov.get(key) is not None:
          pick[key] = ov[key]
      row["quote_source"] = "kalshi_live"
    else:
      row["quote_source"] = "prediction_book"
    side = str(pos.get("side") or "yes").lower()
    mark = None
    if pick and (pick.get("yes_bid") is not None or pick.get("kalshi_mid") is not None):
      fill = paper_exit_fill(pick=pick, side=side)
      if fill.get("ok") and fill.get("price_cents") is not None:
        mark = int(fill["price_cents"])
      row["mark_bid_cents"] = fill.get("bid_cents")
      row["mark_ask_cents"] = fill.get("ask_cents")
    else:
      row["mark_bid_cents"] = None
      row["mark_ask_cents"] = None
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
    row["current_signal"] = pick.get("signal") if pick else None
    try:
      row["bot_exit_signal"] = assess_held_hourly_position_alert(
        pos=row,
        pick=pick,
        live_price=live_price,
        regime_allow_trade=bool(regime.get("allow_trade", True)),
        regime_reasons=list(regime.get("reasons") or []),
        unrealized_pnl_usd=ur,
        hours_to_settle=hours_f,
        cfg=cfg,
      )
    except Exception:
      row["bot_exit_signal"] = {
        "alert": "HOLD",
        "alert_tone": "neutral",
        "headline": "HOLD",
        "detail": "Bot exit signal unavailable",
      }
    out.append(row)
  return out


def fetch_kalshi_quote_overrides(
  open_positions: list[dict[str, Any]],
  kalshi: Any,
) -> dict[str, dict[str, Any]]:
  """Per-ticker Kalshi /markets quotes for open legs only (no full discovery)."""
  from src.trading.hourly_bot import _pick_from_kalshi_market

  if not kalshi or not open_positions:
    return {}
  out: dict[str, dict[str, Any]] = {}
  for pos in open_positions:
    ticker = str(pos.get("market_ticker") or "").strip()
    if not ticker or ticker.upper() in out:
      continue
    pick = _pick_from_kalshi_market(kalshi, ticker)
    if pick:
      out[ticker.upper()] = pick
  return out


def enrich_open_positions_fast_marks(
  open_positions: list[dict[str, Any]],
  *,
  kalshi: Any,
  tab: dict[str, Any] | None,
  cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
  """Fast mark path: live Kalshi bid/ask on held tickers + cached spot for exit advice."""
  overrides = fetch_kalshi_quote_overrides(open_positions, kalshi)
  return enrich_open_positions_marks(
    open_positions,
    tab,
    cfg,
    quote_overrides=overrides,
  )


def pick_from_tab(tab: dict[str, Any] | None, market_ticker: str) -> dict[str, Any] | None:
  if not tab or not tab.get("ok"):
    return None
  live = tab.get("live") or {}
  target = str(market_ticker).upper()
  for block in ("strategy_threshold", "strategy_range"):
    strat = live.get(block) or {}
    for row in strat.get("contracts") or []:
      if str(row.get("ticker") or "").upper() == target:
        return dict(row)
  return None


def event_ticker_from_tab(tab: dict[str, Any] | None) -> str | None:
  if not tab or not tab.get("ok"):
    return None
  ev = tab.get("event") or {}
  return ev.get("event_ticker")


def side_from_signal(signal: str | None) -> str | None:
  if is_buy_yes(signal):
    return "yes"
  if is_buy_no(signal):
    return "no"
  return None


def build_feature_snapshot(
  *,
  tab: dict[str, Any] | None,
  pick: dict[str, Any],
  side: str,
) -> dict[str, Any]:
  live = (tab or {}).get("live") or {}
  regime = live.get("regime") or {}
  ref = live.get("current_price")
  hours = live.get("hours_to_settle")
  return {
    "market_ticker": pick.get("ticker"),
    "label": pick.get("label"),
    "side": side,
    "signal": pick.get("signal"),
    "edge": pick.get("edge"),
    "model_prob": pick.get("model_prob"),
    "kalshi_mid": pick.get("kalshi_mid"),
    "yes_bid": pick.get("yes_bid"),
    "yes_ask": pick.get("yes_ask"),
    "contract_type": pick.get("contract_type"),
    "strike_type": pick.get("strike_type"),
    "floor_strike": pick.get("floor_strike"),
    "cap_strike": pick.get("cap_strike"),
    "spot_price": ref,
    "hours_to_settle": hours,
    "regime_allow_trade": regime.get("allow_trade"),
    "regime_reasons": list(regime.get("reasons") or []),
    "terminal_sigma": live.get("terminal_sigma"),
    "index_id": live.get("index_id"),
  }


def build_bot_counterfactual(
  *,
  pick: dict[str, Any],
  side: str,
  tab: dict[str, Any] | None,
  bot_status: dict[str, Any] | None,
  cfg: dict[str, Any] | None,
  asset: str,
) -> dict[str, Any]:
  live = (tab or {}).get("live") or {}
  ref = live.get("current_price")
  try:
    spot_f = float(ref) if ref is not None else None
  except (TypeError, ValueError):
    spot_f = None
  range_block = range_band_spot_entry_block_reason(
    pick=pick,
    side=side,
    spot_price=spot_f,
    terminal_sigma=live.get("terminal_sigma"),
    cfg=cfg,
    kind="hourly",
    asset=asset,
  )
  thresh_block = threshold_spot_entry_block_reason(
    pick=pick,
    side=side,
    spot_price=spot_f,
    terminal_sigma=live.get("terminal_sigma"),
    cfg=cfg,
    kind="hourly",
    asset=asset,
  )
  signal = pick.get("signal")
  signal_ok = (
    (side == "yes" and is_buy_yes(signal))
    or (side == "no" and is_buy_no(signal))
  )
  ticker = str(pick.get("ticker") or "").upper()
  bot_open = [
    p for p in (bot_status or {}).get("open_positions") or []
    if str(p.get("market_ticker") or "").upper() == ticker
    and str(p.get("side") or "").lower() == side
  ]
  bot_entered_same = len(bot_open) > 0
  skip_reasons: list[str] = []
  hard_skips: list[str] = []
  if not signal_ok:
    skip_reasons.append("signal_not_actionable_for_side")
    hard_skips.append("signal_not_actionable_for_side")
  if range_block:
    skip_reasons.append(range_block)
    hard_skips.append(range_block)
  if thresh_block:
    # Shadow Phase 1 logs would-blocks but does not hard-stop bot entries.
    if threshold_spot_entry_guard_shadow_only(cfg, kind="hourly", asset=asset):
      skip_reasons.append(f"shadow:{thresh_block}")
    else:
      skip_reasons.append(thresh_block)
      hard_skips.append(thresh_block)
  would_enter = not hard_skips
  return {
    "would_enter": would_enter,
    "skip_reasons": skip_reasons,
    "bot_last_skip_reason": (bot_status or {}).get("last_skip_reason"),
    "bot_open_same_ticker_side": bot_entered_same,
    "bot_open_positions": len((bot_status or {}).get("open_positions") or []),
    "bot_remaining_usd": (bot_status or {}).get("remaining_usd"),
    "bot_kind": (bot_status or {}).get("bot_kind"),
  }


def preview_manual_entry(
  *,
  store: HumanTradeStore,
  tab: dict[str, Any] | None,
  market_ticker: str,
  side: str,
  mode: str,
  bot_status: dict[str, Any] | None,
  cfg: dict[str, Any] | None,
  asset: str,
) -> dict[str, Any]:
  hcfg = human_trade_cfg(cfg)
  if not hcfg["enabled"]:
    return {"ok": False, "error": "human_trading_disabled"}
  settings = settings_from_cfg(cfg, store)
  pick = pick_from_tab(tab, market_ticker)
  if not pick:
    return {"ok": False, "error": "contract_not_in_live_book"}
  side_l = str(side).lower()
  if side_l not in ("yes", "no"):
    return {"ok": False, "error": "invalid_side"}
  event_ticker = event_ticker_from_tab(tab)
  if not event_ticker:
    return {"ok": False, "error": "no_active_hour"}

  features = build_feature_snapshot(tab=tab, pick=pick, side=side_l)
  counterfactual = build_bot_counterfactual(
    pick=pick,
    side=side_l,
    tab=tab,
    bot_status=bot_status,
    cfg=cfg,
    asset=asset,
  )

  fill_preview: dict[str, Any] | None = None
  if mode == "paper":
    paper = store.get_paper_state_dict(settings.paper_bankroll_initial_usd)
    remaining = float(paper.get("paper_bankroll_usd") or 0)
    fill_preview = paper_entry_fill(
      pick=pick,
      side=side_l,
      remaining_budget_usd=min(remaining, settings.max_stake_per_entry_usd),
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
    "event_ticker": event_ticker,
    "pick": pick,
    "side": side_l,
    "mode": mode,
    "features": features,
    "bot_counterfactual": counterfactual,
    "fill_preview": fill_preview,
    "max_stake_usd": settings.max_stake_per_entry_usd,
  }


def _open_cost_paper(store: HumanTradeStore, event_ticker: str) -> float:
  return round(
    sum(float(p.get("cost_usd") or 0) for p in store.open_positions(event_ticker)),
    2,
  )


def _side_quotes(pick: dict[str, Any], side: str) -> tuple[int | None, int | None]:
  from src.trading.paper_execution import _side_quotes_cents

  bid, ask = _side_quotes_cents(pick, side)
  return bid, ask


def _contracts_for_stake(stake_usd: float, ask_cents: int | None) -> int:
  if ask_cents is None or ask_cents <= 0:
    return 0
  return max(0, int(stake_usd * 100 / ask_cents))


def execute_manual_enter(
  *,
  store: HumanTradeStore,
  tab: dict[str, Any] | None,
  market_ticker: str,
  side: str,
  mode: str,
  bot_status: dict[str, Any] | None,
  cfg: dict[str, Any] | None,
  asset: str,
  kalshi: Any | None = None,
) -> dict[str, Any]:
  preview = preview_manual_entry(
    store=store,
    tab=tab,
    market_ticker=market_ticker,
    side=side,
    mode=mode,
    bot_status=bot_status,
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

  entry_context = {
    "features": preview["features"],
    "bot_counterfactual": preview["bot_counterfactual"],
  }
  pid = str(uuid.uuid4())

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
        client_order_id=f"human-{uuid.uuid4()}",
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
      detail = f"Manual LIVE enter YES@{price_cents}¢" if side_l == "yes" else f"Manual LIVE enter NO@{price_cents}¢"
    except Exception as e:
      return {"ok": False, "error": f"kalshi_order_failed:{e}", "preview": preview}
  else:
    if not store.debit_paper_for_entry(cost_usd, settings.paper_bankroll_initial_usd):
      return {"ok": False, "error": "paper_bankroll_insufficient", "preview": preview}
    kalshi_order_id = None
    status = "filled"
    detail = f"Manual PAPER enter · {format_entry_book_detail(fill).strip()}"

  live = (tab or {}).get("live") or {}
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
    "contract_type": pick.get("contract_type"),
    "strike_type": pick.get("strike_type"),
    "floor_strike": pick.get("floor_strike"),
    "cap_strike": pick.get("cap_strike"),
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


def execute_manual_exit(
  *,
  store: HumanTradeStore,
  tab: dict[str, Any] | None,
  position_id: str,
  cfg: dict[str, Any] | None,
  kalshi: Any | None = None,
  verify_take_profit: bool = False,
) -> dict[str, Any]:
  hcfg = human_trade_cfg(cfg)
  if not hcfg["enabled"]:
    return {"ok": False, "error": "human_trading_disabled"}

  open_pos = None
  for p in store.open_positions():
    if p.get("id") == position_id:
      open_pos = p
      break
  if not open_pos:
    return {"ok": False, "error": "position_not_open"}

  pick = pick_from_tab(tab, str(open_pos["market_ticker"]))
  if not pick:
    pick = {
      "ticker": open_pos["market_ticker"],
      "label": open_pos.get("label"),
      "signal": open_pos.get("signal"),
      "strike_type": open_pos.get("strike_type"),
      "contract_type": open_pos.get("contract_type"),
      "floor_strike": open_pos.get("floor_strike"),
      "cap_strike": open_pos.get("cap_strike"),
      "yes_bid": None,
      "yes_ask": None,
      "kalshi_mid": None,
    }

  side_l = str(open_pos["side"]).lower()
  mode = str(open_pos.get("mode") or "paper")
  contracts = int(open_pos["contracts"])
  entry_c = int(open_pos["entry_price_cents"])
  event_ticker = str(open_pos["event_ticker"])
  ticker = str(open_pos["market_ticker"])

  # Always prefer a live Kalshi bid for the exit mark (paper + live).
  if kalshi:
    from src.trading.hourly_bot import _pick_from_kalshi_market

    fresh = _pick_from_kalshi_market(kalshi, ticker)
    if fresh and (
      fresh.get("yes_bid") is not None or fresh.get("yes_ask") is not None
    ):
      pick = dict(pick)
      for key in ("yes_bid", "yes_ask", "kalshi_mid", "no_bid", "no_ask"):
        if fresh.get(key) is not None:
          pick[key] = fresh[key]

  fill = paper_exit_fill(pick=pick, side=side_l)
  if not fill.get("ok"):
    return {"ok": False, "error": fill.get("skip_reason") or "no_liquidity"}

  # If Sell was driven by a TAKE PROFIT badge, re-check on the fresh mark.
  if verify_take_profit:
    try:
      marked = enrich_open_positions_marks(
        [{
          **open_pos,
          "contracts": contracts,
          "entry_price_cents": entry_c,
        }],
        tab,
        cfg=cfg,
        quote_overrides={ticker: pick},
      )
      fresh_sig = (marked[0].get("bot_exit_signal") if marked else None) or {}
      ur = marked[0].get("unrealized_pnl_usd") if marked else None
    except Exception:
      fresh_sig = {"alert": "HOLD", "detail": "Could not re-check TAKE PROFIT"}
      ur = None
    alert = str(fresh_sig.get("alert") or "HOLD")
    mark_c = fill.get("price_cents")
    still_tp = alert == "TAKE PROFIT" and (ur is None or float(ur) > 0)
    if not still_tp:
      ur_txt = f"{float(ur):+.2f}" if ur is not None else "—"
      return {
        "ok": False,
        "error": "take_profit_stale",
        "message": (
          f"TAKE PROFIT no longer valid on live quote — "
          f"fresh signal is {alert} at mark {mark_c}¢ (P&L {ur_txt}). Sell blocked."
        ),
        "bot_exit_signal": fresh_sig,
        "mark_price_cents": mark_c,
        "unrealized_pnl_usd": ur,
      }

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
    settings = settings_from_cfg(cfg, store)

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
  features = build_feature_snapshot(tab=tab, pick=pick, side=side_l) if tab else {}
  # Capture bot exit advice at Sell time for later “adopt human exits” analysis.
  bot_exit_signal = None
  try:
    marked = enrich_open_positions_marks(
      [{
        **open_pos,
        "contracts": contracts,
        "entry_price_cents": entry_c,
      }],
      tab,
      cfg=cfg,
      quote_overrides={ticker: pick},
    )
    if marked:
      bot_exit_signal = marked[0].get("bot_exit_signal")
  except Exception:
    bot_exit_signal = None
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
    "detail": f"Manual {mode.upper()} exit @ {exit_cents}¢ · P&L {pnl:+.2f}",
    "kalshi_order_id": kalshi_order_id,
    "position_id": position_id,
    "entry_context": {
      "features": features,
      "exit_reason": "manual_dashboard",
      "bot_exit_signal": bot_exit_signal,
      "mark_price_cents": exit_cents,
      "realized_pnl_usd": round(pnl, 2),
      "verify_take_profit": bool(verify_take_profit),
    },
  })
  return {
    "ok": True,
    "trade": trade,
    "pnl_usd": round(pnl, 2),
    "status": store.status(event_ticker),
  }


def _kalshi_fetch_market_row(kalshi: Any, market_ticker: str) -> dict[str, Any] | None:
  """Live market row, then historical archive (settled hours leave /markets)."""
  if not kalshi or not market_ticker:
    return None
  ticker = str(market_ticker).strip()
  try:
    row = kalshi.get_market_ticker(ticker)
    if isinstance(row, dict) and row:
      # Unwrap occasional envelopes.
      inner = row.get("market")
      if isinstance(inner, dict) and inner:
        row = inner
      return row
  except Exception:
    pass
  get = getattr(kalshi, "get", None)
  if not callable(get):
    return None
  for path, params in (
    (f"/markets/{ticker}", None),
    ("/historical/markets", {"tickers": ticker, "limit": 1}),
    ("/markets", {"tickers": ticker, "limit": 1}),
  ):
    try:
      data = get(path, params=params) if params else get(path)
    except Exception:
      continue
    if not isinstance(data, dict):
      continue
    if isinstance(data.get("market"), dict):
      return data["market"]
    markets = data.get("markets")
    if isinstance(markets, list) and markets:
      for m in markets:
        if isinstance(m, dict) and str(m.get("ticker") or "").upper() == ticker.upper():
          return m
      if isinstance(markets[0], dict):
        return markets[0]
  return None


def _result_from_market_row(row: dict[str, Any]) -> str | None:
  result = str(row.get("result") or row.get("market_result") or "").strip().lower()
  if result in ("yes", "no"):
    return result
  # YES settlement payout in dollars (0 or 1) after finalize.
  for key in ("settlement_value_dollars", "settlement_value", "yes_settlement"):
    raw = row.get(key)
    if raw in (None, ""):
      continue
    try:
      val = float(raw)
    except (TypeError, ValueError):
      continue
    if val >= 0.99:
      return "yes"
    if val <= 0.01:
      return "no"
  return None


def _kalshi_market_result_exit_cents(
  kalshi: Any,
  market_ticker: str,
  side: str,
) -> tuple[int, str] | None:
  """If Kalshi already posted the binary result, cash the held side at 100/0."""
  row = _kalshi_fetch_market_row(kalshi, market_ticker)
  if not row:
    return None
  result = _result_from_market_row(row)
  if result not in ("yes", "no"):
    return None
  held_yes = str(side).lower() == "yes"
  won = (result == "yes" and held_yes) or (result == "no" and not held_yes)
  cents = 100 if won else 0
  return cents, f"kalshi result={result} → settled @ {cents}¢"


def _kalshi_expiration_settle_price(
  kalshi: Any,
  market_ticker: str,
  *,
  event_ticker: str | None = None,
  cache: dict[str, float | None] | None = None,
) -> float | None:
  """Official hour close index from Kalshi (market or any sibling on the event)."""
  cache = cache if cache is not None else {}
  ticker = str(market_ticker or "").strip()
  event = str(event_ticker or "").strip()
  cache_key = f"exp:{ticker or event}"
  if cache_key in cache:
    return cache[cache_key]

  def _exp_from_row(row: dict[str, Any] | None) -> float | None:
    if not row:
      return None
    exp = row.get("expiration_value")
    if exp in (None, ""):
      return None
    try:
      px = float(exp)
    except (TypeError, ValueError):
      return None
    return px if px > 0 else None

  px = _exp_from_row(_kalshi_fetch_market_row(kalshi, ticker)) if ticker else None
  if px is None and event and kalshi and callable(getattr(kalshi, "get", None)):
    for path, params in (
      ("/markets", {"event_ticker": event, "status": "settled", "limit": 50}),
      ("/markets", {"event_ticker": event, "limit": 50}),
      ("/historical/markets", {"event_ticker": event, "limit": 50}),
    ):
      try:
        data = kalshi.get(path, params=params)
      except Exception:
        continue
      markets = (data or {}).get("markets") if isinstance(data, dict) else None
      if not isinstance(markets, list):
        continue
      for m in markets:
        if not isinstance(m, dict):
          continue
        px = _exp_from_row(m)
        if px is not None:
          break
      if px is not None:
        break

  cache[cache_key] = px
  if event and px is not None:
    cache[f"exp-event:{event}"] = px
  return px


def _enrich_pos_contract_spec(pos: dict[str, Any]) -> dict[str, Any]:
  """Fill strike metadata from label / ticker when missing on orphan repairs."""
  from src.trading.hourly_settlement import contract_spec_from_label, contract_spec_from_position

  out = dict(pos)
  spec = contract_spec_from_position(out)
  if spec.get("floor_strike") is None and spec.get("cap_strike") is None:
    spec = {**spec, **contract_spec_from_label(str(out.get("label") or ""))}
  if spec.get("floor_strike") is None and spec.get("cap_strike") is None:
    ticker = str(out.get("market_ticker") or "")
    m = re.search(r"-T([\d.]+)$", ticker)
    if m:
      spec = {
        "contract_type": "threshold",
        "strike_type": "greater",
        "floor_strike": float(m.group(1)),
      }
  for k, v in spec.items():
    if out.get(k) is None and v is not None:
      out[k] = v
  return out


def _event_settle_brti(
  *,
  asset: str,
  event_ticker: str,
  cfg: dict[str, Any] | None,
  cache: dict[str, float | None],
) -> float | None:
  """Hour-specific settle index from our tracker (not the live tape)."""
  if event_ticker in cache:
    return cache[event_ticker]
  price: float | None = None
  if not cfg:
    cache[event_ticker] = None
    return None
  candidates = [str(event_ticker)]
  e = str(event_ticker)
  if e.startswith("KXBTCD-"):
    candidates.append("KXBTC-" + e[len("KXBTCD-"):])
  elif e.startswith("KXBTC-") and not e.startswith("KXBTCD-"):
    candidates.append("KXBTCD-" + e[len("KXBTC-"):])
  if e.startswith("KXETHD-"):
    candidates.append("KXETH-" + e[len("KXETHD-"):])
  elif e.startswith("KXETH-") and not e.startswith("KXETHD-"):
    candidates.append("KXETHD-" + e[len("KXETH-"):])
  try:
    from src.assets import asset_cfg
    from src.db.hourly_store import create_hourly_store

    acfg = cfg if asset == "btc" else asset_cfg(cfg, asset)
    store = create_hourly_store(acfg, asset=asset)
    for cand in candidates:
      row = store.get_by_event_ticker(cand)
      if row and row.get("settle_brti") is not None:
        price = float(row["settle_brti"])
        break
  except Exception as e:
    log.warning("Human settle_brti lookup failed for %s %s: %s", asset, event_ticker, e)
    price = None
  cache[event_ticker] = price
  return price


def _resolve_human_settlement_exit_cents(
  pos: dict[str, Any],
  *,
  asset: str,
  cfg: dict[str, Any] | None,
  kalshi: Any | None,
  live_settle_price: float | None,
  just_rolled: bool,
  settle_brti_cache: dict[str, float | None],
) -> tuple[int, str, float | None]:
  """
  Resolve exit cents for an expired human leg.

  Prefer Kalshi official result / expiration_value, then hour settle_brti, then
  live spot only when the hour *just* rolled. Never wipe winners with a late
  tape print — if unsure, refund at entry (cash back, 0 P&L).
  """
  from src.trading.hourly_settlement import settlement_exit_cents

  pos = _enrich_pos_contract_spec(pos)
  side_l = str(pos.get("side") or "yes").lower()
  entry_c = int(pos["entry_price_cents"])
  ticker = str(pos.get("market_ticker") or "")
  event = str(pos.get("event_ticker") or "")

  kalshi_res = _kalshi_market_result_exit_cents(kalshi, ticker, side_l)
  if kalshi_res:
    cents, note = kalshi_res
    return cents, note, None

  hour_settle = _kalshi_expiration_settle_price(
    kalshi,
    ticker,
    event_ticker=event,
    cache=settle_brti_cache,
  )
  settle_src = "kalshi expiration_value"
  if hour_settle is None:
    hour_settle = _event_settle_brti(
      asset=asset,
      event_ticker=event,
      cfg=cfg,
      cache=settle_brti_cache,
    )
    settle_src = "hour settle_brti"
  if hour_settle is None and just_rolled and live_settle_price is not None:
    hour_settle = float(live_settle_price)
    settle_src = "live roll print"

  if hour_settle is not None and hour_settle > 0:
    settled = settlement_exit_cents(
      side=side_l,
      settle_price=float(hour_settle),
      spec=pos,
    )
    if settled is not None:
      outcome = "won" if settled == 100 else "lost"
      return (
        int(settled),
        f"settled @ {settled}¢ ({outcome} vs ${float(hour_settle):,.2f} · {settle_src})",
        float(hour_settle),
      )

  # No unknown settle print yet — return principal (scratch), never guess-lose.
  return entry_c, f"cash-back @ entry {entry_c}¢ (awaiting official result)", None


def _credit_human_paper_exit(
  store: HumanTradeStore,
  *,
  pos: dict[str, Any],
  exit_cents: int,
  note: str,
  settle_price: float | None,
  index_id: str,
  cfg: dict[str, Any] | None,
  source: str,
) -> dict[str, Any]:
  from src.trading.paper_execution import leg_pnl_usd

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
    f"{mode.upper()} EXIT (HOUR SETTLEMENT): {side_l.upper()} ×{contracts} "
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
      "exit_reason": "hour_settlement",
      "settlement_note": note,
      "settle_price": settle_price,
      "index_id": index_id,
      "realized_pnl_usd": round(pnl, 2),
      "settlement_source": source,
    },
  })
  log.info("Human hour settlement: %s", detail)
  return trade


def settle_expired_human_positions(
  store: HumanTradeStore,
  *,
  current_event_ticker: str | None,
  settle_price: float | None,
  cfg: dict[str, Any] | None = None,
  kalshi: Any | None = None,
  index_id: str = "BRTI",
  asset: str = "btc",
) -> list[dict[str, Any]]:
  """
  Cash out open human legs after their hourly event has settled.

  Dashboard only lists opens for the *current* hour, so without this paper legs
  from the prior hour vanish from Open legs with no exit row / bankroll credit.
  """
  from src.trading.hourly_event_time import (
    canonical_hourly_event_ticker,
    hourly_event_has_settled,
    hourly_event_settle_utc,
  )

  current = (
    canonical_hourly_event_ticker(str(current_event_ticker))
    if current_event_ticker
    else None
  )
  settled_rows: list[dict[str, Any]] = []
  settle_brti_cache: dict[str, float | None] = {}
  now = datetime.now(timezone.utc)

  for pos in list(store.open_positions()):
    event = str(pos.get("event_ticker") or "")
    if not event:
      continue
    canon = canonical_hourly_event_ticker(event)
    if current and canon == current:
      continue
    # Hour rolled to a new event → prior opens must cash out even if settle-time
    # parsing fails / is slightly skewed (these were invisible but still locking bankroll).
    hour_rolled = bool(current and canon != current)
    if not hour_rolled and not hourly_event_has_settled(canon, now=now):
      continue

    settle_at = hourly_event_settle_utc(canon)
    just_rolled = False
    if settle_at is not None:
      age_s = (now - settle_at).total_seconds()
      just_rolled = 0 <= age_s <= 10 * 60
    elif hour_rolled:
      # Unknown settle clock but new hour is live — treat as rolled for refund path.
      just_rolled = False

    pos = dict(pos)
    pos["event_ticker"] = canon
    try:
      exit_cents, note, used_px = _resolve_human_settlement_exit_cents(
        pos,
        asset=asset,
        cfg=cfg,
        kalshi=kalshi,
        live_settle_price=settle_price,
        just_rolled=just_rolled,
        settle_brti_cache=settle_brti_cache,
      )
      trade = _credit_human_paper_exit(
        store,
        pos=pos,
        exit_cents=exit_cents,
        note=note,
        settle_price=used_px if used_px is not None else settle_price,
        index_id=index_id,
        cfg=cfg,
        source="open_leg_rollover",
      )
      settled_rows.append(trade)
    except Exception as e:
      log.exception(
        "Human open-leg settlement failed for %s: %s",
        pos.get("market_ticker"),
        e,
      )

  # Repair orphan paper enters (visible as EXIT — in the log): still funded,
  # but no exit / bankroll credit after the hour UI scrolled away.
  settled_rows.extend(
    repair_orphan_human_paper_enters(
      store,
      current_event_ticker=current,
      settle_price=settle_price,
      cfg=cfg,
      kalshi=kalshi,
      index_id=index_id,
      asset=asset,
      settle_brti_cache=settle_brti_cache,
    ),
  )
  return settled_rows


def repair_orphan_human_paper_enters(
  store: HumanTradeStore,
  *,
  current_event_ticker: str | None,
  settle_price: float | None,
  cfg: dict[str, Any] | None = None,
  kalshi: Any | None = None,
  index_id: str = "BRTI",
  asset: str = "btc",
  settle_brti_cache: dict[str, float | None] | None = None,
) -> list[dict[str, Any]]:
  """Settle paper enters that have no exit after the hour settled (cash must come back)."""
  from src.trading.hourly_event_time import (
    canonical_hourly_event_ticker,
    hourly_event_has_settled,
    hourly_event_settle_utc,
  )
  from src.trading.hourly_settlement import contract_spec_from_label
  from src.trading.paper_execution import leg_pnl_usd

  current = (
    canonical_hourly_event_ticker(str(current_event_ticker))
    if current_event_ticker
    else None
  )
  cache = settle_brti_cache if settle_brti_cache is not None else {}
  now = datetime.now(timezone.utc)
  all_trades = store.list_trades(limit=5000)
  exits_by_pos = {
    str(t.get("position_id") or ""): t
    for t in all_trades
    if t.get("action") == "exit" and t.get("position_id")
  }
  exits_by_leg: dict[str, dict[str, Any]] = {}
  for t in all_trades:
    if t.get("action") != "exit":
      continue
    key = _leg_key(
      event=str(t.get("event_ticker") or ""),
      market=str(t.get("market_ticker") or ""),
      side=str(t.get("side") or ""),
    )
    if key and key not in exits_by_leg:
      exits_by_leg[key] = t

  open_by_id = {str(p["id"]): p for p in store.open_positions()}
  open_by_leg: dict[str, dict[str, Any]] = {}
  for p in store.open_positions():
    key = _leg_key(
      event=str(p.get("event_ticker") or ""),
      market=str(p.get("market_ticker") or ""),
      side=str(p.get("side") or ""),
    )
    if key:
      open_by_leg[key] = p

  repaired: list[dict[str, Any]] = []
  settings = settings_from_cfg(cfg, store)
  seen_keys: set[str] = set()

  for enter in all_trades:
    if str(enter.get("action") or "") != "enter":
      continue
    if str(enter.get("mode") or "").lower() != "paper":
      continue
    if str(enter.get("status") or "") not in ("filled", "reconciled", ""):
      continue
    event = canonical_hourly_event_ticker(str(enter.get("event_ticker") or ""))
    if not event:
      continue
    if current and event == current:
      continue
    hour_rolled = bool(current and event != current)
    if not hour_rolled and not hourly_event_has_settled(event, now=now):
      continue

    side_l = str(enter.get("side") or "yes").lower()
    market = str(enter.get("market_ticker") or "")
    entry_c = int(enter.get("entry_price_cents") or enter.get("price_cents") or 0)
    contracts = int(enter.get("contracts") or 0)
    if entry_c <= 0 or contracts <= 0:
      continue

    pid = str(enter.get("position_id") or "").strip()
    leg_key = _leg_key(event=event, market=market, side=side_l)
    # Dedup: one settlement per leg (position_id preferred).
    dedupe = pid or leg_key
    if not dedupe or dedupe in seen_keys:
      continue
    seen_keys.add(dedupe)

    existing_exit = (exits_by_pos.get(pid) if pid else None) or (
      exits_by_leg.get(leg_key) if leg_key else None
    )
    open_pos = (open_by_id.get(pid) if pid else None) or (
      open_by_leg.get(leg_key) if leg_key else None
    )

    pos_id = pid or (str(open_pos["id"]) if open_pos else f"orphan-{leg_key}")
    pos = dict(open_pos) if open_pos else {
      "id": pos_id,
      "event_ticker": event,
      "market_ticker": market,
      "side": side_l,
      "contracts": contracts,
      "entry_price_cents": entry_c,
      "cost_usd": enter.get("cost_usd") or (contracts * entry_c / 100.0),
      "signal": enter.get("signal"),
      "label": enter.get("label"),
      "mode": "paper",
      "status": "open" if open_pos else "closed",
    }
    pos["event_ticker"] = event
    pos["side"] = side_l
    pos["entry_price_cents"] = entry_c
    pos["contracts"] = contracts
    if enter.get("label") and not pos.get("label"):
      pos["label"] = enter.get("label")
    if enter.get("cost_usd") is not None:
      pos["cost_usd"] = enter.get("cost_usd")

    feats = {}
    ctx = enter.get("entry_context")
    if isinstance(ctx, dict):
      feats = ctx.get("features") or {}
    for key in ("strike_type", "floor_strike", "cap_strike", "contract_type"):
      if pos.get(key) is None and feats.get(key) is not None:
        pos[key] = feats.get(key)
    if not pos.get("floor_strike") and not pos.get("cap_strike"):
      spec = contract_spec_from_label(str(pos.get("label") or enter.get("label") or ""))
      pos.update({k: v for k, v in spec.items() if v is not None})

    settle_at = hourly_event_settle_utc(event)
    just_rolled = False
    if settle_at is not None:
      age_s = (now - settle_at).total_seconds()
      just_rolled = 0 <= age_s <= 10 * 60

    try:
      exit_cents, note, used_px = _resolve_human_settlement_exit_cents(
        pos,
        asset=asset,
        cfg=cfg,
        kalshi=kalshi,
        live_settle_price=settle_price,
        just_rolled=just_rolled,
        settle_brti_cache=cache,
      )
    except Exception as e:
      log.exception("Human orphan settle resolve failed for %s: %s", market, e)
      # Absolute floor: return entry capital so bankroll cannot stay robbed.
      exit_cents, note, used_px = entry_c, f"cash-back @ entry {entry_c}¢ (resolve error)", None

    fair_pnl = float(
      leg_pnl_usd(
        entry_price_cents=entry_c,
        mark_or_exit_cents=int(exit_cents),
        contracts=contracts,
      )
      or 0.0,
    )

    if existing_exit:
      old_exit = int(existing_exit.get("exit_price_cents") or existing_exit.get("price_cents") or 0)
      old_pnl = float(existing_exit.get("pnl_usd") or 0.0)
      is_scratch = abs(old_pnl) < 0.009 and old_exit == entry_c
      # Still no official print — leave cash-back alone (will retry next poll).
      if "cash-back @ entry" in note or "awaiting official result" in note:
        if is_scratch:
          log.info(
            "Human scratch still awaiting settle print for %s (%s)",
            market, pos_id,
          )
        continue
      if int(exit_cents) == old_exit and abs(fair_pnl - old_pnl) < 0.009:
        continue
      delta = round(fair_pnl - old_pnl, 2)
      if abs(delta) < 0.009 and int(exit_cents) == old_exit:
        continue
      store.update_trade_exit(
        str(existing_exit["id"]),
        exit_price_cents=int(exit_cents),
        pnl_usd=round(fair_pnl, 2),
        detail=(
          f"PAPER EXIT (HOUR SETTLEMENT CORRECTED): {side_l.upper()} ×{contracts} "
          f"@ {exit_cents}¢ (entry {entry_c}¢) — {note} "
          f"[was {old_exit}¢ / {old_pnl:+.2f}{' scratch' if is_scratch else ''}]"
        ),
      )
      if abs(delta) >= 0.009:
        store.apply_paper_exit_settlement(0.0, delta, settings.paper_bankroll_initial_usd)
      repaired.append({**existing_exit, "pnl_usd": round(fair_pnl, 2), "exit_price_cents": exit_cents})
      log.info(
        "Human settlement corrected %s: %s¢ → %s¢ (ΔP&L %+0.2f)",
        pos_id, old_exit, exit_cents, delta,
      )
      continue

    try:
      trade = _credit_human_paper_exit(
        store,
        pos=pos,
        exit_cents=exit_cents,
        note=note,
        settle_price=used_px if used_px is not None else settle_price,
        index_id=index_id,
        cfg=cfg,
        source="orphan_enter_repair",
      )
    except Exception as e:
      log.exception("Human orphan credit failed for %s: %s", market, e)
      continue
    repaired.append(trade)
    if pid:
      exits_by_pos[pid] = trade
    if leg_key:
      exits_by_leg[leg_key] = trade
    if open_pos:
      open_by_id.pop(str(open_pos.get("id") or ""), None)
      open_by_leg.pop(leg_key, None)

  # Sweep any prior-hour opens that never had a logged enter (still lock bankroll).
  for pos in list(store.open_positions()):
    if str(pos.get("mode") or "paper").lower() != "paper":
      continue
    event = canonical_hourly_event_ticker(str(pos.get("event_ticker") or ""))
    if not event:
      continue
    if current and event == current:
      continue
    hour_rolled = bool(current and event != current)
    if not hour_rolled and not hourly_event_has_settled(event, now=now):
      continue
    leg_key = _leg_key(
      event=event,
      market=str(pos.get("market_ticker") or ""),
      side=str(pos.get("side") or ""),
    )
    pid = str(pos.get("id") or "")
    if (pid and pid in exits_by_pos) or (leg_key and leg_key in exits_by_leg):
      # Exit logged but position left open — just unlock.
      store.close_position(pid)
      continue
    dedupe = pid or leg_key
    if dedupe and dedupe in seen_keys:
      store.close_position(pid)
      continue
    if dedupe:
      seen_keys.add(dedupe)
    pos = dict(pos)
    pos["event_ticker"] = event
    if not pos.get("floor_strike") and not pos.get("cap_strike"):
      spec = contract_spec_from_label(str(pos.get("label") or ""))
      pos.update({k: v for k, v in spec.items() if v is not None})
    settle_at = hourly_event_settle_utc(event)
    just_rolled = False
    if settle_at is not None:
      just_rolled = 0 <= (now - settle_at).total_seconds() <= 10 * 60
    try:
      exit_cents, note, used_px = _resolve_human_settlement_exit_cents(
        pos,
        asset=asset,
        cfg=cfg,
        kalshi=kalshi,
        live_settle_price=settle_price,
        just_rolled=just_rolled,
        settle_brti_cache=cache,
      )
      trade = _credit_human_paper_exit(
        store,
        pos=pos,
        exit_cents=exit_cents,
        note=note,
        settle_price=used_px if used_px is not None else settle_price,
        index_id=index_id,
        cfg=cfg,
        source="orphan_open_sweep",
      )
      repaired.append(trade)
    except Exception as e:
      log.exception("Human open sweep failed for %s: %s", pos.get("market_ticker"), e)

  store.reconcile_paper_bankroll(settings.paper_bankroll_initial_usd)
  return repaired


def _leg_key(*, event: str, market: str, side: str) -> str:
  from src.trading.hourly_event_time import canonical_hourly_event_ticker

  e = canonical_hourly_event_ticker(str(event or "").strip())
  m = str(market or "").strip().upper()
  s = str(side or "").strip().lower()
  if not e or not m or s not in ("yes", "no"):
    return ""
  return f"{e}|{m}|{s}"


def apply_human_settings_body(
  store: HumanTradeStore,
  body: dict[str, Any],
  *,
  cfg: dict[str, Any] | None,
) -> HumanTradeSettings:
  current = settings_from_cfg(cfg, store)
  mode = str(body.get("mode", current.mode)).lower()
  if mode not in ("paper", "live"):
    mode = current.mode
  updated = HumanTradeSettings(
    mode=mode,
    max_stake_per_entry_usd=float(
      body.get("max_stake_per_entry_usd", current.max_stake_per_entry_usd),
    ),
    paper_bankroll_initial_usd=float(
      body.get("paper_bankroll_initial_usd", current.paper_bankroll_initial_usd),
    ),
    max_open_positions=int(body.get("max_open_positions", current.max_open_positions)),
  )
  store.save_settings(updated)
  return updated
