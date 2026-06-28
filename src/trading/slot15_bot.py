"""15-minute auto-bet bot — continuous paper/live trading within per-slot exposure cap."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from src.trading.bot_period_rollover import force_close_period_positions, resolve_rollover_exit_cents
from src.trading.bot_profit_exit import (
  AdaptiveExitContext,
  evaluate_adaptive_profit_exit,
  is_profit_exit_reason,
  position_hold_seconds,
)
from src.trading.edge import Signal
from src.trading.paper_execution import (
  entry_quote_log_fields,
  format_entry_book_detail,
  paper_entry_fill,
  paper_exit_fill,
)
from src.trading.slot15_bet_assessment import assess_slot15_bet
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore

log = logging.getLogger(__name__)

CUT_LOSS_EXIT_MIN_LOSS_USD = 0.05

_ACTIONABLE_LONG = frozenset({
  Signal.LONG.value,
  "LATE LONG",
  "FLIP LONG",
})
_ACTIONABLE_SHORT = frozenset({
  Signal.SHORT.value,
  "LATE SHORT",
  "FLIP SHORT",
})
def is_actionable_long(signal: str | None) -> bool:
  return str(signal or "") in _ACTIONABLE_LONG


def is_actionable_short(signal: str | None) -> bool:
  return str(signal or "") in _ACTIONABLE_SHORT


def is_actionable_entry(signal: str | None) -> bool:
  return is_actionable_long(signal) or is_actionable_short(signal)


def bet_qualifies(
  signal: str | None,
  bet_assessment: dict[str, Any] | None,
  settings: Slot15BotSettings,
) -> bool:
  if not settings.enabled:
    return False
  if not is_actionable_entry(signal):
    return False
  # Both filters off = free mode: trade any LONG/SHORT within budget.
  if not settings.allow_strong and not settings.allow_actionable:
    return True
  if not bet_assessment or not bet_assessment.get("actionable_bet"):
    return False
  tone = bet_assessment.get("actionable_tone")
  if tone == "strong" and settings.allow_strong:
    return True
  if tone == "moderate" and settings.allow_actionable:
    return True
  return False


def _yes_mid_cents(kalshi: dict[str, Any] | None) -> int | None:
  if not kalshi:
    return None
  mid = kalshi.get("yes_mid")
  if mid is None:
    return None
  return max(1, min(99, int(round(float(mid) * 100))))


def _price_cents_for_side(yes_mid_cents: int | None, side: str) -> int | None:
  if yes_mid_cents is None:
    return None
  if side == "yes":
    return yes_mid_cents
  return max(1, min(99, 100 - yes_mid_cents))


def _contracts_for_budget(remaining_usd: float, price_cents: int) -> int:
  if price_cents <= 0 or remaining_usd <= 0:
    return 0
  cost_per = price_cents / 100.0
  return max(0, int(remaining_usd // cost_per))


def _side_from_signal(signal: str | None) -> str | None:
  if is_actionable_long(signal):
    return "yes"
  if is_actionable_short(signal):
    return "no"
  return None


def _normalize_signal_for_assessment(signal: str) -> str:
  if signal in ("LATE LONG", "FLIP LONG"):
    return Signal.LONG.value
  if signal in ("LATE SHORT", "FLIP SHORT"):
    return Signal.SHORT.value
  return signal


def _bet_assessment_for_signal(
  signal: str,
  pred: dict[str, Any] | None,
  bet: dict[str, Any] | None,
) -> dict[str, Any]:
  norm = _normalize_signal_for_assessment(signal)
  if bet and norm in (Signal.LONG.value, Signal.SHORT.value):
    return bet
  if not pred:
    return {"actionable_bet": True, "actionable_tone": "moderate", "actionable_headline": "ACTIONABLE BET"}
  ref = pred.get("reference_price") or pred.get("price")
  expected_move_pct = None
  if ref and pred.get("expected_move") is not None:
    expected_move_pct = float(pred["expected_move"]) / float(ref) * 100
  regime_allow = True
  model_sig = pred.get("model_signal") or pred.get("signal")
  if model_sig in (Signal.LONG.value, Signal.SHORT.value) and pred.get("signal") == Signal.NO_TRADE.value:
    regime_allow = False
  return assess_slot15_bet(
    signal=norm,
    model_signal=model_sig,
    regime_allow_trade=regime_allow,
    regime_reasons=list(pred.get("regime_notes") or []),
    prob_up=float(pred.get("prob_up", 0.5)),
    expected_move_pct=expected_move_pct,
    min_confidence=float(pred.get("min_confidence", 0.57)),
    min_expected_move_pct=float(pred.get("min_expected_move_pct", 0.08)),
  )


def _entry_candidates(tab: dict[str, Any]) -> list[tuple[float, str, dict[str, Any], dict[str, Any]]]:
  """Ranked (score, signal, pick, bet_assessment) entry opportunities."""
  pred = tab.get("prediction") or {}
  monitor = tab.get("monitor") or {}
  bet = tab.get("bet_assessment")
  kalshi = tab.get("kalshi") or {}
  market_ticker = str(kalshi.get("market_ticker") or "")
  out: list[tuple[float, str, dict[str, Any], dict[str, Any]]] = []
  seen: set[str] = set()

  def add(signal: str | None, score_boost: float = 0.0, label: str | None = None) -> None:
    sig = str(signal or "")
    if not sig or not is_actionable_entry(sig) or sig in seen:
      return
    seen.add(sig)
    assessment = _bet_assessment_for_signal(sig, pred, bet)
    edge = abs(float(pred.get("prob_up", 0.5)) - 0.5)
    score = edge + score_boost
    if assessment.get("actionable_tone") == "strong":
      score += 0.05
    pick = {
      "ticker": market_ticker,
      "signal": sig,
      "label": label or sig,
      "edge": edge,
      "yes_bid": kalshi.get("yes_bid"),
      "yes_ask": kalshi.get("yes_ask"),
      "yes_mid": kalshi.get("yes_mid"),
    }
    out.append((score, sig, pick, assessment))

  primary_sig = pred.get("signal")
  if primary_sig in (Signal.LONG.value, Signal.SHORT.value):
    add(primary_sig, score_boost=0.08, label=f"Open {primary_sig}")

  late = monitor.get("late_entry_action") or ""
  if late in ("LATE LONG", "LATE SHORT"):
    add(late, score_boost=0.12, label=late)

  flip = monitor.get("flip_action") or ""
  if flip in ("FLIP LONG", "FLIP SHORT"):
    add(flip, score_boost=0.10, label=flip)

  out.sort(key=lambda x: x[0], reverse=True)
  return out


def _should_paper_exit(alert_label: str, unrealized_pnl: float | None) -> bool:
  if alert_label == "TAKE PROFIT":
    return True
  if alert_label in ("CUT LOSS", "CUT LOSSES"):
    if unrealized_pnl is None:
      return False
    return unrealized_pnl < -CUT_LOSS_EXIT_MIN_LOSS_USD
  return False


def _unrealized_pnl_usd(pos: dict[str, Any], mark_cents: int | None) -> float | None:
  if mark_cents is None:
    return None
  entry_c = int(pos["entry_price_cents"])
  contracts = int(pos["contracts"])
  if pos["side"] == "yes":
    return round(contracts * (mark_cents - entry_c) / 100.0, 2)
  return round(contracts * (entry_c - mark_cents) / 100.0, 2)


def _position_alert_from_monitor(monitor: dict[str, Any]) -> dict[str, Any]:
  action = str(monitor.get("action") or "HOLD")
  if action == "TAKE PROFIT":
    return {"alert": "TAKE PROFIT", "detail": monitor.get("message", "")}
  if action == "CUT LOSS":
    return {"alert": "CUT LOSSES", "detail": monitor.get("message", "")}
  return {"alert": "HOLD", "detail": monitor.get("message", "Monitoring slot")}


def enrich_open_positions_live(
  positions: list[dict[str, Any]],
  tab: dict[str, Any],
) -> list[dict[str, Any]]:
  """Attach live mark, unrealized P&L, and position alert to open bot legs."""
  kalshi = tab.get("kalshi") or {}
  monitor = tab.get("monitor") or {}
  yes_cents = _yes_mid_cents(kalshi)
  alert = _position_alert_from_monitor(monitor)
  out: list[dict[str, Any]] = []

  for pos in positions:
    row = dict(pos)
    quote = {
      "yes_bid": kalshi.get("yes_bid"),
      "yes_ask": kalshi.get("yes_ask"),
      "yes_mid": kalshi.get("yes_mid"),
    }
    fill = paper_exit_fill(pick=quote, side=str(pos["side"]))
    mark = int(fill["price_cents"]) if fill.get("ok") else _price_cents_for_side(yes_cents, str(pos["side"]))
    row["mark_price_cents"] = mark
    row["unrealized_pnl_usd"] = _unrealized_pnl_usd(pos, mark)
    row["current_signal"] = monitor.get("signal_at_open")
    row["position_alert"] = alert
    out.append(row)
  return out


class Slot15Bot:
  def __init__(self, store: Slot15BotStore, kalshi_client: Any | None = None, *, asset: str = "btc"):
    self.store = store
    self.kalshi = kalshi_client
    self.asset = asset.lower()

  def run_continuous_cycle(self, tab: dict[str, Any]) -> list[dict[str, Any]]:
    """Evaluate exits then entries on live 15m data. Returns actions taken."""
    if not tab.get("ok"):
      self.store.set_last_skip_reason("prediction_unavailable")
      return []

    slot_key = tab.get("slot_key")
    if not slot_key:
      self.store.set_last_skip_reason("missing_slot_key")
      return []

    settings, prev_period = self.store.sync_period(str(slot_key), self.store.get_settings())
    if not settings.enabled:
      self.store.set_last_skip_reason("auto_bet_off")
      return []
    if not settings.continuous:
      self.store.set_last_skip_reason("continuous_mode_off")
      return []

    actions: list[dict[str, Any]] = []
    if prev_period:
      kalshi = tab.get("kalshi") or {}
      market_ticker = str(kalshi.get("market_ticker") or "") or None
      yes_cents = _yes_mid_cents(kalshi)

      def _exit_cents(pos: dict[str, Any]) -> int:
        return resolve_rollover_exit_cents(
          pos,
          current_market_ticker=market_ticker,
          quote={
            "yes_bid": kalshi.get("yes_bid"),
            "yes_ask": kalshi.get("yes_ask"),
            "yes_mid": kalshi.get("yes_mid"),
          },
          yes_mid_cents=yes_cents,
          price_for_side=_price_cents_for_side,
        )

      actions.extend(
        force_close_period_positions(
          self.store,
          prev_period,
          exit_cents_for_position=_exit_cents,
          settings=settings,
          log_label=f"{self.asset.upper()} 15m",
        )
      )
      settings = self.store.get_settings()

    actions.extend(self._process_exits(tab, slot_key, settings))
    settings = self.store.get_settings()
    stop_row = self._maybe_auto_stop_on_budget_exhausted(slot_key, settings)
    if stop_row:
      actions.append(stop_row)
      settings = self.store.get_settings()
    entry_actions = self._process_entries(tab, slot_key, settings)
    actions.extend(entry_actions)
    if not entry_actions and not any(a.get("action") == "enter" for a in actions):
      if self.store.last_skip_reason() is None and not settings.auto_stopped:
        self.store.set_last_skip_reason("no_entry_this_cycle")
    return actions

  def _maybe_auto_stop_on_budget_exhausted(
    self,
    slot_key: str,
    settings: Slot15BotSettings,
  ) -> dict[str, Any] | None:
    if not settings.enabled or not settings.auto_stop_on_budget_exhausted:
      return None
    max_cap = settings.max_spend_per_slot_usd
    bankroll = self.store.slot_bankroll_usd(slot_key, max_cap, settings)
    exposure = self.store.open_exposure_usd(slot_key)
    if settings.mode == "paper":
      if exposure > 0:
        return None
      if bankroll > 0:
        return None
      if settings.paper_auto_refill:
        state = self.store.refill_paper_bankroll(max_cap)
        detail = (
          f"Paper bankroll refilled to ${max_cap:.2f} "
          f"(refill #{state['paper_refill_count']}, "
          f"total invested ${state['paper_total_invested_usd']:.2f}, "
          f"net P&L ${state['paper_net_vs_invested_usd']:.2f})"
        )
        row = self.store.log_trade({
          "event_ticker": slot_key,
          "trigger": "continuous",
          "action": "paper_refill",
          "mode": settings.mode,
          "status": "filled",
          "detail": detail,
        })
        log.info("%s 15m bot paper refill: %s", self.asset.upper(), detail)
        return row
      realized = self.store.get_paper_state_dict(max_cap).get("paper_realized_all_time_usd", 0)
      detail = (
        f"Paper bankroll exhausted (${realized:.2f} all-time since reset, "
        f"${exposure:.2f} at risk, bankroll ${bankroll:.2f})"
      )
    else:
      if bankroll > 0:
        return None
      realized = self.store.realized_pnl_usd(slot_key)
      detail = (
        f"Slot bankroll exhausted (${realized:.2f} realized, "
        f"${exposure:.2f} at risk, max ${max_cap:.2f})"
      )
    updated = Slot15BotSettings(
      **{
        **settings.to_dict(),
        "auto_stopped": True,
      }
    )
    self.store.save_settings(updated)
    self.store.set_last_skip_reason("auto_stopped_budget_exhausted")
    row = self.store.log_trade({
      "event_ticker": slot_key,
      "trigger": "continuous",
      "action": "auto_stop",
      "mode": settings.mode,
      "status": "filled",
      "detail": detail,
    })
    log.warning("%s 15m bot auto-stopped: %s", self.asset.upper(), detail)
    return row

  def _resolve_exit(
    self,
    pos: dict[str, Any],
    monitor_action: str,
    monitor_message: str,
    unrealized: float | None,
    settings: Slot15BotSettings,
    *,
    peaks: dict[str, float],
    exit_ctx: AdaptiveExitContext,
  ) -> tuple[str | None, str]:
    if monitor_action == "TAKE PROFIT" and _should_paper_exit(monitor_action, unrealized):
      return "TAKE PROFIT", monitor_message
    if monitor_action in ("CUT LOSS", "CUT LOSSES") and _should_paper_exit(monitor_action, unrealized):
      return "CUT LOSSES", monitor_message
    reason, detail = evaluate_adaptive_profit_exit(
      settings=settings,
      unrealized_usd=unrealized,
      cost_usd=float(pos.get("cost_usd") or 0),
      peaks=peaks,
      hold_seconds=position_hold_seconds(pos),
      ctx=exit_ctx,
    )
    if reason:
      return reason, detail
    return None, ""

  def _process_exits(
    self,
    tab: dict[str, Any],
    slot_key: str,
    settings: Slot15BotSettings,
  ) -> list[dict[str, Any]]:
    monitor = tab.get("monitor") or {}
    kalshi = tab.get("kalshi") or {}
    action = str(monitor.get("action") or "")
    message = str(monitor.get("message") or "")
    yes_cents = _yes_mid_cents(kalshi)
    seconds_remaining = monitor.get("seconds_remaining")
    if seconds_remaining is not None:
      seconds_remaining = float(seconds_remaining)
    results: list[dict[str, Any]] = []

    for pos in self.store.open_positions(slot_key):
      fill = paper_exit_fill(
        pick={
          "yes_bid": kalshi.get("yes_bid"),
          "yes_ask": kalshi.get("yes_ask"),
          "yes_mid": kalshi.get("yes_mid"),
        },
        side=str(pos["side"]),
      )
      exit_price = int(fill["price_cents"]) if fill.get("ok") else _price_cents_for_side(yes_cents, str(pos["side"]))
      if exit_price is None:
        exit_price = pos["entry_price_cents"]
      self.store.update_position_mark(pos["id"], exit_price)

      unrealized = _unrealized_pnl_usd(pos, exit_price)
      cost_usd = float(pos.get("cost_usd") or 0)
      peaks = self.store.update_position_peaks(
        pos["id"],
        float(unrealized or 0),
        cost_usd,
      )
      exit_ctx = AdaptiveExitContext(
        seconds_remaining=seconds_remaining,
        period_seconds=900.0,
        current_edge=None,
        entry_edge=pos.get("entry_edge"),
        regime_allow_trade=True,
      )
      exit_reason, detail_suffix = self._resolve_exit(
        pos, action, message, unrealized, settings, peaks=peaks, exit_ctx=exit_ctx,
      )
      if not exit_reason:
        continue

      entry_c = int(pos["entry_price_cents"])
      contracts = int(pos["contracts"])
      if pos["side"] == "yes":
        pnl = contracts * (exit_price - entry_c) / 100.0
      else:
        pnl = contracts * (entry_c - exit_price) / 100.0

      self.store.close_position(pos["id"])
      detail = (
        f"Paper EXIT ({exit_reason}): {pos['side'].upper()} ×{contracts} "
        f"@ {exit_price}¢ (entry {entry_c}¢) — {detail_suffix}"
      )
      row = self.store.log_trade({
        "event_ticker": slot_key,
        "trigger": "continuous",
        "action": "exit",
        "mode": settings.mode,
        "market_ticker": pos["market_ticker"],
        "side": pos["side"],
        "contracts": contracts,
        "price_cents": exit_price,
        "entry_price_cents": entry_c,
        "exit_price_cents": exit_price,
        "cost_usd": 0,
        "pnl_usd": round(pnl, 2),
        "signal": monitor.get("signal_at_open"),
        "label": pos.get("label"),
        "status": "filled",
        "detail": detail,
        "position_id": pos["id"],
      })
      log.info("%s 15m bot [paper exit]: %s", self.asset.upper(), detail)
      cooldown = (
        settings.profit_exit_cooldown_seconds
        if is_profit_exit_reason(exit_reason)
        else settings.reentry_cooldown_seconds
      )
      self.store.record_exit_cooldown(
        slot_key, pos["market_ticker"], cooldown_seconds=cooldown
      )
      results.append(row)

    return results

  def _process_entries(
    self,
    tab: dict[str, Any],
    slot_key: str,
    settings: Slot15BotSettings,
  ) -> list[dict[str, Any]]:
    if settings.auto_stopped:
      self.store.set_last_skip_reason("auto_stopped_budget_exhausted")
      return []

    kalshi = tab.get("kalshi") or {}
    market_ticker = str(kalshi.get("market_ticker") or "")
    if not market_ticker:
      self.store.set_last_skip_reason("missing_market_ticker")
      return []

    remaining = self.store.remaining_budget_usd(slot_key, settings.max_spend_per_slot_usd, settings)
    bankroll = self.store.slot_bankroll_usd(slot_key, settings.max_spend_per_slot_usd, settings)
    if bankroll <= 0:
      self.store.set_last_skip_reason("slot_budget_exhausted")
      return []
    if remaining <= 0:
      self.store.set_last_skip_reason("fully_deployed")
      return []

    yes_cents = _yes_mid_cents(kalshi)
    candidates = _entry_candidates(tab)
    if not candidates:
      self.store.set_last_skip_reason("no_long_short_candidates")
      return []

    results: list[dict[str, Any]] = []
    last_reason = "no_entry_this_cycle"
    for _score, signal, pick, bet in candidates:
      if not bet_qualifies(signal, bet, settings):
        last_reason = "signal_filtered_by_settings"
        continue

      if self.store.has_open_position(slot_key, market_ticker):
        last_reason = f"already_open:{market_ticker}"
        continue

      if self.store.is_in_cooldown(slot_key, market_ticker, settings.reentry_cooldown_seconds):
        last_reason = f"reentry_cooldown:{market_ticker}"
        continue

      side = _side_from_signal(signal)
      if not side:
        last_reason = "unrecognized_signal"
        continue

      remaining = self.store.remaining_budget_usd(slot_key, settings.max_spend_per_slot_usd, settings)
      if remaining <= 0:
        last_reason = "fully_deployed"
        break

      if settings.mode == "paper":
        max_spread = int(tab.get("paper_max_spread_cents") or 40)
        entry_fill = paper_entry_fill(
          pick=pick,
          side=side,
          remaining_budget_usd=remaining,
          max_spread_cents=max_spread,
        )
        if not entry_fill.get("ok"):
          last_reason = str(entry_fill.get("skip_reason") or "no_liquidity")
          bid = entry_fill.get("bid_cents")
          ask = entry_fill.get("ask_cents")
          spread = int(ask) - int(bid) if bid is not None and ask is not None else None
          self.store.set_last_entry_attempt({
            "signal": signal,
            "side": side,
            "market_ticker": market_ticker,
            "skip_reason": last_reason,
            "bid_cents": bid,
            "ask_cents": ask,
            "spread_cents": spread,
            "max_spread_cents": max_spread,
          })
          continue
        price_cents = int(entry_fill["price_cents"])
        count = int(entry_fill["contracts"])
      else:
        price_cents = _price_cents_for_side(yes_cents, side)
        if price_cents is None:
          last_reason = "missing_kalshi_mid"
          continue
        count = _contracts_for_budget(remaining, price_cents)
        if count <= 0:
          last_reason = "budget_too_small_for_contract"
          continue

      cost_usd = round(count * price_cents / 100.0, 2)
      pid = str(uuid.uuid4())
      ref = (tab.get("monitor") or {}).get("reference_price")

      if settings.mode == "live":
        result = self._place_live_enter(
          slot_key, pick, side, count, price_cents, cost_usd, bet, settings, pid
        )
      else:
        self.store.open_position({
          "id": pid,
          "event_ticker": slot_key,
          "market_ticker": market_ticker,
          "side": side,
          "contracts": count,
          "entry_price_cents": price_cents,
          "cost_usd": cost_usd,
          "signal": signal,
          "label": pick.get("label"),
          "entry_edge": pick.get("edge"),
          "reference_price": ref,
        })
        detail = (
          f"Paper ENTER: {side.upper()} ×{count} @ {price_cents}¢ "
          f"on {market_ticker} ({signal})"
          f"{format_entry_book_detail(entry_fill)}"
        )
        result = self.store.log_trade({
          "event_ticker": slot_key,
          "trigger": "continuous",
          "action": "enter",
          "mode": "paper",
          "market_ticker": market_ticker,
          "side": side,
          "contracts": count,
          "price_cents": price_cents,
          "entry_price_cents": price_cents,
          "cost_usd": cost_usd,
          "signal": signal,
          "label": pick.get("label"),
          "actionable_headline": bet.get("actionable_headline"),
          "status": "filled",
          "detail": detail,
          "position_id": pid,
          **entry_quote_log_fields(entry_fill),
        })
        log.info("%s 15m bot [paper enter]: %s", self.asset.upper(), detail)

      self.store.set_last_entry_attempt(None)
      self.store.set_last_skip_reason(None)
      results.append(result)
      break

    if not results:
      self.store.set_last_skip_reason(last_reason)
    return results

  def _place_live_enter(
    self,
    slot_key: str,
    pick: dict[str, Any],
    side: str,
    count: int,
    price_cents: int,
    cost_usd: float,
    bet: dict[str, Any],
    settings: Slot15BotSettings,
    pid: str,
  ) -> dict[str, Any]:
    if not self.kalshi or not getattr(self.kalshi, "authenticated", False):
      return self.store.log_trade({
        "event_ticker": slot_key,
        "trigger": "continuous",
        "action": "enter",
        "mode": "live",
        "market_ticker": pick.get("ticker"),
        "status": "failed",
        "detail": "Live mode requires Kalshi API credentials",
      })
    try:
      order = self.kalshi.create_order(
        ticker=str(pick["ticker"]),
        side=side,
        count=count,
        yes_price=price_cents if side == "yes" else None,
        no_price=price_cents if side == "no" else None,
      )
      oid = (order.get("order") or order).get("order_id") or order.get("order_id")
      self.store.open_position({
        "id": pid,
        "event_ticker": slot_key,
        "market_ticker": str(pick["ticker"]),
        "side": side,
        "contracts": count,
        "entry_price_cents": price_cents,
        "cost_usd": cost_usd,
        "signal": pick.get("signal"),
        "label": pick.get("label"),
        "entry_edge": pick.get("edge"),
      })
      return self.store.log_trade({
        "event_ticker": slot_key,
        "trigger": "continuous",
        "action": "enter",
        "mode": "live",
        "market_ticker": pick.get("ticker"),
        "side": side,
        "contracts": count,
        "price_cents": price_cents,
        "entry_price_cents": price_cents,
        "cost_usd": cost_usd,
        "signal": pick.get("signal"),
        "label": pick.get("label"),
        "actionable_headline": bet.get("actionable_headline"),
        "status": "filled",
        "kalshi_order_id": oid,
        "position_id": pid,
        "detail": f"Live ENTER order {oid}",
      })
    except Exception as e:
      log.exception("%s 15m bot live enter failed: %s", self.asset.upper(), e)
      return self.store.log_trade({
        "event_ticker": slot_key,
        "trigger": "continuous",
        "action": "enter",
        "mode": "live",
        "status": "failed",
        "detail": str(e),
      })

  def evaluate_from_tab(self, tab: dict[str, Any], *, trigger: str) -> dict[str, Any] | None:
    settings = self.store.get_settings()
    if settings.continuous:
      actions = self.run_continuous_cycle(tab)
      return actions[-1] if actions else None
    return None
