"""Aggregate paper bot trade logs into calibration-style performance reports."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

PRICE_BUCKETS = (
  (1, 20, "1–20¢"),
  (21, 40, "21–40¢"),
  (41, 60, "41–60¢"),
  (61, 80, "61–80¢"),
  (81, 99, "81–99¢"),
)

SPREAD_BUCKETS = (
  (0, 2, "0–2¢"),
  (3, 5, "3–5¢"),
  (6, 10, "6–10¢"),
  (11, 99, "11¢+"),
)


def _bucket_label(value: int | None, buckets: tuple[tuple[int, int, str], ...]) -> str:
  if value is None:
    return "unknown"
  for lo, hi, label in buckets:
    if lo <= value <= hi:
      return label
  return "unknown"


def _exit_pnl(row: dict[str, Any]) -> float:
  pnl = row.get("pnl_usd")
  if pnl is not None:
    return float(pnl)
  entry_c = row.get("entry_price_cents")
  exit_c = row.get("exit_price_cents")
  contracts = row.get("contracts")
  side = str(row.get("side") or "").lower()
  if entry_c is None or exit_c is None or contracts is None:
    return 0.0
  entry_c, exit_c, contracts = int(entry_c), int(exit_c), int(contracts)
  if side == "yes":
    return round(contracts * (exit_c - entry_c) / 100.0, 2)
  if side == "no":
    return round(contracts * (entry_c - exit_c) / 100.0, 2)
  return 0.0


def _parse_ts(value: str | None) -> datetime | None:
  if not value:
    return None
  try:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
  except ValueError:
    return None


def _filter_trades_since(trades: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
  if days <= 0:
    return trades
  cutoff = datetime.now(timezone.utc) - timedelta(days=days)
  out: list[dict[str, Any]] = []
  for t in trades:
    ts = _parse_ts(t.get("created_at"))
    if ts is None or ts >= cutoff:
      out.append(t)
  return out


def _filter_trades_between(
  trades: list[dict[str, Any]],
  start: datetime,
  end: datetime,
) -> list[dict[str, Any]]:
  out: list[dict[str, Any]] = []
  for t in trades:
    ts = _parse_ts(t.get("created_at"))
    if ts is None:
      continue
    if start <= ts < end:
      out.append(t)
  return out


def _free_mode_from_enter(ent: dict[str, Any]) -> bool | None:
  settings = ent.get("entry_settings")
  if isinstance(settings, dict) and "free_mode" in settings:
    return bool(settings["free_mode"])
  raw = ent.get("entry_settings_json")
  if isinstance(raw, str):
    try:
      import json

      parsed = json.loads(raw)
      if isinstance(parsed, dict) and "free_mode" in parsed:
        return bool(parsed["free_mode"])
    except json.JSONDecodeError:
      pass
  return None


def _closed_round_trips_with_mode(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
  rows = _closed_round_trips(trades)
  enters_by_pid: dict[str, dict[str, Any]] = {}
  for t in trades:
    if t.get("action") == "enter" and t.get("status") == "filled":
      pid = str(t.get("position_id") or t.get("id") or "")
      if pid:
        enters_by_pid[pid] = t
  for r in rows:
    pid = str(r.get("position_id") or "")
    ent = enters_by_pid.get(pid, {})
    fm = _free_mode_from_enter(ent)
    r["free_mode"] = fm
    r["mode_label"] = (
      "free_mode" if fm is True else "filtered" if fm is False else "unknown"
    )
  return rows


def _max_drawdown_usd(pnls_chronological: list[float]) -> float:
  peak = 0.0
  equity = 0.0
  max_dd = 0.0
  for p in pnls_chronological:
    equity += p
    peak = max(peak, equity)
    max_dd = min(max_dd, equity - peak)
  return round(max_dd, 2)


def _summary_with_drawdown(
  rows: list[dict[str, Any]],
  enters: list[dict[str, Any]],
  *,
  trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
  sm = _summary(rows, enters)
  if not rows or not trades:
    sm["max_drawdown_usd"] = 0.0 if not rows else None
    return sm
  exit_rows = [
    t for t in trades if t.get("action") == "exit" and t.get("status") == "filled"
  ]
  exit_rows.sort(key=lambda t: str(t.get("created_at") or ""))
  pnls = [_exit_pnl(t) for t in exit_rows]
  sm["max_drawdown_usd"] = _max_drawdown_usd(pnls)
  return sm


def _split_by_mode(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
  groups: dict[str, list[dict[str, Any]]] = {}
  for r in rows:
    label = str(r.get("mode_label") or "unknown")
    groups.setdefault(label, []).append(r)
  out: dict[str, dict[str, Any]] = {}
  for label, group in groups.items():
    sm = _summary(group, [])
    out[label] = sm
  return out


def build_window_report(
  *,
  kind: str,
  asset: str,
  trades: list[dict[str, Any]],
  window_days: int,
  min_ask_edge_cents: float = 8.0,
) -> dict[str, Any]:
  filtered = _filter_trades_since(trades, window_days)
  enters = [t for t in filtered if t.get("action") == "enter" and t.get("status") == "filled"]
  closed = _closed_round_trips_with_mode(filtered)
  sm = _summary_with_drawdown(closed, enters, trades=filtered)
  return {
    "window_days": window_days,
    "summary": sm,
    "by_free_mode": _split_by_mode(closed),
    "by_entry_price": _aggregate_bucket(
      closed,
      lambda r: _bucket_label(r.get("entry_price_cents"), PRICE_BUCKETS),
    ),
    "by_signal": _signal_rows(closed),
    "gates": {"min_ask_edge_cents": min_ask_edge_cents},
  }


def build_rolling_hours_report(
  *,
  kind: str,
  asset: str,
  trades: list[dict[str, Any]],
  window_hours: float,
  end_hours_ago: float = 0.0,
  min_ask_edge_cents: float = 8.0,
) -> dict[str, Any]:
  """Rolling window ending `end_hours_ago` before now (0 = now)."""
  now = datetime.now(timezone.utc)
  end = now - timedelta(hours=end_hours_ago)
  start = end - timedelta(hours=window_hours)
  filtered = _filter_trades_between(trades, start, end)
  enters = [t for t in filtered if t.get("action") == "enter" and t.get("status") == "filled"]
  closed = _closed_round_trips_with_mode(filtered)
  sm = _summary(closed, enters)
  loss_pnls = [float(r["pnl_usd"]) for r in closed if float(r["pnl_usd"]) < 0]
  win_pnls = [float(r["pnl_usd"]) for r in closed if float(r["pnl_usd"]) > 0]
  sm["loss_count"] = len(loss_pnls)
  sm["loss_pnl_usd"] = round(sum(loss_pnls), 2) if loss_pnls else 0.0
  sm["win_pnl_usd"] = round(sum(win_pnls), 2) if win_pnls else 0.0
  sm["enter_count"] = len(enters)
  return {
    "window_hours": window_hours,
    "end_hours_ago": end_hours_ago,
    "window_start": start.isoformat(),
    "window_end": end.isoformat(),
    "summary": sm,
    "gates": {"min_ask_edge_cents": min_ask_edge_cents},
  }


def _signal_rows(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
  by_signal: dict[str, list[float]] = {}
  for r in closed:
    sig = str(r.get("signal") or "—")
    by_signal.setdefault(sig, []).append(float(r["pnl_usd"]))
  signal_rows = []
  for sig, pnls in sorted(by_signal.items(), key=lambda x: -len(x[1])):
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    signal_rows.append({
      "signal": sig,
      "trades": n,
      "win_rate": round(wins / n, 3) if n else None,
      "avg_pnl_usd": round(sum(pnls) / n, 2) if n else None,
    })
  return signal_rows


def _closed_round_trips(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Exit rows with entry metadata for bucketing."""
  out: list[dict[str, Any]] = []
  enters_by_pid: dict[str, dict[str, Any]] = {}
  for t in trades:
    if t.get("action") == "enter" and t.get("status") == "filled":
      pid = str(t.get("position_id") or t.get("id") or "")
      if pid:
        enters_by_pid[pid] = t

  for t in trades:
    if t.get("action") != "exit" or t.get("status") != "filled":
      continue
    pid = str(t.get("position_id") or "")
    ent = enters_by_pid.get(pid, {})
    entry_c = t.get("entry_price_cents") or ent.get("entry_price_cents") or ent.get("price_cents")
    spread = t.get("entry_spread_cents")
    if spread is None:
      spread = ent.get("entry_spread_cents")
    bid = t.get("entry_bid_cents") or ent.get("entry_bid_cents")
    ask = t.get("entry_ask_cents") or ent.get("entry_ask_cents")
    pnl = _exit_pnl(t)
    out.append({
      "pnl_usd": pnl,
      "position_id": pid or None,
      "entry_price_cents": int(entry_c) if entry_c is not None else None,
      "entry_spread_cents": int(spread) if spread is not None else None,
      "entry_bid_cents": bid,
      "entry_ask_cents": ask,
      "signal": ent.get("signal") or t.get("signal"),
      "side": t.get("side") or ent.get("side"),
      "cost_usd": float(ent.get("cost_usd") or t.get("cost_usd") or 0),
      "market_ticker": t.get("market_ticker") or ent.get("market_ticker"),
    })
  return out


def _aggregate_bucket(rows: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
  groups: dict[str, list[float]] = {}
  costs: dict[str, float] = {}
  for r in rows:
    label = key_fn(r)
    groups.setdefault(label, []).append(float(r["pnl_usd"]))
    costs[label] = costs.get(label, 0.0) + float(r.get("cost_usd") or 0)
  out = []
  for label, pnls in sorted(groups.items(), key=lambda x: x[0]):
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    total = round(sum(pnls), 2)
    out.append({
      "bucket": label,
      "trades": n,
      "wins": wins,
      "losses": n - wins,
      "win_rate": round(wins / n, 3) if n else None,
      "total_pnl_usd": total,
      "avg_pnl_usd": round(total / n, 2) if n else None,
      "total_cost_usd": round(costs.get(label, 0), 2),
    })
  return out


def _summary(rows: list[dict[str, Any]], enters: list[dict[str, Any]]) -> dict[str, Any]:
  if not rows:
    return {
      "closed_trades": 0,
      "wins": 0,
      "losses": 0,
      "win_rate": None,
      "total_pnl_usd": 0.0,
      "avg_pnl_usd": None,
      "total_entered_usd": round(sum(float(e.get("cost_usd") or 0) for e in enters), 2),
      "roi_on_deployed_pct": None,
    }
  pnls = [float(r["pnl_usd"]) for r in rows]
  wins = sum(1 for p in pnls if p > 0)
  n = len(pnls)
  total_pnl = round(sum(pnls), 2)
  deployed = round(sum(float(r.get("cost_usd") or 0) for r in rows), 2)
  return {
    "closed_trades": n,
    "wins": wins,
    "losses": n - wins,
    "win_rate": round(wins / n, 3),
    "total_pnl_usd": total_pnl,
    "avg_pnl_usd": round(total_pnl / n, 2),
    "total_entered_usd": round(sum(float(e.get("cost_usd") or 0) for e in enters), 2),
    "roi_on_closed_cost_pct": round(total_pnl / deployed * 100, 2) if deployed > 0 else None,
  }


def _recommendations(
  summary: dict[str, Any],
  by_price: list[dict[str, Any]],
  by_spread: list[dict[str, Any]],
  *,
  min_ask_edge_cents: float,
) -> list[str]:
  recs: list[str] = []
  n = int(summary.get("closed_trades") or 0)
  if n < 5:
    recs.append(f"Only {n} closed trades — need ~20+ before tuning thresholds.")
    return recs

  wr = summary.get("win_rate")
  if wr is not None and wr < 0.48:
    recs.append(f"Win rate {wr * 100:.0f}% below breakeven — raise min_ask_edge_cents (now {min_ask_edge_cents:.0f}¢) or use STRONG-only.")
  elif wr is not None and wr >= 0.55:
    recs.append(f"Win rate {wr * 100:.0f}% healthy — current gates may be workable.")

  for row in by_spread:
    if row["bucket"] == "11¢+" and row["trades"] >= 3:
      avg = row.get("avg_pnl_usd")
      if avg is not None and avg < 0:
        recs.append(f"Wide spreads ({row['bucket']}) avg {avg:+.2f}$ — tighten paper_max_spread or skip.")

  for row in by_price:
    if row["trades"] < 3:
      continue
    avg = row.get("avg_pnl_usd")
    if row["bucket"] in ("1–20¢", "81–99¢") and avg is not None and avg < 0:
      recs.append(f"Entry {row['bucket']} bucket losing ({avg:+.2f}$/trade) — avoid tail strikes.")

  if not recs:
    recs.append("No strong warnings — review buckets before loosening filters.")
  return recs


def build_bot_performance_report(
  *,
  kind: str,
  asset: str,
  trades: list[dict[str, Any]],
  min_ask_edge_cents: float = 8.0,
) -> dict[str, Any]:
  """Build report for one bot store trade list."""
  enters = [t for t in trades if t.get("action") == "enter" and t.get("status") == "filled"]
  closed = _closed_round_trips(trades)
  sm = _summary(closed, enters)
  by_price = _aggregate_bucket(
    closed,
    lambda r: _bucket_label(r.get("entry_price_cents"), PRICE_BUCKETS),
  )
  by_spread = _aggregate_bucket(
    closed,
    lambda r: _bucket_label(r.get("entry_spread_cents"), SPREAD_BUCKETS),
  )
  by_signal: dict[str, list[float]] = {}
  for r in closed:
    sig = str(r.get("signal") or "—")
    by_signal.setdefault(sig, []).append(float(r["pnl_usd"]))
  signal_rows = []
  for sig, pnls in sorted(by_signal.items(), key=lambda x: -len(x[1])):
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    signal_rows.append({
      "signal": sig,
      "trades": n,
      "win_rate": round(wins / n, 3) if n else None,
      "avg_pnl_usd": round(sum(pnls) / n, 2) if n else None,
    })

  return {
    "kind": kind,
    "asset": asset,
    "label": f"{asset.upper()} {kind}",
    "summary": sm,
    "by_entry_price": by_price,
    "by_spread": by_spread,
    "by_signal": signal_rows,
    "recommendations": _recommendations(sm, by_price, by_spread, min_ask_edge_cents=min_ask_edge_cents),
    "gates": {
      "min_ask_edge_cents": min_ask_edge_cents,
      "note": "Entries now require model_prob − ask_implied ≥ min_ask_edge_cents.",
    },
  }


def build_combined_report(bot_reports: list[dict[str, Any]]) -> dict[str, Any]:
  closed = sum(int(r["summary"].get("closed_trades") or 0) for r in bot_reports)
  pnl = round(sum(float(r["summary"].get("total_pnl_usd") or 0) for r in bot_reports), 2)
  wins = sum(int(r["summary"].get("wins") or 0) for r in bot_reports)
  return {
    "closed_trades": closed,
    "total_pnl_usd": pnl,
    "win_rate": round(wins / closed, 3) if closed else None,
  }


def build_all_bots_performance_report(loop: Any) -> dict[str, Any]:
  """Collect all four paper bot stores from PredictionLoop."""
  from src.trading.bot_auto_tuning import effective_entry_strategy

  reports: list[dict[str, Any]] = []
  specs = (
    ("hourly", "btc", loop.hourly_bot_store("btc"), loop.cfg),
    ("hourly", "eth", loop.hourly_bot_store("eth"), loop._eth_cfg or loop.cfg),
    ("slot15", "btc", loop.slot15_bot_store("btc"), loop._acfg_15m("btc")),
    ("slot15", "eth", loop.slot15_bot_store("eth"), loop._acfg_15m("eth")),
  )
  for kind, asset, store, acfg in specs:
    if asset == "eth" and kind == "slot15":
      try:
        if not loop._slot15m_enabled("eth"):
          continue
      except Exception:
        pass
    tuning = store.get_auto_tuning()
    estrat = effective_entry_strategy(acfg, kind=kind, tuning=tuning)
    trades = store.list_trades(limit=5000)
    report = build_bot_performance_report(
      kind=kind,
      asset=asset,
      trades=trades,
      min_ask_edge_cents=float(getattr(estrat, "min_ask_edge_cents", 8)),
    )
    report["auto_tuning"] = tuning
    report["last_60_days"] = build_window_report(
      kind=kind,
      asset=asset,
      trades=trades,
      window_days=60,
      min_ask_edge_cents=float(getattr(estrat, "min_ask_edge_cents", 8)),
    )
    edge = float(getattr(estrat, "min_ask_edge_cents", 8))
    report["last_60_min"] = build_rolling_hours_report(
      kind=kind, asset=asset, trades=trades, window_hours=1, end_hours_ago=0, min_ask_edge_cents=edge,
    )
    report["prior_4h"] = build_rolling_hours_report(
      kind=kind, asset=asset, trades=trades, window_hours=4, end_hours_ago=1, min_ask_edge_cents=edge,
    )
    reports.append(report)

  def _combined_rolling(key: str) -> dict[str, Any]:
    closed = sum(int(r.get(key, {}).get("summary", {}).get("closed_trades") or 0) for r in reports)
    pnl = round(sum(float(r.get(key, {}).get("summary", {}).get("total_pnl_usd") or 0) for r in reports), 2)
    wins = sum(int(r.get(key, {}).get("summary", {}).get("wins") or 0) for r in reports)
    losses = sum(int(r.get(key, {}).get("summary", {}).get("loss_count") or 0) for r in reports)
    loss_pnl = round(
      sum(float(r.get(key, {}).get("summary", {}).get("loss_pnl_usd") or 0) for r in reports), 2
    )
    out = {
      "closed_trades": closed,
      "total_pnl_usd": pnl,
      "loss_count": losses,
      "loss_pnl_usd": loss_pnl,
    }
    if closed:
      out["win_rate"] = round(wins / closed, 3)
    return out

  combined_60d = {
    "closed_trades": sum(
      int(r.get("last_60_days", {}).get("summary", {}).get("closed_trades") or 0)
      for r in reports
    ),
    "total_pnl_usd": round(
      sum(
        float(r.get("last_60_days", {}).get("summary", {}).get("total_pnl_usd") or 0)
        for r in reports
      ),
      2,
    ),
  }
  wins_60 = sum(
    int(r.get("last_60_days", {}).get("summary", {}).get("wins") or 0) for r in reports
  )
  if combined_60d["closed_trades"]:
    combined_60d["win_rate"] = round(wins_60 / combined_60d["closed_trades"], 3)

  return {
    "ok": True,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "bots": reports,
    "combined": build_combined_report(reports),
    "combined_60_days": combined_60d,
    "combined_last_60_min": _combined_rolling("last_60_min"),
    "combined_prior_4h": _combined_rolling("prior_4h"),
  }
