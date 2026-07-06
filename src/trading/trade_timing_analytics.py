"""PnL by minutes-to-settle at entry/exit (when in the hour trades win or lose)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.trading.hourly_event_time import hourly_event_settle_utc

MINUTES_TO_SETTLE_BUCKETS: tuple[tuple[float, float, str], ...] = (
  (0, 5, "0–5m left"),
  (5, 10, "5–10m left"),
  (10, 15, "10–15m left"),
  (15, 30, "15–30m left"),
  (30, 45, "30–45m left"),
  (45, 60, "45–60m left"),
  (60, 9999, "60m+ left"),
)


def _parse_ts(value: str | None) -> datetime | None:
  if not value:
    return None
  try:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
  except ValueError:
    return None


def minutes_to_settle_at_trade(trade: dict[str, Any]) -> float | None:
  """Minutes until hourly event settlement at trade time (from log or event ticker)."""
  if trade.get("action") == "exit":
    ctx = trade.get("exit_context")
    if ctx is None and trade.get("exit_context_json"):
      try:
        ctx = json.loads(str(trade["exit_context_json"]))
      except (json.JSONDecodeError, TypeError):
        ctx = None
    if isinstance(ctx, dict) and ctx.get("hours_to_settle") is not None:
      try:
        return round(float(ctx["hours_to_settle"]) * 60.0, 1)
      except (TypeError, ValueError):
        pass

  settings = trade.get("entry_settings")
  if settings is None and trade.get("entry_settings_json"):
    try:
      settings = json.loads(str(trade["entry_settings_json"]))
    except (json.JSONDecodeError, TypeError):
      settings = None
  if isinstance(settings, dict) and settings.get("hours_to_settle") is not None:
    try:
      return round(float(settings["hours_to_settle"]) * 60.0, 1)
    except (TypeError, ValueError):
      pass

  event = trade.get("event_ticker")
  at = _parse_ts(trade.get("created_at"))
  if not event or at is None:
    return None
  settle = hourly_event_settle_utc(str(event))
  if settle is None:
    return None
  return round((settle - at).total_seconds() / 60.0, 1)


def bucket_minutes_to_settle(minutes: float | None) -> str:
  if minutes is None:
    return "unknown"
  m = max(0.0, float(minutes))
  for lo, hi, label in MINUTES_TO_SETTLE_BUCKETS:
    if lo <= m < hi:
      return label
  return "unknown"


def _exit_pnl(row: dict[str, Any]) -> float:
  from src.trading.paper_execution import leg_pnl_usd

  pnl = row.get("pnl_usd")
  if pnl is not None:
    return float(pnl)
  entry_c = row.get("entry_price_cents")
  exit_c = row.get("exit_price_cents")
  contracts = row.get("contracts")
  if entry_c is None or exit_c is None or contracts is None:
    return 0.0
  return float(
    leg_pnl_usd(
      entry_price_cents=int(entry_c),
      mark_or_exit_cents=int(exit_c),
      contracts=int(contracts),
    )
    or 0.0
  )


def _exit_reason(trade: dict[str, Any]) -> str | None:
  ctx = trade.get("exit_context")
  if ctx is None and trade.get("exit_context_json"):
    try:
      ctx = json.loads(str(trade["exit_context_json"]))
    except (json.JSONDecodeError, TypeError):
      ctx = None
  if isinstance(ctx, dict) and ctx.get("exit_reason"):
    return str(ctx["exit_reason"])
  detail = str(trade.get("detail") or "")
  for token in ("CUT LOSSES", "LEG STOP", "TAKE PROFIT", "SETTLEMENT", "TRAIL"):
    if token in detail.upper():
      return token
  return None


def closed_legs_with_timing(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Filled exits paired with enter rows; minutes-to-settle at entry and exit."""
  enters_by_pid: dict[str, dict[str, Any]] = {}
  for t in trades:
    if t.get("action") == "enter" and t.get("status") == "filled":
      pid = str(t.get("position_id") or t.get("id") or "")
      if pid:
        enters_by_pid[pid] = t

  out: list[dict[str, Any]] = []
  for t in trades:
    if t.get("action") != "exit" or t.get("status") != "filled":
      continue
    pid = str(t.get("position_id") or "")
    ent = enters_by_pid.get(pid, {})
    entry_min = minutes_to_settle_at_trade(ent) if ent else None
    exit_min = minutes_to_settle_at_trade(t)
    pnl = _exit_pnl(t)
    out.append({
      "pnl_usd": round(pnl, 2),
      "position_id": pid or None,
      "market_ticker": t.get("market_ticker") or ent.get("market_ticker"),
      "label": ent.get("label") or t.get("label"),
      "side": t.get("side") or ent.get("side"),
      "signal": ent.get("signal") or t.get("signal"),
      "mode": str(t.get("mode") or ent.get("mode") or ""),
      "exit_reason": _exit_reason(t),
      "entry_minutes_to_settle": entry_min,
      "exit_minutes_to_settle": exit_min,
      "entry_bucket": bucket_minutes_to_settle(entry_min),
      "exit_bucket": bucket_minutes_to_settle(exit_min),
      "entered_at": ent.get("created_at"),
      "exited_at": t.get("created_at"),
      "event_ticker": t.get("event_ticker") or ent.get("event_ticker"),
    })
  return out


def _aggregate_timing_buckets(rows: list[dict[str, Any]], *, field: str) -> list[dict[str, Any]]:
  groups: dict[str, list[float]] = {}
  for r in rows:
    bucket = str(r.get(field) or "unknown")
    groups.setdefault(bucket, []).append(float(r["pnl_usd"]))

  order = [b[2] for b in MINUTES_TO_SETTLE_BUCKETS] + ["unknown"]
  out: list[dict[str, Any]] = []
  for bucket in order:
    pnls = groups.get(bucket)
    if not pnls:
      continue
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    total = round(sum(pnls), 2)
    out.append({
      "bucket": bucket,
      "trades": n,
      "wins": wins,
      "losses": n - wins,
      "win_rate": round(wins / n, 3) if n else None,
      "total_pnl_usd": total,
      "avg_pnl_usd": round(total / n, 2) if n else None,
      "max_win_usd": round(max(pnls), 2),
      "max_loss_usd": round(min(pnls), 2),
    })
  return out


def build_trade_timing_report(
  trades: list[dict[str, Any]],
  *,
  mode: str | None = "live",
  since: datetime | None = None,
) -> dict[str, Any]:
  """Summarize closed-leg PnL by minutes remaining in the hour at entry (and exit)."""
  filtered = list(trades)
  if since is not None:
    filtered = [
      t for t in filtered
      if (_parse_ts(t.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since
    ]
  if mode:
    filtered = [
      t for t in filtered
      if str(t.get("mode") or "").lower() == str(mode).lower()
    ]

  closed = closed_legs_with_timing(filtered)
  by_entry = _aggregate_timing_buckets(closed, field="entry_bucket")
  by_exit = _aggregate_timing_buckets(closed, field="exit_bucket")

  best = max(closed, key=lambda r: float(r["pnl_usd"]), default=None)
  worst = min(closed, key=lambda r: float(r["pnl_usd"]), default=None)

  total_pnl = round(sum(float(r["pnl_usd"]) for r in closed), 2)
  return {
    "closed_legs": len(closed),
    "total_pnl_usd": total_pnl,
    "by_minutes_to_settle_at_entry": by_entry,
    "by_minutes_to_settle_at_exit": by_exit,
    "best_leg": best,
    "worst_leg": worst,
    "legs": closed[-50:],
  }
