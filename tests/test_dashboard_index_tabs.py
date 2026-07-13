"""Dashboard DOM ids required for SPX/NDX hourly tabs."""

from __future__ import annotations

from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / "src" / "api" / "static" / "dashboard.html"

INDEX_PREFIXES = ("spx", "ndx")

REQUIRED_SUFFIXES = (
  "hourly-bot",
  "hourly-live-preflight",
  "hourly-live-bot",
  "hourly-trial-bot",
  "hourly-live-trial-compare",
  "hourly-guide",
  "daily-meta",
  "daily-freq-badge",
  "intrahour-opportunity",
  "daily-primary-signal",
  "daily-late-call",
  "daily-summary",
  "hourly-calibration",
  "hourly-history",
  "daily-threshold",
  "daily-range-summary",
  "daily-range",
  "daily-structure",
)


def test_index_hourly_panels_have_prefixed_dom_ids():
  html = DASHBOARD.read_text(encoding="utf-8")
  for prefix in INDEX_PREFIXES:
    assert f'id="panel-{prefix}-hourly"' in html
    for suffix in REQUIRED_SUFFIXES:
      assert f'id="{prefix}-{suffix}"' in html, f"missing {prefix}-{suffix}"


def test_dashboard_wires_index_hourly_api_paths():
  html = DASHBOARD.read_text(encoding="utf-8")
  assert "function indexLivePreflightUrl(asset)" in html
  assert "function indexLiveArmUrl(asset)" in html
  assert "/hourly-live/preflight" in html
  assert "/hourly-live/arm" in html
  for asset in INDEX_PREFIXES:
    assert f"/api/{asset}/hourly/prediction" in html
    assert f"/api/{asset}/hourly/bot" in html
    assert f"/api/{asset}/hourly-live/bot" in html
    assert f"/api/{asset}/hourly-trial/bot" in html


def test_should_render_hourly_bot_supports_index_assets():
  html = DASHBOARD.read_text(encoding="utf-8")
  assert "function hourlyTabForAsset(asset)" in html
  assert "return activeTab === hourlyTabForAsset(asset)" in html
  assert "asset === 'spx'" in html
  assert "asset === 'ndx'" in html
  assert "loadIndexHourlyLiveTrialCompare" in html
  assert "refreshIndexTrialBot" in html
