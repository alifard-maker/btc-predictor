"""Tests for ETH 15-minute (KXETH15M) prediction support."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.assets import asset_cfg, asset_enabled, index_id_for_cfg
from src.config import load_config
from src.data.kalshi import KalshiClient
from src.db.store import SqlitePredictionStore


@pytest.fixture
def base_cfg():
  return load_config()


def test_asset_cfg_eth_kalshi_15m(base_cfg):
  eth = asset_cfg(base_cfg, "eth")
  assert eth["kalshi"]["series_ticker"] == "KXETH15M"
  assert eth["kalshi"]["brti_index_id"] == "ETHUSD_RTI"
  assert eth["paths"]["db"].endswith("eth/logs/predictions.db")
  assert "eth" in eth["paths"]["candles"]
  assert index_id_for_cfg(eth) == "ETHUSD_RTI"


def test_asset_cfg_btc_kalshi_unchanged(base_cfg):
  btc = asset_cfg(base_cfg, "btc")
  assert btc["kalshi"]["series_ticker"] == "KXBTC15M"
  assert index_id_for_cfg(btc) == "BRTI"


def test_kalshi_client_eth_labels():
  cfg = {
    "price_feed": {
      "label": "Kalshi CF Benchmarks ETHUSD_RTI",
      "index_id": "ETHUSD_RTI",
      "settlement_reference": "CF Benchmarks ETHUSD_RTI (Kalshi KXETH15M)",
    },
    "kalshi": {
      "enabled": False,
      "series_ticker": "KXETH15M",
      "brti_index_id": "ETHUSD_RTI",
    },
  }
  client = KalshiClient(cfg)
  assert client.series_ticker == "KXETH15M"
  assert client._brtI_index == "ETHUSD_RTI"
  assert "ETHUSD_RTI" in client.price_feed_label()
  assert "KXETH15M" in client.settlement_reference_label()
  assert client._index_target_source() == "kalshi_erti_target"


def test_prediction_store_eth_separate_db():
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    btc_db = str(root / "btc" / "predictions.db")
    eth_db = str(root / "eth" / "predictions.db")
    btc_store = SqlitePredictionStore(btc_db)
    eth_store = SqlitePredictionStore(eth_db)
    btc_store.init()
    eth_store.init()

    btc_store.log_prediction(
      "2026-06-28T12:00:00+00:00",
      100000.0,
      0.55,
      0.45,
      0.55,
      "LONG",
      50.0,
    )
    eth_store.log_prediction(
      "2026-06-28T12:00:00+00:00",
      2500.0,
      0.52,
      0.48,
      0.52,
      "NO TRADE",
      5.0,
    )

    assert len(btc_store.load_recent(10)) == 1
    assert len(eth_store.load_recent(10)) == 1
    assert btc_store.latest()["price"] == 100000.0
    assert eth_store.latest()["price"] == 2500.0

    assert btc_store.clear_all() == 1
    assert len(eth_store.load_recent(10)) == 1


def test_asset_enabled_eth_15m(base_cfg):
  assert asset_enabled(base_cfg, "eth") is True
  eth = asset_cfg(base_cfg, "eth")
  assert eth.get("kalshi", {}).get("enabled") is True
