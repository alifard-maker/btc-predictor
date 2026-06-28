"""Tests for live-mode password gate."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.trading.live_mode_auth import require_live_password


def test_paper_to_live_requires_password():
  with pytest.raises(HTTPException) as exc:
    require_live_password(
      current_mode="paper",
      new_mode="live",
      body={},
      password="Ducati1098R!",
    )
  assert exc.value.status_code == 403
  assert exc.value.detail == "Wrong Password - Denied Access"


def test_paper_to_live_accepts_correct_password():
  require_live_password(
    current_mode="paper",
    new_mode="live",
    body={"live_password": "Ducati1098R!"},
    password="Ducati1098R!",
  )


def test_staying_live_skips_password():
  require_live_password(
    current_mode="live",
    new_mode="live",
    body={},
    password="Ducati1098R!",
  )


def test_switch_to_paper_skips_password():
  require_live_password(
    current_mode="live",
    new_mode="paper",
    body={},
    password="Ducati1098R!",
  )
