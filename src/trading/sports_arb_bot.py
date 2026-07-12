"""Sports arb bot — Goal 2 dutch_same + Goal 3 value_sharp (Kalshi/Poly paper)."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from src.data.odds_api import OddsApiClient
from src.data.polymarket import PolymarketClient
from src.data.polymarket_clob import PolymarketClobClient
from src.data.sports_markets import SportsMarketDiscovery, sports_cfg, sports_enabled
from src.trading.sports_arb_engine import (
  scan_dutch_same_opportunities_with_meta,
  strategy_params_from_cfg,
)
from src.trading.sports_arb_store import SportsArbSettings, SportsArbStore
from src.trading.sports_bet_assessment import assess_sports_opportunities
from src.trading.sports_value_engine import (
  scan_value_opportunities_with_meta,
  value_params_from_cfg,
)

log = logging.getLogger(__name__)


def _ask_cents(ask: float) -> int:
  return max(1, min(99, int(math.ceil(float(ask) * 100 - 1e-9))))


def _fingerprint(opp: dict[str, Any]) -> str:
  base = f"{opp.get('event_ticker') or ''}|{opp.get('kind') or ''}"
  venue = str(opp.get("venue") or "")
  sel = str(opp.get("selection") or "")
  if venue or sel:
    return f"{base}|{venue}|{sel}"
  return base


def _already_seen(fp: str, seen: set[str]) -> bool:
  if fp in seen:
    return True
  # Dutch back-compat only: short event|kind form (no venue/selection).
  parts = fp.split("|")
  if len(parts) == 2 and fp in seen:
    return True
  if len(parts) == 2:
    return False
  # Do not let event|kind alone block value picks (venue|selection present).
  return False


class SportsArbBot:
  def __init__(self, cfg: dict[str, Any], store: SportsArbStore, *, kalshi=None):
    self.cfg = cfg
    self.store = store
    self.kalshi = kalshi
    self.discovery = SportsMarketDiscovery(cfg, kalshi=kalshi)
    self.odds = OddsApiClient(cfg)
    self.poly = PolymarketClient(cfg)
    self.poly_clob = PolymarketClobClient(cfg)
    self._seed_settings_from_cfg()

  def _seed_settings_from_cfg(self) -> None:
    bot = dict(sports_cfg(self.cfg).get("bot") or {})
    dutch = strategy_params_from_cfg(self.cfg)
    value = value_params_from_cfg(self.cfg)
    if not self.store.settings_initialized():
      patch = {
        "enabled": bool(bot.get("enabled", True)),
        "dutch_live": False,
        "value_live": False,
        "dutch_max_open_usd": float(bot.get("max_open_usd", 40.0)),
        "dutch_max_stake_usd": float(dutch.get("max_stake_usd", 5.0)),
        "value_max_open_usd": float(bot.get("max_open_usd", 40.0)),
        "value_max_stake_usd": float(value.get("max_stake_usd", 5.0)),
        "paper_bankroll_usd": float(bot.get("paper_bankroll_usd", 50.0)),
        "max_live_per_scan": int(bot.get("max_live_per_scan", 0)),
        "max_live_trades_per_day": int(bot.get("max_live_trades_per_day", 0)),
      }
      self.store.save_settings(SportsArbSettings.from_dict(patch), source="cfg_seed")
      return
    # After init: dashboard is source of truth — do not clobber.

  def _live_allowed(self) -> bool:
    bot = dict(sports_cfg(self.cfg).get("bot") or {})
    return bool(bot.get("allow_live", False))

  def _max_live_per_scan(self, settings: SportsArbSettings | None = None) -> int:
    """0 = unlimited."""
    if settings is not None:
      return max(0, int(settings.max_live_per_scan))
    bot = dict(sports_cfg(self.cfg).get("bot") or {})
    return max(0, int(bot.get("max_live_per_scan", 0)))

  def _max_live_per_day(self, settings: SportsArbSettings | None = None) -> int:
    """0 = unlimited."""
    if settings is not None:
      return max(0, int(settings.max_live_trades_per_day))
    bot = dict(sports_cfg(self.cfg).get("bot") or {})
    return max(0, int(bot.get("max_live_trades_per_day", 0)))

  def run_scan_cycle(self) -> dict[str, Any]:
    if not sports_enabled(self.cfg):
      return {"ok": True, "skipped": True, "reason": "sports_disabled"}

    settings = self.store.get_settings()
    if not settings.enabled:
      return {"ok": True, "skipped": True, "reason": "bot_disabled"}

    # Heartbeat for scans/min (expected ~6/min at poll_seconds=10)
    self.store.record_scan_tick()

    dutch_params = strategy_params_from_cfg(self.cfg)
    value_params = value_params_from_cfg(self.cfg)
    dutch_on = bool(dutch_params.get("enabled"))
    value_on = bool(value_params.get("enabled"))

    if not dutch_on and not value_on:
      self.store.record_scan([], ok=True)
      return {"ok": True, "skipped": True, "reason": "all_strategies_disabled", "opportunities": 0}

    payloads: list[dict[str, Any]] = []
    events_scanned = 0
    value_meta: dict[str, Any] = {}

    try:
      books = []
      if dutch_on or value_on:
        books = self.discovery.fetch_open_event_books()
        events_scanned = len(books)

      dutch_meta: dict[str, Any] = {}
      if dutch_on:
        dutch_stake = float(dutch_params["max_stake_usd"])
        if float(settings.dutch_max_stake_usd) > 0:
          dutch_stake = min(dutch_stake, float(settings.dutch_max_stake_usd))
        opps, dutch_meta = scan_dutch_same_opportunities_with_meta(
          books,
          fee_rate=float(dutch_params["fee_rate"]),
          min_edge_usd=float(dutch_params["min_edge_usd"]),
          max_stake_usd=dutch_stake,
          include_binary_yes_no=bool(dutch_params["include_binary_yes_no"]),
          include_multi_outcome=bool(dutch_params["include_multi_outcome"]),
          multi_max_outcomes=int(dutch_params.get("multi_max_outcomes", 8)),
          multi_min_ask_sum=float(dutch_params.get("multi_min_ask_sum", 0.78)),
          multi_max_edge_prob=float(dutch_params.get("multi_max_edge_prob", 0.06)),
        )
        payloads.extend(o.to_dict() for o in opps)

      if value_on:
        value_payloads, value_meta = self._scan_value(books, settings, value_params)
        payloads.extend(value_payloads)

      payloads = assess_sports_opportunities(payloads, books=books, cfg=self.cfg)
      payloads.sort(key=lambda o: float(o.get("edge_usd") or 0), reverse=True)
      assessment_counts = {"STRONG": 0, "MODERATE": 0, "WEAK": 0}
      for p in payloads:
        tier = str((p.get("bet_assessment") or {}).get("edge_tier") or "")
        if tier in assessment_counts:
          assessment_counts[tier] += 1
      scan_meta = {
        "events_scanned": events_scanned,
        "dutch_opportunities": sum(1 for p in payloads if p.get("strategy") == "dutch_same"),
        "value_opportunities": sum(1 for p in payloads if p.get("strategy") == "value_sharp"),
        "bet_assessment": assessment_counts,
        "dutch": dutch_meta,
        "value": value_meta,
        "books_stale": bool(getattr(self.discovery, "_last_books_stale", False)),
      }
      self.store.record_scan(payloads, ok=True, meta=scan_meta)

      actions = self._act_on_opportunities(payloads, settings, value_params)

      return {
        "ok": True,
        "events_scanned": events_scanned,
        "opportunities": len(payloads),
        "dutch_opportunities": scan_meta["dutch_opportunities"],
        "value_opportunities": scan_meta["value_opportunities"],
        "mode": settings.mode,
        "top_edge_usd": payloads[0]["edge_usd"] if payloads else 0.0,
        "actions": actions,
        "value": value_meta,
        "books_stale": scan_meta["books_stale"],
      }
    except Exception as exc:
      log.exception("sports arb scan failed: %s", exc)
      self.store.record_scan([], ok=False, error=str(exc))
      return {"ok": False, "error": str(exc)}

  def _scan_value(
    self,
    books: list,
    settings: SportsArbSettings,
    value_params: dict[str, Any],
  ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    meta: dict[str, Any] = {
      "odds_configured": self.odds.configured,
      "poly_enabled": self.poly.enabled,
    }
    if not self.odds.configured:
      meta["skipped"] = "odds_api_key_missing"
      log.warning("value_sharp enabled but ODDS_API_KEY missing")
      return [], meta

    sport_keys = self.odds.resolve_sport_keys(
      list(value_params.get("sport_keys") or self.odds.sport_keys)
    )
    try:
      sharp = self.odds.fetch_sharp_events(sport_keys)
    except Exception as exc:
      meta["error"] = str(exc)
      log.warning("value_sharp odds fetch failed: %s", exc)
      return [], meta

    meta["sharp_events"] = len(sharp)
    meta["odds_quota"] = self.odds.quota_status()

    poly_quotes = []
    if self.poly.enabled:
      try:
        poly_quotes = self.poly.fetch_moneyline_quotes(sport_keys)
      except Exception as exc:
        meta["poly_error"] = str(exc)
        log.warning("polymarket fetch failed: %s", exc)
    meta["poly_quotes"] = len(poly_quotes)

    max_stake = float(value_params["max_stake_usd"])
    if float(settings.value_max_stake_usd) > 0:
      max_stake = min(max_stake, float(settings.value_max_stake_usd))
    opps, funnel = scan_value_opportunities_with_meta(
      sharp,
      kalshi_books=books,
      poly_quotes=poly_quotes,
      min_edge_prob=float(value_params["min_edge_prob"]),
      max_stake_usd=max_stake,
      kalshi_fee_rate=float(value_params["assumed_fee_rate"]),
      poly_fee_rate=float(value_params["poly_fee_rate"]),
      min_match_score=float(value_params["min_match_score"]),
      max_edge_prob=float(value_params.get("max_edge_prob", 0.12)),
      fifa_draw_legs=bool(value_params.get("fifa_draw_legs", True)),
    )
    meta.update(funnel)
    meta["matched"] = len(opps)
    return [o.to_dict() for o in opps], meta

  def _act_on_opportunities(
    self,
    payloads: list[dict[str, Any]],
    settings: SportsArbSettings,
    value_params: dict[str, Any] | None = None,
  ) -> list[dict[str, Any]]:
    if not payloads:
      return []
    value_params = value_params or value_params_from_cfg(self.cfg)
    dutch_live = bool(settings.dutch_live)
    value_live = bool(settings.value_live) and bool(value_params.get("allow_kalshi_live", True))
    any_live = dutch_live or value_live

    if any_live:
      seen = self.store.recent_trade_fingerprints(
        hours=12.0,
        statuses=("live_filled", "live_partial", "live_submitted"),
      )
    else:
      seen = self.store.recent_trade_fingerprints(hours=12.0)
    actions: list[dict[str, Any]] = []

    paper_candidates: list[dict[str, Any]] = []
    live_candidates: list[dict[str, Any]] = []
    for opp in payloads:
      strat = str(opp.get("strategy") or "")
      venue = str(opp.get("venue") or "kalshi")
      if strat == "value_sharp":
        if venue == "polymarket":
          poly_live = (
            bool(value_live)
            and bool(self.poly_clob.allow_live)
            and str(self.poly_clob.mode) == "live"
            and bool(self.poly_clob.authenticated)
          )
          if poly_live:
            live_candidates.append(opp)
          else:
            paper_candidates.append(opp)
        elif not value_live:
          paper_candidates.append(opp)
        else:
          live_candidates.append(opp)
      elif strat == "dutch_same":
        if dutch_live:
          live_candidates.append(opp)
        else:
          paper_candidates.append(opp)
      else:
        paper_candidates.append(opp)

    for opp in paper_candidates[:8]:
      fp = _fingerprint(opp)
      if _already_seen(fp, seen):
        continue
      self.store.log_trade(opp, mode="paper", status="paper_signal")
      seen.add(fp)
      actions.append({
        "action": "paper_signal",
        "fingerprint": fp,
        "strategy": opp.get("strategy"),
        "venue": opp.get("venue"),
        "edge_usd": opp.get("edge_usd"),
      })

    if not live_candidates:
      return actions
    if not self._live_allowed():
      actions.append({"action": "live_blocked", "reason": "allow_live_false"})
      return actions
    if self.kalshi is None or not getattr(self.kalshi, "authenticated", False):
      actions.append({"action": "live_blocked", "reason": "kalshi_not_authenticated"})
      return actions

    day_n = self.store.live_trades_today_count()
    dutch_spend = self.store.live_spend_today_usd(strategy="dutch_same")
    value_spend = self.store.live_spend_today_usd(strategy="value_sharp")
    max_day = self._max_live_per_day(settings)
    max_scan = self._max_live_per_scan(settings)
    placed = 0

    for opp in live_candidates:
      if max_scan > 0 and placed >= max_scan:
        break
      if max_day > 0 and day_n + placed >= max_day:
        actions.append({"action": "live_blocked", "reason": "max_live_trades_per_day"})
        break
      cost = float(opp.get("total_cost_usd") or 0)
      strat = str(opp.get("strategy") or "")
      if strat == "dutch_same":
        budget = float(settings.dutch_max_open_usd)
        stake_cap = float(settings.dutch_max_stake_usd)
        spend = dutch_spend
      elif strat == "value_sharp":
        budget = float(settings.value_max_open_usd)
        stake_cap = float(settings.value_max_stake_usd)
        spend = value_spend
      else:
        continue
      if budget > 0 and spend + cost > budget:
        actions.append({
          "action": "live_blocked",
          "reason": f"{strat}_max_open_usd",
          "cost": cost,
          "spend": spend,
          "budget": budget,
        })
        continue
      if stake_cap > 0 and cost > stake_cap + 1e-6:
        continue
      fp = _fingerprint(opp)
      if _already_seen(fp, seen):
        continue
      if strat == "dutch_same":
        result = self._execute_live_cover(opp)
      elif strat == "value_sharp" and str(opp.get("venue") or "") == "kalshi":
        result = self._execute_live_value(opp)
      else:
        continue
      actions.append(result)
      if result.get("ok"):
        placed += 1
        day_n += 1
        seen.add(fp)
        if strat == "dutch_same":
          dutch_spend += cost
        else:
          value_spend += cost
    return actions

  def _execute_live_value(self, opp: dict[str, Any]) -> dict[str, Any]:
    """Single-leg FOK buy for Goal 3 Kalshi value (hold to settlement)."""
    legs = list(opp.get("legs") or [])
    if len(legs) < 1:
      return {"ok": False, "action": "live_skip", "reason": "need_1_leg"}
    if str(opp.get("venue") or "") == "polymarket":
      # CLOB path is scaffolded but hard-gated until allow_live + mode=live.
      token_id = str((legs[0] or {}).get("token_id") or "")
      ask = float(legs[0].get("ask") or opp.get("venue_ask") or 0)
      size = float(legs[0].get("contracts") or opp.get("contracts") or 0)
      result = self.poly_clob.place_buy(token_id=token_id, price=ask, size=size, order_type="FOK")
      if not result.get("ok"):
        self.store.log_trade(
          opp,
          mode="live" if self.poly_clob.allow_live else "paper",
          status=str(result.get("action") or "live_skip"),
          extra=result,
        )
        return result
      self.store.log_trade(opp, mode="live", status="live_submitted", extra=result)
      return result

    leg = legs[0]
    side = str(leg.get("side") or "yes").lower()
    ticker = str(leg.get("ticker") or "")
    contracts = int(leg.get("contracts") or opp.get("contracts") or 0)
    ask = float(leg.get("ask") or opp.get("venue_ask") or 0)
    if not ticker or contracts < 1 or ask <= 0:
      return {"ok": False, "action": "live_skip", "reason": "bad_leg"}

    try:
      cents = _ask_cents(ask)
      kwargs: dict[str, Any] = {
        "ticker": ticker,
        "side": side,
        "count": contracts,
        "action": "buy",
        "time_in_force": "fill_or_kill",
      }
      if side == "yes":
        kwargs["yes_price"] = cents
      else:
        kwargs["no_price"] = cents
      resp = self.kalshi.create_order(**kwargs)
      order = resp.get("order") if isinstance(resp, dict) else None
      if not isinstance(order, dict):
        order = resp if isinstance(resp, dict) else {}
      oid = str(order.get("order_id") or "")
      status = str(order.get("status") or "").lower()
      fill_count = order.get("fill_count")
      try:
        filled_n = int(float(fill_count)) if fill_count is not None else 0
      except (TypeError, ValueError):
        filled_n = 0
      leg_ok = status in ("executed", "filled") or filled_n >= contracts
      filled_leg = {
        "ticker": ticker,
        "side": side,
        "contracts": contracts,
        "ask_cents": cents,
        "order_id": oid,
        "status": status,
        "fill_count": filled_n,
      }
      if not leg_ok:
        raise RuntimeError(f"leg_not_filled:{ticker}:{status}:fill={filled_n}/{contracts}")

      self.store.log_trade(
        opp,
        mode="live",
        status="live_filled",
        extra={"orders": [filled_leg], "order_ids": [oid] if oid else [], "selection": opp.get("selection")},
      )
      log.info(
        "sports live value filled ticker=%s sel=%s edge_prob=%.3f contracts=%s",
        ticker,
        opp.get("selection"),
        float(opp.get("edge_prob") or 0),
        contracts,
      )
      return {
        "ok": True,
        "action": "live_filled",
        "strategy": "value_sharp",
        "event_ticker": opp.get("event_ticker"),
        "ticker": ticker,
        "edge_usd": opp.get("edge_usd"),
        "order_ids": [oid] if oid else [],
      }
    except Exception as exc:
      log.warning("sports live value failed: %s", exc)
      try:
        self.kalshi.cancel_resting_orders_for_ticker(ticker)
      except Exception:
        pass
      self.store.log_trade(
        opp,
        mode="live",
        status="live_failed",
        extra={"error": str(exc), "selection": opp.get("selection")},
      )
      return {
        "ok": False,
        "action": "live_failed",
        "strategy": "value_sharp",
        "error": str(exc),
        "event_ticker": opp.get("event_ticker"),
      }

  def _execute_live_cover(self, opp: dict[str, Any]) -> dict[str, Any]:
    """Place FOK/IOC buys for every cover leg; cancel leftovers on failure."""
    legs = list(opp.get("legs") or [])
    if len(legs) < 2:
      return {"ok": False, "action": "live_skip", "reason": "need_2_legs"}

    order_ids: list[str] = []
    filled_legs: list[dict[str, Any]] = []
    try:
      for leg in legs:
        side = str(leg.get("side") or "yes").lower()
        ticker = str(leg.get("ticker") or "")
        contracts = int(leg.get("contracts") or 0)
        ask = float(leg.get("ask") or 0)
        if not ticker or contracts < 1 or ask <= 0:
          raise RuntimeError(f"bad_leg:{leg}")
        cents = _ask_cents(ask)
        kwargs: dict[str, Any] = {
          "ticker": ticker,
          "side": side,
          "count": contracts,
          "action": "buy",
          "time_in_force": "fill_or_kill",
        }
        if side == "yes":
          kwargs["yes_price"] = cents
        else:
          kwargs["no_price"] = cents
        resp = self.kalshi.create_order(**kwargs)
        order = resp.get("order") if isinstance(resp, dict) else None
        if not isinstance(order, dict):
          order = resp if isinstance(resp, dict) else {}
        oid = str(order.get("order_id") or "")
        status = str(order.get("status") or "").lower()
        if oid:
          order_ids.append(oid)
        filled_legs.append({
          "ticker": ticker,
          "side": side,
          "contracts": contracts,
          "ask_cents": cents,
          "order_id": oid,
          "status": status,
          "raw": {k: order.get(k) for k in ("status", "fill_count", "remaining_count", "yes_price", "no_price") if k in order},
        })
        fill_count = order.get("fill_count")
        try:
          filled_n = int(float(fill_count)) if fill_count is not None else 0
        except (TypeError, ValueError):
          filled_n = 0
        leg_ok = status in ("executed", "filled") or filled_n >= contracts
        if not leg_ok:
          raise RuntimeError(f"leg_not_filled:{ticker}:{status}:fill={filled_n}/{contracts}")

      self.store.log_trade(
        opp,
        mode="live",
        status="live_filled",
        extra={"orders": filled_legs, "order_ids": order_ids},
      )
      log.info(
        "sports live cover filled event=%s edge=%.3f legs=%s",
        opp.get("event_ticker"),
        float(opp.get("edge_usd") or 0),
        len(filled_legs),
      )
      return {
        "ok": True,
        "action": "live_filled",
        "event_ticker": opp.get("event_ticker"),
        "edge_usd": opp.get("edge_usd"),
        "order_ids": order_ids,
      }
    except Exception as exc:
      log.warning("sports live cover failed: %s — canceling %s", exc, order_ids)
      for oid in order_ids:
        try:
          self.kalshi.cancel_order(oid)
        except Exception:
          pass
      # Also cancel any resting on those tickers
      for leg in legs:
        try:
          self.kalshi.cancel_resting_orders_for_ticker(str(leg.get("ticker")))
        except Exception:
          pass
      self.store.log_trade(
        opp,
        mode="live",
        status="live_failed",
        extra={"error": str(exc), "orders": filled_legs, "order_ids": order_ids},
      )
      return {
        "ok": False,
        "action": "live_failed",
        "error": str(exc),
        "event_ticker": opp.get("event_ticker"),
      }

  def status(self) -> dict[str, Any]:
    settings = self.store.get_settings()
    runtime = self.store.runtime()
    opps = self.store.list_opportunities(limit=50)
    params = strategy_params_from_cfg(self.cfg)
    value = value_params_from_cfg(self.cfg)
    poll = int(sports_cfg(self.cfg).get("poll_seconds", 10))
    expected_per_min = max(1, int(round(60.0 / max(1, poll))))
    scans_last_min = self.store.scans_in_last_seconds(60.0)
    runtime = {
      **runtime,
      "scans_last_minute": scans_last_min,
      "scans_per_minute_expected": expected_per_min,
      "poll_seconds": poll,
    }
    return {
      "ok": True,
      "enabled": sports_enabled(self.cfg) and settings.enabled,
      "settings": settings.to_dict(),
      "strategies": {
        "dutch_same": {
          "enabled": params.get("enabled"),
          "live": settings.dutch_live,
          "max_open_usd": settings.dutch_max_open_usd,
          "max_stake_usd": settings.dutch_max_stake_usd,
          "spend_today_usd": round(self.store.live_spend_today_usd(strategy="dutch_same"), 2),
          "trades_today": self.store.live_trades_today_count(strategy="dutch_same"),
          **{k: params[k] for k in params if k != "enabled"},
        },
        "surebet_cross": {
          "enabled": bool(((sports_cfg(self.cfg).get("strategies") or {}).get("surebet_cross") or {}).get("enabled")),
        },
        "value_sharp": {
          "enabled": value.get("enabled"),
          "live": settings.value_live,
          "max_open_usd": settings.value_max_open_usd,
          "max_stake_usd": settings.value_max_stake_usd,
          "spend_today_usd": round(self.store.live_spend_today_usd(strategy="value_sharp"), 2),
          "trades_today": self.store.live_trades_today_count(strategy="value_sharp"),
          "min_edge_prob": value.get("min_edge_prob"),
          "allow_kalshi_live": value.get("allow_kalshi_live"),
          "sport_keys": value.get("sport_keys"),
          "odds_configured": self.odds.configured,
          "odds_quota": self.odds.quota_status(),
        },
      },
      "polymarket": {
        **self.poly.status(),
        "clob": self.poly_clob.status(),
      },
      "runtime": runtime,
      "opportunities": opps,
      "opportunity_count": len(opps),
      "recent_trades": self.store.list_trades(limit=50),
      "live": {
        "allow_live": self._live_allowed(),
        "kalshi_authenticated": bool(getattr(self.kalshi, "authenticated", False)),
        "trades_today": self.store.live_trades_today_count(),
        "spend_today_usd": round(self.store.live_spend_today_usd(), 2),
        "max_live_trades_per_day": self._max_live_per_day(settings),
        "max_live_per_scan": self._max_live_per_scan(settings),
        "dutch_live": settings.dutch_live,
        "value_live": settings.value_live,
        "dutch_spend_today_usd": round(self.store.live_spend_today_usd(strategy="dutch_same"), 2),
        "value_spend_today_usd": round(self.store.live_spend_today_usd(strategy="value_sharp"), 2),
        "dutch_max_open_usd": settings.dutch_max_open_usd,
        "value_max_open_usd": settings.value_max_open_usd,
        "poly_clob_ready": bool(self.poly_clob.live_ready),
        "poly_allow_live": bool(self.poly_clob.allow_live),
      },
      "phase": "phase1_bet_assessment",
      "bet_assessment": {
        "enabled": bool(((sports_cfg(self.cfg).get("bet_assessment") or {}).get("enabled", True))),
        "phase": 1,
        "note": "Annotate only — does not block live/paper execution yet.",
      },
      "note": (
        "Shared scan. Goal 2 / Goal 3 live + $ budgets are separate. "
        "Poly CLOB auth ready but allow_live=false (no Poly orders). "
        "0 on Max/scan or Max/day = unlimited entries. "
        f"Odds key={'yes' if self.odds.configured else 'NO'}."
      ),
    }


def sports_db_path(cfg: dict[str, Any] | None) -> Path:
  logs = Path(((cfg or {}).get("paths") or {}).get("logs") or "data/logs")
  return logs / "sports_arb_bot.db"
