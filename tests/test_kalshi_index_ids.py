"""Kalshi CF Benchmarks index id helpers."""

from __future__ import annotations

from src.data.kalshi import index_live_source, index_slug


def test_index_slug_eth_aliases():
  assert index_slug("ETHUSD_RTI") == "erti"
  assert index_slug("ERTI") == "erti"
  assert index_slug("BRTI") == "brti"


def test_index_live_source_canonical():
  assert index_live_source("ETHUSD_RTI") == "erti_live"
  assert index_live_source("BRTI") == "brti_live"
