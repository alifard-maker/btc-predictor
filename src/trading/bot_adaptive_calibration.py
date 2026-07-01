"""Closed-loop bucket calibration from recent closed trades (price / spread buckets)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src.trading.bot_performance_report import (
  PRICE_BUCKETS,
  SPREAD_BUCKETS,
  _bucket_label,
  _closed_round_trips,
)


def _bot_cfg_for_kind(cfg: dict[str, Any] | None, kind: str) -> dict[str, Any]:
  if not cfg:
    return {}
  from src.backtest.mechanics_profiles import entry_kind_for_bot

  if entry_kind_for_bot(kind) == "hourly":
    return (cfg.get("hourly") or {}).get("bot") or {}
  return (cfg.get("intra_slot") or {}).get("bot") or {}


def adaptive_calibration_cfg(cfg: dict[str, Any] | None, *, kind: str = "hourly") -> dict[str, Any]:
  """Merge global + per-bot overrides. Slot15 defaults off; hourly defaults on."""
  global_raw = (cfg or {}).get("bot_adaptive_calibration") or {}
  bot_raw = _bot_cfg_for_kind(cfg, kind).get("bot_adaptive_calibration") or {}
  raw = {**global_raw, **bot_raw}
  if kind == "slot15":
    enabled = bool(bot_raw.get("enabled", False))
  else:
    enabled = bool(raw.get("enabled", True))
  return {
    "enabled": enabled,
    "short_window_hours": float(raw.get("short_window_hours", 4)),
    "long_window_hours": float(raw.get("long_window_hours", 24)),
    "short_min_trades": int(raw.get("short_min_trades", 4)),
    "short_max_win_rate": float(raw.get("short_max_win_rate", 0.25)),
    "short_min_loss_usd": float(raw.get("short_min_loss_usd", 2.0)),
    "long_min_trades": int(raw.get("long_min_trades", 8)),
    "long_max_win_rate": float(raw.get("long_max_win_rate", 0.35)),
    "long_min_loss_usd": float(raw.get("long_min_loss_usd", 5.0)),
    "pause_hours_short": float(raw.get("pause_hours_short", 6)),
    "pause_hours_long": float(raw.get("pause_hours_long", 24)),
    "probe_max_entries": int(raw.get("probe_max_entries", 2)),
    "probe_pause_hours_on_fail": float(raw.get("probe_pause_hours_on_fail", 6)),
    "tightened_edge_boost_cents": float(raw.get("tightened_edge_boost_cents", 4)),
    "apply_in_aggressive_mode": bool(raw.get("apply_in_aggressive_mode", True)),
    "price_buckets": bool(raw.get("price_buckets", True)),
    "spread_buckets": bool(raw.get("spread_buckets", False)),
    "refresh_interval_minutes": int(raw.get("refresh_interval_minutes", 30)),
  }


def price_bucket_key(entry_price_cents: int | None) -> str:
  label = _bucket_label(entry_price_cents, PRICE_BUCKETS)
  return f"price:{label}"


def spread_bucket_key(entry_spread_cents: int | None) -> str:
  label = _bucket_label(entry_spread_cents, SPREAD_BUCKETS)
  return f"spread:{label}"


def _parse_ts(value: str | None) -> datetime | None:
  if not value:
    return None
  try:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
      dt = dt.replace(tzinfo=timezone.utc)
    return dt
  except ValueError:
    return None


def _bucket_stats(
  closed: list[dict[str, Any]],
  *,
  key_fn,
  window_hours: float,
  now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
  now = now or datetime.now(timezone.utc)
  cutoff = now - timedelta(hours=window_hours)
  groups: dict[str, list[float]] = {}
  for row in closed:
    ts = _parse_ts(row.get("exit_at"))
    if ts is None or ts < cutoff:
      continue
    key = key_fn(row)
    if key.endswith(":unknown"):
      continue
    groups.setdefault(key, []).append(float(row.get("pnl_usd") or 0))
  out: dict[str, dict[str, Any]] = {}
  for key, pnls in groups.items():
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    total = round(sum(pnls), 2)
    out[key] = {
      "trades": n,
      "wins": wins,
      "losses": n - wins,
      "win_rate": round(wins / n, 3) if n else None,
      "total_pnl_usd": total,
      "avg_pnl_usd": round(total / n, 2) if n else None,
    }
  return out


def _closed_with_exit_ts(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Attach exit timestamp to closed round-trip rows."""
  exit_ts_by_pid: dict[str, str | None] = {}
  for t in trades:
    if t.get("action") == "exit" and t.get("status") == "filled":
      pid = str(t.get("position_id") or "")
      if pid:
        exit_ts_by_pid[pid] = t.get("created_at")
  out: list[dict[str, Any]] = []
  for row in _closed_round_trips(trades):
    pid = str(row.get("position_id") or "")
    if pid not in exit_ts_by_pid:
      continue
    tagged = dict(row)
    tagged["exit_at"] = exit_ts_by_pid[pid]
    out.append(tagged)
  return out


def _default_bucket_state() -> dict[str, Any]:
  return {"state": "normal", "updated_at": datetime.now(timezone.utc).isoformat()}


def _pause_until(hours: float, *, now: datetime | None = None) -> str:
  now = now or datetime.now(timezone.utc)
  return (now + timedelta(hours=hours)).isoformat()


def _should_pause(stats: dict[str, Any], *, min_trades: int, max_wr: float, min_loss: float) -> bool:
  if int(stats.get("trades") or 0) < min_trades:
    return False
  total = float(stats.get("total_pnl_usd") or 0)
  if total > -abs(min_loss):
    return False
  wr = stats.get("win_rate")
  if wr is None:
    return False
  return float(wr) <= max_wr


def _should_tighten(stats: dict[str, Any], *, min_trades: int, max_wr: float, min_loss: float) -> bool:
  if int(stats.get("trades") or 0) < min_trades:
    return False
  total = float(stats.get("total_pnl_usd") or 0)
  if total >= 0:
    return False
  wr = stats.get("win_rate")
  if wr is None:
    return False
  return float(wr) < max_wr + 0.12


def _effective_bucket(
  bucket: dict[str, Any] | None,
  *,
  now: datetime,
  acfg: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
  """Resolve stored bucket state, promoting expired pauses to probing."""
  b = dict(bucket or _default_bucket_state())
  mode = str(b.get("state") or "normal")
  if mode == "paused":
    until = _parse_ts(b.get("paused_until"))
    if until is not None and now >= until:
      b["state"] = "probing"
      if b.get("probe_entries_remaining") is None:
        b["probe_entries_remaining"] = acfg["probe_max_entries"]
      b["paused_until"] = None
      b["updated_at"] = now.isoformat()
      mode = "probing"
  return mode, b


def refresh_adaptive_buckets(
  trades: list[dict[str, Any]],
  state: dict[str, Any] | None,
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
  now: datetime | None = None,
) -> dict[str, Any]:
  """Recompute bucket states from trade log."""
  acfg = adaptive_calibration_cfg(cfg, kind=kind)
  now = now or datetime.now(timezone.utc)
  if not acfg["enabled"]:
    return state or {"enabled": False, "buckets": {}}

  closed = _closed_with_exit_ts(trades)
  buckets: dict[str, dict[str, Any]] = dict((state or {}).get("buckets") or {})

  key_fns: list[Any] = []
  if acfg["price_buckets"]:
    key_fns.append(lambda r: price_bucket_key(r.get("entry_price_cents")))
  if acfg["spread_buckets"]:
    key_fns.append(lambda r: spread_bucket_key(r.get("entry_spread_cents")))

  short_stats: dict[str, dict[str, Any]] = {}
  long_stats: dict[str, dict[str, Any]] = {}
  for key_fn in key_fns:
    short_stats.update(
      _bucket_stats(closed, key_fn=key_fn, window_hours=acfg["short_window_hours"], now=now)
    )
    long_stats.update(
      _bucket_stats(closed, key_fn=key_fn, window_hours=acfg["long_window_hours"], now=now)
    )

  all_keys = set(short_stats) | set(long_stats) | set(buckets)
  for key in all_keys:
    cur = dict(buckets.get(key) or _default_bucket_state())
    mode = str(cur.get("state") or "normal")

    if mode == "paused":
      until = _parse_ts(cur.get("paused_until"))
      if until and now >= until:
        cur["state"] = "probing"
        cur["probe_entries_remaining"] = acfg["probe_max_entries"]
        cur["paused_until"] = None
        cur["updated_at"] = now.isoformat()
      buckets[key] = cur
      continue

    if mode == "probing":
      buckets[key] = cur
      continue

    short = short_stats.get(key) or {}
    long = long_stats.get(key) or {}
    cur["short_stats"] = short
    cur["long_stats"] = long
    cur["updated_at"] = now.isoformat()

    if _should_pause(
      long,
      min_trades=acfg["long_min_trades"],
      max_wr=acfg["long_max_win_rate"],
      min_loss=acfg["long_min_loss_usd"],
    ):
      cur["state"] = "paused"
      cur["paused_until"] = _pause_until(acfg["pause_hours_long"], now=now)
      cur["reason"] = "long_window_losses"
    elif _should_pause(
      short,
      min_trades=acfg["short_min_trades"],
      max_wr=acfg["short_max_win_rate"],
      min_loss=acfg["short_min_loss_usd"],
    ):
      cur["state"] = "paused"
      cur["paused_until"] = _pause_until(acfg["pause_hours_short"], now=now)
      cur["reason"] = "short_window_losses"
    elif _should_tighten(
      short,
      min_trades=acfg["short_min_trades"],
      max_wr=acfg["short_max_win_rate"],
      min_loss=acfg["short_min_loss_usd"] * 0.5,
    ):
      cur["state"] = "tightened"
      cur["reason"] = "marginal_short_window"
    elif mode == "tightened" and short.get("total_pnl_usd", 0) >= 0 and int(short.get("trades") or 0) >= 2:
      cur["state"] = "normal"
      cur["reason"] = "recovered"
    else:
      cur["state"] = "normal"
      cur.pop("reason", None)

    buckets[key] = cur

  return {
    "enabled": True,
    "kind": kind,
    "refreshed_at": now.isoformat(),
    "buckets": buckets,
  }


def adaptive_entry_allowed(
  state: dict[str, Any] | None,
  *,
  entry_price_cents: int | None,
  entry_spread_cents: int | None,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
  aggressive: bool = False,
  now: datetime | None = None,
) -> tuple[bool, str | None, float]:
  """Return (ok, skip_reason, extra_min_ask_edge_cents)."""
  acfg = adaptive_calibration_cfg(cfg, kind=kind)
  if not acfg["enabled"]:
    return True, None, 0.0
  if aggressive and not acfg["apply_in_aggressive_mode"]:
    return True, None, 0.0

  buckets = (state or {}).get("buckets") or {}
  now = now or datetime.now(timezone.utc)
  keys: list[str] = []
  if acfg["price_buckets"] and entry_price_cents is not None:
    keys.append(price_bucket_key(entry_price_cents))
  if acfg["spread_buckets"] and entry_spread_cents is not None:
    keys.append(spread_bucket_key(entry_spread_cents))
  if not keys:
    return True, None, 0.0

  extra_edge = 0.0
  for key in keys:
    mode, b = _effective_bucket(buckets.get(key), now=now, acfg=acfg)
    if mode == "paused":
      until = _parse_ts(b.get("paused_until"))
      if until is None or now < until:
        return False, f"adaptive_bucket_paused:{key}", 0.0
    if mode == "probing":
      remaining = int(b.get("probe_entries_remaining") or 0)
      if remaining <= 0:
        return False, f"adaptive_probe_exhausted:{key}", 0.0
    if mode == "tightened":
      extra_edge = max(extra_edge, acfg["tightened_edge_boost_cents"])

  return True, None, extra_edge


def record_adaptive_probe_entry(
  state: dict[str, Any] | None,
  *,
  entry_price_cents: int | None,
  entry_spread_cents: int | None,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
) -> dict[str, Any]:
  """Decrement probe allowance when a probing bucket entry is placed."""
  acfg = adaptive_calibration_cfg(cfg, kind=kind)
  state = dict(state or {"buckets": {}})
  buckets = dict(state.get("buckets") or {})
  now = datetime.now(timezone.utc)
  for key in _entry_bucket_keys(entry_price_cents, entry_spread_cents, acfg):
    mode, b = _effective_bucket(buckets.get(key), now=now, acfg=acfg)
    if mode == "probing":
      b["probe_entries_remaining"] = max(0, int(b.get("probe_entries_remaining") or 0) - 1)
      b["updated_at"] = now.isoformat()
      buckets[key] = b
  state["buckets"] = buckets
  return state


def record_adaptive_probe_exit(
  state: dict[str, Any] | None,
  *,
  entry_price_cents: int | None,
  entry_spread_cents: int | None,
  pnl_usd: float,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
) -> dict[str, Any]:
  """After a probe-window exit, resume or re-pause the bucket."""
  acfg = adaptive_calibration_cfg(cfg, kind=kind)
  state = dict(state or {"buckets": {}})
  buckets = dict(state.get("buckets") or {})
  now = datetime.now(timezone.utc)
  for key in _entry_bucket_keys(entry_price_cents, entry_spread_cents, acfg):
    mode, b = _effective_bucket(buckets.get(key), now=now, acfg=acfg)
    if mode != "probing":
      continue
    if float(pnl_usd) > 0:
      b["state"] = "normal"
      b["probe_entries_remaining"] = 0
      b["paused_until"] = None
      b["reason"] = "probe_won"
    else:
      b["state"] = "paused"
      b["paused_until"] = _pause_until(acfg["probe_pause_hours_on_fail"], now=now)
      b["probe_entries_remaining"] = 0
      b["reason"] = "probe_lost"
    b["updated_at"] = now.isoformat()
    buckets[key] = b
  state["buckets"] = buckets
  return state


def _entry_bucket_keys(
  entry_price_cents: int | None,
  entry_spread_cents: int | None,
  acfg: dict[str, Any],
) -> list[str]:
  keys: list[str] = []
  if acfg.get("price_buckets") and entry_price_cents is not None:
    keys.append(price_bucket_key(entry_price_cents))
  if acfg.get("spread_buckets") and entry_spread_cents is not None:
    keys.append(spread_bucket_key(entry_spread_cents))
  return keys


def run_adaptive_calibration_for_store(
  store: Any,
  *,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
) -> dict[str, Any]:
  """Refresh adaptive bucket state from a bot store trade log."""
  acfg = adaptive_calibration_cfg(cfg, kind=kind)
  if not acfg["enabled"]:
    return {"ok": False, "reason": "adaptive_calibration_disabled", "kind": kind}
  previous = store.get_adaptive_calibration()
  trades = store.list_trades(limit=5000)
  updated = refresh_adaptive_buckets(trades, previous, cfg, kind=kind)
  store.save_adaptive_calibration(updated)
  paused = sum(1 for b in (updated.get("buckets") or {}).values() if b.get("state") == "paused")
  probing = sum(1 for b in (updated.get("buckets") or {}).values() if b.get("state") == "probing")
  tightened = sum(1 for b in (updated.get("buckets") or {}).values() if b.get("state") == "tightened")
  return {
    "ok": True,
    "kind": kind,
    "paused_buckets": paused,
    "probing_buckets": probing,
    "tightened_buckets": tightened,
    "refreshed_at": updated.get("refreshed_at"),
  }
