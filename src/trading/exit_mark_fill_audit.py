"""Live exit audit: decision mark vs fill price vs peak unrealized PnL."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.trading.paper_execution import leg_pnl_usd


def enrich_exit_mark_fill_fields(
  ctx: dict[str, Any],
  *,
  peaks: dict[str, float],
  decision_mark_cents: int,
  unrealized_at_decision_usd: float | None,
  fill_exit_cents: int | None = None,
  min_hold_seconds: int | None = None,
) -> dict[str, Any]:
  """Attach mark/fill/peak fields to exit_context for post-trade audit."""
  out = dict(ctx)
  out["decision_mark_cents"] = int(decision_mark_cents)
  peak_usd = float(peaks.get("peak_unrealized_usd") or 0)
  out["peak_unrealized_usd"] = round(peak_usd, 4)
  out["peak_profit_pct"] = round(float(peaks.get("peak_profit_pct") or 0), 4)
  if unrealized_at_decision_usd is not None:
    out["unrealized_at_decision_usd"] = round(float(unrealized_at_decision_usd), 4)
    if peak_usd > 0:
      out["peak_vs_decision_usd"] = round(peak_usd - float(unrealized_at_decision_usd), 2)
  if min_hold_seconds is not None:
    out["min_hold_seconds"] = int(min_hold_seconds)
  if fill_exit_cents is not None:
    out["fill_exit_cents"] = int(fill_exit_cents)
    out["mark_vs_fill_cents"] = int(fill_exit_cents) - int(decision_mark_cents)
  return out


def _parse_ts(value: str | None) -> datetime | None:
  if not value:
    return None
  try:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
  except ValueError:
    return None


def _exit_context(trade: dict[str, Any]) -> dict[str, Any]:
  ctx = trade.get("exit_context")
  if ctx is None and trade.get("exit_context_json"):
    try:
      ctx = json.loads(str(trade["exit_context_json"]))
    except (json.JSONDecodeError, TypeError):
      ctx = None
  return dict(ctx) if isinstance(ctx, dict) else {}


def _row_from_exit(trade: dict[str, Any]) -> dict[str, Any] | None:
  if trade.get("action") != "exit" or trade.get("status") != "filled":
    return None
  if str(trade.get("mode") or "").lower() != "live":
    return None

  ctx = _exit_context(trade)
  entry_c = trade.get("entry_price_cents")
  exit_c = trade.get("exit_price_cents") or trade.get("price_cents")
  contracts = trade.get("contracts")
  if entry_c is None or exit_c is None or contracts is None:
    return None

  realized = trade.get("pnl_usd")
  if realized is None:
    realized = leg_pnl_usd(
      entry_price_cents=int(entry_c),
      mark_or_exit_cents=int(exit_c),
      contracts=int(contracts),
    )
  realized_f = round(float(realized or 0), 2)

  decision_mark = ctx.get("decision_mark_cents")
  fill_cents = ctx.get("fill_exit_cents")
  if fill_cents is None:
    fill_cents = int(exit_c)

  peak_usd = ctx.get("peak_unrealized_usd")
  if peak_usd is not None:
    peak_usd = round(float(peak_usd), 2)
  decision_unreal = ctx.get("unrealized_at_decision_usd")
  if decision_unreal is None:
    decision_unreal = ctx.get("unrealized_pnl_usd")

  mark_vs_fill = ctx.get("mark_vs_fill_cents")
  if mark_vs_fill is None and decision_mark is not None:
    mark_vs_fill = int(fill_cents) - int(decision_mark)

  leakage_peak = None
  if peak_usd is not None:
    leakage_peak = round(float(peak_usd) - realized_f, 2)

  left_on_table = None
  if peak_usd is not None and decision_unreal is not None:
    left_on_table = round(float(peak_usd) - float(decision_unreal), 2)

  hold_s = ctx.get("hold_seconds")
  min_hold = ctx.get("min_hold_seconds")
  exit_reason = str(ctx.get("exit_reason") or "")

  return {
    "exited_at": trade.get("created_at"),
    "event_ticker": trade.get("event_ticker"),
    "market_ticker": trade.get("market_ticker"),
    "label": trade.get("label"),
    "exit_reason": exit_reason,
    "contracts": int(contracts),
    "entry_cents": int(entry_c),
    "decision_mark_cents": int(decision_mark) if decision_mark is not None else None,
    "fill_exit_cents": int(fill_cents),
    "mark_vs_fill_cents": int(mark_vs_fill) if mark_vs_fill is not None else None,
    "realized_pnl_usd": realized_f,
    "unrealized_at_decision_usd": round(float(decision_unreal), 2) if decision_unreal is not None else None,
    "peak_unrealized_usd": peak_usd,
    "peak_vs_realized_usd": leakage_peak,
    "peak_vs_decision_usd": left_on_table,
    "hold_seconds": round(float(hold_s), 1) if hold_s is not None else None,
    "min_hold_seconds": int(min_hold) if min_hold is not None else None,
    "hours_to_settle": ctx.get("hours_to_settle"),
    "has_audit_fields": decision_mark is not None and peak_usd is not None,
  }


def build_exit_mark_fill_audit_report(
  trades: list[dict[str, Any]],
  *,
  mode: str = "live",
  since: datetime | None = None,
  leakage_threshold_usd: float = 0.05,
) -> dict[str, Any]:
  """Summarize live exits: mark vs fill slippage and peak vs realized leakage."""
  rows: list[dict[str, Any]] = []
  for t in trades:
    if since is not None:
      ts = _parse_ts(t.get("created_at"))
      if ts is None or ts < since:
        continue
    row = _row_from_exit(t)
    if row:
      rows.append(row)

  rows.sort(key=lambda r: str(r.get("exited_at") or ""))

  by_reason: dict[str, list[dict[str, Any]]] = {}
  for r in rows:
    by_reason.setdefault(str(r.get("exit_reason") or "unknown"), []).append(r)

  def _agg(group: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(group)
    if not n:
      return {"trades": 0}
    realized = sum(float(r["realized_pnl_usd"]) for r in group)
    peaks = [r["peak_unrealized_usd"] for r in group if r.get("peak_unrealized_usd") is not None]
    leakages = [r["peak_vs_realized_usd"] for r in group if r.get("peak_vs_realized_usd") is not None]
    slippages = [r["mark_vs_fill_cents"] for r in group if r.get("mark_vs_fill_cents") is not None]
    big_leak = sum(
      1 for r in group
      if r.get("peak_vs_realized_usd") is not None
      and float(r["peak_vs_realized_usd"]) >= leakage_threshold_usd
    )
    return {
      "trades": n,
      "total_realized_usd": round(realized, 2),
      "avg_realized_usd": round(realized / n, 2),
      "with_peak_data": len(peaks),
      "avg_peak_vs_realized_usd": round(sum(leakages) / len(leakages), 2) if leakages else None,
      "big_leakage_count": big_leak,
      "avg_mark_vs_fill_cents": round(sum(slippages) / len(slippages), 2) if slippages else None,
    }

  profit_reasons = {"PROFIT TARGET", "PROFIT TRAIL", "TAKE PROFIT", "LEG TAKE PROFIT", "REASSESS NEUTRAL TP"}
  profit_exits = [r for r in rows if r.get("exit_reason") in profit_reasons]
  cut_exits = [r for r in rows if "CUT" in str(r.get("exit_reason") or "").upper() or "LEG STOP" in str(r.get("exit_reason") or "")]

  near_min_hold = [
    r for r in profit_exits
    if r.get("hold_seconds") is not None and r.get("min_hold_seconds") is not None
    and float(r["hold_seconds"]) < float(r["min_hold_seconds"]) + 15
  ]

  enriched = sum(1 for r in rows if r.get("has_audit_fields"))
  return {
    "closed_live_exits": len(rows),
    "enriched_rows": enriched,
    "enrichment_pct": round(enriched / len(rows), 3) if rows else None,
    "totals": _agg(rows),
    "by_exit_reason": {k: _agg(v) for k, v in sorted(by_reason.items())},
    "profit_exits": _agg(profit_exits),
    "cut_exits": _agg(cut_exits),
    "profit_exits_near_min_hold": {
      "count": len(near_min_hold),
      "note": "Profit exits within 15s of min_hold_seconds (gate likely just cleared)",
      "sample": near_min_hold[-5:],
    },
    "worst_leakage": sorted(
      [r for r in rows if r.get("peak_vs_realized_usd") is not None],
      key=lambda r: float(r["peak_vs_realized_usd"]),
      reverse=True,
    )[:8],
    "worst_mark_vs_fill": sorted(
      [r for r in rows if r.get("mark_vs_fill_cents") is not None],
      key=lambda r: abs(int(r["mark_vs_fill_cents"])),
      reverse=True,
    )[:8],
    "recent": rows[-20:],
    "leakage_threshold_usd": leakage_threshold_usd,
  }


def run_exit_mark_fill_audit(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  """Build audit from BTC hourly live exits since stats epoch; persist artifact."""
  from src.trading.pnl_first_backtest_runner import backtest_log_dir
  from src.trading.pnl_first_railway_manager import _stats_epoch

  store = loop.hourly_bot_store("btc", kind="hourly")
  trades = store.list_trades(limit=2000)
  since = _stats_epoch(cfg)
  report = build_exit_mark_fill_audit_report(trades, mode="live", since=since)
  report["generated_at"] = datetime.now(timezone.utc).isoformat()
  report["epoch_start_at"] = since.isoformat() if since else None

  log_dir = backtest_log_dir(cfg)
  out_path = log_dir / "exit_mark_fill_audit_latest.json"
  out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
  report["artifact_path"] = str(out_path)
  return report
