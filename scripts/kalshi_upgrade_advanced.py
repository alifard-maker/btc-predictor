#!/usr/bin/env python3
"""One-shot Kalshi Advanced API tier upgrade (manual; not run on deploy).

Uses the same KalshiClient auth as the app. Requires KALSHI_KEY_ID plus
KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH (env, .env, or config.yaml).

Kalshi docs: POST /account/api_usage_level/upgrade grants permanent Advanced
when at least 1 of your last 100 Predictions orders was API-created.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.data.kalshi import KalshiClient

LIMITS_PATH = "/account/limits"
UPGRADE_PATH = "/account/api_usage_level/upgrade"
ADVANCED_TIERS = frozenset({
  "advanced",
  "expert",
  "premier",
  "paragon",
  "prime",
  "prestige",
})


def _load_dotenv() -> None:
  """Populate os.environ from project .env when vars are not already set."""
  env_path = ROOT / ".env"
  if not env_path.is_file():
    return
  for raw in env_path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    key, _, value = line.partition("=")
    key = key.strip()
    if not key or key in os.environ:
      continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
      value = value[1:-1]
    os.environ[key] = value


def _format_limits(data: dict[str, Any]) -> str:
  tier = data.get("usage_tier", "?")
  read = data.get("read") or {}
  write = data.get("write") or {}
  grants = data.get("grants") or []
  lines = [
    f"usage_tier: {tier}",
    (
      "read:  "
      f"refill_rate={read.get('refill_rate')} "
      f"bucket_capacity={read.get('bucket_capacity')}"
    ),
    (
      "write: "
      f"refill_rate={write.get('refill_rate')} "
      f"bucket_capacity={write.get('bucket_capacity')}"
    ),
  ]
  if grants:
    lines.append(f"grants ({len(grants)}):")
    for grant in grants:
      if not isinstance(grant, dict):
        continue
      lane = grant.get("exchange_instance", "?")
      level = grant.get("level", "?")
      source = grant.get("source", "?")
      expires = grant.get("expires_ts")
      expiry = "permanent" if expires is None else f"expires_ts={expires}"
      lines.append(f"  - {lane} {level} ({source}, {expiry})")
  else:
    lines.append("grants: (none)")
  return "\n".join(lines)


def _tier_at_least_advanced(tier: str) -> bool:
  return str(tier or "").lower() in ADVANCED_TIERS


def _fetch_limits(client: KalshiClient) -> dict[str, Any]:
  data = client.get(LIMITS_PATH, auth=True)
  if not isinstance(data, dict):
    raise RuntimeError(f"unexpected limits response type: {type(data).__name__}")
  return data


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Inspect Kalshi API usage tier and optionally upgrade to Advanced.",
  )
  parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Show current limits only; do not upgrade",
  )
  parser.add_argument(
    "--upgrade",
    action="store_true",
    help="POST upgrade if below Advanced (needs API-created order in last 100)",
  )
  args = parser.parse_args()

  if not args.dry_run and not args.upgrade:
    parser.error("specify --dry-run and/or --upgrade")

  _load_dotenv()
  cfg = load_config()
  client = KalshiClient(cfg)

  if not client.authenticated:
    print("ERROR: Kalshi credentials not configured.", file=sys.stderr)
    print(
      "Set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY "
      "(or KALSHI_PRIVATE_KEY_PATH) in env or .env.",
      file=sys.stderr,
    )
    return 1

  print(f"Kalshi base URL: {client.base_url}")
  print("Fetching account limits...")
  try:
    limits = _fetch_limits(client)
  except Exception as exc:
    print(f"ERROR: GET {LIMITS_PATH} failed: {exc}", file=sys.stderr)
    return 1

  print("\n--- Current API limits ---")
  print(_format_limits(limits))

  tier = str(limits.get("usage_tier") or "").lower()
  if args.dry_run and not args.upgrade:
    if _tier_at_least_advanced(tier):
      print("\nAlready at Advanced tier or higher. No upgrade needed.")
    else:
      print("\nDry run only. Re-run with --upgrade to request Advanced tier.")
    return 0

  if not args.upgrade:
    return 0

  if _tier_at_least_advanced(tier):
    print("\nAlready at Advanced tier or higher; skipping upgrade POST.")
    return 0

  print(f"\nPOST {UPGRADE_PATH} (empty body)...")
  try:
    client.post(UPGRADE_PATH, auth=True)
    print("Upgrade request accepted (HTTP 2xx).")
  except Exception as exc:
    print(f"ERROR: Upgrade failed: {exc}", file=sys.stderr)
    err = str(exc)
    if "403" in err:
      print(
        "Hint: Kalshi requires at least 1 API-created order in your "
        "last 100 Predictions orders.",
        file=sys.stderr,
      )
    return 1

  print("\nRe-fetching limits to verify...")
  try:
    limits_after = _fetch_limits(client)
  except Exception as exc:
    print(f"WARNING: verification GET failed: {exc}", file=sys.stderr)
    return 1

  print("\n--- API limits after upgrade ---")
  print(_format_limits(limits_after))
  tier_after = str(limits_after.get("usage_tier") or "").lower()
  if _tier_at_least_advanced(tier_after):
    print("\nSuccess: usage tier is now Advanced or higher.")
    return 0

  print(
    f"\nWARNING: usage_tier is still {tier_after!r}; "
    "check grants or Kalshi account criteria.",
    file=sys.stderr,
  )
  return 2


if __name__ == "__main__":
  raise SystemExit(main())
