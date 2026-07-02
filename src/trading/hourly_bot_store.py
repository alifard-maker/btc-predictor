"""Persist hourly auto-bet bot settings, open positions, and trade log (per asset)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class HourlyBotSettings:
  enabled: bool = False
  mode: str = "paper"  # paper | live
  max_spend_per_hour_usd: float = 10.0
  allow_strong: bool = False
  allow_actionable: bool = False
  continuous: bool = True
  reentry_cooldown_seconds: int = 120
  take_profit_enabled: bool = True
  take_profit_mode: str = "hybrid"  # fixed | adaptive | trailing | hybrid
  take_profit_pct: float = 0.25
  take_profit_usd: float = 0.0
  trail_arm_profit_pct: float = 0.08
  trail_giveback_pct: float = 0.35
  trail_arm_profit_usd: float = 0.50
  trail_giveback_usd: float = 0.0
  min_take_profit_pct: float = 0.10
  max_take_profit_pct: float = 0.40
  min_hold_seconds: int = 120
  profit_exit_cooldown_seconds: int = 60
  auto_stop_on_budget_exhausted: bool = True
  auto_stopped: bool = False
  auto_stop_reason: str | None = None
  paper_auto_refill: bool = True
  use_accumulated_profit: bool = False
  profit_use_pct: float = 100.0
  aggressive_entries: bool = False

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)

  @classmethod
  def from_dict(cls, raw: dict[str, Any] | None) -> HourlyBotSettings:
    if not raw:
      return cls()
    return cls(
      enabled=bool(raw.get("enabled", False)),
      mode=str(raw.get("mode", "paper")),
      max_spend_per_hour_usd=float(raw.get("max_spend_per_hour_usd", 10.0)),
      allow_strong=bool(raw.get("allow_strong", False)),
      allow_actionable=bool(raw.get("allow_actionable", False)),
      continuous=bool(raw.get("continuous", True)),
      reentry_cooldown_seconds=int(raw.get("reentry_cooldown_seconds", 120)),
      take_profit_enabled=bool(raw.get("take_profit_enabled", True)),
      take_profit_mode=str(raw.get("take_profit_mode", "hybrid")),
      take_profit_pct=float(raw.get("take_profit_pct", 0.25)),
      take_profit_usd=float(raw.get("take_profit_usd", 0.0)),
      trail_arm_profit_pct=float(raw.get("trail_arm_profit_pct", 0.08)),
      trail_giveback_pct=float(raw.get("trail_giveback_pct", 0.35)),
      trail_arm_profit_usd=float(raw.get("trail_arm_profit_usd", 0.50)),
      trail_giveback_usd=float(raw.get("trail_giveback_usd", 0.0)),
      min_take_profit_pct=float(raw.get("min_take_profit_pct", 0.10)),
      max_take_profit_pct=float(raw.get("max_take_profit_pct", 0.40)),
      min_hold_seconds=int(raw.get("min_hold_seconds", 120)),
      profit_exit_cooldown_seconds=int(raw.get("profit_exit_cooldown_seconds", 60)),
      auto_stop_on_budget_exhausted=bool(raw.get("auto_stop_on_budget_exhausted", True)),
      auto_stopped=bool(raw.get("auto_stopped", False)),
      auto_stop_reason=raw.get("auto_stop_reason"),
      paper_auto_refill=bool(raw.get("paper_auto_refill", True)),
      use_accumulated_profit=bool(raw.get("use_accumulated_profit", False)),
      profit_use_pct=float(raw.get("profit_use_pct", 100.0)),
      aggressive_entries=bool(raw.get("aggressive_entries", False)),
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_trades (
  id TEXT PRIMARY KEY,
  event_ticker TEXT NOT NULL,
  trigger TEXT NOT NULL,
  action TEXT NOT NULL DEFAULT 'enter',
  mode TEXT NOT NULL,
  market_ticker TEXT,
  side TEXT,
  contracts INTEGER,
  price_cents INTEGER,
  entry_price_cents INTEGER,
  exit_price_cents INTEGER,
  cost_usd REAL,
  pnl_usd REAL,
  signal TEXT,
  label TEXT,
  actionable_headline TEXT,
  status TEXT NOT NULL,
  detail TEXT,
  kalshi_order_id TEXT,
  position_id TEXT,
  entry_bid_cents INTEGER,
  entry_ask_cents INTEGER,
  entry_spread_cents INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_trades_event ON bot_trades(event_ticker, created_at);
CREATE TABLE IF NOT EXISTS bot_positions (
  id TEXT PRIMARY KEY,
  event_ticker TEXT NOT NULL,
  market_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  contracts INTEGER NOT NULL,
  entry_price_cents INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  signal TEXT,
  label TEXT,
  entry_edge REAL,
  reference_price REAL,
  opened_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_bot_positions_open ON bot_positions(event_ticker, status);
CREATE TABLE IF NOT EXISTS bot_cooldowns (
  event_ticker TEXT NOT NULL,
  market_ticker TEXT NOT NULL,
  exited_at TEXT NOT NULL,
  PRIMARY KEY (event_ticker, market_ticker)
);
"""


class HourlyBotStore:
  def __init__(self, db_path: Path):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    self._last_period_key: str | None = None
    self._last_skip_reason: str | None = None
    self._position_peaks: dict[str, dict[str, float]] = {}
    self._init_db()

  def set_last_skip_reason(self, reason: str | None) -> None:
    self._last_skip_reason = reason

  def last_skip_reason(self) -> str | None:
    return self._last_skip_reason

  def record_cycle(self, *, active: bool) -> None:
    from src.trading.bot_runtime import record_bot_cycle

    with self._connect() as conn:
      record_bot_cycle(conn, active=active)

  def get_runtime(self) -> dict[str, Any]:
    from src.trading.bot_runtime import bot_runtime_dict

    with self._connect() as conn:
      return bot_runtime_dict(conn)

  def sync_period(self, event_ticker: str, settings: HourlyBotSettings) -> tuple[HourlyBotSettings, str | None]:
    """Track hour changes; clear auto_stop on rollover (paper + live)."""
    prev = self._last_period_key
    self._last_period_key = event_ticker
    rolled = bool(prev and prev != event_ticker)
    if rolled and settings.auto_stopped:
      reason = settings.auto_stop_reason
      if reason in (None, "", "budget_exhausted"):
        updated = HourlyBotSettings(
          **{**settings.to_dict(), "auto_stopped": False, "auto_stop_reason": None}
        )
        self.save_settings(updated)
        self.set_last_skip_reason(None)
        return updated, prev
    return settings, prev if rolled else None

  def _connect(self) -> sqlite3.Connection:
    conn = sqlite3.connect(self.db_path)
    conn.row_factory = sqlite3.Row
    return conn

  def _migrate(self, conn: sqlite3.Connection) -> None:
    from src.trading.bot_runtime import migrate_bot_runtime
    from src.trading.paper_bankroll import migrate_paper_state

    migrate_paper_state(conn)
    migrate_bot_runtime(conn)
    from src.trading.bot_tuning_store import migrate_adaptive_calibration, migrate_auto_tuning

    migrate_auto_tuning(conn)
    migrate_adaptive_calibration(conn)
    from src.trading.bot_cheap_leg_cooldown import migrate_cheap_leg_cut_cooldowns

    migrate_cheap_leg_cut_cooldowns(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_trades)").fetchall()}
    if cols and "action" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN action TEXT NOT NULL DEFAULT 'enter'")
    if cols and "pnl_usd" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN pnl_usd REAL")
    if cols and "position_id" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN position_id TEXT")
    if cols and "entry_price_cents" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN entry_price_cents INTEGER")
    if cols and "exit_price_cents" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN exit_price_cents INTEGER")
    cd_cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_cooldowns)").fetchall()}
    if cd_cols and "cooldown_seconds" not in cd_cols:
      conn.execute("ALTER TABLE bot_cooldowns ADD COLUMN cooldown_seconds INTEGER")
    pos_cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_positions)").fetchall()}
    if pos_cols and "last_mark_cents" not in pos_cols:
      conn.execute("ALTER TABLE bot_positions ADD COLUMN last_mark_cents INTEGER")
    for col in ("entry_bid_cents", "entry_ask_cents", "entry_spread_cents"):
      if cols and col not in cols:
        conn.execute(f"ALTER TABLE bot_trades ADD COLUMN {col} INTEGER")
    if cols and "entry_settings_json" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN entry_settings_json TEXT")
    if cols and "exit_context_json" not in cols:
      conn.execute("ALTER TABLE bot_trades ADD COLUMN exit_context_json TEXT")
    for col in ("stop_order_id", "take_profit_order_id"):
      if pos_cols and col not in pos_cols:
        conn.execute(f"ALTER TABLE bot_positions ADD COLUMN {col} TEXT")
    for col, typ in (
      ("contract_type", "TEXT"),
      ("strike_type", "TEXT"),
      ("floor_strike", "REAL"),
      ("cap_strike", "REAL"),
    ):
      if pos_cols and col not in pos_cols:
        conn.execute(f"ALTER TABLE bot_positions ADD COLUMN {col} {typ}")
    if pos_cols and "mode" not in pos_cols:
      conn.execute("ALTER TABLE bot_positions ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper'")
      from src.trading.bot_position_mode import backfill_position_modes

      backfill_position_modes(conn)
    if pos_cols and "contracts_fp" not in pos_cols:
      conn.execute("ALTER TABLE bot_positions ADD COLUMN contracts_fp REAL")
    if pos_cols and "entry_source" not in pos_cols:
      conn.execute("ALTER TABLE bot_positions ADD COLUMN entry_source TEXT")

  def _init_db(self) -> None:
    with self._connect() as conn:
      conn.executescript(_SCHEMA)
      self._migrate(conn)
      row = conn.execute("SELECT json FROM bot_settings WHERE id = 1").fetchone()
      if row is None:
        conn.execute(
          "INSERT INTO bot_settings (id, json) VALUES (1, ?)",
          (json.dumps(HourlyBotSettings().to_dict()),),
        )

  def get_auto_tuning(self) -> dict[str, Any]:
    from src.trading.bot_tuning_store import get_auto_tuning

    with self._connect() as conn:
      return get_auto_tuning(conn)

  def save_auto_tuning(self, tuning: dict[str, Any]) -> dict[str, Any]:
    from src.trading.bot_tuning_store import save_auto_tuning

    with self._connect() as conn:
      return save_auto_tuning(conn, tuning)

  def get_adaptive_calibration(self) -> dict[str, Any]:
    from src.trading.bot_tuning_store import get_adaptive_calibration

    with self._connect() as conn:
      return get_adaptive_calibration(conn)

  def save_adaptive_calibration(self, state: dict[str, Any]) -> dict[str, Any]:
    from src.trading.bot_tuning_store import save_adaptive_calibration

    with self._connect() as conn:
      return save_adaptive_calibration(conn, state)

  def get_settings(self) -> HourlyBotSettings:
    with self._connect() as conn:
      row = conn.execute("SELECT json FROM bot_settings WHERE id = 1").fetchone()
    return HourlyBotSettings.from_dict(json.loads(row["json"]) if row else {})

  def save_settings(
    self,
    settings: HourlyBotSettings,
    *,
    source: str = "internal",
    cfg: dict[str, Any] | None = None,
  ) -> HourlyBotSettings:
    old = self.get_settings()
    with self._connect() as conn:
      conn.execute(
        "UPDATE bot_settings SET json = ? WHERE id = 1",
        (json.dumps(settings.to_dict()),),
      )
    if old.to_dict() != settings.to_dict():
      try:
        from src.backup.logs_backup import on_settings_saved
        from src.trading.bot_entry_settings import infer_store_meta

        asset, bot_type = infer_store_meta(self.db_path)
        on_settings_saved(
          cfg,
          asset=asset,
          bot_type=bot_type,
          old_settings=old.to_dict(),
          new_settings=settings.to_dict(),
          source=source,
        )
      except Exception:
        pass
    return settings

  def open_positions(self, event_ticker: str) -> list[dict[str, Any]]:
    with self._connect() as conn:
      rows = conn.execute(
        "SELECT * FROM bot_positions WHERE event_ticker = ? AND status = 'open' ORDER BY opened_at",
        (event_ticker,),
      ).fetchall()
    return [dict(r) for r in rows]

  def open_exposure_usd(self, event_ticker: str, mode: str | None = None) -> float:
    positions = self.open_positions(event_ticker)
    if mode:
      from src.trading.bot_position_mode import normalize_position_mode

      want = normalize_position_mode(mode)
      positions = [p for p in positions if normalize_position_mode(p.get("mode")) == want]
    return round(sum(float(p.get("cost_usd") or 0) for p in positions), 2)

  def open_exposure_by_mode_usd(self, event_ticker: str) -> dict[str, float]:
    from src.trading.bot_position_mode import exposure_by_mode

    paper, live, total = exposure_by_mode(self.open_positions(event_ticker))
    return {
      "paper": paper,
      "live": live,
      "total": total,
    }

  def realized_pnl_usd(self, event_ticker: str) -> float:
    """Sum of filled exit P&L for this hour (wins add, losses subtract from bankroll)."""
    return self._realized_pnl_usd(event_ticker)

  def _all_paper_realized_pnl(self) -> float:
    with self._connect() as conn:
      exits = conn.execute(
        """
        SELECT pnl_usd, entry_price_cents, exit_price_cents, contracts, side
        FROM bot_trades
        WHERE action = 'exit' AND status = 'filled' AND mode = 'paper'
        """,
      ).fetchall()
    total = 0.0
    for r in exits:
      row = dict(r)
      pnl = row.get("pnl_usd")
      if pnl is None:
        pnl = self._exit_pnl_from_prices(row)
      total += float(pnl or 0)
    return round(total, 2)

  def ensure_paper_state(self, default_cap: float):
    from src.trading.paper_bankroll import ensure_paper_state

    with self._connect() as conn:
      return ensure_paper_state(
        conn,
        default_cap,
        backfill_pnl_fn=self._all_paper_realized_pnl,
      )

  def get_paper_state_dict(self, default_cap: float) -> dict[str, Any]:
    return self.ensure_paper_state(default_cap).to_dict()

  def reset_paper_bankroll(self, max_cap: float) -> dict[str, Any]:
    from src.trading.paper_bankroll import reset_paper_bankroll

    with self._connect() as conn:
      state = reset_paper_bankroll(conn, max_cap)
    settings = self.get_settings()
    if settings.auto_stopped:
      self.save_settings(HourlyBotSettings(**{**settings.to_dict(), "auto_stopped": False}))
    self.set_last_skip_reason(None)
    return state.to_dict()

  def sync_paper_cap_on_max_increase(self, old_cap: float, new_cap: float) -> dict[str, Any] | None:
    from src.trading.paper_bankroll import sync_paper_cap_on_max_increase

    with self._connect() as conn:
      state = sync_paper_cap_on_max_increase(conn, old_cap, new_cap)
    return state.to_dict() if state else None

  def fresh_start_paper(self, max_cap: float, *, preserve_settings: bool = True) -> dict[str, Any]:
    """Clear trade log, positions, cooldowns, tuning; reset paper bankroll."""
    from src.trading.bot_fresh_start import fresh_start_paper_bot

    settings = self.get_settings()
    with self._connect() as conn:
      paper = fresh_start_paper_bot(conn, max_cap)
    self._position_peaks.clear()
    self._last_period_key = None
    if preserve_settings:
      updated = {
        **settings.to_dict(),
        "max_spend_per_hour_usd": float(max_cap),
        "auto_stopped": False,
        "auto_stop_reason": None,
      }
      self.save_settings(HourlyBotSettings.from_dict(updated))
    else:
      self.save_settings(HourlyBotSettings(max_spend_per_hour_usd=float(max_cap)))
    self.set_last_skip_reason(None)
    return paper

  def fresh_start_live(self, *, preserve_settings: bool = True) -> None:
    """Clear trade log and open positions; keep live settings and Kalshi account untouched."""
    from src.trading.bot_fresh_start import fresh_start_live_bot

    settings = self.get_settings()
    with self._connect() as conn:
      fresh_start_live_bot(conn)
    self._position_peaks.clear()
    self._last_period_key = None
    if preserve_settings:
      updated = {
        **settings.to_dict(),
        "auto_stopped": False,
        "auto_stop_reason": None,
      }
      self.save_settings(HourlyBotSettings.from_dict(updated))
    else:
      self.save_settings(HourlyBotSettings())
    self.set_last_skip_reason(None)

  def clear_history(self, max_cap: float, *, mode: str) -> dict[str, Any] | None:
    """Clear bot history; paper mode also resets bankroll to max_cap."""
    if mode == "paper":
      return self.fresh_start_paper(max_cap, preserve_settings=True)
    self.fresh_start_live(preserve_settings=True)
    return None

  def refill_paper_bankroll(self, max_cap: float) -> dict[str, Any]:
    from src.trading.paper_bankroll import refill_paper_bankroll

    with self._connect() as conn:
      state = refill_paper_bankroll(conn, max_cap)
    settings = self.get_settings()
    if settings.auto_stopped:
      self.save_settings(HourlyBotSettings(**{**settings.to_dict(), "auto_stopped": False}))
    self.set_last_skip_reason(None)
    return state.to_dict()

  def _apply_paper_exit_pnl(self, pnl: float, default_cap: float) -> None:
    from src.trading.paper_bankroll import apply_paper_exit_pnl, get_paper_state

    with self._connect() as conn:
      if get_paper_state(conn) is None:
        self.ensure_paper_state(default_cap)
      else:
        apply_paper_exit_pnl(conn, pnl, default_cap)

  def hour_bankroll_usd(
    self,
    event_ticker: str,
    max_hourly: float,
    settings: HourlyBotSettings | None = None,
  ) -> float:
    """Deployable capital for new entries this interval."""
    from src.trading.bot_budget import deploy_bankroll_usd

    settings = settings or self.get_settings()
    if settings.mode == "paper":
      paper = self.ensure_paper_state(max_hourly).paper_bankroll_usd
      return deploy_bankroll_usd(
        mode="paper",
        use_accumulated_profit=settings.use_accumulated_profit,
        profit_use_pct=settings.profit_use_pct,
        max_cap=max_hourly,
        paper_bankroll_usd=paper,
        interval_realized_pnl_usd=0.0,
      )
    return deploy_bankroll_usd(
      mode="live",
      use_accumulated_profit=settings.use_accumulated_profit,
      profit_use_pct=settings.profit_use_pct,
      max_cap=max_hourly,
      paper_bankroll_usd=0.0,
      interval_realized_pnl_usd=self.realized_pnl_usd(event_ticker),
    )

  def _interval_total_entered_usd(self, event_ticker: str) -> float:
    return self.hour_interval_summary(event_ticker)["total_entered_usd"]

  def remaining_budget_usd(
    self,
    event_ticker: str,
    max_hourly: float,
    settings: HourlyBotSettings | None = None,
  ) -> float:
    """Room for new entries after open exposure and optional interval cap."""
    from src.trading.bot_budget import remaining_budget_usd

    settings = settings or self.get_settings()
    paper = self.ensure_paper_state(max_hourly).paper_bankroll_usd if settings.mode == "paper" else 0.0
    return remaining_budget_usd(
      settings=settings,
      max_cap=max_hourly,
      paper_bankroll_usd=paper,
      interval_realized_pnl_usd=self.realized_pnl_usd(event_ticker),
      open_exposure_usd=self.open_exposure_usd(event_ticker, mode=settings.mode),
      interval_total_entered_usd=self._interval_total_entered_usd(event_ticker),
    )

  def record_exit_cooldown(
    self,
    event_ticker: str,
    market_ticker: str,
    *,
    exited_at: str | None = None,
    cooldown_seconds: int | None = None,
  ) -> None:
    now = exited_at or datetime.now(timezone.utc).isoformat()
    with self._connect() as conn:
      conn.execute(
        """
        INSERT INTO bot_cooldowns (event_ticker, market_ticker, exited_at, cooldown_seconds)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(event_ticker, market_ticker) DO UPDATE SET
          exited_at = excluded.exited_at,
          cooldown_seconds = excluded.cooldown_seconds
        """,
        (event_ticker, market_ticker, now, cooldown_seconds),
      )

  def is_in_cooldown(self, event_ticker: str, market_ticker: str, cooldown_seconds: int) -> bool:
    with self._connect() as conn:
      row = conn.execute(
        "SELECT exited_at, cooldown_seconds FROM bot_cooldowns WHERE event_ticker = ? AND market_ticker = ?",
        (event_ticker, market_ticker),
      ).fetchone()
    if not row:
      return False
    effective = row["cooldown_seconds"]
    if effective is None:
      effective = cooldown_seconds
    if int(effective) <= 0:
      return False
    exited_at = datetime.fromisoformat(str(row["exited_at"]).replace("Z", "+00:00"))
    if exited_at.tzinfo is None:
      exited_at = exited_at.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - exited_at).total_seconds()
    return elapsed < float(effective)

  def record_cheap_leg_cut_cooldown(
    self,
    event_ticker: str,
    *,
    label: str | None,
    market_ticker: str,
    cooldown_seconds: int,
    exited_at: str | None = None,
  ) -> None:
    from src.trading.bot_cheap_leg_cooldown import record_cheap_leg_cut_cooldown

    with self._connect() as conn:
      record_cheap_leg_cut_cooldown(
        conn,
        event_ticker,
        label=label,
        market_ticker=market_ticker,
        cooldown_seconds=cooldown_seconds,
        exited_at=exited_at,
      )

  def is_in_cheap_leg_cut_cooldown(
    self,
    event_ticker: str,
    *,
    label: str | None,
    market_ticker: str,
    cooldown_seconds: int,
  ) -> bool:
    from src.trading.bot_cheap_leg_cooldown import is_in_cheap_leg_cut_cooldown

    with self._connect() as conn:
      return is_in_cheap_leg_cut_cooldown(
        conn,
        event_ticker,
        label=label,
        market_ticker=market_ticker,
        cooldown_seconds=cooldown_seconds,
      )

  def has_open_position(self, event_ticker: str, market_ticker: str) -> bool:
    with self._connect() as conn:
      row = conn.execute(
        "SELECT 1 FROM bot_positions WHERE event_ticker = ? AND market_ticker = ? AND status = 'open'",
        (event_ticker, market_ticker),
      ).fetchone()
    return row is not None

  def open_position(self, pos: dict[str, Any]) -> dict[str, Any]:
    pid = pos.get("id") or str(uuid.uuid4())
    now = pos.get("opened_at") or datetime.now(timezone.utc).isoformat()
    row = {
      "id": pid,
      "event_ticker": pos["event_ticker"],
      "market_ticker": pos["market_ticker"],
      "side": pos["side"],
      "contracts": int(pos["contracts"]),
      "contracts_fp": pos.get("contracts_fp"),
      "entry_price_cents": int(pos["entry_price_cents"]),
      "cost_usd": float(pos["cost_usd"]),
      "signal": pos.get("signal"),
      "label": pos.get("label"),
      "entry_edge": pos.get("entry_edge"),
      "reference_price": pos.get("reference_price"),
      "contract_type": pos.get("contract_type"),
      "strike_type": pos.get("strike_type"),
      "floor_strike": pos.get("floor_strike"),
      "cap_strike": pos.get("cap_strike"),
      "stop_order_id": pos.get("stop_order_id"),
      "take_profit_order_id": pos.get("take_profit_order_id"),
      "mode": pos.get("mode") or "paper",
      "entry_source": pos.get("entry_source"),
      "opened_at": now,
      "status": "open",
    }
    with self._connect() as conn:
      conn.execute(
        """
        INSERT INTO bot_positions (
          id, event_ticker, market_ticker, side, contracts, contracts_fp, entry_price_cents,
          cost_usd, signal, label, entry_edge, reference_price,
          contract_type, strike_type, floor_strike, cap_strike,
          stop_order_id, take_profit_order_id, mode, entry_source, opened_at, status
        ) VALUES (
          :id, :event_ticker, :market_ticker, :side, :contracts, :contracts_fp, :entry_price_cents,
          :cost_usd, :signal, :label, :entry_edge, :reference_price,
          :contract_type, :strike_type, :floor_strike, :cap_strike,
          :stop_order_id, :take_profit_order_id, :mode, :entry_source, :opened_at, :status
        )
        """,
        row,
      )
    return row

  def update_position_peaks(
    self,
    position_id: str,
    unrealized_usd: float,
    cost_usd: float,
  ) -> dict[str, float]:
    from src.trading.bot_profit_exit import update_position_peaks

    peaks = self._position_peaks.get(
      position_id,
      {"peak_unrealized_usd": 0.0, "peak_profit_pct": 0.0},
    )
    updated = update_position_peaks(peaks, unrealized_usd, cost_usd)
    self._position_peaks[position_id] = updated
    return updated

  def clear_position_peaks(self, position_id: str) -> None:
    self._position_peaks.pop(position_id, None)

  def update_position_mark(self, position_id: str, mark_cents: int | None) -> None:
    if mark_cents is None:
      return
    with self._connect() as conn:
      conn.execute(
        "UPDATE bot_positions SET last_mark_cents = ? WHERE id = ? AND status = 'open'",
        (int(mark_cents), position_id),
      )

  def update_position_contracts(
    self,
    position_id: str,
    *,
    contracts: int,
    cost_usd: float,
    contracts_fp: float | None = None,
    entry_price_cents: int | None = None,
  ) -> None:
    sets = ["contracts = ?", "cost_usd = ?"]
    params: list[Any] = [int(contracts), round(float(cost_usd), 2)]
    if contracts_fp is not None:
      sets.append("contracts_fp = ?")
      params.append(round(float(contracts_fp), 2))
    if entry_price_cents is not None:
      sets.append("entry_price_cents = ?")
      params.append(int(entry_price_cents))
    params.append(position_id)
    with self._connect() as conn:
      conn.execute(
        f"""
        UPDATE bot_positions
        SET {", ".join(sets)}
        WHERE id = ? AND status = 'open'
        """,
        params,
      )

  def update_position_orders(
    self,
    position_id: str,
    *,
    stop_order_id: str | None = None,
    take_profit_order_id: str | None = None,
  ) -> None:
    with self._connect() as conn:
      conn.execute(
        """
        UPDATE bot_positions
        SET stop_order_id = COALESCE(?, stop_order_id),
            take_profit_order_id = COALESCE(?, take_profit_order_id)
        WHERE id = ? AND status = 'open'
        """,
        (stop_order_id, take_profit_order_id, position_id),
      )

  def close_position(self, position_id: str) -> dict[str, Any] | None:
    with self._connect() as conn:
      row = conn.execute("SELECT * FROM bot_positions WHERE id = ?", (position_id,)).fetchone()
      if not row:
        return None
      conn.execute("UPDATE bot_positions SET status = 'closed' WHERE id = ?", (position_id,))
    self.clear_position_peaks(position_id)
    return dict(row)

  @staticmethod
  def _exit_pnl_from_prices(row: dict[str, Any]) -> float | None:
    """Derive exit P&L from entry/exit cents when pnl_usd was not persisted."""
    from src.trading.paper_execution import leg_pnl_usd

    entry_c = row.get("entry_price_cents")
    exit_c = row.get("exit_price_cents")
    contracts = row.get("contracts")
    if entry_c is None or exit_c is None or contracts is None:
      return None
    return leg_pnl_usd(
      entry_price_cents=int(entry_c),
      mark_or_exit_cents=int(exit_c),
      contracts=int(contracts),
    )

  def _realized_pnl_usd(self, event_ticker: str, mode: str | None = None) -> float:
    clause, params = "", []
    if mode:
      clause = " AND mode = ?"
      params = [mode]
    with self._connect() as conn:
      exits = conn.execute(
        f"""
        SELECT pnl_usd, entry_price_cents, exit_price_cents, contracts, side
        FROM bot_trades
        WHERE event_ticker = ? AND action = 'exit' AND status IN ('filled', 'reconciled'){clause}
        """,
        [event_ticker, *params],
      ).fetchall()
    total = 0.0
    for r in exits:
      row = dict(r)
      pnl = row.get("pnl_usd")
      if pnl is None:
        pnl = self._exit_pnl_from_prices(row)
      total += float(pnl or 0)
    return round(total, 2)

  def _enrich_trade(self, row: dict[str, Any]) -> dict[str, Any]:
    action = row.get("action") or "enter"
    entry_c = row.get("entry_price_cents")
    exit_c = row.get("exit_price_cents")
    price_c = row.get("price_cents")
    if entry_c is None and action == "enter" and price_c is not None:
      entry_c = price_c
    if exit_c is None and action == "exit" and price_c is not None:
      exit_c = price_c
    out = dict(row)
    out["entry_price_cents"] = entry_c
    out["exit_price_cents"] = exit_c
    if action == "exit" and out.get("pnl_usd") is not None:
      out["realized_pnl_usd"] = out["pnl_usd"]
    raw_settings = out.get("entry_settings_json")
    if raw_settings and isinstance(raw_settings, str):
      try:
        out["entry_settings"] = json.loads(raw_settings)
      except json.JSONDecodeError:
        pass
    raw_exit = out.get("exit_context_json")
    if raw_exit and isinstance(raw_exit, str):
      try:
        out["exit_context"] = json.loads(raw_exit)
      except json.JSONDecodeError:
        pass
    return out

  def hour_interval_summary(
    self,
    event_ticker: str,
    *,
    mode: str | None = None,
  ) -> dict[str, Any]:
    """Per-hour stats. total_entered_usd sums all enter fills (can exceed max at-risk with churn)."""
    from src.trading.bot_mode_stats import interval_summary_row
    from src.trading.bot_position_mode import normalize_position_mode

    with self._connect() as conn:
      row = interval_summary_row(conn, event_ticker, mode=mode)
    open_pos = self.open_positions(event_ticker)
    if mode:
      want = normalize_position_mode(mode)
      open_pos = [p for p in open_pos if normalize_position_mode(p.get("mode")) == want]
    exposure = round(sum(float(p.get("cost_usd") or 0) for p in open_pos), 2)
    exit_count = int(row["exit_count"] or 0)
    realized = round(float(row["realized_pnl_usd"] or 0), 2)
    if exit_count > 0 and realized == 0:
      realized = self._realized_pnl_usd(event_ticker, mode=mode)
    return {
      "event_ticker": event_ticker,
      "mode": mode,
      "realized_pnl_usd": realized,
      "enter_count": int(row["enter_count"] or 0),
      "filled_enter_count_this_hour": int(row.get("filled_enter_count_this_hour") or row["enter_count"] or 0),
      "exit_count": exit_count,
      "total_entered_usd": round(float(row["total_entered_usd"] or 0), 2),
      "resting_enter_count": int(row.get("resting_enter_count") or 0),
      "resting_exit_count": int(row.get("resting_exit_count") or 0),
      "open_exposure_usd": exposure,
      "open_position_count": len(open_pos),
      "net_result_usd": realized,
    }

  def interval_performance(
    self,
    current_event_ticker: str | None = None,
    *,
    mode: str | None = None,
  ) -> dict[str, Any]:
    from src.trading.bot_interval_stats import compute_interval_performance

    with self._connect() as conn:
      return compute_interval_performance(
        conn,
        current_event_ticker=current_event_ticker,
        realized_pnl_fn=lambda event: self._realized_pnl_usd(event, mode=mode),
        mode=mode,
      )

  def mode_performance_summary(self, mode: str) -> dict[str, Any]:
    from src.trading.bot_mode_stats import mode_performance_summary

    with self._connect() as conn:
      return mode_performance_summary(conn, mode)

  def log_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
    tid = trade.get("id") or str(uuid.uuid4())
    now = trade.get("created_at") or datetime.now(timezone.utc).isoformat()
    action = trade.get("action", "enter")
    entry_cents = trade.get("entry_price_cents")
    exit_cents = trade.get("exit_price_cents")
    price_cents = trade.get("price_cents")
    if action == "enter" and entry_cents is None:
      entry_cents = price_cents
    elif action == "exit":
      if exit_cents is None:
        exit_cents = price_cents
      if price_cents is None:
        price_cents = exit_cents
    row = {
      "id": tid,
      "event_ticker": trade["event_ticker"],
      "trigger": trade.get("trigger", "continuous"),
      "action": action,
      "mode": trade.get("mode", "paper"),
      "market_ticker": trade.get("market_ticker"),
      "side": trade.get("side"),
      "contracts": trade.get("contracts"),
      "price_cents": price_cents,
      "entry_price_cents": entry_cents,
      "exit_price_cents": exit_cents,
      "cost_usd": trade.get("cost_usd"),
      "pnl_usd": trade.get("pnl_usd"),
      "signal": trade.get("signal"),
      "label": trade.get("label"),
      "actionable_headline": trade.get("actionable_headline"),
      "status": trade.get("status", "filled"),
      "detail": trade.get("detail"),
      "kalshi_order_id": trade.get("kalshi_order_id"),
      "position_id": trade.get("position_id"),
      "entry_bid_cents": trade.get("entry_bid_cents"),
      "entry_ask_cents": trade.get("entry_ask_cents"),
      "entry_spread_cents": trade.get("entry_spread_cents"),
      "created_at": now,
    }
    entry_settings = trade.get("entry_settings")
    if action == "enter" and entry_settings is not None:
      row["entry_settings_json"] = (
        entry_settings if isinstance(entry_settings, str) else json.dumps(entry_settings)
      )
    else:
      row["entry_settings_json"] = None
    exit_context = trade.get("exit_context")
    if action == "exit" and exit_context is not None:
      row["exit_context_json"] = (
        exit_context if isinstance(exit_context, str) else json.dumps(exit_context)
      )
    else:
      row["exit_context_json"] = None
    with self._connect() as conn:
      conn.execute(
        """
        INSERT INTO bot_trades (
          id, event_ticker, trigger, action, mode, market_ticker, side, contracts,
          price_cents, entry_price_cents, exit_price_cents, cost_usd, pnl_usd, signal, label, actionable_headline,
          status, detail, kalshi_order_id, position_id, entry_bid_cents, entry_ask_cents, entry_spread_cents,
          entry_settings_json, exit_context_json, created_at
        ) VALUES (
          :id, :event_ticker, :trigger, :action, :mode, :market_ticker, :side, :contracts,
          :price_cents, :entry_price_cents, :exit_price_cents, :cost_usd, :pnl_usd, :signal, :label, :actionable_headline,
          :status, :detail, :kalshi_order_id, :position_id, :entry_bid_cents, :entry_ask_cents, :entry_spread_cents,
          :entry_settings_json, :exit_context_json, :created_at
        )
        """,
        row,
      )
    if (
      action == "exit"
      and row.get("mode") == "paper"
      and row.get("status") == "filled"
    ):
      pnl = row.get("pnl_usd")
      if pnl is None:
        pnl = self._exit_pnl_from_prices(row)
      settings = self.get_settings()
      self._apply_paper_exit_pnl(float(pnl or 0), settings.max_spend_per_hour_usd)
    enriched = self._enrich_trade(row)
    try:
      from src.backup.trade_hook import notify_trade_logged

      notify_trade_logged(self.db_path, trade=enriched)
    except Exception:
      pass
    return enriched

  def latest_resting_enter(
    self,
    event_ticker: str,
    market_ticker: str,
    *,
    mode: str = "live",
  ) -> dict[str, Any] | None:
    with self._connect() as conn:
      row = conn.execute(
        """
        SELECT * FROM bot_trades
        WHERE event_ticker = ? AND market_ticker = ? AND action = 'enter'
          AND status = 'resting' AND mode = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (event_ticker, market_ticker, mode),
      ).fetchone()
    return self._enrich_trade(dict(row)) if row else None

  def promote_resting_enter_to_filled(
    self,
    trade_id: str | int,
    *,
    event_ticker: str,
    contracts: int,
    cost_usd: float,
    entry_price_cents: int,
    position_id: str,
    detail: str,
  ) -> dict[str, Any] | None:
    """Promote a resting live enter row to filled (avoids duplicate trade rows)."""
    tid = str(trade_id)
    with self._connect() as conn:
      row = conn.execute("SELECT * FROM bot_trades WHERE id = ?", (tid,)).fetchone()
      if not row:
        return None
      conn.execute(
        """
        UPDATE bot_trades
        SET status = 'filled',
            event_ticker = ?,
            contracts = ?,
            cost_usd = ?,
            price_cents = ?,
            entry_price_cents = ?,
            position_id = ?,
            detail = ?
        WHERE id = ? AND action = 'enter' AND status = 'resting' AND mode = 'live'
        """,
        (
          event_ticker,
          int(contracts),
          float(cost_usd),
          int(entry_price_cents),
          int(entry_price_cents),
          str(position_id),
          detail,
          tid,
        ),
      )
      updated = conn.execute("SELECT * FROM bot_trades WHERE id = ?", (tid,)).fetchone()
    return self._enrich_trade(dict(updated)) if updated else None

  def count_resting_live_enters(self, event_ticker: str, *, mode: str = "live") -> int:
    """Unfilled resting live enter rows for this hour (spam guard)."""
    with self._connect() as conn:
      row = conn.execute(
        """
        SELECT COUNT(*) FROM bot_trades
        WHERE event_ticker = ? AND action = 'enter'
          AND status = 'resting' AND mode = ?
        """,
        (event_ticker, mode),
      ).fetchone()
    return int(row[0] or 0) if row else 0

  def list_trades(self, *, limit: int = 30, event_ticker: str | None = None) -> list[dict[str, Any]]:
    with self._connect() as conn:
      if event_ticker:
        rows = conn.execute(
          "SELECT * FROM bot_trades WHERE event_ticker = ? ORDER BY created_at DESC LIMIT ?",
          (event_ticker, limit),
        ).fetchall()
      else:
        rows = conn.execute(
          "SELECT * FROM bot_trades ORDER BY created_at DESC LIMIT ?",
          (limit,),
        ).fetchall()
    return [self._enrich_trade(dict(r)) for r in rows]

  def last_auto_stop_trade(self) -> dict[str, Any] | None:
    with self._connect() as conn:
      row = conn.execute(
        "SELECT * FROM bot_trades WHERE action = 'auto_stop' ORDER BY created_at DESC LIMIT 1",
      ).fetchone()
    return self._enrich_trade(dict(row)) if row else None

  def status(self, event_ticker: str | None = None) -> dict[str, Any]:
    settings = self.get_settings()
    exposure_split = self.open_exposure_by_mode_usd(event_ticker) if event_ticker else {
      "paper": 0.0,
      "live": 0.0,
      "total": 0.0,
    }
    exposure = exposure_split["total"]
    max_cap = settings.max_spend_per_hour_usd
    bankroll = (
      self.hour_bankroll_usd(event_ticker, max_cap, settings)
      if event_ticker
      else max_cap
    )
    remaining = (
      self.remaining_budget_usd(event_ticker, max_cap, settings)
      if event_ticker
      else max_cap
    )
    open_pos = self.open_positions(event_ticker) if event_ticker else []
    from src.trading.bot_position_mode import normalize_position_mode

    mode = normalize_position_mode(settings.mode)
    open_pos = [p for p in open_pos if normalize_position_mode(p.get("mode")) == mode]
    hour_summary = (
      self.hour_interval_summary(event_ticker, mode=mode) if event_ticker else None
    )
    auto_stop_row = self.last_auto_stop_trade() if settings.auto_stopped else None
    paper_state = (
      self.get_paper_state_dict(max_cap) if settings.mode == "paper" else None
    )
    live_performance = (
      self.mode_performance_summary("live") if settings.mode == "live" else None
    )
    return {
      "settings": settings.to_dict(),
      "event_ticker": event_ticker,
      "open_exposure_usd": round(exposure, 2),
      "open_exposure_paper_usd": exposure_split["paper"],
      "open_exposure_live_usd": exposure_split["live"],
      "hour_bankroll_usd": round(bankroll, 2),
      "remaining_usd": round(remaining, 2),
      "max_spend_per_hour_usd": max_cap,
      "open_positions": open_pos,
      "open_position_count": len(open_pos),
      "hour_summary": hour_summary,
      "hourly_summary": hour_summary,
      "interval_performance": self.interval_performance(event_ticker, mode=mode),
      "paper_bankroll": paper_state,
      "live_performance": live_performance,
      "auto_stopped": settings.auto_stopped,
      "auto_stop_reason": settings.auto_stop_reason or (
        auto_stop_row.get("detail") if auto_stop_row else None
      ),
      "last_skip_reason": self._last_skip_reason,
      "server_runtime": self.get_runtime(),
    }
