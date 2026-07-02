"""Gate switching bot mode from paper to live behind a shared password."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def live_bet_password(cfg: dict[str, Any]) -> str:
  return str(cfg.get("live_bet_password") or "")


def require_live_password(
  *,
  current_mode: str,
  new_mode: str,
  body: dict[str, Any],
  password: str,
) -> None:
  """Reject mode switches involving live unless body.live_password matches.

  This prevents accidental UI cross-talk from flipping a live bot back to paper.
  """
  if current_mode == new_mode:
    return
  if current_mode != "live" and new_mode != "live":
    return
  supplied = str(body.get("live_password") or "")
  if supplied != password:
    raise HTTPException(403, "Wrong Password - Denied Access")
