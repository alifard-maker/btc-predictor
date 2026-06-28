"""Hourly auto-bet bot — continuous paper/live trading within hourly exposure cap."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from src.trading.bot_profit_exit import (
  AdaptiveExitContext,
  evaluate_adaptive_profit_exit,
  is_profit_exit_reason,
  position_hold_seconds,
)
from src.trading.contract_signals import is_actionable_buy, is_buy_no, is_buy_yes
from src.trading.hourly_bet_assessment import assess_contract_bet
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.hourly_intrahour_alert import assess_intrahour_opportunity
from src.trading.hourly_position_alert import assess_hourly_position_alert

log = logging.getLogger(__name__)

# Skip CUT LOSSES paper-exit when mark-to-market loss is below this (avoids regime churn at ~0 P&L).
CUT_LOSS_EXIT_MIN_LOSS_USD = 0.05


def bet_qualifies(
  pick: dict[str, Any],
  bet_assessment: dict[str, Any] | None,
  settings: HourlyBotSettings,
) -> bool:
  if not settings.enabled:
    return False
  if not is_actionable_buy(pick.get("signal")):
    return False
  # Both filters off = free mode: trade any explicit BUY YES/NO within budget.
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


def _price_cents_for_pick(pick: dict[str, Any], side: str) -> int | None:
  mid = pick.get("kalshi_mid")
  if mid is None:
    prob = pick.get("model_prob")
    if prob is not None:
      mid = float(prob) if side == "yes" else 1.0 - float(prob)
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


def _side_from_signal(signal: str | None) -> str | None:
  if is_buy_yes(signal):
    return "yes"
  if is_buy_no(signal):
    return "no"
  return None


def _find_contract_in_live(live: dict[str, Any], market_ticker: str) -> dict[str, Any] | None:
  ticker = str(market_ticker)
  primary = live.get("primary_pick")
  if primary and str(primary.get("ticker")) == ticker:
    return primary
  for key in ("strategy_threshold", "strategy_range"):
    block = live.get(key) or {}
    for field in ("contracts",):
      for row in block.get(field) or []:
        if str(row.get("ticker")) == ticker:
          return row
    for field in ("most_likely", "best_edge"):
      row = block.get(field)
      if row and str(row.get("ticker")) == ticker:
        return row
  return None


def _entry_candidates(tab: dict[str, Any], cfg: dict[str, Any] | None) -> list[tuple[float, dict[str, Any], dict[str, Any]]]:
  """Ranked (score, pick, bet_assessment) entry opportunities."""
  live = tab.get("live") or tab
  locked = tab.get("locked")
  acfg = cfg or {}
  index_label = live.get("index_id") or "BRTI"
  price = tab.get("brti_live") or live.get("current_price")
  out: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
  seen: set[str] = set()

  def add(pick: dict[str, Any] | None, bet: dict[str, Any] | None, score_boost: float = 0.0) -> None:
    if not pick or not pick.get("ticker"):
      return
    t = str(pick["ticker"])
    if t in seen or not is_actionable_buy(pick.get("signal")):
      return
    seen.add(t)
    edge = abs(float(pick.get("edge") or 0))
    score = edge + score_boost
    if bet and bet.get("actionable_tone") == "strong":
      score += 0.05
    out.append((score, pick, bet or {}))

  intrahour = tab.get("intrahour_opportunity") or assess_intrahour_opportunity(
    live=live,
    locked=locked,
    hour_open=tab.get("hour_open"),
    current_price=float(price) if price else None,
    index_label=index_label,
    cfg=acfg,
  )
  if intrahour and intrahour.get("highlight"):
    add(intrahour.get("primary_pick"), intrahour.get("bet_assessment"), score_boost=0.12)

  primary = live.get("primary_pick")
  if primary:
    bet = assess_contract_bet(
      signal=primary.get("signal"),
      edge=primary.get("edge"),
      live=live,
      locked=locked,
      use_live_regime=True,
      cfg=acfg,
    )
    add(primary, bet)

  for block_key in ("strategy_threshold", "strategy_range"):
    block = live.get(block_key) or {}
    block_rows: list[dict[str, Any]] = []
    for row in (block.get("best_edge"), block.get("most_likely")):
      if row:
        block_rows.append(row)
    for row in block.get("contracts") or []:
      if row:
        block_rows.append(row)
    for row in block_rows:
      bet = assess_contract_bet(
        signal=row.get("signal"),
        edge=row.get("edge"),
        live=live,
        locked=locked,
        use_live_regime=True,
        cfg=acfg,
      )
      add(row, bet)

  out.sort(key=lambda x: x[0], reverse=True)
  return out


def _should_paper_exit(alert: dict[str, Any], unrealized_pnl: float | None) -> bool:
  """TAKE PROFIT always exits; CUT LOSSES only when position is meaningfully underwater."""
  kind = alert.get("alert")
  if kind == "TAKE PROFIT":
    return True
  if kind == "CUT LOSSES":
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


def enrich_open_positions_live(
  positions: list[dict[str, Any]],
  tab: dict[str, Any],
  cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
  """Attach live mark, unrealized P&L, and position alert to open bot legs."""
  live = tab.get("live") or tab
  locked = tab.get("locked")
  price = tab.get("brti_live") or live.get("current_price")
  out: list[dict[str, Any]] = []

  for pos in positions:
    row = dict(pos)
    pick = _find_contract_in_live(live, pos["market_ticker"])
    mark = _price_cents_for_pick(pick, pos["side"]) if pick else None
    row["mark_price_cents"] = mark
    row["unrealized_pnl_usd"] = _unrealized_pnl_usd(pos, mark)
    row["current_signal"] = pick.get("signal") if pick else None

    if pick:
      regime = live.get("regime") or {}
      row["position_alert"] = assess_hourly_position_alert(
        snapshot_kind="late_call",
        signal=pick.get("signal"),
        edge=pick.get("edge"),
        regime_allow_trade=bool(regime.get("allow_trade", True)),
        regime_reasons=list(regime.get("reasons") or []),
        bet_assessment=assess_contract_bet(
          signal=pick.get("signal"),
          edge=pick.get("edge"),
          live=live,
          locked=locked,
          use_live_regime=True,
          cfg=cfg,
        ),
        locked_signal=pos.get("signal"),
        locked_edge=pos.get("entry_edge"),
        locked_regime_allow_trade=True,
        locked_reference_price=pos.get("reference_price"),
        reference_price=live.get("current_price"),
        locked_terminal_mu=(locked or {}).get("terminal_mu"),
        terminal_mu=live.get("terminal_mu"),
        live_price=float(price) if price else None,
        cfg=cfg,
      )
    else:
      row["position_alert"] = {"alert": "HOLD", "detail": "Awaiting live quote"}

    out.append(row)
  return out


class HourlyBot:
  def __init__(self, store: HourlyBotStore, kalshi_client: Any | None = None, *, asset: str = "btc"):
    self.store = store
    self.kalshi = kalshi_client
    self.asset = asset.lower()

  def run_continuous_cycle(self, tab: dict[str, Any], *, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Evaluate exits then entries on live hourly data. Returns actions taken."""
    if not tab.get("ok"):
      self.store.set_last_skip_reason("prediction_unavailable")
      return []

    event_ticker = (tab.get("event") or {}).get("event_ticker")
    if not event_ticker:
      self.store.set_last_skip_reason("missing_event_ticker")
      return []

    settings = self.store.sync_period(str(event_ticker), self.store.get_settings())
    if not settings.enabled:
      self.store.set_last_skip_reason("auto_bet_off")
      return []
    if not settings.continuous:
      self.store.set_last_skip_reason("continuous_mode_off")
      return []

    actions: list[dict[str, Any]] = []
    actions.extend(self._process_exits(tab, event_ticker, settings, cfg))
    settings = self.store.get_settings()
    stop_row = self._maybe_auto_stop_on_budget_exhausted(event_ticker, settings)
    if stop_row:
      actions.append(stop_row)
      settings = self.store.get_settings()
    entry_actions = self._process_entries(tab, event_ticker, settings, cfg)
    actions.extend(entry_actions)
    if not entry_actions and not any(a.get("action") == "enter" for a in actions):
      if self.store.last_skip_reason() is None and not settings.auto_stopped:
        self.store.set_last_skip_reason("no_entry_this_cycle")
    return actions

  def _maybe_auto_stop_on_budget_exhausted(
    self,
    event_ticker: str,
    settings: HourlyBotSettings,
  ) -> dict[str, Any] | None:
    if not settings.enabled or not settings.auto_stop_on_budget_exhausted:
      return None
    max_cap = settings.max_spend_per_hour_usd
    bankroll = self.store.hour_bankroll_usd(event_ticker, max_cap, settings)
    exposure = self.store.open_exposure_usd(event_ticker)
    if settings.mode == "paper":
      if bankroll - exposure > 0:
        return None
      realized = self.store.get_paper_state_dict(max_cap).get("paper_realized_all_time_usd", 0)
      detail = (
        f"Paper bankroll exhausted (${realized:.2f} all-time since reset, "
        f"${exposure:.2f} at risk, bankroll ${bankroll:.2f})"
      )
    else:
      if bankroll > 0:
        return None
      realized = self.store.realized_pnl_usd(event_ticker)
      detail = (
        f"Hour bankroll exhausted (${realized:.2f} realized, "
        f"${exposure:.2f} at risk, max ${max_cap:.2f})"
      )
    updated = HourlyBotSettings(
      **{
        **settings.to_dict(),
        "auto_stopped": True,
      }
    )
    self.store.save_settings(updated)
    self.store.set_last_skip_reason("auto_stopped_budget_exhausted")
    row = self.store.log_trade({
      "event_ticker": event_ticker,
      "trigger": "continuous",
      "action": "auto_stop",
      "mode": settings.mode,
      "status": "filled",
      "detail": detail,
    })
    log.warning("%s hourly bot auto-stopped: %s", self.asset.upper(), detail)
    return row

  def _resolve_exit(
    self,
    pos: dict[str, Any],
    alert: dict[str, Any],
    unrealized: float | None,
    settings: HourlyBotSettings,
    *,
    peaks: dict[str, float],
    exit_ctx: AdaptiveExitContext,
  ) -> tuple[str | None, str]:
    """Return (exit_reason, detail_suffix) or (None, '') if position should stay open."""
    kind = alert.get("alert")
    if kind == "TAKE PROFIT" and _should_paper_exit(alert, unrealized):
      return "TAKE PROFIT", str(alert.get("detail", ""))
    if kind == "CUT LOSSES" and _should_paper_exit(alert, unrealized):
      return "CUT LOSSES", str(alert.get("detail", ""))
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
    event_ticker: str,
    settings: HourlyBotSettings,
    cfg: dict[str, Any] | None,
  ) -> list[dict[str, Any]]:
    live = tab.get("live") or tab
    locked = tab.get("locked")
    price = tab.get("brti_live") or live.get("current_price")
    hours_left = live.get("hours_to_settle")
    seconds_remaining = float(hours_left) * 3600.0 if hours_left is not None else None
    results: list[dict[str, Any]] = []

    for pos in self.store.open_positions(event_ticker):
      pick = _find_contract_in_live(live, pos["market_ticker"])
      if not pick:
        continue

      regime = live.get("regime") or {}
      alert = assess_hourly_position_alert(
        snapshot_kind="late_call",
        signal=pick.get("signal"),
        edge=pick.get("edge"),
        regime_allow_trade=bool(regime.get("allow_trade", True)),
        regime_reasons=list(regime.get("reasons") or []),
        bet_assessment=assess_contract_bet(
          signal=pick.get("signal"),
          edge=pick.get("edge"),
          live=live,
          locked=locked,
          use_live_regime=True,
          cfg=cfg,
        ),
        locked_signal=pos.get("signal"),
        locked_edge=pos.get("entry_edge"),
        locked_regime_allow_trade=True,
        locked_reference_price=pos.get("reference_price"),
        reference_price=live.get("current_price"),
        locked_terminal_mu=(locked or {}).get("terminal_mu"),
        terminal_mu=live.get("terminal_mu"),
        live_price=float(price) if price else None,
        cfg=cfg,
      )

      exit_price = _price_cents_for_pick(pick, pos["side"])
      if exit_price is None:
        exit_price = pos["entry_price_cents"]

      unrealized = _unrealized_pnl_usd(pos, exit_price)
      cost_usd = float(pos.get("cost_usd") or 0)
      peaks = self.store.update_position_peaks(
        pos["id"],
        float(unrealized or 0),
        cost_usd,
      )
      exit_ctx = AdaptiveExitContext(
        seconds_remaining=seconds_remaining,
        period_seconds=3600.0,
        current_edge=pick.get("edge"),
        entry_edge=pos.get("entry_edge"),
        regime_allow_trade=bool(regime.get("allow_trade", True)),
      )
      exit_reason, detail_suffix = self._resolve_exit(
        pos, alert, unrealized, settings, peaks=peaks, exit_ctx=exit_ctx,
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
        "event_ticker": event_ticker,
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
        "signal": pick.get("signal"),
        "label": pos.get("label"),
        "status": "filled",
        "detail": detail,
        "position_id": pos["id"],
      })
      log.info("%s hourly bot [paper exit]: %s", self.asset.upper(), detail)
      cooldown = (
        settings.profit_exit_cooldown_seconds
        if is_profit_exit_reason(exit_reason)
        else settings.reentry_cooldown_seconds
      )
      self.store.record_exit_cooldown(
        event_ticker, pos["market_ticker"], cooldown_seconds=cooldown
      )
      results.append(row)

    return results

  def _process_entries(
    self,
    tab: dict[str, Any],
    event_ticker: str,
    settings: HourlyBotSettings,
    cfg: dict[str, Any] | None,
  ) -> list[dict[str, Any]]:
    live = tab.get("live") or tab
    results: list[dict[str, Any]] = []
    if settings.auto_stopped:
      self.store.set_last_skip_reason("auto_stopped_budget_exhausted")
      return results

    max_cap = settings.max_spend_per_hour_usd
    bankroll = self.store.hour_bankroll_usd(event_ticker, max_cap, settings)
    remaining = self.store.remaining_budget_usd(event_ticker, max_cap, settings)
    if bankroll <= 0:
      self.store.set_last_skip_reason("hour_budget_exhausted")
      return results
    if remaining <= 0:
      self.store.set_last_skip_reason("fully_deployed")
      return results

    candidates = _entry_candidates(tab, cfg)
    if not candidates:
      self.store.set_last_skip_reason("no_buy_yes_no_candidates")
      return results

    last_reason = "no_entry_this_cycle"
    for _score, pick, bet in candidates:
      if not bet_qualifies(pick, bet, settings):
        last_reason = "signal_filtered_by_settings"
        continue

      market_ticker = str(pick["ticker"])
      if self.store.has_open_position(event_ticker, market_ticker):
        last_reason = f"already_open:{market_ticker}"
        continue

      if self.store.is_in_cooldown(
        event_ticker, market_ticker, settings.reentry_cooldown_seconds
      ):
        last_reason = f"reentry_cooldown:{market_ticker}"
        continue

      side = _side_from_signal(pick.get("signal"))
      if not side:
        last_reason = "unrecognized_signal"
        continue

      price_cents = _price_cents_for_pick(pick, side)
      if price_cents is None:
        last_reason = f"missing_price:{market_ticker}"
        continue

      remaining = self.store.remaining_budget_usd(event_ticker, settings.max_spend_per_hour_usd, settings)
      if remaining <= 0:
        last_reason = "hour_budget_exhausted"
        break

      count = _contracts_for_budget(remaining, price_cents)
      if count <= 0:
        last_reason = "budget_too_small_for_contract"
        continue

      cost_usd = round(count * price_cents / 100.0, 2)
      pid = str(uuid.uuid4())
      ref = live.get("current_price") or tab.get("brti_live")

      if settings.mode == "live":
        result = self._place_live_enter(
          event_ticker, pick, side, count, price_cents, cost_usd, bet, settings, pid
        )
      else:
        self.store.open_position({
          "id": pid,
          "event_ticker": event_ticker,
          "market_ticker": market_ticker,
          "side": side,
          "contracts": count,
          "entry_price_cents": price_cents,
          "cost_usd": cost_usd,
          "signal": pick.get("signal"),
          "label": pick.get("label"),
          "entry_edge": pick.get("edge"),
          "reference_price": ref,
        })
        detail = (
          f"Paper ENTER: {side.upper()} ×{count} @ {price_cents}¢ "
          f"on {market_ticker} ({pick.get('signal')})"
        )
        result = self.store.log_trade({
          "event_ticker": event_ticker,
          "trigger": "continuous",
          "action": "enter",
          "mode": "paper",
          "market_ticker": market_ticker,
          "side": side,
          "contracts": count,
          "price_cents": price_cents,
          "entry_price_cents": price_cents,
          "cost_usd": cost_usd,
          "signal": pick.get("signal"),
          "label": pick.get("label"),
          "actionable_headline": bet.get("actionable_headline"),
          "status": "filled",
          "detail": detail,
          "position_id": pid,
        })
        log.info("%s hourly bot [paper enter]: %s", self.asset.upper(), detail)

      self.store.set_last_skip_reason(None)
      results.append(result)
      break  # one new entry per cycle; exits free budget next minute

    if not results:
      self.store.set_last_skip_reason(last_reason)
    return results

  def _place_live_enter(
    self,
    event_ticker: str,
    pick: dict[str, Any],
    side: str,
    count: int,
    price_cents: int,
    cost_usd: float,
    bet: dict[str, Any],
    settings: HourlyBotSettings,
    pid: str,
  ) -> dict[str, Any]:
    if not self.kalshi or not getattr(self.kalshi, "authenticated", False):
      return self.store.log_trade({
        "event_ticker": event_ticker,
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
        "event_ticker": event_ticker,
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
        "event_ticker": event_ticker,
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
      log.exception("%s hourly bot live enter failed: %s", self.asset.upper(), e)
      return self.store.log_trade({
        "event_ticker": event_ticker,
        "trigger": "continuous",
        "action": "enter",
        "mode": "live",
        "status": "failed",
        "detail": str(e),
      })

  # Legacy trigger-based path (delegates to continuous when enabled)
  def evaluate_from_tab(self, tab: dict[str, Any], *, trigger: str) -> dict[str, Any] | None:
    settings = self.store.get_settings()
    if settings.continuous:
      actions = self.run_continuous_cycle(tab)
      return actions[-1] if actions else None
    return None
