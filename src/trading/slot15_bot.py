"""15-minute auto-bet bot — continuous paper/live trading within per-slot exposure cap."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from src.trading.edge import Signal
from src.trading.slot15_bet_assessment import assess_slot15_bet
from src.trading.slot15_bot_store import Slot15BotSettings, Slot15BotStore

log = logging.getLogger(__name__)

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
_EXIT_ACTIONS = frozenset({"TAKE PROFIT", "CUT LOSS"})


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
    mark = _price_cents_for_side(yes_cents, pos["side"])
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
    settings = self.store.get_settings()
    if not settings.enabled or not settings.continuous or not tab.get("ok"):
      return []

    slot_key = tab.get("slot_key")
    if not slot_key:
      return []

    actions: list[dict[str, Any]] = []
    actions.extend(self._process_exits(tab, slot_key, settings))
    actions.extend(self._process_entries(tab, slot_key, settings))
    return actions

  def _process_exits(
    self,
    tab: dict[str, Any],
    slot_key: str,
    settings: Slot15BotSettings,
  ) -> list[dict[str, Any]]:
    monitor = tab.get("monitor") or {}
    kalshi = tab.get("kalshi") or {}
    action = str(monitor.get("action") or "")
    if action not in _EXIT_ACTIONS:
      return []

    yes_cents = _yes_mid_cents(kalshi)
    results: list[dict[str, Any]] = []

    for pos in self.store.open_positions(slot_key):
      exit_price = _price_cents_for_side(yes_cents, pos["side"])
      if exit_price is None:
        exit_price = pos["entry_price_cents"]

      entry_c = int(pos["entry_price_cents"])
      contracts = int(pos["contracts"])
      if pos["side"] == "yes":
        pnl = contracts * (exit_price - entry_c) / 100.0
      else:
        pnl = contracts * (entry_c - exit_price) / 100.0

      self.store.close_position(pos["id"])
      alert_label = "TAKE PROFIT" if action == "TAKE PROFIT" else "CUT LOSSES"
      detail = (
        f"Paper EXIT ({alert_label}): {pos['side'].upper()} ×{contracts} "
        f"@ {exit_price}¢ (entry {entry_c}¢) — {monitor.get('message', '')}"
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
      results.append(row)

    return results

  def _process_entries(
    self,
    tab: dict[str, Any],
    slot_key: str,
    settings: Slot15BotSettings,
  ) -> list[dict[str, Any]]:
    kalshi = tab.get("kalshi") or {}
    market_ticker = str(kalshi.get("market_ticker") or "")
    if not market_ticker:
      return []

    yes_cents = _yes_mid_cents(kalshi)
    results: list[dict[str, Any]] = []

    for _score, signal, pick, bet in _entry_candidates(tab):
      if not bet_qualifies(signal, bet, settings):
        continue

      if self.store.has_open_position(slot_key, market_ticker):
        continue

      side = _side_from_signal(signal)
      if not side:
        continue

      price_cents = _price_cents_for_side(yes_cents, side)
      if price_cents is None:
        continue

      remaining = self.store.remaining_budget_usd(slot_key, settings.max_spend_per_slot_usd)
      if remaining <= 0:
        break

      count = _contracts_for_budget(remaining, price_cents)
      if count <= 0:
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
        })
        log.info("%s 15m bot [paper enter]: %s", self.asset.upper(), detail)

      results.append(result)
      break

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
