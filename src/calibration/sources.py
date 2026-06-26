"""Price source labels for prediction logging and Kalshi-consistent calibration."""

from __future__ import annotations

# Kalshi KXBTC15M BRTI at slot open (floor_strike).
KALSHI_REF_SOURCE = "kalshi_brti_target"

# Kalshi settlement BRTI at slot close (expiration_value).
KALSHI_EXIT_SOURCE = "kalshi_brti_expiration"

# Pre-Kalshi rows before backfill.
LEGACY_EXCHANGE_REF = "exchange_legacy"
LEGACY_EXCHANGE_EXIT = "exchange_15m_close"

KALSHI_REF_SOURCES = frozenset({KALSHI_REF_SOURCE, "kalshi_backfill"})
KALSHI_EXIT_SOURCES = frozenset({KALSHI_EXIT_SOURCE})


def is_kalshi_consistent(reference_source: str | None, exit_source: str | None) -> bool:
  if not reference_source or not exit_source:
    return False
  return reference_source in KALSHI_REF_SOURCES and exit_source in KALSHI_EXIT_SOURCES
