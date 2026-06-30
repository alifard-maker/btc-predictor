"""Tests for Kalshi API circuit breaker."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.trading.kalshi_circuit import CircuitConfig, KalshiCircuitBreaker


def test_degraded_blocks_entries_before_pause():
  with tempfile.TemporaryDirectory() as td:
    cb = KalshiCircuitBreaker(
      CircuitConfig(warn_threshold=2, failure_threshold=5),
      Path(td) / "circuit.json",
    )
    cb.record_failure("timeout")
    assert not cb.is_degraded()
    assert not cb.entries_blocked()

    cb.record_failure("timeout")
    assert cb.is_degraded()
    assert cb.entries_blocked()
    assert not cb.is_paused()


def test_critical_requests_allowed_during_pause():
  with tempfile.TemporaryDirectory() as td:
    cb = KalshiCircuitBreaker(
      CircuitConfig(failure_threshold=2, pause_seconds=30),
      Path(td) / "circuit.json",
    )
    cb.record_failure("err")
    cb.record_failure("err")
    assert cb.is_paused()
    assert not cb.allows_request(critical=False)
    assert cb.allows_request(critical=True)


def test_record_success_does_not_clear_pause():
  with tempfile.TemporaryDirectory() as td:
    cb = KalshiCircuitBreaker(
      CircuitConfig(failure_threshold=2, pause_seconds=30),
      Path(td) / "circuit.json",
    )
    cb.record_failure("err")
    cb.record_failure("err")
    assert cb.is_paused()
    trip = cb.status_dict()["consecutive_failures"]
    cb.record_success()
    assert cb.is_paused()
    assert cb.status_dict()["consecutive_failures"] == trip
