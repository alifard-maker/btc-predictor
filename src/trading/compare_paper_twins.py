"""Fair paper twins for BTC/ETH live↔trial compare cards.

BTC compare: live hourly vs paper hourly_trial_mech
ETH compare: live hourly_live vs paper hourly_trial

Store `enabled` gates trading; yaml `continuous_enabled` only schedules the job.
This module keeps the paper twin stores armed while compare_paper_twins is on.
"""

from __future__ import annotations

import logging
from typing import Any

from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore

log = logging.getLogger(__name__)

# (asset, trial_kind, live_kind, yaml keys under root cfg → trial section)
_COMPARE_TWINS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
  ("btc", "hourly_trial_mech", "hourly", ("hourly", "bot", "trial_mech")),
  ("eth", "hourly_trial", "hourly_live", ("eth", "hourly", "bot", "trial")),
)


def compare_paper_twins_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict((cfg or {}).get("compare_paper_twins") or {})


def compare_paper_twins_active(cfg: dict[str, Any] | None) -> bool:
  block = compare_paper_twins_cfg(cfg)
  if block:
    return bool(block.get("enabled", True))
  # Default on when block omitted — compare cards need twins.
  return True


def _nested(cfg: dict[str, Any] | None, keys: tuple[str, ...]) -> dict[str, Any]:
  cur: Any = cfg or {}
  for key in keys:
    cur = (cur or {}).get(key) or {}
  return dict(cur) if isinstance(cur, dict) else {}


def _trial_wants_twin(trial_cfg: dict[str, Any], *, global_on: bool) -> bool:
  if not global_on:
    return False
  if "compare_twin" in trial_cfg:
    return bool(trial_cfg.get("compare_twin"))
  # continuous_enabled is the historical opt-in for trial continuous jobs
  return bool(trial_cfg.get("continuous_enabled", True))


def _max_spend_for_twin(
  trial_cfg: dict[str, Any],
  live_store: HourlyBotStore | None,
  *,
  default: float = 15.0,
) -> float:
  if trial_cfg.get("max_spend_per_hour_usd") is not None:
    return float(trial_cfg["max_spend_per_hour_usd"])
  if live_store is not None:
    try:
      return float(live_store.get_settings().max_spend_per_hour_usd or default)
    except Exception:
      pass
  return default


def seed_compare_paper_twin(
  store: HourlyBotStore,
  *,
  trial_cfg: dict[str, Any],
  live_store: HourlyBotStore | None = None,
  source: str = "compare_paper_twins",
) -> dict[str, Any]:
  """Arm one paper twin store: enabled + paper + continuous + fair spend."""
  max_spend = _max_spend_for_twin(trial_cfg, live_store)
  patch = {
    "enabled": True,
    "mode": "paper",
    "continuous": bool(trial_cfg.get("continuous_enabled", True)),
    "paper_auto_refill": True,
    "max_spend_per_hour_usd": max_spend,
    "auto_stopped": False,
    "auto_stop_reason": None,
  }
  cur = store.get_settings()
  merged = {**cur.to_dict(), **patch}
  changed = [k for k, v in patch.items() if cur.to_dict().get(k) != v]
  if not changed:
    return {
      "ok": True,
      "synced": True,
      "changed_fields": [],
      "unchanged": True,
      "enabled": cur.enabled,
      "mode": cur.mode,
      "continuous": cur.continuous,
      "max_spend_per_hour_usd": cur.max_spend_per_hour_usd,
    }
  store.save_settings(HourlyBotSettings.from_dict(merged), source=source)
  log.info(
    "compare_paper_twins: armed %s enabled=True mode=paper continuous=%s max=$%.2f (%s)",
    source,
    merged.get("continuous"),
    max_spend,
    ", ".join(changed),
  )
  return {
    "ok": True,
    "synced": True,
    "changed_fields": changed,
    "enabled": merged.get("enabled"),
    "mode": merged.get("mode"),
    "continuous": merged.get("continuous"),
    "max_spend_per_hour_usd": merged.get("max_spend_per_hour_usd"),
  }


def ensure_compare_paper_twins(loop: Any, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
  """Enable BTC trial_mech + ETH trial paper twins for compare cards."""
  cfg = cfg if cfg is not None else getattr(loop, "cfg", None)
  global_on = compare_paper_twins_active(cfg)
  results: list[dict[str, Any]] = []

  for asset, trial_kind, live_kind, yaml_keys in _COMPARE_TWINS:
    trial_cfg = _nested(cfg, yaml_keys)
    if not _trial_wants_twin(trial_cfg, global_on=global_on):
      results.append({
        "asset": asset,
        "kind": trial_kind,
        "skipped": True,
        "reason": "compare_twin_off",
      })
      continue
    if asset == "eth":
      from src.assets import asset_enabled

      if not asset_enabled(cfg, "eth"):
        results.append({
          "asset": asset,
          "kind": trial_kind,
          "skipped": True,
          "reason": "eth_disabled",
        })
        continue

    trial_store = loop.hourly_bot_store(asset, kind=trial_kind)
    live_store = loop.hourly_bot_store(asset, kind=live_kind)
    seed = seed_compare_paper_twin(
      trial_store,
      trial_cfg=trial_cfg,
      live_store=live_store,
      source=f"compare_paper_twins:{asset}:{trial_kind}",
    )
    results.append({
      "asset": asset,
      "kind": trial_kind,
      "live_kind": live_kind,
      **seed,
    })

  return {
    "ok": True,
    "active": global_on,
    "twins": results,
  }


def compare_store_kinds(asset: str) -> tuple[str, str]:
  """Return (live_kind, trial_kind) for the dashboard compare card."""
  asset = asset.lower()
  if asset == "btc":
    return "hourly", "hourly_trial_mech"
  if asset == "eth":
    return "hourly_live", "hourly_trial"
  return "hourly", "hourly_trial"
