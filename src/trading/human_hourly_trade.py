"""Dashboard manual hourly trades — enter/exit, snapshots, bot counterfactual."""

from __future__ import annotations

import logging
import uuid
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
      "yes_bid": None,
      "yes_ask": None,
      "kalshi_mid": None,
    }

  side_l = str(open_pos["side"]).lower()
  mode = str(open_pos.get("mode") or "paper")
  contracts = int(open_pos["contracts"])
  entry_c = int(open_pos["entry_price_cents"])
  event_ticker = str(open_pos["event_ticker"])

  if mode == "live":
    if not kalshi or not getattr(kalshi, "authenticated", False):
      return {"ok": False, "error": "kalshi_not_authenticated"}
    from src.trading.live_bracket_orders import (
      aggressive_exit_limit_cents,
      live_exit_haircut_cents,
      place_live_exit_sell,
    )
    from src.trading.live_position_sync import kalshi_sellable_contracts

    ticker = str(open_pos["market_ticker"])
    sellable = kalshi_sellable_contracts(kalshi, ticker, side_l, critical=True)
    if sellable is None or sellable < 0.05:
      return {"ok": False, "error": "no_kalshi_inventory"}
    sell_ct = min(contracts, int(sellable))
    fill = paper_exit_fill(pick=pick, side=side_l)
    if not fill.get("ok"):
      return {"ok": False, "error": fill.get("skip_reason") or "no_liquidity"}
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
    fill = paper_exit_fill(pick=pick, side=side_l)
    if not fill.get("ok"):
      return {"ok": False, "error": fill.get("skip_reason") or "no_liquidity"}
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
    },
  })
  return {
    "ok": True,
    "trade": trade,
    "pnl_usd": round(pnl, 2),
    "status": store.status(event_ticker),
  }


def _kalshi_market_result_exit_cents(
  kalshi: Any,
  market_ticker: str,
  side: str,
) -> tuple[int, str] | None:
  """If Kalshi already posted the binary result, cash the held side at 100/0."""
  if not kalshi:
    return None
  try:
    row = kalshi.get_market_ticker(market_ticker)
  except Exception:
    return None
  if not row:
    return None
  result = str(row.get("result") or row.get("market_result") or "").strip().lower()
  if result not in ("yes", "no"):
    return None
  held_yes = str(side).lower() == "yes"
  won = (result == "yes" and held_yes) or (result == "no" and not held_yes)
  cents = 100 if won else 0
  return cents, f"kalshi result={result} → settled @ {cents}¢"


def settle_expired_human_positions(
  store: HumanTradeStore,
  *,
  current_event_ticker: str | None,
  settle_price: float | None,
  cfg: dict[str, Any] | None = None,
  kalshi: Any | None = None,
  index_id: str = "BRTI",
) -> list[dict[str, Any]]:
  """
  Cash out open human legs after their hourly event has settled.

  Dashboard only lists opens for the *current* hour, so without this paper legs
  from the prior hour vanish from Open legs with no exit row / bankroll credit.
  """
  from src.trading.hourly_event_time import (
    canonical_hourly_event_ticker,
    hourly_event_has_settled,
  )
  from src.trading.hourly_settlement import resolve_hourly_rollover_exit_cents
  from src.trading.paper_execution import leg_pnl_usd

  current = (
    canonical_hourly_event_ticker(str(current_event_ticker))
    if current_event_ticker
    else None
  )
  settings = settings_from_cfg(cfg, store)
  settled_rows: list[dict[str, Any]] = []

  for pos in list(store.open_positions()):
    event = str(pos.get("event_ticker") or "")
    if not event:
      continue
    canon = canonical_hourly_event_ticker(event)
    if current and canon == current:
      continue
    if not hourly_event_has_settled(canon):
      continue

    side_l = str(pos.get("side") or "yes").lower()
    entry_c = int(pos["entry_price_cents"])
    contracts = int(pos["contracts"])
    mode = str(pos.get("mode") or "paper").lower()
    ticker = str(pos.get("market_ticker") or "")

    note = ""
    exit_cents: int | None = None
    kalshi_res = _kalshi_market_result_exit_cents(kalshi, ticker, side_l)
    if kalshi_res:
      exit_cents, note = kalshi_res
    else:
      cents, note = resolve_hourly_rollover_exit_cents(
        pos,
        settle_price=settle_price,
        pick=None,
        market_exit_cents=entry_c,
      )
      exit_cents = int(cents)

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
      },
    })
    settled_rows.append(trade)
    log.info("Human hour settlement: %s", detail)

  return settled_rows


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
