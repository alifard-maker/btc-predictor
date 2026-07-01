"""Adaptive passive entry modes: rally vs defense vs hour profit lock."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.hourly_intrahour_alert import assess_intrahour_opportunity
from src.trading.live_entry_price import LiveEntryPricingConfig

AdaptiveEntryMode = Literal["rally", "defense", "locked"]


@dataclass(frozen=True)
class AdaptivePassiveConfig:
  enabled: bool = True
  profit_lock_usd: float = 1.25
  min_rally_expected_move_pct: float = 0.15
  min_rally_grind_pct: float = 0.10
  rally_cross_spread_enabled: bool = True
  rally_cross_spread_min_edge_cents: float = 15.0
  rally_cross_spread_requires_intrahour: bool = True
  rally_max_same_side_threshold_legs: int = 2
  rally_max_same_side_range_legs: int = 0
  rally_max_entries_per_cycle: int = 2
  rally_min_ask_edge_cents: float = 8.0
  rally_block_range_bands: bool = True
  defense_max_same_side_threshold_legs: int = 1
  defense_max_same_side_range_legs: int = 0
  defense_max_entries_per_cycle: int = 1
  defense_min_ask_edge_cents: float = 12.0
  defense_block_range_bands: bool = True


@dataclass(frozen=True)
class AdaptiveDecision:
  mode: AdaptiveEntryMode
  reasons: tuple[str, ...]
  intrahour_highlight: bool = False
  realized_pnl_usd: float = 0.0


def adaptive_passive_config(cfg: dict[str, Any] | None) -> AdaptivePassiveConfig:
  raw = dict(((cfg or {}).get("hourly") or {}).get("bot") or {}).get("live_adaptive") or {}
  if not raw:
    return AdaptivePassiveConfig()
  kw: dict[str, Any] = {}
  for field in AdaptivePassiveConfig.__dataclass_fields__:
    if field in raw:
      kw[field] = raw[field]
  return replace(AdaptivePassiveConfig(), **kw)


def _grind_pct(tab: dict[str, Any]) -> float | None:
  live = tab.get("live") or tab
  ref_src = tab.get("locked") or tab.get("hour_open") or live
  ref_price = ref_src.get("reference_price")
  price = live.get("current_price") or tab.get("brti_live")
  if ref_price is None or price is None:
    return None
  try:
    ref_f = float(ref_price)
    price_f = float(price)
  except (TypeError, ValueError):
    return None
  if ref_f <= 0:
    return None
  return (price_f - ref_f) / ref_f * 100.0


def assess_adaptive_passive_mode(
  *,
  tab: dict[str, Any],
  cfg: dict[str, Any] | None,
  realized_pnl_usd: float,
  aggressive: bool,
  mode: str,
) -> AdaptiveDecision:
  """Classify the hour into rally, defense, or profit-locked (no new entries)."""
  acfg = adaptive_passive_config(cfg)
  if not acfg.enabled or aggressive or mode != "live":
    return AdaptiveDecision("defense", ("adaptive_disabled",), realized_pnl_usd=realized_pnl_usd)

  if realized_pnl_usd >= acfg.profit_lock_usd:
    return AdaptiveDecision(
      "locked",
      (f"hour_profit_lock>={acfg.profit_lock_usd:.2f}",),
      realized_pnl_usd=realized_pnl_usd,
    )

  live = tab.get("live") or tab
  intrahour = tab.get("intrahour_opportunity")
  if intrahour is None:
    intrahour = assess_intrahour_opportunity(
      live=live,
      locked=tab.get("locked"),
      hour_open=tab.get("hour_open"),
      current_price=live.get("current_price") or tab.get("brti_live"),
      index_label=str(live.get("index_id") or "BRTI"),
      cfg=cfg,
    )
  highlight = bool(intrahour and intrahour.get("highlight"))

  regime = live.get("regime") or {}
  allow_trade = bool(regime.get("allow_trade", True))
  rally_reasons: list[str] = []

  if highlight:
    rally_reasons.append("intrahour_highlight")

  exp_move = live.get("expected_move_pct")
  if exp_move is not None:
    try:
      exp_f = float(exp_move)
      if allow_trade and abs(exp_f) >= acfg.min_rally_expected_move_pct:
        rally_reasons.append(f"expected_move_{exp_f:+.2f}pct")
    except (TypeError, ValueError):
      pass

  grind = _grind_pct(tab)
  if grind is not None and allow_trade and grind >= acfg.min_rally_grind_pct:
    rally_reasons.append(f"grind_up_{grind:+.2f}pct")

  if rally_reasons:
    return AdaptiveDecision(
      "rally",
      tuple(rally_reasons),
      intrahour_highlight=highlight,
      realized_pnl_usd=realized_pnl_usd,
    )

  defense_reasons: list[str] = []
  if not allow_trade:
    defense_reasons.append("regime_blocked")
  if not defense_reasons:
    defense_reasons.append("default_defense")
  return AdaptiveDecision(
    "defense",
    tuple(defense_reasons),
    intrahour_highlight=highlight,
    realized_pnl_usd=realized_pnl_usd,
  )


def apply_adaptive_passive_guards(
  estrat: EntryStrategyConfig,
  decision: AdaptiveDecision,
  cfg: dict[str, Any] | None,
) -> EntryStrategyConfig:
  acfg = adaptive_passive_config(cfg)
  if not acfg.enabled or decision.mode == "locked":
    return estrat

  if decision.mode == "rally":
    return replace(
      estrat,
      max_same_side_threshold_legs=acfg.rally_max_same_side_threshold_legs,
      max_same_side_range_legs=max(acfg.rally_max_same_side_range_legs, 1),
      max_entries_per_cycle=min(estrat.max_entries_per_cycle, acfg.rally_max_entries_per_cycle),
      min_ask_edge_cents=max(estrat.min_ask_edge_cents, acfg.rally_min_ask_edge_cents),
      allow_scale_in=False,
    )

  return replace(
    estrat,
    max_same_side_threshold_legs=acfg.defense_max_same_side_threshold_legs,
    max_same_side_range_legs=max(acfg.defense_max_same_side_range_legs, 1),
    max_entries_per_cycle=min(estrat.max_entries_per_cycle, acfg.defense_max_entries_per_cycle),
    min_ask_edge_cents=max(estrat.min_ask_edge_cents, acfg.defense_min_ask_edge_cents),
    allow_scale_in=False,
  )


def adaptive_range_band_block_reason(
  pick: dict[str, Any],
  decision: AdaptiveDecision,
  cfg: dict[str, Any] | None,
) -> str | None:
  acfg = adaptive_passive_config(cfg)
  if not acfg.enabled or str(pick.get("strike_type") or "") != "between":
    return None
  if decision.mode == "defense" and acfg.defense_block_range_bands:
    return "adaptive_defense_range_blocked"
  if decision.mode == "rally" and acfg.rally_block_range_bands:
    return "adaptive_rally_range_blocked"
  return None


def adaptive_live_entry_pricing(
  pricing: LiveEntryPricingConfig,
  decision: AdaptiveDecision,
  cfg: dict[str, Any] | None,
) -> LiveEntryPricingConfig:
  acfg = adaptive_passive_config(cfg)
  if not acfg.enabled or decision.mode != "rally" or not acfg.rally_cross_spread_enabled:
    return pricing
  return replace(
    pricing,
    cross_spread_enabled=True,
    cross_spread_min_edge_cents=acfg.rally_cross_spread_min_edge_cents,
  )


def cross_spread_allowed_for_adaptive(
  decision: AdaptiveDecision,
  cfg: dict[str, Any] | None,
) -> bool:
  acfg = adaptive_passive_config(cfg)
  if not acfg.enabled or decision.mode != "rally":
    return False
  if not acfg.rally_cross_spread_enabled:
    return False
  if acfg.rally_cross_spread_requires_intrahour and not decision.intrahour_highlight:
    return False
  return True
