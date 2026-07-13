"""Auto-tune bot entry thresholds from paper trade logs after sufficient history."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from src.trading.bot_performance_report import _closed_round_trips, _summary
from src.trading.entry_strategy import EntryStrategyConfig, entry_strategy_from_cfg


def _parse_ts(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def trade_log_span_days(trades: list[dict[str, Any]]) -> float:
  dates = [_parse_ts(t.get("created_at")) for t in trades if t.get("created_at")]
  dates = [d for d in dates if d is not None]
  if len(dates) < 2:
    return 0.0
  span = max(dates) - min(dates)
  return span.total_seconds() / 86400.0


def auto_tune_cfg(base_cfg: dict[str, Any] | None) -> dict[str, Any]:
  raw = (base_cfg or {}).get("bot_auto_tune") or {}
  return {
    "enabled": bool(raw.get("enabled", True)),
    "min_days": float(raw.get("min_days", 7)),
    "min_closed_trades": int(raw.get("min_closed_trades", 15)),
    "min_ask_edge_floor": float(raw.get("min_ask_edge_floor", 3)),
    "min_ask_edge_ceiling": float(raw.get("min_ask_edge_ceiling", 15)),
    "kelly_floor": float(raw.get("kelly_floor", 0.05)),
    "kelly_ceiling": float(raw.get("kelly_ceiling", 0.25)),
    "ask_edge_step": float(raw.get("ask_edge_step", 1)),
    "kelly_step": float(raw.get("kelly_step", 0.02)),
  }


def effective_entry_strategy(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  tuning: dict[str, Any] | None = None,
) -> EntryStrategyConfig:
  """Merge config defaults with persisted auto-tuning overrides."""
  base = entry_strategy_from_cfg(cfg, kind=kind)
  if not tuning or not tuning.get("active"):
    return base
  overrides: dict[str, Any] = {}
  if tuning.get("min_ask_edge_cents") is not None:
    overrides["min_ask_edge_cents"] = float(tuning["min_ask_edge_cents"])
  if tuning.get("kelly_fraction") is not None:
    overrides["kelly_fraction"] = float(tuning["kelly_fraction"])
  if not overrides:
    return base
  return replace(base, **overrides)


def propose_auto_tuning(
  trades: list[dict[str, Any]],
  *,
  estrat: EntryStrategyConfig,
  tune_cfg: dict[str, Any],
  previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Propose threshold adjustments from closed paper round-trips."""
  closed = _closed_round_trips(trades)
  sm = _summary(closed, [t for t in trades if t.get("action") == "enter"])
  span_days = trade_log_span_days(trades)
  min_days = float(tune_cfg.get("min_days", 7))
  min_trades = int(tune_cfg.get("min_closed_trades", 15))

  base_out = {
    "span_days": round(span_days, 2),
    "closed_trades": sm.get("closed_trades", 0),
    "win_rate": sm.get("win_rate"),
    "total_pnl_usd": sm.get("total_pnl_usd"),
    "avg_pnl_usd": sm.get("avg_pnl_usd"),
  }

  if span_days < min_days:
    return {
      **base_out,
      "ok": False,
      "active": False,
      "reason": f"need_{min_days:.0f}d_history",
      "message": f"Only {span_days:.1f} days of trades — waiting for {min_days:.0f}+ days.",
    }
  if len(closed) < min_trades:
    return {
      **base_out,
      "ok": False,
      "active": False,
      "reason": "insufficient_trades",
      "message": f"Only {len(closed)} closed trades — need {min_trades}+.",
    }

  edge_floor = float(tune_cfg.get("min_ask_edge_floor", 3))
  edge_ceil = float(tune_cfg.get("min_ask_edge_ceiling", 15))
  kelly_floor = float(tune_cfg.get("kelly_floor", 0.05))
  kelly_ceil = float(tune_cfg.get("kelly_ceiling", 0.25))
  edge_step = float(tune_cfg.get("ask_edge_step", 1))
  kelly_step = float(tune_cfg.get("kelly_step", 0.02))

  prev_edge = float((previous or {}).get("min_ask_edge_cents", estrat.min_ask_edge_cents))
  prev_kelly = float((previous or {}).get("kelly_fraction", estrat.kelly_fraction))
  new_edge = prev_edge
  new_kelly = prev_kelly
  changes: list[str] = []

  wr = float(sm.get("win_rate") or 0)
  avg_pnl = float(sm.get("avg_pnl_usd") or 0)
  total_pnl = float(sm.get("total_pnl_usd") or 0)

  if wr < 0.48:
    if new_edge < edge_ceil:
      new_edge = min(edge_ceil, new_edge + edge_step)
      changes.append(f"Raised min_ask_edge to {new_edge:.0f}¢ (win rate {wr * 100:.0f}%).")
    if avg_pnl < -0.10 and new_kelly > kelly_floor:
      new_kelly = max(kelly_floor, round(new_kelly - kelly_step, 3))
      changes.append(f"Reduced Kelly to {new_kelly:.2f} (avg loss ${avg_pnl:+.2f}/trade).")
  elif wr >= 0.55 and total_pnl > 0 and len(closed) >= max(min_trades + 10, 25):
    if new_edge > edge_floor:
      new_edge = max(edge_floor, new_edge - edge_step)
      changes.append(f"Lowered min_ask_edge to {new_edge:.0f}¢ (win rate {wr * 100:.0f}%, profitable).")

  wide_losses = [
    r for r in closed
    if (r.get("entry_spread_cents") or 0) >= 11 and float(r.get("pnl_usd") or 0) < 0
  ]
  if len(wide_losses) >= 3 and new_edge < edge_ceil:
    new_edge = min(edge_ceil, new_edge + edge_step)
    changes.append("Tightened ask-edge after wide-spread losses.")

  if not changes:
    return {
      **base_out,
      "ok": True,
      "active": bool(previous and previous.get("active")),
      "reason": "no_change",
      "message": "Thresholds unchanged — performance within target band.",
      "min_ask_edge_cents": prev_edge,
      "kelly_fraction": prev_kelly,
      "changes": [],
    }

  return {
    **base_out,
    "ok": True,
    "active": True,
    "reason": "tuned",
    "message": " ".join(changes),
    "min_ask_edge_cents": round(new_edge, 1),
    "kelly_fraction": round(new_kelly, 3),
    "base_min_ask_edge_cents": estrat.min_ask_edge_cents,
    "base_kelly_fraction": estrat.kelly_fraction,
    "changes": changes,
    "tuned_at": datetime.now(timezone.utc).isoformat(),
  }


def run_auto_tune_for_store(
  store: Any,
  *,
  cfg: dict[str, Any] | None,
  kind: str,
) -> dict[str, Any]:
  """Analyze trade log and persist tuning overrides when criteria met."""
  tune_cfg = auto_tune_cfg(cfg)
  if not tune_cfg["enabled"]:
    return {"ok": False, "reason": "auto_tune_disabled"}

  estrat = entry_strategy_from_cfg(cfg, kind=kind)
  previous = store.get_auto_tuning()
  trades = store.list_trades(limit=5000)
  proposal = propose_auto_tuning(
    trades,
    estrat=estrat,
    tune_cfg=tune_cfg,
    previous=previous if previous.get("active") else None,
  )

  if not proposal.get("ok"):
    store.save_auto_tuning({**proposal, "active": False})
    return proposal

  if proposal.get("reason") == "no_change" and previous.get("active"):
    store.save_auto_tuning({**previous, **{k: proposal[k] for k in base_out_keys() if k in proposal}})
    return {**previous, **proposal}

  if proposal.get("reason") == "tuned":
    store.save_auto_tuning(proposal)
    return proposal

  store.save_auto_tuning(proposal)
  return proposal


def base_out_keys() -> tuple[str, ...]:
  return (
    "span_days",
    "closed_trades",
    "win_rate",
    "total_pnl_usd",
    "avg_pnl_usd",
    "message",
    "reason",
  )


def audit_tuning_vs_kalshi_wallet(
  proposal: dict[str, Any],
  kalshi_wallet: dict[str, Any] | None,
) -> dict[str, Any]:
  """Flag when auto-tune moves opposite to Kalshi wallet week P&L."""
  week_pnl = None
  if kalshi_wallet and kalshi_wallet.get("ok"):
    week_pnl = float(kalshi_wallet.get("week_pnl_usd") or 0)
  changes = list(proposal.get("changes") or [])
  tightening = any(
    "Raised min_ask_edge" in c or "Reduced Kelly" in c or "Tightened ask-edge" in c
    for c in changes
  )
  loosening = any("Lowered min_ask_edge" in c for c in changes)
  warning: str | None = None
  if week_pnl is not None:
    bot_total = float(proposal.get("total_pnl_usd") or 0)
    if loosening and week_pnl < 0:
      warning = (
        "Auto-tune is loosening gates while Kalshi wallet week is "
        f"${week_pnl:+.2f} — likely wrong direction; trust Kalshi P&L tab."
      )
    elif tightening and week_pnl > 0 and bot_total < -1.0:
      warning = (
        "Auto-tune is tightening on negative bot log "
        f"(${bot_total:+.2f}) while Kalshi wallet week is "
        f"${week_pnl:+.2f} — bot log may be stale; verify before applying."
      )
  return {
    "pnl_source": "bot_log",
    "kalshi_week_pnl_usd": week_pnl,
    "tightening": tightening,
    "loosening": loosening,
    "aligned": warning is None,
    "warning": warning,
  }


def audit_all_auto_tuning(
  bot_reports: list[dict[str, Any]],
  kalshi_wallet: dict[str, Any] | None,
) -> dict[str, Any]:
  """Summarize auto-tune direction vs Kalshi wallet for all bots with tuning."""
  rows: list[dict[str, Any]] = []
  warnings: list[str] = []
  for report in bot_reports:
    tuning = report.get("auto_tuning") or {}
    if not tuning or tuning.get("reason") in ("auto_tune_disabled",):
      continue
    audit = audit_tuning_vs_kalshi_wallet(tuning, kalshi_wallet)
    label = report.get("label") or f"{report.get('asset')}-{report.get('kind')}"
    if audit.get("warning"):
      warnings.append(f"{label}: {audit['warning']}")
    rows.append({
      "label": label,
      "kind": report.get("kind"),
      "asset": report.get("asset"),
      **audit,
    })
  return {
    "ok": True,
    "kalshi_week_pnl_usd": (kalshi_wallet or {}).get("week_pnl_usd") if kalshi_wallet else None,
    "warnings": warnings,
    "bots": rows,
    "any_misaligned": bool(warnings),
  }
