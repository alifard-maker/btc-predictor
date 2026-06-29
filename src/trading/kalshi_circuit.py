"""Kalshi API circuit breaker — pause bot activity when the API is unreachable."""

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
  failure_threshold: int = 5
  pause_seconds: int = 120


class KalshiCircuitBreaker:
  def __init__(self, cfg: CircuitConfig, state_path: Path):
    self.cfg = cfg
    self.state_path = state_path
    self._consecutive_failures = 0
    self._paused_until_mono: float = 0.0
    self._last_error: str | None = None
    self._paused_at: str | None = None
    self._load()

  def _load(self) -> None:
    if not self.state_path.is_file():
      return
    try:
      raw = json.loads(self.state_path.read_text(encoding="utf-8"))
      self._consecutive_failures = int(raw.get("consecutive_failures") or 0)
      self._last_error = raw.get("last_error")
      self._paused_at = raw.get("paused_at")
      until = raw.get("paused_until_epoch")
      if until is not None:
        self._paused_until_mono = max(0.0, float(until) - time.time())
        if self._paused_until_mono > 0:
          self._paused_until_mono += time.monotonic()
        else:
          self._paused_until_mono = 0.0
    except Exception as e:
      log.warning("Kalshi circuit state load failed: %s", e)

  def _save(self) -> None:
    try:
      self.state_path.parent.mkdir(parents=True, exist_ok=True)
      paused_until_epoch = None
      if self.is_paused():
        paused_until_epoch = time.time() + max(0.0, self._paused_until_mono - time.monotonic())
      payload = {
        "consecutive_failures": self._consecutive_failures,
        "last_error": self._last_error,
        "paused_at": self._paused_at,
        "paused_until_epoch": paused_until_epoch,
        "updated_at": datetime.now(timezone.utc).isoformat(),
      }
      self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
      log.warning("Kalshi circuit state save failed: %s", e)

  def record_success(self) -> None:
    if not self.cfg.enabled:
      return
    self._consecutive_failures = 0
    self._last_error = None
    if self._paused_until_mono and time.monotonic() >= self._paused_until_mono:
      self._paused_until_mono = 0.0
      self._paused_at = None
    self._save()

  def record_failure(self, error: str) -> None:
    if not self.cfg.enabled:
      return
    self._consecutive_failures += 1
    self._last_error = error[:500]
    if self._consecutive_failures >= self.cfg.failure_threshold:
      self._paused_until_mono = time.monotonic() + float(self.cfg.pause_seconds)
      self._paused_at = datetime.now(timezone.utc).isoformat()
      log.warning(
        "Kalshi circuit OPEN — %s consecutive failures; pausing %.0fs",
        self._consecutive_failures,
        self.cfg.pause_seconds,
      )
    self._save()

  def is_paused(self) -> bool:
    if not self.cfg.enabled:
      return False
    if self._paused_until_mono and time.monotonic() < self._paused_until_mono:
      return True
    if self._paused_until_mono and time.monotonic() >= self._paused_until_mono:
      self._paused_until_mono = 0.0
      self._consecutive_failures = 0
      self._paused_at = None
      self._save()
    return False

  def status_dict(self) -> dict[str, Any]:
    paused = self.is_paused()
    remaining = 0.0
    if paused:
      remaining = max(0.0, self._paused_until_mono - time.monotonic())
    return {
      "enabled": self.cfg.enabled,
      "paused": paused,
      "consecutive_failures": self._consecutive_failures,
      "failure_threshold": self.cfg.failure_threshold,
      "pause_seconds": self.cfg.pause_seconds,
      "seconds_until_resume": round(remaining, 1) if paused else 0.0,
      "last_error": self._last_error,
      "paused_at": self._paused_at,
    }


def circuit_config_from_cfg(cfg: dict[str, Any] | None) -> CircuitConfig:
  raw = ((cfg or {}).get("kalshi") or {}).get("circuit_breaker") or {}
  return CircuitConfig(
    enabled=bool(raw.get("enabled", True)),
    failure_threshold=int(raw.get("failure_threshold", 5)),
    pause_seconds=int(raw.get("pause_seconds", 120)),
  )


def init_circuit_breaker(cfg: dict[str, Any], data_dir: Path) -> KalshiCircuitBreaker:
  global _STATE
  state_path = Path(data_dir) / "kalshi_circuit.json"
  _STATE = KalshiCircuitBreaker(circuit_config_from_cfg(cfg), state_path)
  return _STATE


def get_circuit_breaker() -> KalshiCircuitBreaker | None:
  return _STATE
