"""Signal-mirror paper bot — manual-lane entry/exit alignment."""

from __future__ import annotations

from src.trading.compare_paper_twins import human_compare_bot_kind
from src.trading.entry_strategy import EntryStrategyConfig
from src.trading.hourly_signal_mirror import (
  apply_signal_mirror_entry_estrat,
  signal_mirror_active,
  signal_mirror_cfg,
  signal_mirror_entry_cfg,
  signal_mirror_uses_thesis_exits,
)


def _cfg() -> dict:
  return {
    "pnl_first": {"max_hours_to_settle_for_entry": 1.35},
    "hourly": {
      "bot": {
        "trial_mech": {
          "signal_mirror": {
            "enabled": True,
            "min_ask_edge_cents": 0,
            "max_entries_per_cycle": 4,
          },
        },
      },
    },
    "eth": {
      "hourly": {
        "bot": {
          "signal_mirror": {"enabled": True},
          "max_hours_to_settle_for_entry": 0.75,
        },
      },
    },
  }


def test_signal_mirror_active_paper_only():
  cfg = _cfg()
  assert signal_mirror_active(cfg, kind="hourly_trial_mech", asset="btc", mode="paper")
  assert not signal_mirror_active(cfg, kind="hourly_trial_mech", asset="btc", mode="live")
  assert signal_mirror_active(cfg, kind="hourly", asset="eth", mode="paper")


def test_signal_mirror_cfg_paths():
  cfg = _cfg()
  assert signal_mirror_cfg(cfg, kind="hourly_trial_mech", asset="btc").get("enabled") is True
  assert signal_mirror_cfg(cfg, kind="hourly", asset="eth").get("enabled") is True


def test_signal_mirror_entry_cfg_widens_settle_window():
  cfg = _cfg()
  mcfg = signal_mirror_cfg(cfg, kind="hourly", asset="eth")
  out = signal_mirror_entry_cfg(cfg, mcfg)
  assert out["hourly"]["bot"]["max_hours_to_settle_for_entry"] == 1.35


def test_apply_signal_mirror_entry_estrat_relaxes_gates():
  base = EntryStrategyConfig(min_ask_edge_cents=18, max_entries_per_cycle=1)
  out = apply_signal_mirror_entry_estrat(base, {"min_ask_edge_cents": 0, "max_entries_per_cycle": 4})
  assert out.min_ask_edge_cents == 0
  assert out.max_entries_per_cycle == 4
  assert out.allow_scale_in is False


def test_signal_mirror_thesis_exits_on_trial_mech():
  cfg = _cfg()
  assert signal_mirror_uses_thesis_exits(
    cfg, kind="hourly_trial_mech", asset="btc", mode="paper",
  )


def test_mirror_tradable_includes_fade_value():
  from src.trading.hourly_bot import _mirror_tradable_signal, _side_from_signal

  assert _mirror_tradable_signal("BUY YES")
  assert _mirror_tradable_signal("BUY NO")
  assert _mirror_tradable_signal("VALUE YES")
  assert _mirror_tradable_signal("FADE YES")
  assert not _mirror_tradable_signal("NEUTRAL")
  assert _side_from_signal("VALUE YES") == "yes"
  assert _side_from_signal("FADE YES") == "no"


def test_human_compare_bot_kind():
  assert human_compare_bot_kind("btc") == "hourly_trial_mech"
  assert human_compare_bot_kind("eth") == "hourly"
