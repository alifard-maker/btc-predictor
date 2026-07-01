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


_DEFAULTS = LiveExitConfig()


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
) -> int:
  """Live cut-loss hold floor; never below bot min_hold_seconds."""
  configured = live_exit_config(cfg, kind=kind).cut_loss_min_hold_seconds
  return max(int(settings_min_hold), int(configured))


def allow_live_cut_loss(
  *,
  exit_reason: str,
  unrealized_usd: float | None,
  pos: dict[str, Any],
  settings_min_hold: int,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
) -> bool:
  """Tighter live guards before CUT LOSSES / CHEAP LEG CUT LOSS fire."""
  if exit_reason not in ("CUT LOSSES", "CHEAP LEG CUT LOSS"):
    return True
  live_exit = live_exit_config(cfg, kind=kind)
  if unrealized_usd is None:
    return False
  if live_exit.block_cut_when_profitable and unrealized_usd >= 0:
    return False
  min_loss = (
    live_exit.cheap_leg_cut_min_loss_usd
    if exit_reason == "CHEAP LEG CUT LOSS"
    else live_exit.cut_loss_min_usd
  )
  min_hold = (
    live_exit.cheap_leg_cut_min_hold_seconds
    if exit_reason == "CHEAP LEG CUT LOSS"
    else live_cut_loss_min_hold_seconds(
      cfg, kind=kind, settings_min_hold=settings_min_hold,
    )
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
) -> Any:
  """Apply live_exit profit-take overlays (take_profit_usd, cooldown)."""
  if mode != "live":
    return settings
  from dataclasses import replace

  tp_usd = effective_live_take_profit_usd(
    pos,
    float(getattr(settings, "take_profit_usd", 0.0)),
    cfg,
    kind=kind,
  )
  cd = live_profit_exit_cooldown_seconds(
    int(getattr(settings, "profit_exit_cooldown_seconds", 60)),
    cfg,
    kind=kind,
  )
  return replace(settings, take_profit_usd=tp_usd, profit_exit_cooldown_seconds=cd)
