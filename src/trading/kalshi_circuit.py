"""Kalshi API circuit breaker — throttle before rate limits; protect exits."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STATE: "KalshiCircuitBreaker | None" = None


@dataclass
class CircuitConfig:
  enabled: bool = True
  warn_threshold: int = 2
  failure_threshold: int = 5
  pause_seconds: int = 30
  pause_seconds_429: int = 15
  pause_on_429_after: int = 3


class KalshiCircuitBreaker:
  def __init__(self, cfg: CircuitConfig, state_path: Path):
    self.cfg = cfg
    self.state_path = state_path
    self._consecutive_failures = 0
    self._paused_until_mono: float = 0.0
    self._last_error: str | None = None
    self._paused_at: str | None = None
    self._trip_failures: int = 0
    self._load()

  def _load(self) -> None:
    if not self.state_path.is_file():
      return
    try:
      raw = json.loads(self.state_path.read_text(encoding="utf-8"))
      self._consecutive_failures = int(raw.get("consecutive_failures") or 0)
      self._trip_failures = int(raw.get("trip_failures") or self._consecutive_failures)
      self._last_error = raw.get("last_error")
      self._paused_at = raw.get("paused_at")
      until = raw.get("paused_until_epoch")
      if until is not None:
        remaining = float(until) - time.time()
        if remaining > 0:
          self._paused_until_mono = time.monotonic() + remaining
        else:
          self._paused_until_mono = 0.0
    except Exception as e:
      log.warning("Kalshi circuit state load failed: %s", e)

  def _save(self) -> None:
    try:
      self.state_path.parent.mkdir(parents=True, exist_ok=True)
      paused_until_epoch = None
      if self._pause_remaining() > 0:
        paused_until_epoch = time.time() + self._pause_remaining()
      payload = {
        "consecutive_failures": self._consecutive_failures,
        "trip_failures": self._trip_failures,
        "last_error": self._last_error,
        "paused_at": self._paused_at,
        "paused_until_epoch": paused_until_epoch,
        "updated_at": datetime.now(timezone.utc).isoformat(),
      }
      self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
      log.warning("Kalshi circuit state save failed: %s", e)

  def _pause_remaining(self) -> float:
    if not self._paused_until_mono:
      return 0.0
    return max(0.0, self._paused_until_mono - time.monotonic())

  def _clear_pause_if_expired(self) -> None:
    if self._pause_remaining() <= 0 and self._paused_until_mono:
      self._paused_until_mono = 0.0
      self._consecutive_failures = 0
      self._trip_failures = 0
      self._paused_at = None
      self._save()

  def is_paused(self) -> bool:
    if not self.cfg.enabled:
      return False
    self._clear_pause_if_expired()
    return self._pause_remaining() > 0

  def is_degraded(self) -> bool:
    if not self.cfg.enabled or self.is_paused():
      return False
    return self._consecutive_failures >= self.cfg.warn_threshold

  def entries_blocked(self) -> bool:
    """Only full pause blocks new entries; degraded is warn-only (still tries to trade)."""
    return self.is_paused()

  def throttle_discovery(self) -> bool:
    """Skip heavy Kalshi market scans when API is stressed."""
    if not self.cfg.enabled:
      return False
    return self.is_paused() or self.is_degraded()

  def allows_request(self, *, critical: bool = False) -> bool:
    if not self.cfg.enabled:
      return True
    if critical:
      return True
    return not self.is_paused()

  def record_success(self) -> None:
    if not self.cfg.enabled:
      return
    if self.is_paused():
      return
    self._consecutive_failures = 0
    self._trip_failures = 0
    self._last_error = None
    self._save()

  def record_failure(self, error: str) -> None:
    if not self.cfg.enabled:
      return
    err = error[:500]
    is_429 = "429" in err or "Too Many Requests" in err
    self._consecutive_failures += 1
    self._last_error = err
    should_pause = self._consecutive_failures >= self.cfg.failure_threshold
    if is_429 and self._consecutive_failures >= self.cfg.pause_on_429_after:
      should_pause = True
    if should_pause and self._pause_remaining() <= 0:
      pause = float(self.cfg.pause_seconds_429 if is_429 else self.cfg.pause_seconds)
      self._trip_failures = self._consecutive_failures
      self._paused_until_mono = time.monotonic() + pause
      self._paused_at = datetime.now(timezone.utc).isoformat()
      log.warning(
        "Kalshi circuit OPEN — %s failures (%s); pausing %.0fs",
        self._consecutive_failures,
        "429 rate limit" if is_429 else "errors",
        pause,
      )
    elif self.is_degraded():
      log.warning(
        "Kalshi API degraded — %s/%s failures; blocking new entries",
        self._consecutive_failures,
        self.cfg.warn_threshold,
      )
    self._save()

  def status_dict(self) -> dict[str, Any]:
    paused = self.is_paused()
    degraded = self.is_degraded()
    remaining = round(self._pause_remaining(), 1) if paused else 0.0
    failures_shown = self._trip_failures if paused and self._trip_failures else self._consecutive_failures
    return {
      "enabled": self.cfg.enabled,
      "paused": paused,
      "degraded": degraded,
      "entries_blocked": self.entries_blocked(),
      "throttle_discovery": self.throttle_discovery(),
      "consecutive_failures": failures_shown,
      "live_failure_count": self._consecutive_failures,
      "failure_threshold": self.cfg.failure_threshold,
      "warn_threshold": self.cfg.warn_threshold,
      "pause_seconds": self.cfg.pause_seconds,
      "pause_seconds_429": self.cfg.pause_seconds_429,
      "seconds_until_resume": remaining,
      "last_error": self._last_error,
      "paused_at": self._paused_at,
    }


def circuit_config_from_cfg(cfg: dict[str, Any] | None) -> CircuitConfig:
  raw = ((cfg or {}).get("kalshi") or {}).get("circuit_breaker") or {}
  return CircuitConfig(
    enabled=bool(raw.get("enabled", True)),
    warn_threshold=int(raw.get("warn_threshold", 2)),
    failure_threshold=int(raw.get("failure_threshold", 5)),
    pause_seconds=int(raw.get("pause_seconds", 30)),
    pause_seconds_429=int(raw.get("pause_seconds_429", 15)),
    pause_on_429_after=int(raw.get("pause_on_429_after", 3)),
  )


def init_circuit_breaker(cfg: dict[str, Any], data_dir: Path) -> KalshiCircuitBreaker:
  global _STATE
  state_path = Path(data_dir) / "kalshi_circuit.json"
  _STATE = KalshiCircuitBreaker(circuit_config_from_cfg(cfg), state_path)
  return _STATE


def get_circuit_breaker() -> KalshiCircuitBreaker | None:
  return _STATE
