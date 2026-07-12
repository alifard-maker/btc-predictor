"""24h P&L-first probe: entry churn caps, trial regime sync, compare epoch."""

from __future__ import annotations

from typing import Any

from src.trading.bot_runtime import parse_stats_epoch_at, set_stats_epoch_at, stats_epoch_at

_PROBE_LIVE_KINDS = frozenset({"hourly", "hourly_live"})
_PROBE_TRIAL_KINDS = frozenset({"hourly_trial_mech", "hourly_trial"})
_PROBE_ENTRY_CAP_KINDS = _PROBE_LIVE_KINDS | _PROBE_TRIAL_KINDS

_TRIAL_REGIME_SYNC: tuple[tuple[str, str], ...] = (
  ("btc", "hourly_trial_mech"),
  ("eth", "hourly_trial"),
)


def probe_24h_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  from src.trading.pnl_first_gates import _pnl_first_cfg

  return dict(_pnl_first_cfg(cfg).get("probe_24h") or {})


def probe_24h_active(cfg: dict[str, Any] | None) -> bool:
  return bool(probe_24h_cfg(cfg).get("enabled"))


def probe_stats_epoch_iso(cfg: dict[str, Any] | None) -> str | None:
  if not probe_24h_active(cfg):
    return None
  block = probe_24h_cfg(cfg)
  raw = block.get("stats_epoch_at") or block.get("started_at")
  if not raw:
    return None
  return str(raw)


def probe_max_filled_enters_per_hour(cfg: dict[str, Any] | None) -> int | None:
  if not probe_24h_active(cfg):
    return None
  block = probe_24h_cfg(cfg)
  raw = block.get("max_filled_enters_per_hour")
  if raw is None:
    return 2
  return max(0, int(raw))


def probe_entry_cap_applies(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
) -> bool:
  if not probe_24h_active(cfg):
    return False
  if kind not in _PROBE_ENTRY_CAP_KINDS:
    return False
  if kind in _PROBE_LIVE_KINDS:
    return str(mode).lower() == "live"
  return True


def probe_filled_enters_this_hour(
  store: Any,
  event_ticker: str,
  *,
  mode: str | None,
) -> tuple[int, int]:
  """Return (filled_enters, resting_enter_tickers) for the hour."""
  summary_fn = getattr(store, "hour_interval_summary", None)
  if not callable(summary_fn):
    return 0, 0
  row = summary_fn(event_ticker, mode=mode)
  filled = int(row.get("filled_enter_count_this_hour") or row.get("enter_count") or 0)
  resting = int(row.get("resting_enter_count") or 0)
  return filled, resting


def probe_entry_churn_block_reason(
  store: Any,
  event_ticker: str,
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
) -> str | None:
  """Block when probe hour enter budget is exhausted (filled + resting slots)."""
  if not probe_entry_cap_applies(cfg, kind=kind, mode=mode):
    return None
  cap = probe_max_filled_enters_per_hour(cfg)
  if cap is None or cap <= 0:
    return None
  filled, resting = probe_filled_enters_this_hour(store, event_ticker, mode=mode)
  used = filled + resting
  if used >= cap:
    return f"probe_24h_entry_cap:{used}>={cap}"
  return None


def trial_regime_sync_pause_when_live_blocked(
  tab: dict[str, Any],
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  asset: str,
) -> str | None:
  """Pause paper twins when live would be regime-blocked (BTC mech + ETH trial)."""
  from src.trading.pnl_first_gates import _regime_block_hint

  asset_key = str(asset).lower()
  kind_key = str(kind)
  probe = probe_24h_cfg(cfg)
  trial_cfg = dict((((cfg or {}).get("hourly") or {}).get("bot") or {}).get("trial_mech") or {})
  eth_trial_cfg = dict(
    ((((cfg or {}).get("eth") or {}).get("hourly") or {}).get("bot") or {}).get("trial") or {}
  )
  allowed = (asset_key, kind_key) in _TRIAL_REGIME_SYNC
  if not allowed:
    return None
  if kind_key == "hourly_trial_mech":
    if not probe.get("enabled") and not trial_cfg.get("pause_when_live_regime_blocked"):
      return None
  elif kind_key == "hourly_trial":
    if not probe.get("enabled") and not eth_trial_cfg.get("pause_when_live_regime_blocked"):
      return None
  hint = _regime_block_hint(tab)
  if hint:
    prefix = "trial_regime_sync" if kind_key == "hourly_trial" else "trial_mech_regime_sync"
    return f"{prefix}:{hint}"
  return None


def apply_probe_entry_estrat_overlay(
  estrat: Any,
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  mode: str,
) -> Any:
  """During probe, keep live/trial churn low — no scale-in, 1 entry/cycle."""
  from dataclasses import replace

  from src.trading.entry_strategy import EntryStrategyConfig

  if not probe_entry_cap_applies(cfg, kind=kind, mode=mode):
    if kind in _PROBE_TRIAL_KINDS and probe_24h_active(cfg):
      cap = probe_max_filled_enters_per_hour(cfg)
      if cap is not None:
        kw = {
          "allow_scale_in": False,
          "max_entries_per_cycle": 1,
          "max_concurrent_positions": min(int(estrat.max_concurrent_positions or 2), cap),
        }
        if isinstance(estrat, EntryStrategyConfig):
          return replace(estrat, **kw)
    return estrat
  cap = probe_max_filled_enters_per_hour(cfg) or 2
  kw = {
    "allow_scale_in": False,
    "scale_in_max_legs_per_ticker": 1,
    "max_entries_per_cycle": 1,
    "max_concurrent_positions": min(int(estrat.max_concurrent_positions or 2), cap),
  }
  if isinstance(estrat, EntryStrategyConfig):
    return replace(estrat, **kw)
  return estrat


def effective_compare_stats_epoch_at(
  live_store: Any,
  cfg: dict[str, Any] | None,
) -> str | None:
  probe_iso = probe_stats_epoch_iso(cfg)
  with live_store._connect() as conn:
    store_iso = stats_epoch_at(conn)
  if not probe_iso:
    return store_iso
  probe_dt = parse_stats_epoch_at(probe_iso)
  store_dt = parse_stats_epoch_at(store_iso)
  if probe_dt and (store_dt is None or probe_dt >= store_dt):
    return probe_iso
  return store_iso


def ensure_probe_stats_epoch(loop: Any, cfg: dict[str, Any] | None) -> dict[str, Any]:
  """Advance BTC/ETH compare store epochs to probe stats_epoch_at when newer."""
  target_iso = probe_stats_epoch_iso(cfg)
  if not target_iso:
    return {"ok": True, "skipped": True, "reason": "probe_inactive_or_no_epoch"}
  target_dt = parse_stats_epoch_at(target_iso)
  if target_dt is None:
    return {"ok": False, "error": f"invalid_probe_epoch:{target_iso}"}

  results: dict[str, Any] = {}
  for asset, kind in (
    ("btc", "hourly"),
    ("btc", "hourly_trial_mech"),
    ("eth", "hourly_live"),
    ("eth", "hourly_trial"),
  ):
    key = f"{asset}:{kind}"
    try:
      store = loop.hourly_bot_store(asset, kind=kind)
      with store._connect() as conn:
        cur_iso = stats_epoch_at(conn)
        cur_dt = parse_stats_epoch_at(cur_iso)
        if cur_dt is None or target_dt > cur_dt:
          set_stats_epoch_at(conn, target_dt.isoformat())
          results[key] = {"stats_epoch_at": target_dt.isoformat(), "updated": True}
        else:
          results[key] = {"stats_epoch_at": cur_iso, "updated": False}
    except Exception as exc:
      results[key] = {"error": f"{type(exc).__name__}:{exc}"}
  return {"ok": True, "probe_stats_epoch_at": target_iso, "stores": results}
