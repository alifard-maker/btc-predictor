"""SPX/NDX paper trial bootstrap — ETH hourly live rules in paper mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.assets import asset_cfg
from src.config import load_config
from src.trading.hourly_bot_store import HourlyBotStore
from src.trading.index_paper_experiment import (
  ensure_index_paper_experiments,
  seed_index_paper_settings_from_cfg,
)


@pytest.fixture
def base_cfg():
  return load_config()


def test_index_paper_experiment_uses_pnl_first_profile(base_cfg):
  for asset in ("spx", "ndx"):
    bot = asset_cfg(base_cfg, asset)["hourly"]["bot"]
    assert bot.get("mode") == "paper"
    assert bot.get("live_mechanics_profile") == "pnl_first"
    assert bot.get("paper_experiment", {}).get("enabled") is True
    assert bot["late_entry"]["enabled"] is False
    assert bot["max_hours_to_settle_for_entry"] == 0.75


def test_seed_index_paper_settings_enables_store(tmp_path: Path, base_cfg):
  acfg = asset_cfg(base_cfg, "spx")
  logs = Path(acfg["paths"]["logs"])
  logs.mkdir(parents=True, exist_ok=True)
  store = HourlyBotStore(logs / "hourly_bot_spx.db")
  store.save_settings(
    store.get_settings().__class__(
      **{**store.get_settings().to_dict(), "enabled": False, "mode": "live"}
    )
  )
  result = seed_index_paper_settings_from_cfg(store, acfg, "spx")
  assert result["synced"] is True
  settings = store.get_settings()
  assert settings.enabled is True
  assert settings.mode == "paper"
  assert settings.continuous is True


def test_ensure_index_paper_experiments_arms_both(tmp_path: Path, base_cfg, monkeypatch):
  class _Loop:
    cfg = base_cfg

    def __init__(self):
      self._index_cfgs = {
        "spx": asset_cfg(base_cfg, "spx"),
        "ndx": asset_cfg(base_cfg, "ndx"),
      }
      self._hourly_bot_stores = {}

    def hourly_bot_store(self, asset: str, *, kind: str = "hourly"):
      assert kind == "hourly"
      key = f"{kind}:{asset}"
      if key not in self._hourly_bot_stores:
        acfg = self._index_cfgs[asset]
        logs = Path(acfg["paths"]["logs"])
        logs.mkdir(parents=True, exist_ok=True)
        self._hourly_bot_stores[key] = HourlyBotStore(logs / f"hourly_bot_{asset}.db")
      return self._hourly_bot_stores[key]

  monkeypatch.setattr(
    "src.trading.index_paper_experiment.asset_enabled",
    lambda _cfg, asset: asset in ("spx", "ndx"),
  )
  out = ensure_index_paper_experiments(_Loop())
  assert out["ok"] is True
  for asset in ("spx", "ndx"):
    assert out["assets"][asset]["synced"] is True
