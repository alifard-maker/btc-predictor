"""Calibration epoch marker — when stats were last reset."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def epoch_path(cfg: dict[str, Any]) -> Path:
  return Path(cfg["paths"]["logs"]) / "stats_epoch.json"


def read_stats_epoch(cfg: dict[str, Any]) -> dict[str, Any] | None:
  path = epoch_path(cfg)
  if not path.exists():
    return None
  try:
    return json.loads(path.read_text())
  except (json.JSONDecodeError, OSError):
    return None


def write_stats_epoch(cfg: dict[str, Any], *, note: str = "") -> dict[str, Any]:
  path = epoch_path(cfg)
  path.parent.mkdir(parents=True, exist_ok=True)
  record = {
    "stats_since": datetime.now(timezone.utc).isoformat(),
    "note": note,
  }
  path.write_text(json.dumps(record, indent=2) + "\n")
  return record
