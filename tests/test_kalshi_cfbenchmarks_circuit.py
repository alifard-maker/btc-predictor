"""Kalshi request circuit-breaker edge cases."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.trading.kalshi_circuit import CircuitConfig, KalshiCircuitBreaker, init_circuit_breaker


def test_cfbenchmarks_429_does_not_trip_circuit():
  with tempfile.TemporaryDirectory() as td:
    init_circuit_breaker({"kalshi": {"circuit_breaker": {"warn_threshold": 2}}}, Path(td))
    from src.data.kalshi import KalshiClient

    client = KalshiClient({"kalshi": {"enabled": False, "key_id": "x", "private_key": ""}})
    client.key_id = "test"
    client._private_key = MagicMock()

    with patch.object(client, "_sign", return_value="sig"):
      with patch("src.data.kalshi.requests.request") as req:
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception(
          "429 Client Error: Too Many Requests for url: "
          "https://api.elections.kalshi.com/trade-api/v2/cfbenchmarks/values?id=ETHUSD_RTI"
        )
        req.return_value = resp
        for _ in range(5):
          try:
            client.get("/cfbenchmarks/values", params={"id": "ETHUSD_RTI"}, auth=True)
          except Exception:
            pass

    from src.trading.kalshi_circuit import get_circuit_breaker

    cb = get_circuit_breaker()
    assert cb is not None
    assert not cb.is_paused()
    assert not cb.is_degraded()


def test_non_cfbenchmarks_429_still_trips_after_threshold():
  with tempfile.TemporaryDirectory() as td:
    cb = KalshiCircuitBreaker(
      CircuitConfig(pause_on_429_after=3, pause_seconds_429=15, pause_seconds=30),
      Path(td) / "circuit.json",
    )
    cb.record_failure("HTTP 429 Too Many Requests for url: https://api.elections.kalshi.com/trade-api/v2/markets")
    cb.record_failure("HTTP 429 Too Many Requests for url: https://api.elections.kalshi.com/trade-api/v2/markets")
    assert not cb.is_paused()
    cb.record_failure("HTTP 429 Too Many Requests for url: https://api.elections.kalshi.com/trade-api/v2/markets")
    assert cb.is_paused()
