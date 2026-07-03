"""Live-mode exit guards, reconcile hygiene, and profit-take overlays."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from src.trading.bot_profit_exit import position_hold_seconds
from src.trading.entry_strategy import EntryStrategyConfig, is_tail_entry_price


@dataclass(frozen=True)
class LiveExitConfig:
  """Knobs under hourly.bot.live_exit / intra_slot.bot.live_exit."""

  cut_loss_min_usd: float = 0.20
  cut_loss_min_hold_seconds: int = 120
  block_cut_when_profitable: bool = True
  cheap_leg_cut_min_hold_seconds: int = 90
  cheap_leg_cut_min_loss_usd: float = 0.15
  block_tail_entries: bool = True
  tail_block_max_cents: int = 20
  reconcile_min_position_age_seconds: int = 45
  reconcile_grace_after_exit_seconds: int = 90
  take_profit_usd: float | None = 0.08
  profit_exit_cooldown_seconds: int | None = 30
  mid_price_take_profit_usd: float = 0.08
  mid_price_max_entry_cents: int = 60
  adopted_leg_cut_loss_min_hold_seconds: int = 300
  adopted_leg_cut_loss_min_usd: float = 0.50
  max_adopted_contracts: int = 2
  max_resting_enters_per_hour: int = 6


@dataclass(frozen=True)
class QuickExitConfig:
  """Defense/chop exit tier — shorter anti-flicker, profit/edge-governed exits."""

  enabled: bool = False
  min_hold_seconds: int = 30
  cut_loss_min_hold_seconds: int = 30
  cut_loss_min_usd: float = 0.12
  take_profit_pct: float = 0.12
  take_profit_usd: float = 0.06
  apply_when_adaptive_mode: str | None = "defense"
  apply_when_hour_momentum_state: str | None = "conservative"


@dataclass(frozen=True)
class HoldOverlayConfig:
  """Mode-aware anti-flicker floors (profit exits still govern after hold)."""

  defense_min_hold_seconds: int = 30
  conservative_min_hold_seconds: int = 30
  rally_min_hold_seconds: int = 90
  pressing_min_hold_seconds: int = 90
  normal_min_hold_seconds: int | None = None
  locked_min_hold_seconds: int | None = None


_DEFAULTS = LiveExitConfig()
_QUICK_EXIT_DEFAULTS = QuickExitConfig()
_HOLD_OVERLAY_DEFAULTS = HoldOverlayConfig()


def _bot_cfg(cfg: dict[str, Any] | None, *, kind: str) -> dict[str, Any]:
  if not cfg:
    return {}
  if kind == "slot15":
    return dict(((cfg.get("intra_slot") or {}).get("bot") or {}).get("live_exit") or {})
  return dict(((cfg.get("hourly") or {}).get("bot") or {}).get("live_exit") or {})


def live_exit_config(cfg: dict[str, Any] | None, *, kind: str = "hourly") -> LiveExitConfig:
  raw = _bot_cfg(cfg, kind=kind)
  if not raw:
    return _DEFAULTS
  kw: dict[str, Any] = {}
  for field in LiveExitConfig.__dataclass_fields__:
    if field in raw:
      kw[field] = raw[field]
  return replace(_DEFAULTS, **kw)


def _quick_exit_cfg(cfg: dict[str, Any] | None, *, kind: str) -> dict[str, Any]:
  if not cfg or kind != "hourly":
    return {}
  return dict(((cfg.get("hourly") or {}).get("bot") or {}).get("quick_exit") or {})


def quick_exit_config(cfg: dict[str, Any] | None, *, kind: str = "hourly") -> QuickExitConfig:
  raw = _quick_exit_cfg(cfg, kind=kind)
  if not raw:
    return _QUICK_EXIT_DEFAULTS
  apply_when = dict(raw.get("apply_when") or {})
  kw: dict[str, Any] = {}
  for field in QuickExitConfig.__dataclass_fields__:
    if field.startswith("apply_when_"):
      continue
    if field in raw:
      kw[field] = raw[field]
  if "adaptive_mode" in apply_when:
    kw["apply_when_adaptive_mode"] = apply_when.get("adaptive_mode")
  if "hour_momentum_state" in apply_when:
    kw["apply_when_hour_momentum_state"] = apply_when.get("hour_momentum_state")
  return replace(_QUICK_EXIT_DEFAULTS, **kw)


def quick_exit_applies(
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
  adaptive_mode: str | None = None,
  hour_momentum_state: str | None = None,
) -> bool:
  """True when quick-exit tier should overlay live profit/cut-loss guards."""
  qcfg = quick_exit_config(cfg, kind=kind)
  if not qcfg.enabled:
    return False
  mode_l = str(adaptive_mode or "").lower()
  mom_l = str(hour_momentum_state or "").lower()
  if qcfg.apply_when_adaptive_mode and mode_l == str(qcfg.apply_when_adaptive_mode).lower():
    return True
  if (
    qcfg.apply_when_hour_momentum_state
    and mom_l == str(qcfg.apply_when_hour_momentum_state).lower()
  ):
    return True
  return False


def _hold_overlay_raw(cfg: dict[str, Any] | None, *, kind: str) -> dict[str, Any]:
  if not cfg or kind != "hourly":
    return {}
  return dict(((cfg.get("hourly") or {}).get("bot") or {}).get("hold_overlays") or {})


def hold_overlay_config(cfg: dict[str, Any] | None, *, kind: str = "hourly") -> HoldOverlayConfig:
  raw = _hold_overlay_raw(cfg, kind=kind)
  if not raw:
    return _HOLD_OVERLAY_DEFAULTS
  kw: dict[str, Any] = {}
  for field in HoldOverlayConfig.__dataclass_fields__:
    if field in raw:
      kw[field] = raw[field]
  return replace(_HOLD_OVERLAY_DEFAULTS, **kw)


def effective_min_hold_seconds(
  settings_min_hold: int,
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
  adaptive_mode: str | None = None,
  hour_momentum_state: str | None = None,
) -> int:
  """Mode-aware anti-flicker floor; quick-exit tier wins in defense/chop."""
  if quick_exit_applies(
    cfg,
    kind=kind,
    adaptive_mode=adaptive_mode,
    hour_momentum_state=hour_momentum_state,
  ):
    return int(quick_exit_config(cfg, kind=kind).min_hold_seconds)

  hcfg = hold_overlay_config(cfg, kind=kind)
  mom = str(hour_momentum_state or "").lower()
  mode = str(adaptive_mode or "").lower()
  if mom == "conservative":
    return int(hcfg.conservative_min_hold_seconds)
  if mom == "pressing":
    return int(hcfg.pressing_min_hold_seconds)
  if mom == "locked" and hcfg.locked_min_hold_seconds is not None:
    return int(hcfg.locked_min_hold_seconds)
  if mode == "rally":
    return int(hcfg.rally_min_hold_seconds)
  if mode == "defense":
    return int(hcfg.defense_min_hold_seconds)
  if hcfg.normal_min_hold_seconds is not None:
    return int(hcfg.normal_min_hold_seconds)
  return int(settings_min_hold)


def apply_live_exit_entry_guards(
  estrat: EntryStrategyConfig,
  cfg: dict[str, Any] | None,
  *,
  mode: str,
  kind: str = "hourly",
) -> EntryStrategyConfig:
  """Hard-block tail entries in live when configured."""
  if mode != "live":
    return estrat
  live_exit = live_exit_config(cfg, kind=kind)
  if not live_exit.block_tail_entries:
    return estrat
  return replace(
    estrat,
    tail_entry_block=True,
    tail_entry_max_cents=min(
      estrat.tail_entry_max_cents,
      live_exit.tail_block_max_cents,
    ),
  )


def live_cut_loss_min_usd(cfg: dict[str, Any] | None, *, kind: str = "hourly") -> float:
  return live_exit_config(cfg, kind=kind).cut_loss_min_usd


def is_adopted_live_leg(pos: dict[str, Any]) -> bool:
  """Leg opened via resting-fill or kalshi-only reconcile adoption."""
  src = str(pos.get("entry_source") or "")
  return src.startswith("adopted_")


def cap_adopted_contracts(contracts_fp: float, cfg: dict[str, Any] | None, *, kind: str) -> tuple[int, float]:
  """Clamp adopted inventory to live_exit max (0 = no cap)."""
  cap = int(live_exit_config(cfg, kind=kind).max_adopted_contracts)
  rounded = max(1, int(round(contracts_fp)))
  if cap <= 0:
    return rounded, float(contracts_fp)
  capped = min(rounded, cap)
  return capped, min(float(contracts_fp), float(cap))


def resting_enter_cap_reached(
  store: Any,
  event_ticker: str,
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
) -> bool:
  """True when too many unfilled resting live enters exist for this hour/slot."""
  cap = int(live_exit_config(cfg, kind=kind).max_resting_enters_per_hour)
  if cap <= 0:
    return False
  count_fn = getattr(store, "count_resting_live_enters", None)
  if not callable(count_fn):
    return False
  return int(count_fn(event_ticker)) >= cap


def live_cut_loss_min_hold_seconds(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  settings_min_hold: int,
  adaptive_mode: str | None = None,
  hour_momentum_state: str | None = None,
) -> int:
  """Live cut-loss hold floor; respects mode-aware anti-flicker."""
  mode_hold = effective_min_hold_seconds(
    settings_min_hold,
    cfg,
    kind=kind,
    adaptive_mode=adaptive_mode,
    hour_momentum_state=hour_momentum_state,
  )
  configured = live_exit_config(cfg, kind=kind).cut_loss_min_hold_seconds
  return max(mode_hold, int(configured))


def allow_live_cut_loss(
  *,
  exit_reason: str,
  unrealized_usd: float | None,
  pos: dict[str, Any],
  settings_min_hold: int,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
  adaptive_mode: str | None = None,
  hour_momentum_state: str | None = None,
) -> bool:
  """Tighter live guards before CUT LOSSES / CHEAP LEG CUT LOSS fire."""
  if exit_reason not in ("CUT LOSSES", "CHEAP LEG CUT LOSS"):
    return True
  live_exit = live_exit_config(cfg, kind=kind)
  quick = quick_exit_applies(
    cfg,
    kind=kind,
    adaptive_mode=adaptive_mode,
    hour_momentum_state=hour_momentum_state,
  )
  qcfg = quick_exit_config(cfg, kind=kind) if quick else None
  if unrealized_usd is None:
    return False
  if live_exit.block_cut_when_profitable and unrealized_usd >= 0:
    return False
  min_loss = (
    live_exit.cheap_leg_cut_min_loss_usd
    if exit_reason == "CHEAP LEG CUT LOSS"
    else live_exit.cut_loss_min_usd
  )
  mode_hold = effective_min_hold_seconds(
    settings_min_hold,
    cfg,
    kind=kind,
    adaptive_mode=adaptive_mode,
    hour_momentum_state=hour_momentum_state,
  )
  if quick and qcfg and exit_reason == "CUT LOSSES":
    min_loss = qcfg.cut_loss_min_usd
    min_hold = max(mode_hold, int(qcfg.cut_loss_min_hold_seconds))
  elif exit_reason == "CHEAP LEG CUT LOSS":
    min_hold = max(mode_hold, int(live_exit.cheap_leg_cut_min_hold_seconds))
  else:
    min_hold = live_cut_loss_min_hold_seconds(
      cfg,
      kind=kind,
      settings_min_hold=settings_min_hold,
      adaptive_mode=adaptive_mode,
      hour_momentum_state=hour_momentum_state,
    )
  if is_adopted_live_leg(pos):
    min_loss = max(min_loss, float(live_exit.adopted_leg_cut_loss_min_usd))
    min_hold = max(min_hold, int(live_exit.adopted_leg_cut_loss_min_hold_seconds))
  if unrealized_usd >= -min_loss:
    return False
  hold = position_hold_seconds(pos)
  if min_hold > 0 and (hold is None or hold < float(min_hold)):
    return False
  return True


def effective_live_take_profit_usd(
  pos: dict[str, Any],
  settings_take_profit_usd: float,
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
) -> float:
  """Lower take-profit $ for mid-price legs in live when configured."""
  live_exit = live_exit_config(cfg, kind=kind)
  base = live_exit.take_profit_usd
  if base is None:
    base = settings_take_profit_usd
  entry_c = int(pos.get("entry_price_cents") or 0)
  if (
    entry_c > live_exit.tail_block_max_cents
    and entry_c <= live_exit.mid_price_max_entry_cents
    and live_exit.mid_price_take_profit_usd > 0
  ):
    if base <= 0:
      return live_exit.mid_price_take_profit_usd
    return min(base, live_exit.mid_price_take_profit_usd)
  return float(base)


def live_profit_exit_cooldown_seconds(
  settings_cooldown: int,
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
) -> int:
  live_exit = live_exit_config(cfg, kind=kind)
  if live_exit.profit_exit_cooldown_seconds is None:
    return settings_cooldown
  return int(live_exit.profit_exit_cooldown_seconds)


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
  return dt


def _seconds_since(raw: str | None) -> float | None:
  dt = _parse_ts(raw)
  if dt is None:
    return None
  return (datetime.now(timezone.utc) - dt).total_seconds()


def recent_unverified_exit_attempt(
  store: Any,
  *,
  event_ticker: str,
  market_ticker: str,
  side: str,
  position_id: str | None = None,
  max_age_seconds: int = 300,
) -> dict[str, Any] | None:
  """Most recent skipped live exit where Kalshi API fill_count did not match inventory."""
  side_l = str(side or "").lower()
  with store._connect() as conn:
    if position_id:
      row = conn.execute(
        """
        SELECT * FROM bot_trades
        WHERE position_id = ? AND action = 'exit' AND status = 'skipped'
          AND detail LIKE '%unverified%'
        ORDER BY created_at DESC LIMIT 1
        """,
        (position_id,),
      ).fetchone()
      if row:
        trade = dict(row)
        age = _seconds_since(trade.get("created_at"))
        if age is not None and age <= max_age_seconds:
          return trade
    row = conn.execute(
      """
      SELECT * FROM bot_trades
      WHERE event_ticker = ? AND market_ticker = ? AND side = ?
        AND action = 'exit' AND mode = 'live' AND status = 'skipped'
        AND detail LIKE '%unverified%'
      ORDER BY created_at DESC LIMIT 1
      """,
      (event_ticker, market_ticker, side_l),
    ).fetchone()
  if not row:
    return None
  trade = dict(row)
  age = _seconds_since(trade.get("created_at"))
  if age is None or age > max_age_seconds:
    return None
  return trade


def recent_exit_trade(
  store: Any,
  *,
  event_ticker: str,
  market_ticker: str,
  side: str,
  position_id: str | None = None,
  max_age_seconds: int = 300,
) -> dict[str, Any] | None:
  """Most recent filled/reconciled exit for this leg (actual Kalshi exit price)."""
  side_l = str(side or "").lower()
  with store._connect() as conn:
    if position_id:
      row = conn.execute(
        """
        SELECT * FROM bot_trades
        WHERE position_id = ? AND action = 'exit'
          AND status IN ('filled', 'reconciled')
          AND COALESCE(exit_price_cents, price_cents) IS NOT NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        (position_id,),
      ).fetchone()
      if row:
        trade = dict(row)
        age = _seconds_since(trade.get("created_at"))
        if age is not None and age <= max_age_seconds:
          return trade
    row = conn.execute(
      """
      SELECT * FROM bot_trades
      WHERE event_ticker = ? AND market_ticker = ? AND side = ?
        AND action = 'exit' AND mode = 'live'
        AND status IN ('filled', 'reconciled')
        AND COALESCE(exit_price_cents, price_cents) IS NOT NULL
      ORDER BY created_at DESC LIMIT 1
      """,
      (event_ticker, market_ticker, side_l),
    ).fetchone()
  if not row:
    return None
  trade = dict(row)
  age = _seconds_since(trade.get("created_at"))
  if age is None or age > max_age_seconds:
    return None
  return trade


def reconcile_close_blocked(
  store: Any,
  pos: dict[str, Any],
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
) -> str | None:
  """Return skip reason when reconcile-close should wait (reduce churn)."""
  live_exit = live_exit_config(cfg, kind=kind)
  hold = position_hold_seconds(pos)
  if hold is not None and hold < float(live_exit.reconcile_min_position_age_seconds):
    return "reconcile_min_age"
  event = str(pos.get("event_ticker") or "")
  ticker = str(pos.get("market_ticker") or "")
  side = str(pos.get("side") or "")
  recent = recent_exit_trade(
    store,
    event_ticker=event,
    market_ticker=ticker,
    side=side,
    position_id=str(pos.get("id") or "") or None,
    max_age_seconds=live_exit.reconcile_grace_after_exit_seconds,
  )
  if recent and str(recent.get("position_id") or "") != str(pos.get("id") or ""):
    return "reconcile_recent_exit_sibling"
  unverified = recent_unverified_exit_attempt(
    store,
    event_ticker=event,
    market_ticker=ticker,
    side=side,
    position_id=str(pos.get("id") or "") or None,
    max_age_seconds=live_exit.reconcile_grace_after_exit_seconds,
  )
  if unverified:
    return "reconcile_recent_unverified_exit"
  return None


def inferred_exit_from_recent_trade(trade: dict[str, Any] | None) -> int | None:
  if not trade:
    return None
  val = trade.get("exit_price_cents")
  if val is None:
    val = trade.get("price_cents")
  try:
    return int(val)
  except (TypeError, ValueError):
    return None


def overlay_live_profit_settings(
  settings: Any,
  pos: dict[str, Any],
  cfg: dict[str, Any] | None,
  *,
  mode: str,
  kind: str = "hourly",
  adaptive_mode: str | None = None,
  hour_momentum_state: str | None = None,
) -> Any:
  """Apply mode-aware holds and quick-exit / live profit overlays."""
  from dataclasses import replace

  min_hold = effective_min_hold_seconds(
    int(getattr(settings, "min_hold_seconds", 0)),
    cfg,
    kind=kind,
    adaptive_mode=adaptive_mode,
    hour_momentum_state=hour_momentum_state,
  )
  kw: dict[str, Any] = {"min_hold_seconds": min_hold}

  quick = quick_exit_applies(
    cfg,
    kind=kind,
    adaptive_mode=adaptive_mode,
    hour_momentum_state=hour_momentum_state,
  )
  if quick:
    qcfg = quick_exit_config(cfg, kind=kind)
    kw["take_profit_pct"] = float(qcfg.take_profit_pct)
    kw["take_profit_usd"] = float(qcfg.take_profit_usd)
    kw["take_profit_either_threshold"] = True

  if mode == "live":
    base_tp = float(kw.get("take_profit_usd", getattr(settings, "take_profit_usd", 0.0)))
    if quick:
      kw["take_profit_usd"] = base_tp
    else:
      kw["take_profit_usd"] = effective_live_take_profit_usd(pos, base_tp, cfg, kind=kind)
    kw["profit_exit_cooldown_seconds"] = live_profit_exit_cooldown_seconds(
      int(getattr(settings, "profit_exit_cooldown_seconds", 60)),
      cfg,
      kind=kind,
    )

  return replace(settings, **kw)
