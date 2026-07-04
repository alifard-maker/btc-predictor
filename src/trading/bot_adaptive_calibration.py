"""Closed-loop bucket calibration from recent closed trades (price / spread buckets).

Uses stats-driven throttle levels (edge + stake) instead of timer-based hard pauses.
Rolling windows are re-evaluated every refresh so buckets release as performance recovers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.trading.bot_performance_report import (
  PRICE_BUCKETS,
  SPREAD_BUCKETS,
  _bucket_label,
  _closed_round_trips,
)


DEFAULT_THROTTLE_LEVELS: list[dict[str, float]] = [
  {"edge_boost_cents": 0.0, "stake_mult": 1.0},
  {"edge_boost_cents": 4.0, "stake_mult": 0.75},
  {"edge_boost_cents": 8.0, "stake_mult": 0.5},
  {"edge_boost_cents": 12.0, "stake_mult": 0.25},
]

THROTTLE_STATE_NAMES = ("normal", "tightened", "restricted", "severe")


@dataclass(frozen=True)
class AdaptiveAdjustments:
  ok: bool
  hint: str | None
  edge_boost_cents: float
  stake_mult: float


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

  levels_raw = raw.get("throttle_levels") or DEFAULT_THROTTLE_LEVELS
  throttle_levels: list[dict[str, float]] = []
  for i, preset in enumerate(DEFAULT_THROTTLE_LEVELS):
    override = levels_raw[i] if i < len(levels_raw) and isinstance(levels_raw[i], dict) else {}
    throttle_levels.append(
      {
        "edge_boost_cents": float(override.get("edge_boost_cents", preset["edge_boost_cents"])),
        "stake_mult": float(override.get("stake_mult", preset["stake_mult"])),
      }
    )

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
    "tightened_edge_boost_cents": float(raw.get("tightened_edge_boost_cents", 4)),
    "apply_in_aggressive_mode": bool(raw.get("apply_in_aggressive_mode", True)),
    "price_buckets": bool(raw.get("price_buckets", True)),
    "spread_buckets": bool(raw.get("spread_buckets", False)),
    "refresh_interval_minutes": int(raw.get("refresh_interval_minutes", 30)),
    "throttle_levels": throttle_levels,
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
  return {
    "state": "normal",
    "throttle_level": 0,
    "updated_at": datetime.now(timezone.utc).isoformat(),
  }


def _stats_fail(
  stats: dict[str, Any],
  *,
  min_trades: int,
  max_wr: float,
  min_loss: float,
) -> bool:
  if int(stats.get("trades") or 0) < min_trades:
    return False
  total = float(stats.get("total_pnl_usd") or 0)
  if total > -abs(min_loss):
    return False
  wr = stats.get("win_rate")
  if wr is None:
    return False
  return float(wr) <= max_wr


def _stats_marginal(
  stats: dict[str, Any],
  *,
  min_trades: int,
  max_wr: float,
  min_loss: float,
) -> bool:
  if int(stats.get("trades") or 0) < min_trades:
    return False
  total = float(stats.get("total_pnl_usd") or 0)
  if total >= 0:
    return False
  wr = stats.get("win_rate")
  if wr is None:
    return False
  return float(wr) < max_wr + 0.12


def compute_throttle_level(
  short: dict[str, Any],
  long: dict[str, Any],
  acfg: dict[str, Any],
) -> int:
  """0=normal … 3=severe from rolling short/long window stats."""
  if _stats_fail(
    long,
    min_trades=acfg["long_min_trades"],
    max_wr=acfg["long_max_win_rate"],
    min_loss=acfg["long_min_loss_usd"],
  ):
    return 3
  if _stats_fail(
    short,
    min_trades=acfg["short_min_trades"],
    max_wr=acfg["short_max_win_rate"],
    min_loss=acfg["short_min_loss_usd"],
  ):
    return 2
  if _stats_marginal(
    short,
    min_trades=acfg["short_min_trades"],
    max_wr=acfg["short_max_win_rate"],
    min_loss=acfg["short_min_loss_usd"] * 0.5,
  ):
    return 1
  if int(short.get("trades") or 0) >= 2 and float(short.get("total_pnl_usd") or 0) >= 0:
    return 0
  return 0


def _legacy_throttle_level(bucket: dict[str, Any] | None) -> int | None:
  """Map pre-soft-throttle stored states before stats refresh runs."""
  if not bucket:
    return None
  level = bucket.get("throttle_level")
  if level is not None:
    try:
      return int(level)
    except (TypeError, ValueError):
      pass
  state = str(bucket.get("state") or "normal")
  if state == "severe":
    return 3
  if state in ("restricted", "paused", "probing"):
    return 2
  if state == "tightened":
    return 1
  return 0 if state == "normal" else None


def _state_name_for_level(level: int) -> str:
  level = max(0, min(level, len(THROTTLE_STATE_NAMES) - 1))
  return THROTTLE_STATE_NAMES[level]


def _throttle_reason(level: int, short: dict[str, Any], long: dict[str, Any]) -> str:
  if level >= 3:
    return "long_window_losses"
  if level >= 2:
    return "short_window_losses"
  if level >= 1:
    return "marginal_short_window"
  if int(short.get("trades") or 0) >= 2 and float(short.get("total_pnl_usd") or 0) >= 0:
    return "recovered"
  return "normal"


def throttle_adjustments_for_level(level: int, acfg: dict[str, Any]) -> tuple[float, float]:
  levels = acfg.get("throttle_levels") or DEFAULT_THROTTLE_LEVELS
  idx = max(0, min(int(level), len(levels) - 1))
  preset = levels[idx]
  return float(preset["edge_boost_cents"]), float(preset["stake_mult"])


def refresh_adaptive_buckets(
  trades: list[dict[str, Any]],
  state: dict[str, Any] | None,
  cfg: dict[str, Any] | None,
  *,
  kind: str = "hourly",
  now: datetime | None = None,
) -> dict[str, Any]:
  """Recompute bucket throttle levels from trade log (stats-driven, no timer holds)."""
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
    short = short_stats.get(key) or {}
    long = long_stats.get(key) or {}
    cur["short_stats"] = short
    cur["long_stats"] = long
    cur["updated_at"] = now.isoformat()
    cur.pop("paused_until", None)
    cur.pop("probe_entries_remaining", None)

    level = compute_throttle_level(short, long, acfg)
    cur["throttle_level"] = level
    cur["state"] = _state_name_for_level(level)
    if level > 0:
      cur["reason"] = _throttle_reason(level, short, long)
    else:
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
) -> AdaptiveAdjustments:
  """Return throttle adjustments; never hard-block entries (soft throttle only)."""
  acfg = adaptive_calibration_cfg(cfg, kind=kind)
  if not acfg["enabled"]:
    return AdaptiveAdjustments(True, None, 0.0, 1.0)
  if aggressive and not acfg["apply_in_aggressive_mode"]:
    return AdaptiveAdjustments(True, None, 0.0, 1.0)

  buckets = (state or {}).get("buckets") or {}
  keys: list[str] = []
  if acfg["price_buckets"] and entry_price_cents is not None:
    keys.append(price_bucket_key(entry_price_cents))
  if acfg["spread_buckets"] and entry_spread_cents is not None:
    keys.append(spread_bucket_key(entry_spread_cents))
  if not keys:
    return AdaptiveAdjustments(True, None, 0.0, 1.0)

  edge_boost = 0.0
  stake_mult = 1.0
  max_level = 0
  throttle_key: str | None = None
  for key in keys:
    bucket = buckets.get(key) or {}
    level = compute_throttle_level(
      bucket.get("short_stats") or {},
      bucket.get("long_stats") or {},
      acfg,
    )
    legacy = _legacy_throttle_level(bucket)
    if legacy is not None and (bucket.get("short_stats") is None):
      level = max(level, legacy)
    if level > max_level:
      max_level = level
      throttle_key = key
    boost, mult = throttle_adjustments_for_level(level, acfg)
    edge_boost = max(edge_boost, boost)
    stake_mult = min(stake_mult, mult)

  hint = f"adaptive_throttle:{max_level}:{throttle_key}" if max_level > 0 and throttle_key else None
  return AdaptiveAdjustments(True, hint, edge_boost, stake_mult)


def record_adaptive_probe_entry(
  state: dict[str, Any] | None,
  *,
  entry_price_cents: int | None,
  entry_spread_cents: int | None,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
) -> dict[str, Any]:
  """Legacy hook — stats refresh handles bucket state; no probe counter."""
  return dict(state or {"buckets": {}})


def record_adaptive_probe_exit(
  state: dict[str, Any] | None,
  *,
  entry_price_cents: int | None,
  entry_spread_cents: int | None,
  pnl_usd: float,
  cfg: dict[str, Any] | None,
  kind: str = "hourly",
) -> dict[str, Any]:
  """Legacy hook — exit handler triggers full stats refresh separately."""
  return dict(state or {"buckets": {}})


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
  buckets = updated.get("buckets") or {}
  throttled = sum(1 for b in buckets.values() if int(b.get("throttle_level") or 0) > 0)
  by_level = {
    lvl: sum(1 for b in buckets.values() if int(b.get("throttle_level") or 0) == lvl)
    for lvl in (1, 2, 3)
  }
  return {
    "ok": True,
    "kind": kind,
    "throttled_buckets": throttled,
    "throttle_level_1": by_level[1],
    "throttle_level_2": by_level[2],
    "throttle_level_3": by_level[3],
    "refreshed_at": updated.get("refreshed_at"),
  }
