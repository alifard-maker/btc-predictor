"""BTC prediction assistant — probabilistic signals, calibration, paper trading."""

from pathlib import Path

def _read_version() -> str:
  p = Path(__file__).resolve().parent.parent / "VERSION"
  if p.exists():
    return p.read_text().strip()
  return "dev"

__version__ = _read_version()
