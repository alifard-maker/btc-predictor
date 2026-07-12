"""SQLite store for sports arb scanner settings, opportunities, and paper trades."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
  return datetime.now(timezone.utc).isoformat()


@dataclass
class SportsArbSettings:
  enabled: bool = True
  # Legacy aggregate mode: "live" if either strategy is live
  mode: str = "paper"
  # Shared legacy fields (mirrors dutch budget for older UI/API clients)
  max_open_usd: float = 40.0
  max_stake_per_opp_usd: float = 5.0
  paper_bankroll_usd: float = 50.0
  # 0 = unlimited entry counts (shared across strategies)
  max_live_per_scan: int = 0
  max_live_trades_per_day: int = 0
  # Per-strategy live + dedicated $ budgets
  dutch_live: bool = False
  dutch_max_open_usd: float = 40.0
  dutch_max_stake_usd: float = 5.0
  value_live: bool = False
  value_max_open_usd: float = 40.0
  value_max_stake_usd: float = 5.0
  # Goal 3: only paper/live value_sharp when bet_assessment edge_tier is STRONG
  value_strong_bets_only: bool = False

  def to_dict(self) -> dict[str, Any]:
    mode = "live" if (self.dutch_live or self.value_live) else "paper"
    return {
      "enabled": self.enabled,
      "mode": mode,
      "max_open_usd": self.max_open_usd,
      "max_stake_per_opp_usd": self.max_stake_per_opp_usd,
      "paper_bankroll_usd": self.paper_bankroll_usd,
      "max_live_per_scan": self.max_live_per_scan,
      "max_live_trades_per_day": self.max_live_trades_per_day,
      "dutch_live": self.dutch_live,
      "dutch_max_open_usd": self.dutch_max_open_usd,
      "dutch_max_stake_usd": self.dutch_max_stake_usd,
      "value_live": self.value_live,
      "value_max_open_usd": self.value_max_open_usd,
      "value_max_stake_usd": self.value_max_stake_usd,
      "value_strong_bets_only": self.value_strong_bets_only,
    }

  @classmethod
  def from_dict(cls, raw: dict[str, Any] | None) -> SportsArbSettings:
    d = dict(raw or {})
    legacy_mode = str(d.get("mode") or "paper").lower()
    legacy_open = max(0.0, float(d.get("max_open_usd", 40.0)))
    legacy_stake = max(0.0, float(d.get("max_stake_per_opp_usd", 5.0)))

    if "dutch_live" in d:
      dutch_live = bool(d.get("dutch_live"))
    else:
      dutch_live = legacy_mode == "live"

    if "value_live" in d:
      value_live = bool(d.get("value_live"))
    else:
      value_live = False  # do not auto-arm Goal 3 on upgrade from legacy mode=live

    dutch_open = max(0.0, float(d.get("dutch_max_open_usd", legacy_open)))
    dutch_stake = max(0.0, float(d.get("dutch_max_stake_usd", legacy_stake)))
    value_open = max(0.0, float(d.get("value_max_open_usd", legacy_open)))
    value_stake = max(0.0, float(d.get("value_max_stake_usd", legacy_stake)))

    mode = "live" if (dutch_live or value_live) else "paper"
    return cls(
      enabled=bool(d.get("enabled", True)),
      mode=mode,
      max_open_usd=dutch_open,  # legacy mirror
      max_stake_per_opp_usd=dutch_stake,
      paper_bankroll_usd=max(0.0, float(d.get("paper_bankroll_usd", 50.0))),
      max_live_per_scan=max(0, int(d.get("max_live_per_scan", 0))),
      max_live_trades_per_day=max(0, int(d.get("max_live_trades_per_day", 0))),
      dutch_live=dutch_live,
      dutch_max_open_usd=dutch_open,
      dutch_max_stake_usd=dutch_stake,
      value_live=value_live,
      value_max_open_usd=value_open,
      value_max_stake_usd=value_stake,
      value_strong_bets_only=bool(d.get("value_strong_bets_only", False)),
    )


class SportsArbStore:
  def __init__(self, db_path: Path | str):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    self._init_db()

  def _connect(self) -> sqlite3.Connection:
    conn = sqlite3.connect(str(self.db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

  def _init_db(self) -> None:
    with self._connect() as conn:
      conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sports_settings (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sports_opportunities (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          scanned_at TEXT NOT NULL,
          strategy TEXT NOT NULL,
          kind TEXT NOT NULL,
          event_ticker TEXT,
          series_ticker TEXT,
          edge_usd REAL,
          json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sports_trades (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          mode TEXT NOT NULL,
          status TEXT NOT NULL,
          strategy TEXT,
          event_ticker TEXT,
          edge_usd REAL,
          cost_usd REAL,
          json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sports_runtime (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          last_scan_at TEXT,
          last_scan_ok INTEGER,
          last_error TEXT,
          cycles_total INTEGER DEFAULT 0,
          last_opp_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sports_scan_ticks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          scanned_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sports_scan_ticks_at ON sports_scan_ticks(scanned_at);
        INSERT OR IGNORE INTO sports_settings (id, json) VALUES (1, '{}');
        INSERT OR IGNORE INTO sports_runtime (id, last_scan_at, last_scan_ok, last_error, cycles_total, last_opp_count)
          VALUES (1, NULL, 0, NULL, 0, 0);
        """
      )

  def get_settings(self) -> SportsArbSettings:
    with self._connect() as conn:
      row = conn.execute("SELECT json FROM sports_settings WHERE id = 1").fetchone()
    raw = json.loads(row["json"]) if row and row["json"] else {}
    return SportsArbSettings.from_dict(raw)

  def save_settings(self, settings: SportsArbSettings, *, source: str = "api") -> None:
    del source
    with self._connect() as conn:
      conn.execute(
        "UPDATE sports_settings SET json = ? WHERE id = 1",
        (json.dumps(settings.to_dict()),),
      )

  def record_scan_tick(self) -> None:
    """Record a scan heartbeat (for scans/min alive counter)."""
    now = _utc_now()
    cutoff = datetime.now(timezone.utc).timestamp() - 300  # keep 5 minutes
    with self._connect() as conn:
      conn.execute("INSERT INTO sports_scan_ticks (scanned_at) VALUES (?)", (now,))
      rows = conn.execute("SELECT id, scanned_at FROM sports_scan_ticks").fetchall()
      for r in rows:
        try:
          ts = datetime.fromisoformat(str(r["scanned_at"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
          ts = 0
        if ts < cutoff:
          conn.execute("DELETE FROM sports_scan_ticks WHERE id = ?", (r["id"],))

  def scans_in_last_seconds(self, seconds: float = 60.0) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - float(seconds)
    with self._connect() as conn:
      rows = conn.execute("SELECT scanned_at FROM sports_scan_ticks").fetchall()
    n = 0
    for r in rows:
      try:
        ts = datetime.fromisoformat(str(r["scanned_at"]).replace("Z", "+00:00")).timestamp()
      except ValueError:
        continue
      if ts >= cutoff:
        n += 1
    return n

  def record_scan(
    self,
    opportunities: list[dict[str, Any]],
    *,
    ok: bool = True,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
  ) -> None:
    now = _utc_now()
    meta_json = json.dumps(meta or {})
    with self._connect() as conn:
      cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(sports_runtime)").fetchall()}
      if "last_scan_meta" not in cols:
        conn.execute("ALTER TABLE sports_runtime ADD COLUMN last_scan_meta TEXT")
      conn.execute("DELETE FROM sports_opportunities")
      for opp in opportunities:
        conn.execute(
          """
          INSERT INTO sports_opportunities
            (scanned_at, strategy, kind, event_ticker, series_ticker, edge_usd, json)
          VALUES (?, ?, ?, ?, ?, ?, ?)
          """,
          (
            now,
            str(opp.get("strategy") or ""),
            str(opp.get("kind") or ""),
            opp.get("event_ticker"),
            opp.get("series_ticker"),
            float(opp.get("edge_usd") or 0),
            json.dumps(opp),
          ),
        )
      conn.execute(
        """
        UPDATE sports_runtime SET
          last_scan_at = ?,
          last_scan_ok = ?,
          last_error = ?,
          cycles_total = cycles_total + 1,
          last_opp_count = ?,
          last_scan_meta = ?
        WHERE id = 1
        """,
        (now, 1 if ok else 0, error, len(opportunities), meta_json),
      )

  def list_opportunities(self, *, limit: int = 100) -> list[dict[str, Any]]:
    with self._connect() as conn:
      rows = conn.execute(
        "SELECT json FROM sports_opportunities ORDER BY edge_usd DESC LIMIT ?",
        (int(limit),),
      ).fetchall()
    return [json.loads(r["json"]) for r in rows]

  def log_paper_trade(self, opp: dict[str, Any]) -> None:
    self.log_trade(opp, mode="paper", status="paper_signal")

  def log_trade(
    self,
    opp: dict[str, Any],
    *,
    mode: str,
    status: str,
    extra: dict[str, Any] | None = None,
  ) -> None:
    payload = {**opp, **(extra or {})}
    with self._connect() as conn:
      conn.execute(
        """
        INSERT INTO sports_trades (created_at, mode, status, strategy, event_ticker, edge_usd, cost_usd, json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
          _utc_now(),
          mode,
          status,
          opp.get("strategy"),
          opp.get("event_ticker"),
          float(opp.get("edge_usd") or 0),
          float(opp.get("total_cost_usd") or 0),
          json.dumps(payload),
        ),
      )

  def recent_trade_fingerprints(
    self,
    *,
    hours: float = 12.0,
    statuses: tuple[str, ...] | None = None,
  ) -> set[str]:
    """Fingerprints already acted on."""
    want = statuses or (
      "paper_signal",
      "live_filled",
      "live_partial",
      "live_submitted",
    )
    placeholders = ",".join("?" * len(want))
    with self._connect() as conn:
      rows = conn.execute(
        f"""
        SELECT event_ticker, json, created_at FROM sports_trades
        WHERE status IN ({placeholders})
        ORDER BY id DESC LIMIT 500
        """,
        tuple(want),
      ).fetchall()
    out: set[str] = set()
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    for r in rows:
      try:
        created = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
        if created.timestamp() < cutoff:
          continue
      except ValueError:
        pass
      payload = json.loads(r["json"] or "{}")
      kind = str(payload.get("kind") or "")
      event = str(r["event_ticker"] or payload.get("event_ticker") or "")
      venue = str(payload.get("venue") or "")
      sel = str(payload.get("selection") or "")
      if event:
        out.add(f"{event}|{kind}|{venue}|{sel}" if (venue or sel) else f"{event}|{kind}")
        if not venue and not sel:
          out.add(f"{event}|{kind}")
    return out

  def live_trades_today_count(self, *, strategy: str | None = None) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with self._connect() as conn:
      if strategy:
        row = conn.execute(
          """
          SELECT COUNT(*) AS n FROM sports_trades
          WHERE mode = 'live' AND status IN ('live_filled', 'live_partial', 'live_submitted')
            AND created_at LIKE ? AND strategy = ?
          """,
          (f"{today}%", strategy),
        ).fetchone()
      else:
        row = conn.execute(
          """
          SELECT COUNT(*) AS n FROM sports_trades
          WHERE mode = 'live' AND status IN ('live_filled', 'live_partial', 'live_submitted')
            AND created_at LIKE ?
          """,
          (f"{today}%",),
        ).fetchone()
    return int(row["n"] if row else 0)

  def live_spend_today_usd(self, *, strategy: str | None = None) -> float:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with self._connect() as conn:
      if strategy:
        row = conn.execute(
          """
          SELECT COALESCE(SUM(cost_usd), 0) AS s FROM sports_trades
          WHERE mode = 'live' AND status IN ('live_filled', 'live_partial', 'live_submitted')
            AND created_at LIKE ? AND strategy = ?
          """,
          (f"{today}%", strategy),
        ).fetchone()
      else:
        row = conn.execute(
          """
          SELECT COALESCE(SUM(cost_usd), 0) AS s FROM sports_trades
          WHERE mode = 'live' AND status IN ('live_filled', 'live_partial', 'live_submitted')
            AND created_at LIKE ?
          """,
          (f"{today}%",),
        ).fetchone()
    return float(row["s"] if row else 0)

  def settings_initialized(self) -> bool:
    with self._connect() as conn:
      row = conn.execute("SELECT json FROM sports_settings WHERE id = 1").fetchone()
    if not row or not row["json"]:
      return False
    raw = str(row["json"]).strip()
    return raw not in ("", "{}")

  def _trade_from_row(self, r: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(r["json"])
    payload.update({
      "id": int(r["id"]),
      "created_at": r["created_at"],
      "mode": r["mode"],
      "status": r["status"],
      "strategy": r["strategy"],
      "event_ticker": r["event_ticker"],
      "edge_usd": r["edge_usd"],
      "cost_usd": r["cost_usd"],
    })
    return payload

  def list_trades(
    self,
    *,
    limit: int = 100,
    for_display: bool = False,
    live_retention_hours: float = 24.0,
    paper_limit: int | None = None,
  ) -> list[dict[str, Any]]:
    """Return trades for dashboard/API.

    When for_display=True, always include every live trade from the last
    live_retention_hours, then append recent paper signals (paper_limit).
    """
    if not for_display:
      with self._connect() as conn:
        rows = conn.execute(
          """
          SELECT id, created_at, mode, status, strategy, event_ticker, edge_usd, cost_usd, json
          FROM sports_trades ORDER BY id DESC LIMIT ?
          """,
          (int(limit),),
        ).fetchall()
      return [self._trade_from_row(r) for r in rows]

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=float(live_retention_hours))).isoformat()
    p_cap = int(paper_limit if paper_limit is not None else max(limit, 40))
    with self._connect() as conn:
      live_rows = conn.execute(
        """
        SELECT id, created_at, mode, status, strategy, event_ticker, edge_usd, cost_usd, json
        FROM sports_trades
        WHERE mode = 'live' AND created_at >= ?
        ORDER BY id DESC
        """,
        (cutoff,),
      ).fetchall()
      paper_rows = conn.execute(
        """
        SELECT id, created_at, mode, status, strategy, event_ticker, edge_usd, cost_usd, json
        FROM sports_trades
        WHERE mode = 'paper' OR status = 'paper_signal'
        ORDER BY id DESC LIMIT ?
        """,
        (p_cap,),
      ).fetchall()

    by_id: dict[int, dict[str, Any]] = {}
    for r in list(live_rows) + list(paper_rows):
      tid = int(r["id"])
      by_id[tid] = self._trade_from_row(r)

    merged = sorted(
      by_id.values(),
      key=lambda t: str(t.get("created_at") or ""),
      reverse=True,
    )
    return merged

  def list_trades_for_display(
    self,
    *,
    live_retention_hours: float = 24.0,
    paper_limit: int = 40,
  ) -> dict[str, Any]:
    """Split live/paper trade log for sports dashboard."""
    rows = self.list_trades(
      for_display=True,
      live_retention_hours=live_retention_hours,
      paper_limit=paper_limit,
    )
    live_rows = [t for t in rows if str(t.get("mode") or "").lower() == "live"]
    live_ids = {int(t["id"]) for t in live_rows if t.get("id") is not None}
    paper_rows = [
      t for t in rows
      if int(t.get("id") or -1) not in live_ids
      and (str(t.get("mode") or "").lower() == "paper" or str(t.get("status") or "") == "paper_signal")
    ]
    return {
      "live_retention_hours": live_retention_hours,
      "live_trades": live_rows,
      "paper_trades": paper_rows,
      "recent_trades": rows,
      "live_count": len(live_rows),
      "paper_count": len(paper_rows),
    }

  def runtime(self) -> dict[str, Any]:
    with self._connect() as conn:
      cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(sports_runtime)").fetchall()}
      if "last_scan_meta" not in cols:
        conn.execute("ALTER TABLE sports_runtime ADD COLUMN last_scan_meta TEXT")
      row = conn.execute(
        "SELECT last_scan_at, last_scan_ok, last_error, cycles_total, last_opp_count, last_scan_meta FROM sports_runtime WHERE id = 1"
      ).fetchone()
    if not row:
      return {}
    meta: dict[str, Any] = {}
    raw_meta = row["last_scan_meta"] if "last_scan_meta" in row.keys() else None
    if raw_meta:
      try:
        parsed = json.loads(str(raw_meta))
        if isinstance(parsed, dict):
          meta = parsed
      except json.JSONDecodeError:
        meta = {}
    return {
      "last_scan_at": row["last_scan_at"],
      "last_scan_ok": bool(row["last_scan_ok"]),
      "last_error": row["last_error"],
      "cycles_total": int(row["cycles_total"] or 0),
      "last_opp_count": int(row["last_opp_count"] or 0),
      "last_scan_meta": meta,
    }

  def fresh_start(self, *, preserve_live: bool = True) -> dict[str, Any]:
    with self._connect() as conn:
      conn.execute("DELETE FROM sports_opportunities")
      if preserve_live:
        paper_deleted = conn.execute(
          "DELETE FROM sports_trades WHERE mode = 'paper' OR status = 'paper_signal'"
        ).rowcount
      else:
        paper_deleted = conn.execute("DELETE FROM sports_trades").rowcount
      conn.execute("DELETE FROM sports_scan_ticks")
      conn.execute(
        "UPDATE sports_runtime SET last_scan_at=NULL, last_scan_ok=0, last_error=NULL, cycles_total=0, last_opp_count=0 WHERE id=1"
      )
    return {
      "ok": True,
      "paper_trades_cleared": int(paper_deleted),
      "live_trades_preserved": bool(preserve_live),
    }
