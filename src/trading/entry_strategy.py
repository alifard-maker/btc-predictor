"""Advanced entry ranking, Kelly sizing, basket entries, and correlation guards for bots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.trading.contract_signals import is_buy_no, is_buy_yes
from src.trading.paper_execution import _side_quotes_cents


@dataclass(frozen=True)
class EntryStrategyConfig:
  enabled: bool = True
  risk_adjusted_ranking: bool = True
  edge_tie_threshold: float = 0.03
  safety_weight: float = 0.40
  kelly_enabled: bool = True
  kelly_fraction: float = 0.25
  min_kelly_stake_usd: float = 1.0
  max_entries_per_cycle: int = 2
  max_concurrent_positions: int = 3
  max_budget_fraction_per_entry: float = 0.55
  correlation_guard: bool = True
  allow_barbell: bool = True
  barbell_min_strike_gap_pct: float = 0.20

  @classmethod
  def from_bot_cfg(cls, bot_cfg: dict[str, Any] | None) -> EntryStrategyConfig:
    raw = (bot_cfg or {}).get("entry_strategy") or {}
    if raw.get("enabled") is False:
      return cls(enabled=False)
    return cls(
      enabled=bool(raw.get("enabled", True)),
      risk_adjusted_ranking=bool(raw.get("risk_adjusted_ranking", True)),
      edge_tie_threshold=float(raw.get("edge_tie_threshold", 0.03)),
      safety_weight=float(raw.get("safety_weight", 0.40)),
      kelly_enabled=bool(raw.get("kelly_enabled", True)),
      kelly_fraction=float(raw.get("kelly_fraction", 0.25)),
      min_kelly_stake_usd=float(raw.get("min_kelly_stake_usd", 1.0)),
      max_entries_per_cycle=int(raw.get("max_entries_per_cycle", 2)),
      max_concurrent_positions=int(raw.get("max_concurrent_positions", 3)),
      max_budget_fraction_per_entry=float(raw.get("max_budget_fraction_per_entry", 0.55)),
      correlation_guard=bool(raw.get("correlation_guard", True)),
      allow_barbell=bool(raw.get("allow_barbell", True)),
      barbell_min_strike_gap_pct=float(raw.get("barbell_min_strike_gap_pct", 0.20)),
    )


def entry_strategy_from_cfg(cfg: dict[str, Any] | None, *, kind: str = "hourly") -> EntryStrategyConfig:
  """Load entry strategy from asset config (hourly or intra_slot bot section)."""
  if not cfg:
    return EntryStrategyConfig()
  if kind == "slot15":
    bot_cfg = (cfg.get("intra_slot") or {}).get("bot") or {}
  else:
    bot_cfg = (cfg.get("hourly") or {}).get("bot") or {}
  return EntryStrategyConfig.from_bot_cfg(bot_cfg)


def threshold_strike(pick: dict[str, Any] | None) -> float | None:
  if not pick:
    return None
  st = str(pick.get("strike_type") or "")
  if st == "greater" and pick.get("floor_strike") is not None:
    return float(pick["floor_strike"])
  if st == "less" and pick.get("cap_strike") is not None:
    return float(pick["cap_strike"])
  return None


def side_from_pick_signal(signal: str | None) -> str | None:
  if is_buy_yes(signal):
    return "yes"
  if is_buy_no(signal):
    return "no"
  return None


def win_prob_for_side(pick: dict[str, Any], side: str) -> float | None:
  raw = pick.get("model_prob")
  if raw is None:
    return None
  p = float(raw)
  if side == "yes":
    return max(0.01, min(0.99, p))
  return max(0.01, min(0.99, 1.0 - p))


def ask_cents_for_side(pick: dict[str, Any], side: str) -> int | None:
  bid, ask = _side_quotes_cents(pick, side)
  if ask is None:
    mid = pick.get("kalshi_mid")
    if mid is not None:
      yes_c = max(1, min(99, int(round(float(mid) * 100))))
      ask = yes_c if side == "yes" else max(1, min(99, 100 - yes_c))
  return int(ask) if ask is not None else None


def expected_value_per_contract_usd(p_win: float, ask_cents: int) -> float:
  """EV in USD for one contract bought at ask (payout $1 if win)."""
  cost = ask_cents / 100.0
  payout = 1.0 - cost
  return p_win * payout - (1.0 - p_win) * cost


def safety_score(p_win: float) -> float:
  """0–1: how decisive the model is (1 = very high or very low win prob)."""
  return abs(p_win - 0.5) * 2.0


def kelly_fraction_full(p_win: float, ask_cents: int) -> float:
  """Full Kelly fraction of bankroll for binary contract at ask."""
  ask = ask_cents / 100.0
  if ask <= 0 or ask >= 1:
    return 0.0
  b = (1.0 - ask) / ask
  if b <= 0:
    return 0.0
  q = 1.0 - p_win
  f = (p_win * b - q) / b
  return max(0.0, min(1.0, f))


def kelly_stake_usd(
  *,
  bankroll_usd: float,
  remaining_usd: float,
  p_win: float,
  ask_cents: int,
  kelly_fraction: float,
  max_budget_fraction_per_entry: float,
  min_stake_usd: float,
) -> float:
  f = kelly_fraction_full(p_win, ask_cents) * kelly_fraction
  if f <= 0:
    return 0.0
  cap = bankroll_usd * max_budget_fraction_per_entry
  stake = min(bankroll_usd * f, remaining_usd, cap)
  if stake < min_stake_usd:
    return 0.0
  return round(stake, 2)


def composite_entry_score(
  pick: dict[str, Any],
  side: str,
  *,
  estrat: EntryStrategyConfig,
) -> tuple[float, float, float]:
  """Return (composite_score, raw_edge, safety) for ranking."""
  edge = abs(float(pick.get("edge") or 0))
  p = win_prob_for_side(pick, side)
  if p is None:
    return edge, edge, 0.0
  saf = safety_score(p)
  ask = ask_cents_for_side(pick, side)
  if not estrat.enabled or not estrat.risk_adjusted_ranking or ask is None:
    return edge, edge, saf
  ev = expected_value_per_contract_usd(p, ask)
  # EV drives ranking; edge + safety break ties (safer bet when edges are close).
  composite = ev * 8.0 + edge + saf * estrat.safety_weight * max(edge, 0.05)
  return composite, edge, saf


def rank_hourly_candidates(
  candidates: list[tuple[float, dict[str, Any], dict[str, Any]]],
  *,
  estrat: EntryStrategyConfig,
) -> list[tuple[float, float, float, dict[str, Any], dict[str, Any]]]:
  """Re-rank (legacy_score, pick, bet) with risk-adjusted composite scoring."""
  scored: list[tuple[float, float, float, dict[str, Any], dict[str, Any]]] = []
  for _legacy, pick, bet in candidates:
    side = side_from_pick_signal(pick.get("signal"))
    if not side:
      continue
    composite, edge, saf = composite_entry_score(pick, side, estrat=estrat)
    boost = 0.0
    if bet.get("actionable_tone") == "strong":
      boost += 0.05
    scored.append((composite + boost, edge, saf, pick, bet))

  scored.sort(key=lambda row: (-row[0], -row[2], -row[1]))
  if not estrat.enabled or not estrat.risk_adjusted_ranking or len(scored) < 2:
    return scored

  # When top edges are within tie band, prefer higher safety (safer 1.1x over spicy 2x).
  top_edge = scored[0][1]
  tied = [row for row in scored if abs(row[1] - top_edge) <= estrat.edge_tie_threshold]
  if len(tied) > 1:
    rest = [row for row in scored if row not in tied]
    tied.sort(key=lambda row: (-row[2], -row[0], -row[1]))
    return tied + rest
  return scored


def _strike_gap_pct(a: float, b: float, ref: float) -> float:
  if ref <= 0:
    return abs(a - b)
  return abs(a - b) / ref * 100.0


def is_barbell_pair(
  existing: dict[str, Any],
  existing_pick: dict[str, Any] | None,
  new_pick: dict[str, Any],
  new_side: str,
  *,
  ref_price: float | None,
  min_gap_pct: float,
) -> bool:
  """YES on lower threshold + NO on higher threshold = intentional barbell."""
  if ref_price is None or ref_price <= 0:
    return False
  ex_side = str(existing.get("side") or "")
  if ex_side != "yes" or new_side != "no":
    return False
  ex_strike = threshold_strike(existing_pick)
  new_strike = threshold_strike(new_pick)
  if ex_strike is None or new_strike is None:
    return False
  if str(existing_pick.get("strike_type") or "") != "greater":
    return False
  if str(new_pick.get("strike_type") or "") != "greater":
    return False
  if new_strike <= ex_strike:
    return False
  return _strike_gap_pct(ex_strike, new_strike, ref_price) >= min_gap_pct


def correlation_block_reason(
  open_positions: list[dict[str, Any]],
  new_pick: dict[str, Any],
  new_side: str,
  *,
  resolve_pick: Any,
  ref_price: float | None,
  estrat: EntryStrategyConfig,
) -> str | None:
  """Return skip reason if new entry correlates too strongly with an open leg."""
  if not estrat.enabled or not estrat.correlation_guard:
    return None
  new_strike = threshold_strike(new_pick)
  new_type = str(new_pick.get("strike_type") or "")

  for pos in open_positions:
    ticker = str(pos.get("market_ticker") or "")
    ex_pick = resolve_pick(ticker) if resolve_pick else None
    ex_side = str(pos.get("side") or "")
    ex_strike = threshold_strike(ex_pick)
    ex_type = str((ex_pick or {}).get("strike_type") or "")

    if estrat.allow_barbell and is_barbell_pair(
      pos, ex_pick, new_pick, new_side, ref_price=ref_price, min_gap_pct=estrat.barbell_min_strike_gap_pct
    ):
      continue

    if ex_side == new_side and ticker == str(new_pick.get("ticker")):
      return f"duplicate_ticker:{ticker}"

    if (
      ex_side == new_side
      and ex_type == "greater"
      and new_type == "greater"
      and ex_strike is not None
      and new_strike is not None
      and ref_price
      and _strike_gap_pct(ex_strike, new_strike, ref_price) < 0.08
    ):
      return "correlated_same_side_strikes"

    if (
      ex_side == "yes"
      and new_side == "no"
      and ex_type == "greater"
      and new_type == "greater"
      and ex_strike is not None
      and new_strike is not None
      and new_strike > ex_strike
    ):
      return "opposing_threshold_hedge"

    if (
      ex_side == "no"
      and new_side == "yes"
      and ex_type == "greater"
      and new_type == "greater"
      and ex_strike is not None
      and new_strike is not None
      and new_strike < ex_strike
    ):
      return "opposing_threshold_hedge"

  return None


def entry_budget_usd(
  *,
  estrat: EntryStrategyConfig,
  bankroll_usd: float,
  remaining_usd: float,
  pick: dict[str, Any],
  side: str,
  entries_left: int = 1,
) -> float:
  """Kelly-sized stake capped by remaining budget; splits across basket entries when configured."""
  entries_left = max(1, int(entries_left))
  if estrat.enabled and estrat.max_entries_per_cycle > 1:
    basket_cap = remaining_usd / entries_left
  else:
    basket_cap = remaining_usd

  if not estrat.enabled or not estrat.kelly_enabled:
    return min(remaining_usd, basket_cap)

  p = win_prob_for_side(pick, side)
  ask = ask_cents_for_side(pick, side)
  if p is None or ask is None:
    return min(remaining_usd, basket_cap)

  stake = kelly_stake_usd(
    bankroll_usd=bankroll_usd,
    remaining_usd=remaining_usd,
    p_win=p,
    ask_cents=ask,
    kelly_fraction=estrat.kelly_fraction,
    max_budget_fraction_per_entry=estrat.max_budget_fraction_per_entry,
    min_stake_usd=estrat.min_kelly_stake_usd,
  )
  if stake <= 0:
    stake = remaining_usd
  return min(stake, basket_cap, remaining_usd)
