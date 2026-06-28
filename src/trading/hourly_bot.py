"""Hourly auto-bet bot — continuous paper/live trading within hourly exposure cap."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from src.trading.contract_signals import is_actionable_buy, is_buy_no, is_buy_yes
from src.trading.hourly_bet_assessment import assess_contract_bet
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.hourly_intrahour_alert import assess_intrahour_opportunity
from src.trading.hourly_position_alert import assess_hourly_position_alert

log = logging.getLogger(__name__)


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
    for row in (block.get("best_edge"), block.get("most_likely")):
      if not row:
        continue
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
    settings = self.store.get_settings()
    if not settings.enabled or not settings.continuous or not tab.get("ok"):
      return []

    event_ticker = (tab.get("event") or {}).get("event_ticker")
    if not event_ticker:
      return []

    actions: list[dict[str, Any]] = []
    actions.extend(self._process_exits(tab, event_ticker, settings, cfg))
    actions.extend(self._process_entries(tab, event_ticker, settings, cfg))
    return actions

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

      if alert.get("alert") not in ("CUT LOSSES", "TAKE PROFIT"):
        continue

      exit_price = _price_cents_for_pick(pick, pos["side"])
      if exit_price is None:
        exit_price = pos["entry_price_cents"]

      entry_c = int(pos["entry_price_cents"])
      contracts = int(pos["contracts"])
      if pos["side"] == "yes":
        pnl = contracts * (exit_price - entry_c) / 100.0
      else:
        pnl = contracts * (entry_c - exit_price) / 100.0

      self.store.close_position(pos["id"])
      detail = (
        f"Paper EXIT ({alert['alert']}): {pos['side'].upper()} ×{contracts} "
        f"@ {exit_price}¢ (entry {entry_c}¢) — {alert.get('detail', '')}"
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
    remaining = self.store.remaining_budget_usd(event_ticker, settings.max_spend_per_hour_usd)

    for _score, pick, bet in _entry_candidates(tab, cfg):
      if not bet_qualifies(pick, bet, settings):
        continue

      market_ticker = str(pick["ticker"])
      if self.store.has_open_position(event_ticker, market_ticker):
        continue

      side = _side_from_signal(pick.get("signal"))
      if not side:
        continue

      price_cents = _price_cents_for_pick(pick, side)
      if price_cents is None:
        continue

      remaining = self.store.remaining_budget_usd(event_ticker, settings.max_spend_per_hour_usd)
      if remaining <= 0:
        break

      count = _contracts_for_budget(remaining, price_cents)
      if count <= 0:
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

      results.append(result)
      break  # one new entry per cycle; exits free budget next minute

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
