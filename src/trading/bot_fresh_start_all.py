"""Fresh-start every bot store (paper bankroll reset + live/paper log wipe)."""

from __future__ import annotations

from typing import Any

from src.assets import asset_enabled, asset_v2_enabled
from src.trading.bot_risk_state import bot_risk_key, get_bot_risk_coordinator


def _clear_hourly_store(
  store: Any,
  *,
  asset: str,
  kind: str,
  key: str,
  results: dict[str, Any],
) -> None:
  settings = store.get_settings()
  cap = float(settings.max_spend_per_hour_usd)
  mode = str(settings.mode or "paper")
  paper_state = store.clear_history(cap, mode=mode)
  coord = get_bot_risk_coordinator()
  if coord:
    coord.reset_bot_daily_pnl(bot_risk_key(kind, asset))
  entry: dict[str, Any] = {"mode": mode, "cleared": True}
  if paper_state:
    entry["paper_bankroll"] = paper_state
  results[key] = entry


def _clear_slot15_store(
  store: Any,
  *,
  asset: str,
  kind: str,
  key: str,
  results: dict[str, Any],
) -> None:
  settings = store.get_settings()
  cap = float(settings.max_spend_per_slot_usd)
  mode = str(settings.mode or "paper")
  paper_state = store.clear_history(cap, mode=mode)
  coord = get_bot_risk_coordinator()
  if coord:
    coord.reset_bot_daily_pnl(bot_risk_key(kind, asset))
  entry: dict[str, Any] = {"mode": mode, "cleared": True}
  if paper_state:
    entry["paper_bankroll"] = paper_state
  results[key] = entry


def fresh_start_all_bot_stores(loop: Any, cfg: dict[str, Any]) -> dict[str, Any]:
  """Clear history for every bot (paper + live). Paper bots reset bankroll to max cap."""
  results: dict[str, Any] = {}

  for asset in ("btc", "eth"):
    _clear_hourly_store(
      loop.hourly_bot_store(asset),
      asset=asset,
      kind="hourly",
      key=f"hourly_{asset}",
      results=results,
    )
    _clear_hourly_store(
      loop.hourly_trial_bot_store(asset),
      asset=asset,
      kind="hourly_trial",
      key=f"hourly_trial_{asset}",
      results=results,
    )
    if asset == "btc":
      for kind, store_fn in (
        ("hourly_trial_rally", loop.hourly_trial_rally_bot_store),
        ("hourly_trial_soft", loop.hourly_trial_soft_bot_store),
        ("hourly_trial_mech", loop.hourly_trial_mech_bot_store),
      ):
        _clear_hourly_store(
          store_fn(asset),
          asset=asset,
          kind=kind,
          key=f"{kind}_{asset}",
          results=results,
        )
    if asset == "btc" or loop._slot15m_enabled("eth"):
      _clear_slot15_store(
        loop.slot15_bot_store(asset),
        asset=asset,
        kind="slot15",
        key=f"slot15_{asset}",
        results=results,
      )
    if asset == "eth" and loop._slot15m_enabled("eth"):
      _clear_slot15_store(
        loop.slot15_trial_bot_store("eth"),
        asset="eth",
        kind="slot15_trial",
        key="slot15_trial_eth",
        results=results,
      )
    if asset_v2_enabled(cfg, asset):
      _clear_hourly_store(
        loop.hourly_bot_store(asset, kind="hourly_v2"),
        asset=asset,
        kind="hourly_v2",
        key=f"hourly_v2_{asset}",
        results=results,
      )

  for asset in ("spx", "ndx"):
    if not asset_enabled(cfg, asset):
      continue
    _clear_hourly_store(
      loop.hourly_bot_store(asset),
      asset=asset,
      kind="hourly",
      key=f"hourly_{asset}",
      results=results,
    )
    _clear_hourly_store(
      loop.hourly_trial_bot_store(asset),
      asset=asset,
      kind="hourly_trial",
      key=f"hourly_trial_{asset}",
      results=results,
    )

  return results
