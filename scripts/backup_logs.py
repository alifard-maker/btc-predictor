#!/usr/bin/env python3
"""Run a full log backup (paper + live) from the CLI."""

from __future__ import annotations

import json
import sys

from src.backup.logs_backup import run_full_backup
from src.config import load_config


def main() -> int:
  cfg = load_config()
  result = run_full_backup(cfg, reason="cli")
  print(json.dumps(result, indent=2))
  return 0 if result.get("paper") is not None else 1


if __name__ == "__main__":
  raise SystemExit(main())
