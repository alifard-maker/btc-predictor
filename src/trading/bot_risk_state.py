"""Shared bot risk state: rolling daily realized P&L cap across all paper/live bots."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_COORDINATOR: "BotRiskCoordinator | None" = None
_REGISTERED_STORES: list[Any] = []


def register_bot_stores(stores: list[Any]) -> None:
  global _REGISTERED_STORES
  _REGISTERED_STORES = list(stores)


def get_registered_bot_stores() -> list[Any]:
  return list(_REGISTERED_STORES)


@dataclass
class DailyLossConfig:
  enabled: bool = True
  cap_usd: float = 50.0
  timezone: str = "America/New_York"


class BotRiskCoordinator:
  def __init__(self, data_dir: Path, cfg: DailyLossConfig):
    self.data_dir = Path(data_dir)
    self.cfg = cfg
    self.state_path = self.data_dir / "bot_daily_risk.json"
    self._date_key = ""
    self._realized_pnl_usd = 0.0
    self._cap_active = False
    self._load()

  def _tz(self) -> ZoneInfo:
    try:
      return ZoneInfo(self.cfg.timezone)
    except Exception:
      return ZoneInfo("America/New_York")

  def _today_key(self) -> str:
    return datetime.now(self._tz()).strftime("%Y-%m-%d")

  def _load(self) -> None:
    if not self.state_path.is_file():
      self._roll_day_if_needed()
      return
    try:
      raw = json.loads(self.state_path.read_text(encoding="utf-8"))
      self._date_key = str(raw.get("date_key") or "")
      self._realized_pnl_usd = float(raw.get("realized_pnl_usd") or 0)
      self._cap_active = bool(raw.get("cap_active", False))
      self._roll_day_if_needed()
    except Exception as e:
      log.warning("Daily risk state load failed: %s", e)
      self._roll_day_if_needed()

  def _save(self) -> None:
    try:
      self.state_path.parent.mkdir(parents=True, exist_ok=True)
      self.state_path.write_text(
        json.dumps(
          {
            "date_key": self._date_key,
            "realized_pnl_usd": round(self._realized_pnl_usd, 2),
            "cap_active": self._cap_active,
            "cap_usd": self.cfg.cap_usd,
            "timezone": self.cfg.timezone,
          },
          indent=2,
        ),
        encoding="utf-8",
      )
    except Exception as e:
      log.warning("Daily risk state save failed: %s", e)

  def _roll_day_if_needed(self) -> bool:
    today = self._today_key()
    if self._date_key == today:
      return False
    self._date_key = today
    self._realized_pnl_usd = 0.0
    self._cap_active = False
    self._save()
    return True

  def refresh_day(self) -> bool:
    """Return True if the calendar day rolled (cap cleared)."""
    return self._roll_day_if_needed()

  def record_exit_pnl(self, pnl_usd: float) -> bool:
    """Add realized P&L; return True if daily cap was just hit."""
    if not self.cfg.enabled or self.cfg.cap_usd <= 0:
      return False
    self._roll_day_if_needed()
    self._realized_pnl_usd = round(self._realized_pnl_usd + float(pnl_usd), 2)
    just_hit = False
    if self._realized_pnl_usd <= -abs(self.cfg.cap_usd):
      if not self._cap_active:
        just_hit = True
        log.warning(
          "Daily loss cap hit: %.2f <= -%.2f (%s)",
          self._realized_pnl_usd,
          self.cfg.cap_usd,
          self._date_key,
        )
      self._cap_active = True
    self._save()
    return just_hit

  def is_cap_active(self) -> bool:
    if not self.cfg.enabled or self.cfg.cap_usd <= 0:
      return False
    self._roll_day_if_needed()
    return self._cap_active

  def status_dict(self) -> dict[str, Any]:
    self._roll_day_if_needed()
    cap = float(self.cfg.cap_usd)
    remaining = round(cap + self._realized_pnl_usd, 2) if cap > 0 else None
    return {
      "enabled": self.cfg.enabled,
      "date_key": self._date_key,
      "timezone": self.cfg.timezone,
      "cap_usd": cap,
      "realized_pnl_usd": round(self._realized_pnl_usd, 2),
      "remaining_usd": remaining,
      "cap_active": self._cap_active,
    }


def daily_loss_config_from_cfg(cfg: dict[str, Any] | None) -> DailyLossConfig:
  raw = (cfg or {}).get("bot_risk") or {}
  return DailyLossConfig(
    enabled=bool(raw.get("daily_loss_cap_enabled", True)),
    cap_usd=float(raw.get("daily_loss_cap_usd", 50.0)),
    timezone=str(raw.get("daily_loss_timezone", "America/New_York")),
  )


def init_bot_risk_coordinator(cfg: dict[str, Any], data_dir: Path) -> BotRiskCoordinator:
  global _COORDINATOR
  _COORDINATOR = BotRiskCoordinator(Path(data_dir), daily_loss_config_from_cfg(cfg))
  return _COORDINATOR


def get_bot_risk_coordinator() -> BotRiskCoordinator | None:
  return _COORDINATOR
