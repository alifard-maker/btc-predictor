"""ETH hourly auto-bet bot — paper-first, optional live Kalshi orders."""

from __future__ import annotations

import logging
from typing import Any, Literal

from src.trading.contract_signals import is_buy_no, is_buy_yes
from src.trading.eth_hourly_bot_store import EthHourlyBotSettings, EthHourlyBotStore

log = logging.getLogger(__name__)

Trigger = Literal["lock_05", "late_45", "intrahour"]


def bet_qualifies(bet_assessment: dict[str, Any] | None, settings: EthHourlyBotSettings) -> bool:
  if not settings.enabled:
    return False
  if not bet_assessment or not bet_assessment.get("actionable_bet"):
    return False
  tone = bet_assessment.get("actionable_tone")
  if tone == "strong" and settings.allow_strong:
    return True
  if tone == "moderate" and settings.allow_actionable:
    return True
  return False


def _entry_blocked_by_position_alert(position_alert: dict[str, Any] | None) -> bool:
  if not position_alert:
    return False
  return position_alert.get("alert") == "CUT LOSSES"


def _price_cents_for_pick(pick: dict[str, Any], side: str) -> int | None:
  mid = pick.get("kalshi_mid")
  if mid is None:
    return None
  yes_cents = int(round(float(mid) * 100))
  yes_cents = max(1, min(99, yes_cents))
  if side == "yes":
    return yes_cents
  return max(1, min(99, 100 - yes_cents))


def _contracts_for_budget(remaining_usd: float, price_cents: int) -> int:
  if price_cents <= 0 or remaining_usd <= 0:
    return 0
  cost_per = price_cents / 100.0
  return max(0, int(remaining_usd // cost_per))


def _pick_from_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
  if not snapshot:
    return None
  return snapshot.get("primary_pick")


class EthHourlyBot:
  def __init__(self, store: EthHourlyBotStore, kalshi_client: Any | None = None):
    self.store = store
    self.kalshi = kalshi_client

  def evaluate_from_tab(
    self,
    tab: dict[str, Any],
    *,
    trigger: Trigger,
  ) -> dict[str, Any] | None:
    """Evaluate and optionally place a bet from hourly tab payload."""
    settings = self.store.get_settings()
    if not settings.enabled:
      return None

    event_ticker = (tab.get("event") or {}).get("event_ticker")
    if not event_ticker:
      return None

    if trigger == "lock_05":
      snap = tab.get("locked")
    elif trigger == "late_45":
      snap = tab.get("late_call")
    else:
      opp = tab.get("intrahour_opportunity")
      if not opp or not opp.get("highlight"):
        return None
      snap = {
        "primary_pick": opp.get("primary_pick"),
        "bet_assessment": opp.get("bet_assessment"),
        "position_alert": None,
      }

    if not snap:
      return None

    pick = _pick_from_snapshot(snap) if trigger != "intrahour" else snap.get("primary_pick")
    bet = snap.get("bet_assessment")
    pos_alert = snap.get("position_alert")

    if trigger in ("lock_05", "late_45") and _entry_blocked_by_position_alert(pos_alert):
      return self._log_skip(
        event_ticker, trigger, settings, pick, "Position alert: CUT LOSSES — no entry"
      )

    if not bet_qualifies(bet, settings):
      return None

    if not pick or not pick.get("ticker"):
      return self._log_skip(event_ticker, trigger, settings, pick, "No market ticker on pick")

    market_ticker = str(pick["ticker"])
    if self.store.already_placed(event_ticker, trigger, market_ticker):
      return None

    signal = pick.get("signal")
    if is_buy_yes(signal):
      side = "yes"
    elif is_buy_no(signal):
      side = "no"
    else:
      return self._log_skip(event_ticker, trigger, settings, pick, f"Non-actionable signal: {signal}")

    price_cents = _price_cents_for_pick(pick, side)
    if price_cents is None:
      return self._log_skip(event_ticker, trigger, settings, pick, "No Kalshi price on pick")

    spent = self.store.spent_usd(event_ticker)
    remaining = settings.max_spend_per_hour_usd - spent
    if remaining <= 0:
      return self._log_skip(event_ticker, trigger, settings, pick, "Hourly budget exhausted")

    count = _contracts_for_budget(remaining, price_cents)
    if count <= 0:
      return self._log_skip(
        event_ticker, trigger, settings, pick,
        f"Remaining ${remaining:.2f} below 1 contract @ {price_cents}¢",
      )

    cost_usd = round(count * price_cents / 100.0, 2)
    trade_base = {
      "event_ticker": event_ticker,
      "trigger": trigger,
      "mode": settings.mode,
      "market_ticker": market_ticker,
      "side": side,
      "contracts": count,
      "price_cents": price_cents,
      "cost_usd": cost_usd,
      "signal": signal,
      "label": pick.get("label"),
      "actionable_headline": (bet or {}).get("actionable_headline"),
    }

    if settings.mode == "live":
      result = self._place_live(trade_base)
    else:
      result = self._place_paper(trade_base)

    if result.get("status") == "filled":
      self.store.mark_placed(event_ticker, trigger, market_ticker)
      self.store.add_spent(event_ticker, cost_usd)

    return result

  def _place_paper(self, trade: dict[str, Any]) -> dict[str, Any]:
    detail = (
      f"Paper {trade['side'].upper()} ×{trade['contracts']} @ {trade['price_cents']}¢ "
      f"on {trade['market_ticker']} ({trade['signal']})"
    )
    row = self.store.log_trade({**trade, "status": "filled", "detail": detail})
    log.info("ETH hourly bot [paper]: %s", detail)
    return row

  def _place_live(self, trade: dict[str, Any]) -> dict[str, Any]:
    if not self.kalshi or not getattr(self.kalshi, "authenticated", False):
      return self.store.log_trade({
        **trade,
        "status": "failed",
        "detail": "Live mode requires Kalshi API credentials",
      })
    try:
      order = self.kalshi.create_order(
        ticker=trade["market_ticker"],
        side=trade["side"],
        count=int(trade["contracts"]),
        yes_price=trade["price_cents"] if trade["side"] == "yes" else None,
        no_price=trade["price_cents"] if trade["side"] == "no" else None,
      )
      oid = (order.get("order") or order).get("order_id") or order.get("order_id")
      detail = f"Live order {oid}: {trade['side'].upper()} ×{trade['contracts']} @ {trade['price_cents']}¢"
      return self.store.log_trade({
        **trade,
        "status": "filled",
        "kalshi_order_id": oid,
        "detail": detail,
      })
    except Exception as e:
      log.exception("ETH hourly bot live order failed: %s", e)
      return self.store.log_trade({
        **trade,
        "status": "failed",
        "detail": str(e),
      })

  def _log_skip(
    self,
    event_ticker: str,
    trigger: str,
    settings: EthHourlyBotSettings,
    pick: dict[str, Any] | None,
    reason: str,
  ) -> dict[str, Any]:
    return self.store.log_trade({
      "event_ticker": event_ticker,
      "trigger": trigger,
      "mode": settings.mode,
      "market_ticker": (pick or {}).get("ticker"),
      "signal": (pick or {}).get("signal"),
      "label": (pick or {}).get("label"),
      "status": "skipped",
      "detail": reason,
      "cost_usd": 0,
      "contracts": 0,
    })
