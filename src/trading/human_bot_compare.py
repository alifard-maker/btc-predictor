"""Compare dashboard manual (human) trades vs auto-bot entries for learning/tuning."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.trading.bot_performance_report import _closed_round_trips, _summary
from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.hourly_live_trial_compare import pair_entries_across_bots
from src.trading.human_trade_store import HumanTradeStore


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def _human_enter_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
  return [
    t for t in trades
    if t.get("action") == "enter" and t.get("status") in ("filled", "reconciled")
  ]


def _bot_enter_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
  return [
    t for t in trades
    if t.get("action") == "enter"
    and t.get("trigger") == "continuous"
    and t.get("status") in ("filled", "reconciled", "resting")
  ]


def build_human_bot_compare(
  human_store: HumanTradeStore,
  bot_store: HourlyBotStore,
  *,
  asset: str,
  bot_kind: str,
  pair_window_seconds: int = 180,
  trade_limit: int = 200,
) -> dict[str, Any]:
  human_trades = human_store.list_trades(limit=trade_limit)
  bot_trades = bot_store.list_trades(limit=trade_limit)
  human_enters = _human_enter_trades(human_trades)
  bot_enters = _bot_enter_trades(bot_trades)

  pairing = pair_entries_across_bots(human_enters, bot_enters, window_seconds=pair_window_seconds)
  pairing["label"] = "human_vs_bot"
  pairing["human_actor"] = "dashboard_manual"
  pairing["bot_actor"] = "continuous"

  human_closed = _closed_round_trips(human_trades)
  bot_closed = _closed_round_trips(bot_trades)
  human_sm = _summary(human_closed, human_enters)
  bot_sm = _summary(bot_closed, bot_enters)

  agreement = 0
  human_only = len(pairing.get("unpaired_live") or [])
  bot_only = len(pairing.get("unpaired_trial") or [])
  for pair in pairing.get("pairs") or []:
    if pair.get("live") and pair.get("trial"):
      agreement += 1

  return {
    "ok": True,
    "asset": asset,
    "bot_kind": bot_kind,
    "pair_window_seconds": pair_window_seconds,
    "human_summary": human_sm,
    "bot_summary": bot_sm,
    "pairing": pairing,
    "agreement_count": agreement,
    "human_only_entries": human_only,
    "bot_only_entries": bot_only,
    "learning_ready": {
      "human_enters": len(human_enters),
      "min_for_bucket_tune": 30,
      "min_for_imitation": 100,
      "sufficient_for_buckets": len(human_enters) >= 30,
      "sufficient_for_imitation": len(human_enters) >= 100,
    },
    "generated_at": datetime.now(timezone.utc).isoformat(),
  }


def export_human_training_rows(
  human_store: HumanTradeStore,
  *,
  limit: int = 2000,
) -> list[dict[str, Any]]:
  """Phase C/D — flat rows for rule tuning or imitation (features + outcome)."""
  trades = human_store.list_trades(limit=limit)
  # Join exits → enters by position_id (human + bot round trips use that key).
  closed_by_pid = {
    str(c["position_id"]): c
    for c in _closed_round_trips(trades)
    if c.get("position_id")
  }
  exit_ctx_by_pid: dict[str, dict[str, Any]] = {}
  for t in trades:
    if t.get("action") != "exit" or t.get("status") != "filled":
      continue
    pid = str(t.get("position_id") or "")
    if not pid:
      continue
    ctx = t.get("entry_context") or {}
    exit_ctx_by_pid[pid] = {
      "exit_at": t.get("created_at"),
      "exit_price_cents": t.get("exit_price_cents") or t.get("price_cents"),
      "exit_reason": ctx.get("exit_reason"),
      "bot_exit_signal": ctx.get("bot_exit_signal"),
      "exit_features": ctx.get("features") or {},
      "pnl_usd": t.get("pnl_usd"),
    }
  rows: list[dict[str, Any]] = []
  for t in trades:
    if t.get("action") != "enter":
      continue
    if t.get("status") not in ("filled", "reconciled"):
      continue
    ctx = t.get("entry_context") or {}
    feat = ctx.get("features") or {}
    cf = ctx.get("bot_counterfactual") or {}
    pid = str(t.get("position_id") or t.get("id") or "")
    rt = closed_by_pid.get(pid)
    ex = exit_ctx_by_pid.get(pid) or {}
    rows.append({
      "trade_id": t.get("id"),
      "position_id": pid or None,
      "mode": t.get("mode"),
      "created_at": t.get("created_at"),
      "market_ticker": t.get("market_ticker"),
      "side": t.get("side"),
      "signal": t.get("signal"),
      "entry_price_cents": t.get("entry_price_cents"),
      "contracts": t.get("contracts"),
      "spot_price": feat.get("spot_price"),
      "hours_to_settle": feat.get("hours_to_settle"),
      "edge": feat.get("edge"),
      "model_prob": feat.get("model_prob"),
      "kalshi_mid": feat.get("kalshi_mid"),
      "strike_type": feat.get("strike_type"),
      "floor_strike": feat.get("floor_strike"),
      "cap_strike": feat.get("cap_strike"),
      "features": feat,
      "bot_would_enter": cf.get("would_enter"),
      "bot_skip_reasons": cf.get("skip_reasons"),
      "closed": rt is not None,
      "closed_pnl_usd": (ex.get("pnl_usd") if ex else None) if rt is not None else None,
      "exit_at": ex.get("exit_at"),
      "exit_price_cents": ex.get("exit_price_cents"),
      "exit_reason": ex.get("exit_reason"),
      "bot_exit_signal": ex.get("bot_exit_signal"),
      "exit_features": ex.get("exit_features"),
    })
  return rows
