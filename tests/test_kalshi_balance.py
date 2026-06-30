"""Kalshi portfolio balance helpers."""

from __future__ import annotations

from src.data.kalshi import KalshiClient


def test_balance_usd_from_cents():
  assert KalshiClient.balance_usd_from_cents(5108) == 51.08
  assert KalshiClient.balance_usd_from_cents(None) is None


def test_balance_cents_from_payload():
  assert KalshiClient.balance_cents_from_payload({"balance": 5108}) == 5108
  assert KalshiClient.balance_cents_from_payload({}) is None
  assert KalshiClient.balance_cents_from_payload(None) is None
