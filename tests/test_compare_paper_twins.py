"""Tests for fair paper twins used by live↔trial compare cards."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.trading.compare_paper_twins import (
  compare_store_kinds,
  ensure_compare_paper_twins,
  seed_compare_paper_twin,
)
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore


def test_compare_store_kinds():
  assert compare_store_kinds("btc") == ("hourly", "hourly_trial_mech")
  assert compare_store_kinds("eth") == ("hourly_live", "hourly_trial")
  assert compare_store_kinds("spx") == ("hourly", "hourly_trial")


def test_seed_compare_paper_twin_enables_disabled_store(tmp_path: Path):
  store = HourlyBotStore(tmp_path / "trial.db")
  store.save_settings(
    HourlyBotSettings(
      enabled=False,
      mode="paper",
      continuous=True,
      max_spend_per_hour_usd=10.0,
    ),
    source="test",
  )
  live = HourlyBotStore(tmp_path / "live.db")
  live.save_settings(
    HourlyBotSettings(enabled=True, mode="live", max_spend_per_hour_usd=15.0),
    source="test",
  )

  out = seed_compare_paper_twin(
    store,
    trial_cfg={"continuous_enabled": True, "compare_twin": True},
    live_store=live,
  )
  assert out["ok"] is True
  assert "enabled" in out["changed_fields"]
  settings = store.get_settings()
  assert settings.enabled is True
  assert settings.mode == "paper"
  assert settings.continuous is True
  assert settings.max_spend_per_hour_usd == 15.0


def test_ensure_compare_paper_twins_arms_btc_and_eth(tmp_path: Path):
  stores: dict[tuple[str, str], HourlyBotStore] = {}

  def hourly_bot_store(asset: str, *, kind: str = "hourly"):
    key = (asset, kind)
    if key not in stores:
      stores[key] = HourlyBotStore(tmp_path / f"{kind}_{asset}.db")
      stores[key].save_settings(
        HourlyBotSettings(enabled=False, mode="paper", continuous=True, max_spend_per_hour_usd=10.0),
        source="test",
      )
    return stores[key]

  cfg = {
    "compare_paper_twins": {"enabled": True},
    "hourly": {
      "bot": {
        "trial_mech": {
          "continuous_enabled": True,
          "compare_twin": True,
          "max_spend_per_hour_usd": 15,
        }
      }
    },
    "eth": {
      "enabled": True,
      "hourly": {
        "bot": {
          "trial": {
            "continuous_enabled": True,
            "compare_twin": True,
            "max_spend_per_hour_usd": 15,
          }
        }
      },
    },
  }
  loop = SimpleNamespace(cfg=cfg, hourly_bot_store=hourly_bot_store)
  out = ensure_compare_paper_twins(loop, cfg)
  assert out["active"] is True
  armed = {f"{t['asset']}:{t['kind']}" for t in out["twins"] if not t.get("skipped")}
  assert "btc:hourly_trial_mech" in armed
  assert "eth:hourly_trial" in armed
  assert stores[("btc", "hourly_trial_mech")].get_settings().enabled is True
  assert stores[("eth", "hourly_trial")].get_settings().enabled is True
  assert stores[("eth", "hourly_trial")].get_settings().max_spend_per_hour_usd == 15.0
