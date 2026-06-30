"""Hourly auto-bet bot — continuous paper/live trading within hourly exposure cap."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from src.trading.bot_risk_gates import record_exit_and_maybe_cap, risk_gate_skip_reason, sync_auto_stop_for_risk
from src.trading.bot_period_rollover import force_close_period_positions
from src.trading.live_bracket_orders import (
  cancel_resting_orders,
  cancel_resting_orders_for_ticker,
  place_live_bracket_orders,
  place_live_exit_sell,
  resting_config_for_kind,
  aggressive_exit_limit_cents,
)
from src.trading.bot_entry_settings import hourly_entry_settings_snapshot
from src.trading.bot_cheap_leg_cooldown import (
  cheap_leg_cut_cooldown_seconds,
  cut_loss_label_cooldown_seconds,
  is_cheap_leg_cut_reason,
  is_label_cut_cooldown_reason,
  resolve_exit_cooldown_seconds,
  resolve_label_reentry_cooldown_seconds,
)
from src.trading.bot_adaptive_calibration import (
  adaptive_entry_allowed,
  record_adaptive_probe_entry,
  record_adaptive_probe_exit,
  run_adaptive_calibration_for_store,
)
from src.trading.bot_settlement_index_gate import live_settlement_index_skip_reason
from src.trading.bot_profit_exit import (
  AdaptiveExitContext,
  cheap_leg_exit_config,
  evaluate_adaptive_profit_exit,
  evaluate_cheap_leg_cut_loss,
  evaluate_slot15_contract_exits,
  effective_hourly_trial_settings,
  position_hold_seconds,
)
from src.trading.contract_signals import is_actionable_buy, is_buy_no, is_buy_yes
from src.trading.bot_entry_presets import (
  apply_bot_runtime_settings,
  effective_bot_entry_strategy,
)
from src.trading.bot_scale_in import evaluate_scale_in
from src.trading.entry_strategy import (
  correlation_block_reason,
  entry_budget_usd,
  passes_ask_edge_gate,
  passes_tail_entry_gate,
  rank_hourly_candidates,
)
from src.trading.hourly_bet_assessment import assess_contract_bet
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.hourly_exit_context import build_hourly_exit_context, format_hourly_exit_context_detail
from src.trading.hourly_intrahour_alert import assess_intrahour_opportunity
from src.trading.hourly_position_alert import assess_held_hourly_position_alert
from src.trading.hourly_regime import entry_too_close_to_settle_skip_reason
from src.trading.hourly_trial_position_alert import assess_hourly_trial_leg_position_alert
from src.trading.paper_execution import (
  entry_quote_log_fields,
  format_entry_book_detail,
  leg_pnl_usd,
  paper_entry_fill,
  paper_exit_fill,
  unrealized_leg_pnl_usd,
)

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
  primary_actionable = primary and is_actionable_buy(primary.get("signal"))
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

  alt_boost = 0.14 if not primary_actionable else 0.0
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
      add(row, bet, score_boost=alt_boost)

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
  return unrealized_leg_pnl_usd(
    side=str(pos.get("side") or "yes"),
    entry_price_cents=int(pos["entry_price_cents"]),
    mark_price_cents=mark_cents,
    contracts=int(pos["contracts"]),
  )


def enrich_open_positions_live(
  positions: list[dict[str, Any]],
  tab: dict[str, Any],
  cfg: dict[str, Any] | None = None,
  *,
  settings: HourlyBotSettings | None = None,
  bot_kind: str = "hourly",
) -> list[dict[str, Any]]:
  """Attach live mark, unrealized P&L, and position alert to open bot legs."""
  live = tab.get("live") or tab
  price = tab.get("brti_live") or live.get("current_price")
  hours_left = live.get("hours_to_settle")
  seconds_remaining = float(hours_left) * 3600.0 if hours_left is not None else None
  is_trial = bot_kind == "hourly_trial"
  out: list[dict[str, Any]] = []

  for pos in positions:
    row = dict(pos)
    pick = _find_contract_in_live(live, pos["market_ticker"])
    mark = None
    if pick:
      exit_fill = paper_exit_fill(pick=pick, side=str(pos["side"]))
      mark = int(exit_fill["price_cents"]) if exit_fill.get("ok") else None
    row["mark_price_cents"] = mark
    row["unrealized_pnl_usd"] = _unrealized_pnl_usd(pos, mark)
    row["current_signal"] = pick.get("signal") if pick else None

    if pick:
      regime = live.get("regime") or {}
      if is_trial:
        exit_ctx = AdaptiveExitContext(
          seconds_remaining=seconds_remaining,
          period_seconds=3600.0,
          current_edge=pick.get("edge"),
          entry_edge=pos.get("entry_edge"),
          regime_allow_trade=bool(regime.get("allow_trade", True)),
        )
        row["position_alert"] = assess_hourly_trial_leg_position_alert(
          pos=pos,
          pick=pick,
          mark_cents=mark,
          unrealized_pnl_usd=row.get("unrealized_pnl_usd"),
          live_price=float(price) if price else None,
          regime_allow_trade=bool(regime.get("allow_trade", True)),
          regime_reasons=list(regime.get("reasons") or []),
          cfg=cfg,
          settings=settings,
          exit_ctx=exit_ctx,
        )
      else:
        row["position_alert"] = assess_held_hourly_position_alert(
          pos=pos,
          pick=pick,
          live_price=float(price) if price else None,
          regime_allow_trade=bool(regime.get("allow_trade", True)),
          regime_reasons=list(regime.get("reasons") or []),
          unrealized_pnl_usd=row.get("unrealized_pnl_usd"),
          cfg=cfg,
        )
    else:
      row["position_alert"] = {"alert": "HOLD", "detail": "Awaiting live quote"}

    out.append(row)
  return out


class HourlyBot:
  def __init__(
    self,
    store: HourlyBotStore,
    kalshi_client: Any | None = None,
    *,
    asset: str = "btc",
    kind: str = "hourly",
  ):
    self.store = store
    self.kalshi = kalshi_client
    self.asset = asset.lower()
    self.kind = kind
    from src.trading.bot_risk_state import bot_risk_key

    self._bot_risk_key = bot_risk_key(kind, self.asset)

  def run_continuous_cycle(self, tab: dict[str, Any], *, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Evaluate exits then entries on live hourly data. Returns actions taken."""
    if not tab.get("ok"):
      self.store.set_last_skip_reason("prediction_unavailable")
      return []

    event_ticker = (tab.get("event") or {}).get("event_ticker")
    if not event_ticker:
      self.store.set_last_skip_reason("missing_event_ticker")
      return []

    settings, prev_period = self.store.sync_period(str(event_ticker), self.store.get_settings())
    sync_auto_stop_for_risk(self.store, bot_key=self._bot_risk_key, cfg=cfg)
    settings = apply_bot_runtime_settings(self.store.get_settings(), bot_kind=self.kind)
    if not settings.enabled:
      self.store.set_last_skip_reason("auto_bet_off")
      return []
    if not settings.continuous:
      self.store.set_last_skip_reason("continuous_mode_off")
      return []

    actions: list[dict[str, Any]] = []
    if prev_period:
      live = tab.get("live") or tab
      settle_price = tab.get("brti_live") or live.get("current_price")
      rollover_notes: dict[str, str] = {}

      def _market_exit_cents(pos: dict[str, Any]) -> int:
        pick = _find_contract_in_live(live, pos["market_ticker"])
        if pick:
          fill = paper_exit_fill(pick=pick, side=str(pos.get("side") or ""))
          if fill.get("ok") and fill.get("price_cents") is not None:
            return int(fill["price_cents"])
        last_mark = pos.get("last_mark_cents")
        if last_mark is not None:
          return int(last_mark)
        return int(pos["entry_price_cents"])

      def _exit_cents(pos: dict[str, Any]) -> int:
        from src.trading.hourly_settlement import resolve_hourly_rollover_exit_cents

        pick = _find_contract_in_live(live, pos["market_ticker"])
        market = _market_exit_cents(pos)
        settle = float(settle_price) if settle_price is not None else None
        cents, note = resolve_hourly_rollover_exit_cents(
          pos,
          settle_price=settle,
          pick=pick,
          market_exit_cents=market,
        )
        rollover_notes[str(pos["id"])] = note
        return cents

      def _rollover_detail(pos: dict[str, Any], exit_price: int, _pnl: float) -> str:
        from src.trading.bot_position_mode import exit_mode_label

        contracts = int(pos["contracts"])
        entry_c = int(pos["entry_price_cents"])
        note = rollover_notes.get(str(pos["id"]), "")
        index_id = str(live.get("index_id") or live.get("settlement_reference") or "BRTI")
        settle_line = ""
        if settle_price is not None:
          try:
            settle_line = f" · Vet: {index_id} ${float(settle_price):,.2f} at settle"
          except (TypeError, ValueError):
            pass
        mode_label = exit_mode_label(pos, settings_mode=settings.mode)
        return (
          f"{mode_label} EXIT (PERIOD SETTLEMENT): {pos['side'].upper()} ×{contracts} "
          f"@ {exit_price}¢ (entry {entry_c}¢) — {note}{settle_line}"
        )

      actions.extend(
        force_close_period_positions(
          self.store,
          prev_period,
          exit_cents_for_position=_exit_cents,
          settings=settings,
          log_label=f"{self.asset.upper()} hourly",
          format_detail=_rollover_detail,
        )
      )
      settings = apply_bot_runtime_settings(self.store.get_settings(), bot_kind=self.kind)

    actions.extend(self._process_exits(tab, event_ticker, settings, cfg))
    settings = apply_bot_runtime_settings(self.store.get_settings(), bot_kind=self.kind)
    stop_row = self._maybe_auto_stop_on_budget_exhausted(event_ticker, settings)
    if stop_row:
      actions.append(stop_row)
      settings = apply_bot_runtime_settings(self.store.get_settings(), bot_kind=self.kind)
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
          "event_ticker": event_ticker,
          "trigger": "continuous",
          "action": "paper_refill",
          "mode": settings.mode,
          "status": "filled",
          "detail": detail,
        })
        log.info("%s hourly bot paper refill: %s", self.asset.upper(), detail)
        return row
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
        "auto_stop_reason": "budget_exhausted",
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
    mark_cents: int | None = None,
    cfg: dict[str, Any] | None = None,
    pick: dict[str, Any] | None = None,
    live_price: float | None = None,
    standard_hourly_alert: str | None = None,
  ) -> tuple[str | None, str]:
    """Return (exit_reason, detail_suffix) or (None, '') if position should stay open."""
    if self.kind == "hourly_trial":
      trial_settings = effective_hourly_trial_settings(settings, cfg)
      return evaluate_slot15_contract_exits(
        pos=pos,
        mark_cents=mark_cents,
        unrealized_usd=unrealized,
        monitor={},
        peaks=peaks,
        hold_seconds=position_hold_seconds(pos),
        settings=trial_settings,
        exit_ctx=exit_ctx,
        cfg=cfg,
        include_monitor_fallback=False,
        cut_loss_min_usd=CUT_LOSS_EXIT_MIN_LOSS_USD,
        bot_kind="hourly_trial",
        pick=pick,
        live_price=live_price,
        standard_hourly_alert=standard_hourly_alert,
      )

    kind = alert.get("alert")
    if kind == "TAKE PROFIT" and _should_paper_exit(alert, unrealized):
      return "TAKE PROFIT", str(alert.get("detail", ""))
    cheap_cfg = cheap_leg_exit_config(cfg, kind="hourly")
    hours_to_settle = (
      exit_ctx.seconds_remaining / 3600.0
      if exit_ctx.seconds_remaining is not None
      else None
    )
    cheap_reason, cheap_detail = evaluate_cheap_leg_cut_loss(
      pos,
      mark_cents,
      cheap_cfg,
      pick=pick,
      live_price=live_price,
      gate_on_hourly_thesis=True,
      hours_to_settle=hours_to_settle,
      bot_cfg=cfg,
    )
    if cheap_reason:
      return cheap_reason, cheap_detail
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
    price = tab.get("brti_live") or live.get("current_price")
    hours_left = live.get("hours_to_settle")
    seconds_remaining = float(hours_left) * 3600.0 if hours_left is not None else None
    results: list[dict[str, Any]] = []

    for pos in self.store.open_positions(event_ticker):
      pick = _find_contract_in_live(live, pos["market_ticker"])
      if not pick:
        continue

      regime = live.get("regime") or {}
      exit_fill = paper_exit_fill(pick=pick, side=str(pos["side"]))
      exit_price = int(exit_fill["price_cents"]) if exit_fill.get("ok") else None
      if exit_price is None:
        exit_price = pos["entry_price_cents"]
      self.store.update_position_mark(pos["id"], exit_price)

      unrealized = _unrealized_pnl_usd(pos, exit_price)
      standard_alert = None
      if self.kind == "hourly_trial":
        standard = assess_held_hourly_position_alert(
          pos=pos,
          pick=pick,
          live_price=float(price) if price else None,
          regime_allow_trade=bool(regime.get("allow_trade", True)),
          regime_reasons=list(regime.get("reasons") or []),
          unrealized_pnl_usd=unrealized,
          hours_to_settle=float(hours_left) if hours_left is not None else None,
          cfg=cfg,
        )
        standard_alert = str(standard.get("alert") or "")
        alert = {"alert": "HOLD", "detail": standard.get("detail", "")}
      else:
        alert = assess_held_hourly_position_alert(
          pos=pos,
          pick=pick,
          live_price=float(price) if price else None,
          regime_allow_trade=bool(regime.get("allow_trade", True)),
          regime_reasons=list(regime.get("reasons") or []),
          unrealized_pnl_usd=unrealized,
          hours_to_settle=float(hours_left) if hours_left is not None else None,
          cfg=cfg,
        )
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
        mark_cents=exit_price, cfg=cfg, pick=pick,
        live_price=float(price) if price is not None else None,
        standard_hourly_alert=standard_alert,
      )
      if not exit_reason:
        continue

      entry_c = int(pos["entry_price_cents"])
      contracts = int(pos["contracts"])
      pnl = float(
        leg_pnl_usd(
          entry_price_cents=entry_c,
          mark_or_exit_cents=exit_price,
          contracts=contracts,
        )
        or 0.0,
      )

      pnl_rounded = round(pnl, 2)
      from src.trading.bot_position_mode import normalize_position_mode

      pos_mode = normalize_position_mode(pos.get("mode"))
      mode_label = "Live" if pos_mode == "live" else "Paper"
      live_exit_oid = None
      if pos_mode == "live":
        cancel_resting_orders(self.kalshi, pos)
        cancel_resting_orders_for_ticker(self.kalshi, str(pos["market_ticker"]))
        sell_cents = aggressive_exit_limit_cents(int(exit_price))
        exit_result = place_live_exit_sell(
          self.kalshi,
          market_ticker=str(pos["market_ticker"]),
          side=str(pos["side"]),
          contracts=contracts,
          limit_cents=sell_cents,
        )
        live_exit_oid = exit_result.get("order_id")
        fill_count = int(exit_result.get("fill_count") or 0)
        if fill_count <= 0:
          self.store.log_trade({
            "event_ticker": event_ticker,
            "trigger": "continuous",
            "action": "exit",
            "mode": pos_mode,
            "market_ticker": pos["market_ticker"],
            "side": pos["side"],
            "contracts": contracts,
            "price_cents": sell_cents,
            "entry_price_cents": entry_c,
            "exit_price_cents": sell_cents,
            "cost_usd": 0,
            "signal": pick.get("signal"),
            "label": pos.get("label"),
            "status": "resting",
            "detail": (
              f"Live EXIT order {live_exit_oid} (0 filled — resting on Kalshi; "
              f"{int(exit_result.get('remaining_count') or contracts)} remaining) "
              f"@ {sell_cents}¢"
            ),
            "position_id": pos["id"],
            "kalshi_order_id": live_exit_oid,
          })
          continue

      self.store.close_position(pos["id"])
      index_id = str(live.get("index_id") or live.get("settlement_reference") or "BRTI")
      exit_context = build_hourly_exit_context(
        pos=pos,
        pick=pick,
        tab=tab,
        live_price=float(price) if price is not None else None,
        unrealized_pnl_usd=unrealized,
        exit_reason=exit_reason,
        position_alert=alert,
        standard_hourly_alert=standard_alert,
        bot_kind=self.kind,
        hours_to_settle=float(hours_left) if hours_left is not None else None,
      )
      vet_line = format_hourly_exit_context_detail(exit_context)
      detail = (
        f"{mode_label} EXIT ({exit_reason}): {pos['side'].upper()} ×{contracts} "
        f"@ {exit_price}¢ (entry {entry_c}¢) — {detail_suffix} · {vet_line}"
      )
      row = self.store.log_trade({
        "event_ticker": event_ticker,
        "trigger": "continuous",
        "action": "exit",
        "mode": pos_mode,
        "market_ticker": pos["market_ticker"],
        "side": pos["side"],
        "contracts": contracts,
        "price_cents": exit_price,
        "entry_price_cents": entry_c,
        "exit_price_cents": exit_price,
        "cost_usd": 0,
        "pnl_usd": pnl_rounded,
        "signal": pick.get("signal"),
        "label": pos.get("label"),
        "status": "filled",
        "detail": detail,
        "position_id": pos["id"],
        "kalshi_order_id": live_exit_oid,
        "exit_context": exit_context,
      })
      log.info("%s hourly bot [%s exit]: %s", self.asset.upper(), mode_label.lower(), detail)
      record_exit_and_maybe_cap(
        pnl_rounded, kind="hourly", asset=self.asset, store=self.store, cfg=cfg,
      )
      self._adaptive_after_exit(entry_c, pnl_rounded, cfg)
      cooldown = resolve_exit_cooldown_seconds(
        settings,
        exit_reason,
        cfg,
        bot_kind=self.kind,
        hours_to_settle=float(hours_left) if hours_left is not None else None,
      )
      self.store.record_exit_cooldown(
        event_ticker, pos["market_ticker"], cooldown_seconds=cooldown
      )
      if is_label_cut_cooldown_reason(exit_reason):
        label_cd = resolve_label_reentry_cooldown_seconds(
          exit_reason,
          cfg,
          bot_kind=self.kind,
          hours_to_settle=float(hours_left) if hours_left is not None else None,
        )
        self.store.record_cheap_leg_cut_cooldown(
          event_ticker,
          label=pos.get("label"),
          market_ticker=pos["market_ticker"],
          cooldown_seconds=label_cd,
        )
      results.append(row)

    return results

  def _adaptive_after_exit(
    self,
    entry_price_cents: int,
    pnl_usd: float,
    cfg: dict[str, Any] | None,
  ) -> None:
    state = self.store.get_adaptive_calibration()
    state = record_adaptive_probe_exit(
      state,
      entry_price_cents=entry_price_cents,
      entry_spread_cents=None,
      pnl_usd=pnl_usd,
      cfg=cfg,
      kind=self.kind,
    )
    self.store.save_adaptive_calibration(state)
    run_adaptive_calibration_for_store(self.store, cfg=cfg, kind=self.kind)

  def _adaptive_after_enter(
    self,
    entry_price_cents: int,
    entry_spread_cents: int | None,
    cfg: dict[str, Any] | None,
  ) -> None:
    state = record_adaptive_probe_entry(
      self.store.get_adaptive_calibration(),
      entry_price_cents=entry_price_cents,
      entry_spread_cents=entry_spread_cents,
      cfg=cfg,
      kind=self.kind,
    )
    self.store.save_adaptive_calibration(state)

  def _process_entries(
    self,
    tab: dict[str, Any],
    event_ticker: str,
    settings: HourlyBotSettings,
    cfg: dict[str, Any] | None,
  ) -> list[dict[str, Any]]:
    live = tab.get("live") or tab
    results: list[dict[str, Any]] = []
    gate = risk_gate_skip_reason(bot_key=self._bot_risk_key)
    if gate:
      self.store.set_last_skip_reason(gate)
      return results

    idx_gate = live_settlement_index_skip_reason(
      tab, cfg=cfg, mode=settings.mode, asset=self.asset,
    )
    if idx_gate:
      self.store.set_last_skip_reason(idx_gate)
      return results

    settle_gate = entry_too_close_to_settle_skip_reason(
      live.get("hours_to_settle"), cfg,
    )
    if settle_gate:
      self.store.set_last_skip_reason(settle_gate)
      return results

    if settings.auto_stopped:
      skip = settings.auto_stop_reason or "auto_stopped_budget_exhausted"
      if skip == "budget_exhausted":
        skip = "auto_stopped_budget_exhausted"
      self.store.set_last_skip_reason(skip)
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

    estrat = effective_bot_entry_strategy(
      cfg,
      kind=self.kind,
      aggressive=settings.aggressive_entries,
      tuning=self.store.get_auto_tuning(),
    )
    ranked = rank_hourly_candidates(candidates, estrat=estrat)
    if not ranked:
      self.store.set_last_skip_reason("no_buy_yes_no_candidates")
      return results

    open_pos = self.store.open_positions(event_ticker)
    ref_price = live.get("current_price") or tab.get("brti_live")
    try:
      ref_f = float(ref_price) if ref_price is not None else None
    except (TypeError, ValueError):
      ref_f = None

    def _resolve_pick(ticker: str) -> dict[str, Any] | None:
      return _find_contract_in_live(live, ticker)

    last_reason = "no_entry_this_cycle"
    entries_this_cycle = 0
    max_entries = estrat.max_entries_per_cycle if estrat.enabled else 1

    for _composite, _edge, _saf, pick, bet in ranked:
      if entries_this_cycle >= max_entries:
        break
      if len(self.store.open_positions(event_ticker)) >= (
        estrat.max_concurrent_positions if estrat.enabled else 99
      ):
        last_reason = "max_concurrent_positions"
        break
      if not bet_qualifies(pick, bet, settings):
        last_reason = "signal_filtered_by_settings"
        continue

      market_ticker = str(pick["ticker"])

      side = _side_from_signal(pick.get("signal"))
      if not side:
        last_reason = "unrecognized_signal"
        continue

      existing_on_ticker = [p for p in open_pos if p["market_ticker"] == market_ticker]
      allow_scale_in_ticker: str | None = None
      if existing_on_ticker:
        ok_scale, scale_reason = evaluate_scale_in(existing_on_ticker, pick, side, estrat)
        if not ok_scale:
          last_reason = scale_reason or f"already_open:{market_ticker}"
          continue
        allow_scale_in_ticker = market_ticker

      if self.store.is_in_cooldown(
        event_ticker, market_ticker, settings.reentry_cooldown_seconds
      ):
        last_reason = f"reentry_cooldown:{market_ticker}"
        continue

      cheap_cut_cd = max(
        cheap_leg_cut_cooldown_seconds(cfg, kind="hourly"),
        cut_loss_label_cooldown_seconds(cfg, kind="hourly"),
      )
      if self.store.is_in_cheap_leg_cut_cooldown(
        event_ticker,
        label=pick.get("label"),
        market_ticker=market_ticker,
        cooldown_seconds=cheap_cut_cd,
      ):
        identity = pick.get("label") or market_ticker
        last_reason = f"cheap_leg_cut_cooldown:{identity}"
        continue

      from dataclasses import replace
      from src.trading.entry_strategy import ask_cents_for_side

      est_adaptive = ask_cents_for_side(pick, side)
      ok_adapt, adapt_reason, edge_boost = adaptive_entry_allowed(
        self.store.get_adaptive_calibration(),
        entry_price_cents=est_adaptive,
        entry_spread_cents=None,
        cfg=cfg,
        kind=self.kind,
        aggressive=settings.aggressive_entries,
      )
      if not ok_adapt:
        last_reason = adapt_reason or "adaptive_bucket_blocked"
        continue
      estrat_entry = (
        replace(
          estrat,
          min_ask_edge_cents=estrat.min_ask_edge_cents + edge_boost,
          tail_entry_min_ask_edge_cents=estrat.tail_entry_min_ask_edge_cents + edge_boost,
        )
        if edge_boost > 0
        else estrat
      )

      if self.asset == "btc":
        est_price = None
        if settings.mode == "paper":
          est_price = ask_cents_for_side(pick, side)
        else:
          est_price = _price_cents_for_pick(pick, side)
        ok_tail, tail_reason, _ = passes_tail_entry_gate(
          pick, side, est_price, estrat_entry
        )
        if not ok_tail:
          last_reason = tail_reason or "tail_entry_blocked"
          continue
      else:
        ok_edge, ask_edge = passes_ask_edge_gate(pick, side, estrat_entry.min_ask_edge_cents)
        if not ok_edge:
          last_reason = f"ask_edge_too_low:{ask_edge:.0f}c"
          continue

      block = correlation_block_reason(
        open_pos,
        pick,
        side,
        resolve_pick=_resolve_pick,
        ref_price=ref_f,
        estrat=estrat,
        allow_scale_in_ticker=allow_scale_in_ticker,
      )
      if block:
        last_reason = block
        continue

      remaining = self.store.remaining_budget_usd(event_ticker, settings.max_spend_per_hour_usd, settings)
      if remaining <= 0:
        last_reason = "hour_budget_exhausted"
        break

      bankroll = self.store.hour_bankroll_usd(event_ticker, max_cap, settings)
      entries_left = max_entries - entries_this_cycle
      stake = entry_budget_usd(
        estrat=estrat,
        bankroll_usd=bankroll,
        remaining_usd=remaining,
        pick=pick,
        side=side,
        entries_left=entries_left,
      )

      if settings.mode == "paper":
        entry_fill = paper_entry_fill(pick=pick, side=side, remaining_budget_usd=stake)
        if not entry_fill.get("ok"):
          last_reason = str(entry_fill.get("skip_reason") or "no_liquidity")
          continue
        price_cents = int(entry_fill["price_cents"])
        count = int(entry_fill["contracts"])
        if self.asset == "btc":
          ok_fill, fill_reason, _ = passes_tail_entry_gate(
            pick, side, price_cents, estrat_entry
          )
          if not ok_fill:
            last_reason = fill_reason or "tail_entry_blocked"
            continue
      else:
        price_cents = _price_cents_for_pick(pick, side)
        if price_cents is None:
          last_reason = f"missing_price:{market_ticker}"
          continue
        count = _contracts_for_budget(stake, price_cents)
        if count <= 0:
          last_reason = "budget_too_small_for_contract"
          continue

      cost_usd = round(count * price_cents / 100.0, 2)
      pid = str(uuid.uuid4())
      ref = live.get("current_price") or tab.get("brti_live")
      index_id = str(live.get("index_id") or live.get("settlement_reference") or "BRTI")
      ref_note = ""
      if ref is not None:
        try:
          ref_note = f" · {index_id} ${float(ref):,.2f}"
        except (TypeError, ValueError):
          pass

      if settings.mode == "live":
        result = self._place_live_enter(
          event_ticker, pick, side, count, price_cents, cost_usd, bet, settings, pid, cfg=cfg
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
          "contract_type": pick.get("contract_type"),
          "strike_type": pick.get("strike_type"),
          "floor_strike": pick.get("floor_strike"),
          "cap_strike": pick.get("cap_strike"),
          "mode": "paper",
        })
        detail = (
          f"Paper ENTER: {side.upper()} ×{count} @ {price_cents}¢ "
          f"on {market_ticker} ({pick.get('signal')})"
          f"{ref_note}"
          f"{format_entry_book_detail(entry_fill)}"
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
          "entry_settings": hourly_entry_settings_snapshot(settings),
          **entry_quote_log_fields(entry_fill),
        })
        log.info("%s hourly bot [paper enter]: %s", self.asset.upper(), detail)

      self._adaptive_after_enter(price_cents, None, cfg)
      self.store.set_last_skip_reason(None)
      results.append(result)
      entries_this_cycle += 1
      open_pos = self.store.open_positions(event_ticker)

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
    *,
    cfg: dict[str, Any] | None = None,
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
        "entry_settings": hourly_entry_settings_snapshot(settings),
      })
    try:
      cancel_resting_orders_for_ticker(self.kalshi, str(pick["ticker"]))
      order = self.kalshi.create_order(
        ticker=str(pick["ticker"]),
        side=side,
        count=count,
        yes_price=price_cents if side == "yes" else None,
        no_price=price_cents if side == "no" else None,
      )
      from src.data.kalshi import parse_v2_order_response

      parsed = parse_v2_order_response(order)
      oid = parsed["order_id"]
      filled = int(parsed["fill_count"])
      if filled <= 0:
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
          "cost_usd": 0,
          "signal": pick.get("signal"),
          "label": pick.get("label"),
          "actionable_headline": bet.get("actionable_headline"),
          "status": "resting",
          "kalshi_order_id": oid,
          "detail": (
            f"Live ENTER order {oid} (0 filled — resting on Kalshi; "
            f"{int(parsed['remaining_count'])} remaining)"
          ),
          "entry_settings": hourly_entry_settings_snapshot(settings),
        })
      fill_count = min(filled, count)
      fill_cost = round(cost_usd * fill_count / count, 2) if count else 0.0
      cheap_cfg, resting_cfg = resting_config_for_kind(cfg, kind="hourly")
      bracket = place_live_bracket_orders(
        self.kalshi,
        market_ticker=str(pick["ticker"]),
        side=side,
        contracts=fill_count,
        entry_cents=price_cents,
        cheap_cfg=cheap_cfg,
        resting_cfg=resting_cfg,
        take_profit_pct=settings.take_profit_pct,
        min_take_profit_pct=settings.min_take_profit_pct,
        max_take_profit_pct=settings.max_take_profit_pct,
      )
      self.store.open_position({
        "id": pid,
        "event_ticker": event_ticker,
        "market_ticker": str(pick["ticker"]),
        "side": side,
        "contracts": fill_count,
        "entry_price_cents": price_cents,
        "cost_usd": fill_cost,
        "signal": pick.get("signal"),
        "label": pick.get("label"),
        "entry_edge": pick.get("edge"),
        "stop_order_id": bracket.get("stop_order_id"),
        "take_profit_order_id": bracket.get("take_profit_order_id"),
        "mode": "live",
      })
      bracket_note = ""
      if bracket.get("stop_order_id") or bracket.get("take_profit_order_id"):
        bracket_note = (
          f" | resting stop={bracket.get('stop_order_id')} "
          f"tp={bracket.get('take_profit_order_id')}"
        )
      return self.store.log_trade({
        "event_ticker": event_ticker,
        "trigger": "continuous",
        "action": "enter",
        "mode": "live",
        "market_ticker": pick.get("ticker"),
        "side": side,
        "contracts": fill_count,
        "price_cents": price_cents,
        "entry_price_cents": price_cents,
        "cost_usd": fill_cost,
        "signal": pick.get("signal"),
        "label": pick.get("label"),
        "actionable_headline": bet.get("actionable_headline"),
        "status": "filled",
        "kalshi_order_id": oid,
        "position_id": pid,
        "detail": f"Live ENTER order {oid} ({fill_count} filled){bracket_note}",
        "entry_settings": hourly_entry_settings_snapshot(settings),
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
        "entry_settings": hourly_entry_settings_snapshot(settings),
      })

  # Legacy trigger-based path (delegates to continuous when enabled)
  def evaluate_from_tab(self, tab: dict[str, Any], *, trigger: str) -> dict[str, Any] | None:
    settings = self.store.get_settings()
    if settings.continuous:
      actions = self.run_continuous_cycle(tab)
      return actions[-1] if actions else None
    return None
