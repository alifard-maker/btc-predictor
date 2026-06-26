"""Persist slot post-mortems for accuracy review."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.calibration.postmortem import build_postmortem


class PostmortemLogger:
  def __init__(self, cfg: dict[str, Any]):
    logs_dir = Path(cfg["paths"]["logs"])
    logs_dir.mkdir(parents=True, exist_ok=True)
    self.path = logs_dir / "postmortems.jsonl"
    self.tz = cfg.get("timezone", "America/New_York")

  def log_row(self, row: dict[str, Any]) -> dict[str, Any]:
    pm = build_postmortem(row, tz_name=self.tz)
    record = {
      **pm,
      "timestamp": row.get("timestamp"),
      "signal": row.get("signal"),
      "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(self.path, "a") as f:
      f.write(json.dumps(record, default=str) + "\n")
    return record

  def load_recent(self, limit: int = 20) -> list[dict[str, Any]]:
    if not self.path.exists():
      return []
    lines = self.path.read_text().strip().splitlines()
    out = []
    for line in lines[-limit:]:
      try:
        out.append(json.loads(line))
      except json.JSONDecodeError:
        continue
    return list(reversed(out))
