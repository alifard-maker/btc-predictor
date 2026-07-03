"""Per-bot daily realized P&L cap with optional same-day override."""

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


def bot_risk_key(kind: str, asset: str) -> str:
  return f"{kind.lower()}:{asset.lower()}"


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


def _empty_bot_row() -> dict[str, Any]:
  return {
    "realized_pnl_usd": 0.0,
    "cap_active": False,
    "cap_override": False,
  }


class BotRiskCoordinator:
  def __init__(self, data_dir: Path, cfg: DailyLossConfig):
    self.data_dir = Path(data_dir)
    self.cfg = cfg
    self.state_path = self.data_dir / "bot_daily_risk.json"
    self._date_key = ""
    self._bots: dict[str, dict[str, Any]] = {}
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
      bots = raw.get("bots")
      if isinstance(bots, dict):
        self._bots = {str(k): dict(v) for k, v in bots.items()}
      else:
        self._bots = {}
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
            "cap_usd": self.cfg.cap_usd,
            "timezone": self.cfg.timezone,
            "bots": self._bots,
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
    self._bots = {}
    self._save()
    return True

  def refresh_day(self) -> bool:
    return self._roll_day_if_needed()

  def _row(self, bot_key: str) -> dict[str, Any]:
    self._roll_day_if_needed()
    row = self._bots.get(bot_key)
    if row is None:
      row = _empty_bot_row()
      self._bots[bot_key] = row
    return row

  def record_exit_pnl(self, bot_key: str, pnl_usd: float) -> bool:
    """Add realized P&L for one bot; return True if that bot's cap was just hit."""
    if not self.cfg.enabled or self.cfg.cap_usd <= 0:
      return False
    row = self._row(bot_key)
    if row.get("cap_override"):
      row["realized_pnl_usd"] = round(float(row.get("realized_pnl_usd") or 0) + float(pnl_usd), 2)
      self._save()
      return False
    row["realized_pnl_usd"] = round(float(row.get("realized_pnl_usd") or 0) + float(pnl_usd), 2)
    just_hit = False
    if row["realized_pnl_usd"] <= -abs(self.cfg.cap_usd):
      if not row.get("cap_active"):
        just_hit = True
        log.warning(
          "Daily loss cap hit for %s: %.2f <= -%.2f (%s)",
          bot_key,
          row["realized_pnl_usd"],
          self.cfg.cap_usd,
          self._date_key,
        )
      row["cap_active"] = True
    self._save()
    return just_hit

  def is_cap_active(self, bot_key: str) -> bool:
    if not self.cfg.enabled or self.cfg.cap_usd <= 0:
      return False
    row = self._row(bot_key)
    if row.get("cap_override"):
      return False
    return bool(row.get("cap_active"))

  def override_cap(self, bot_key: str) -> None:
    """Allow entries for this bot for the rest of the calendar day."""
    row = self._row(bot_key)
    row["cap_override"] = True
    row["cap_active"] = False
    self._save()
    log.info("Daily loss cap overridden for %s (%s)", bot_key, self._date_key)

  def reset_bot_daily_pnl(self, bot_key: str) -> None:
    """Clear today's realized P&L and cap flags for one bot (e.g. fresh start)."""
    self._roll_day_if_needed()
    self._bots[bot_key] = _empty_bot_row()
    self._save()
    log.info("Daily risk reset for %s (%s)", bot_key, self._date_key)

  def sync_bot_realized_pnl(self, bot_key: str, realized_pnl_usd: float) -> dict[str, Any]:
    """Set today's realized P&L from trade-log reconciliation (not incremental)."""
    row = self._row(bot_key)
    realized = round(float(realized_pnl_usd), 2)
    row["realized_pnl_usd"] = realized
    if row.get("cap_override"):
      self._save()
      return self.status_for_bot(bot_key)
    cap = float(self.cfg.cap_usd)
    capped = bool(self.cfg.enabled and cap > 0 and realized <= -abs(cap))
    row["cap_active"] = capped
    self._save()
    return self.status_for_bot(bot_key)

  def status_for_bot(self, bot_key: str) -> dict[str, Any]:
    self._roll_day_if_needed()
    cap = float(self.cfg.cap_usd)
    row = self._row(bot_key)
    realized = round(float(row.get("realized_pnl_usd") or 0), 2)
    remaining = round(cap + realized, 2) if cap > 0 else None
    capped = bool(row.get("cap_active")) and not bool(row.get("cap_override"))
    return {
      "bot_key": bot_key,
      "enabled": self.cfg.enabled,
      "date_key": self._date_key,
      "timezone": self.cfg.timezone,
      "cap_usd": cap,
      "realized_pnl_usd": realized,
      "remaining_usd": remaining,
      "cap_active": capped,
      "cap_override": bool(row.get("cap_override")),
    }

  def status_dict(self) -> dict[str, Any]:
    self._roll_day_if_needed()
    cap = float(self.cfg.cap_usd)
    bots_out: dict[str, Any] = {}
    for key in sorted(self._bots.keys()):
      bots_out[key] = self.status_for_bot(key)
    any_active = any(st.get("cap_active") for st in bots_out.values())
    return {
      "enabled": self.cfg.enabled,
      "date_key": self._date_key,
      "timezone": self.cfg.timezone,
      "cap_usd": cap,
      "per_bot": True,
      "bots": bots_out,
      "cap_active": any_active,
      "any_cap_active": any_active,
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


def coordinator_for_data_dir(
  data_dir: Path | str,
  cfg: dict[str, Any] | None = None,
) -> BotRiskCoordinator:
  """Use the process singleton when its data_dir matches; else a standalone instance."""
  root = Path(data_dir).resolve()
  coord = _COORDINATOR
  if coord is not None and Path(coord.data_dir).resolve() == root:
    return coord
  return BotRiskCoordinator(root, daily_loss_config_from_cfg(cfg))


def reload_bot_risk_coordinator() -> BotRiskCoordinator | None:
  """Re-read bot_daily_risk.json into the in-memory singleton (e.g. after external sync)."""
  if _COORDINATOR is None:
    return None
  _COORDINATOR._load()
  return _COORDINATOR
