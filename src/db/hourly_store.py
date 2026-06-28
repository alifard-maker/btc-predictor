"""Persistence for Kalshi hourly/daily threshold predictions."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pandas as pd

from src.db.store import _py, normalize_database_url
from src.models.hourly_late_call_log import LATE_CALL_LOG_FIELDS, late_call_migrations, late_call_migrations_pg
from src.models.hourly_range_log import RANGE_BAND_LOG_FIELDS, range_band_migrations, range_band_migrations_pg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HourlyResolution:
  settle_brti: float
  outcome: int  # 1 = primary contract YES, 0 = NO
  actual_return: float
  exit_source: str


class HourlyPredictionStore(ABC):
  @abstractmethod
  def init(self) -> None: ...

  @abstractmethod
  def log_prediction(self, row: dict[str, Any], *, force: bool = False) -> int: ...

  @abstractmethod
  def get_by_event_ticker(self, event_ticker: str) -> dict[str, Any] | None: ...

  @abstractmethod
  def get_pending(self) -> list[dict[str, Any]]: ...

  @abstractmethod
  def resolve(self, event_ticker: str, resolution: HourlyResolution) -> bool: ...

  @abstractmethod
  def load_all(self) -> pd.DataFrame: ...

  @abstractmethod
  def load_resolved(self) -> pd.DataFrame: ...

  @abstractmethod
  def load_recent(self, limit: int = 50) -> pd.DataFrame: ...

  @abstractmethod
  def clear_all(self) -> int: ...

  @abstractmethod
  def log_open_snapshot(self, row: dict[str, Any]) -> int: ...

  @abstractmethod
  def get_open_snapshot(self, event_ticker: str) -> dict[str, Any] | None: ...

  @abstractmethod
  def clear_open_snapshots(self) -> int: ...

  @abstractmethod
  def log_late_call(self, row: dict[str, Any], *, force: bool = False) -> bool: ...


_HOURLY_COLS = """
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  logged_at TEXT NOT NULL,
  event_ticker TEXT NOT NULL UNIQUE,
  frequency TEXT NOT NULL,
  settle_time TEXT NOT NULL,
  series_ticker TEXT,
  title TEXT,
  reference_price REAL NOT NULL,
  terminal_mu REAL,
  terminal_sigma REAL,
  ml_prob_up REAL,
  ml_mu REAL,
  structure_mu REAL,
  blended_mu REAL,
  hours_to_settle REAL,
  primary_ticker TEXT,
  primary_type TEXT,
  primary_label TEXT,
  primary_strike_type TEXT,
  primary_floor REAL,
  primary_cap REAL,
  primary_model_prob REAL,
  primary_kalshi_mid REAL,
  primary_edge REAL,
  primary_signal TEXT,
  most_likely_label TEXT,
  most_likely_prob REAL,
  confidence REAL,
  expected_move_pct REAL,
  direction TEXT,
  method TEXT,
  regime_blocked INTEGER DEFAULT 0,
  regime_notes TEXT,
  prob_15m_avg REAL,
  settlement_zone_low REAL,
  settlement_zone_high REAL,
  range_ml_ticker TEXT,
  range_ml_label TEXT,
  range_ml_prob REAL,
  range_ml_signal TEXT,
  range_ml_edge REAL,
  range_ml_floor REAL,
  range_ml_cap REAL,
  range_ml_kalshi_mid REAL,
  range_be_ticker TEXT,
  range_be_label TEXT,
  range_be_prob REAL,
  range_be_signal TEXT,
  range_be_edge REAL,
  range_be_floor REAL,
  range_be_cap REAL,
  range_be_kalshi_mid REAL,
  outcome INTEGER,
  settle_brti REAL,
  actual_return REAL,
  resolved_at TEXT,
  asset TEXT NOT NULL DEFAULT 'btc'
"""

_HOURLY_COLS_PG = _HOURLY_COLS.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY").replace(
  "INTEGER DEFAULT 0", "INTEGER DEFAULT 0"
)

_OPEN_SNAPSHOT_COLS = """
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  logged_at TEXT NOT NULL,
  event_ticker TEXT NOT NULL,
  frequency TEXT NOT NULL,
  settle_time TEXT NOT NULL,
  series_ticker TEXT,
  title TEXT,
  reference_price REAL NOT NULL,
  terminal_mu REAL,
  terminal_sigma REAL,
  ml_prob_up REAL,
  ml_mu REAL,
  structure_mu REAL,
  blended_mu REAL,
  hours_to_settle REAL,
  primary_ticker TEXT,
  primary_type TEXT,
  primary_label TEXT,
  primary_strike_type TEXT,
  primary_floor REAL,
  primary_cap REAL,
  primary_model_prob REAL,
  primary_kalshi_mid REAL,
  primary_edge REAL,
  primary_signal TEXT,
  most_likely_label TEXT,
  most_likely_prob REAL,
  confidence REAL,
  expected_move_pct REAL,
  direction TEXT,
  method TEXT,
  regime_blocked INTEGER DEFAULT 0,
  regime_notes TEXT,
  prob_15m_avg REAL,
  settlement_zone_low REAL,
  settlement_zone_high REAL,
  range_ml_ticker TEXT,
  range_ml_label TEXT,
  range_ml_prob REAL,
  range_ml_signal TEXT,
  range_ml_edge REAL,
  range_ml_floor REAL,
  range_ml_cap REAL,
  range_ml_kalshi_mid REAL,
  range_be_ticker TEXT,
  range_be_label TEXT,
  range_be_prob REAL,
  range_be_signal TEXT,
  range_be_edge REAL,
  range_be_floor REAL,
  range_be_cap REAL,
  range_be_kalshi_mid REAL,
  range_lean_bands TEXT,
  asset TEXT NOT NULL DEFAULT 'btc',
  UNIQUE(event_ticker, asset)
"""

_OPEN_SNAPSHOT_COLS_PG = _OPEN_SNAPSHOT_COLS.replace(
  "INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"
).replace("INTEGER DEFAULT 0", "INTEGER DEFAULT 0")

_OPEN_LOG_COLS = [
  "logged_at", "event_ticker", "frequency", "settle_time", "series_ticker", "title",
  "reference_price", "terminal_mu", "terminal_sigma", "ml_prob_up", "ml_mu",
  "structure_mu", "blended_mu", "hours_to_settle", "primary_ticker", "primary_type",
  "primary_label", "primary_strike_type", "primary_floor", "primary_cap",
  "primary_model_prob", "primary_kalshi_mid", "primary_edge", "primary_signal",
  "most_likely_label", "most_likely_prob", "confidence", "expected_move_pct",
  "direction", "method", "regime_blocked", "regime_notes", "prob_15m_avg",
  "settlement_zone_low", "settlement_zone_high",
  *RANGE_BAND_LOG_FIELDS,
  "asset",
]


class SqliteHourlyStore(HourlyPredictionStore):
  def __init__(self, db_path: str, *, asset: str = "btc"):
    self.db_path = Path(db_path)
    self.asset = asset

  def _conn(self):
    import sqlite3
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(self.db_path)

  def init(self) -> None:
    with self._conn() as conn:
      conn.execute(f"CREATE TABLE IF NOT EXISTS hourly_predictions ({_HOURLY_COLS})")
      conn.execute(f"CREATE TABLE IF NOT EXISTS hourly_open_snapshots ({_OPEN_SNAPSHOT_COLS})")
      self._migrate_sqlite(conn)
      conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hourly_settle ON hourly_predictions(settle_time)"
      )
      conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hourly_open_settle ON hourly_open_snapshots(settle_time)"
      )

  @staticmethod
  def _migrate_sqlite(conn) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(hourly_predictions)")}
    for col, typ in range_band_migrations():
      if col not in existing:
        conn.execute(f"ALTER TABLE hourly_predictions ADD COLUMN {col} {typ}")
    for col, typ in late_call_migrations():
      if col not in existing:
        conn.execute(f"ALTER TABLE hourly_predictions ADD COLUMN {col} {typ}")
    if "asset" not in existing:
      conn.execute("ALTER TABLE hourly_predictions ADD COLUMN asset TEXT NOT NULL DEFAULT 'btc'")
      conn.execute("UPDATE hourly_predictions SET asset = 'btc' WHERE asset IS NULL")

  def log_prediction(self, row: dict[str, Any], *, force: bool = False) -> int:
    row = {**row, "asset": row.get("asset") or self.asset}
    cols = [
      "logged_at", "event_ticker", "frequency", "settle_time", "series_ticker", "title",
      "reference_price", "terminal_mu", "terminal_sigma", "ml_prob_up", "ml_mu",
      "structure_mu", "blended_mu", "hours_to_settle", "primary_ticker", "primary_type",
      "primary_label", "primary_strike_type", "primary_floor", "primary_cap",
      "primary_model_prob", "primary_kalshi_mid", "primary_edge", "primary_signal",
      "most_likely_label", "most_likely_prob", "confidence", "expected_move_pct",
      "direction", "method", "regime_blocked", "regime_notes", "prob_15m_avg",
      "settlement_zone_low", "settlement_zone_high",
      *RANGE_BAND_LOG_FIELDS,
      "asset",
    ]
    vals = [row.get(c) for c in cols]
    with self._conn() as conn:
      existing = conn.execute(
        "SELECT id FROM hourly_predictions WHERE event_ticker = ? AND asset = ?",
        (row["event_ticker"], row["asset"]),
      ).fetchone()
      if existing and not force:
        return int(existing[0])
      if existing:
        placeholders = ", ".join(f"{c}=?" for c in cols if c not in ("event_ticker", "asset"))
        update_vals = [row.get(c) for c in cols if c not in ("event_ticker", "asset")] + [row["event_ticker"], row["asset"]]
        conn.execute(
          f"UPDATE hourly_predictions SET {placeholders} WHERE event_ticker = ? AND asset = ?",
          update_vals,
        )
        return int(existing[0])
      cur = conn.execute(
        f"INSERT INTO hourly_predictions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        vals,
      )
      return int(cur.lastrowid)

  def get_by_event_ticker(self, event_ticker: str) -> dict[str, Any] | None:
    with self._conn() as conn:
      conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
      row = conn.execute(
        "SELECT * FROM hourly_predictions WHERE event_ticker = ? AND asset = ? LIMIT 1",
        (event_ticker, self.asset),
      ).fetchone()
      return dict(row) if row else None

  def get_pending(self) -> list[dict[str, Any]]:
    with self._conn() as conn:
      conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
      rows = conn.execute(
        "SELECT * FROM hourly_predictions WHERE outcome IS NULL AND asset = ? ORDER BY settle_time",
        (self.asset,),
      ).fetchall()
      return list(rows)

  def resolve(self, event_ticker: str, resolution: HourlyResolution) -> bool:
    with self._conn() as conn:
      cur = conn.execute(
        """UPDATE hourly_predictions SET
             outcome=?, settle_brti=?, actual_return=?, resolved_at=datetime('now')
           WHERE event_ticker=? AND outcome IS NULL AND asset=?""",
        (resolution.outcome, resolution.settle_brti, resolution.actual_return, event_ticker, self.asset),
      )
      return cur.rowcount > 0

  def load_all(self) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        "SELECT * FROM hourly_predictions WHERE asset = ? ORDER BY logged_at",
        conn,
        params=(self.asset,),
      )

  def load_resolved(self) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        "SELECT * FROM hourly_predictions WHERE outcome IS NOT NULL AND asset = ? ORDER BY settle_time",
        conn,
        params=(self.asset,),
      )

  def load_recent(self, limit: int = 50) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        f"SELECT * FROM hourly_predictions WHERE asset = ? ORDER BY logged_at DESC LIMIT {int(limit)}",
        conn,
        params=(self.asset,),
      )

  def clear_all(self) -> int:
    with self._conn() as conn:
      n = int(conn.execute(
        "SELECT COUNT(*) FROM hourly_predictions WHERE asset = ?", (self.asset,)
      ).fetchone()[0])
      conn.execute("DELETE FROM hourly_predictions WHERE asset = ?", (self.asset,))
    return n

  def log_open_snapshot(self, row: dict[str, Any]) -> int:
    row = {**row, "asset": row.get("asset") or self.asset}
    cols = _OPEN_LOG_COLS
    vals = [row.get(c) for c in cols]
    with self._conn() as conn:
      existing = conn.execute(
        "SELECT id FROM hourly_open_snapshots WHERE event_ticker = ? AND asset = ?",
        (row["event_ticker"], row["asset"]),
      ).fetchone()
      if existing:
        placeholders = ", ".join(f"{c}=?" for c in cols if c not in ("event_ticker", "asset"))
        update_vals = [row.get(c) for c in cols if c not in ("event_ticker", "asset")] + [
          row["event_ticker"],
          row["asset"],
        ]
        conn.execute(
          f"UPDATE hourly_open_snapshots SET {placeholders} WHERE event_ticker = ? AND asset = ?",
          update_vals,
        )
        return int(existing[0])
      cur = conn.execute(
        f"INSERT INTO hourly_open_snapshots ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        vals,
      )
      return int(cur.lastrowid)

  def get_open_snapshot(self, event_ticker: str) -> dict[str, Any] | None:
    with self._conn() as conn:
      conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
      row = conn.execute(
        "SELECT * FROM hourly_open_snapshots WHERE event_ticker = ? AND asset = ? LIMIT 1",
        (event_ticker, self.asset),
      ).fetchone()
      return dict(row) if row else None

  def clear_open_snapshots(self) -> int:
    with self._conn() as conn:
      n = int(conn.execute(
        "SELECT COUNT(*) FROM hourly_open_snapshots WHERE asset = ?", (self.asset,)
      ).fetchone()[0])
      conn.execute("DELETE FROM hourly_open_snapshots WHERE asset = ?", (self.asset,))
    return n

  def log_late_call(self, row: dict[str, Any], *, force: bool = False) -> bool:
    """Persist :45 ET late-call fields on an existing hourly_predictions row."""
    event_ticker = row.get("event_ticker")
    if not event_ticker:
      return False
    asset = row.get("asset") or self.asset
    cols = list(LATE_CALL_LOG_FIELDS)
    with self._conn() as conn:
      existing = conn.execute(
        "SELECT late_call_logged_at FROM hourly_predictions WHERE event_ticker = ? AND asset = ?",
        (event_ticker, asset),
      ).fetchone()
      if not existing:
        log.warning("Late call skipped — no :05 row for %s (%s)", event_ticker, asset)
        return False
      if existing[0] and not force:
        return False
      placeholders = ", ".join(f"{c}=?" for c in cols)
      vals = [row.get(c) for c in cols] + [event_ticker, asset]
      conn.execute(
        f"UPDATE hourly_predictions SET {placeholders} WHERE event_ticker = ? AND asset = ?",
        vals,
      )
    return True


def _strip_sslmode(url: str) -> str:
  parsed = urlparse(url)
  if not parsed.query:
    return url
  params = parse_qs(parsed.query, keep_blank_values=True)
  params.pop("sslmode", None)
  new_query = urlencode({k: v[0] for k, v in params.items()})
  return urlunparse(parsed._replace(query=new_query))


class PostgresHourlyStore(HourlyPredictionStore):
  def __init__(self, database_url: str, *, asset: str = "btc"):
    import psycopg2
    self.database_url = _strip_sslmode(normalize_database_url(database_url))
    self._psycopg2 = psycopg2
    self.asset = asset

  def _conn(self):
    return self._psycopg2.connect(self.database_url, sslmode="require")

  def init(self) -> None:
    ddl = f"""
      CREATE TABLE IF NOT EXISTS hourly_predictions (
        id SERIAL PRIMARY KEY,
        logged_at TIMESTAMPTZ NOT NULL,
        event_ticker TEXT NOT NULL UNIQUE,
        frequency TEXT NOT NULL,
        settle_time TIMESTAMPTZ NOT NULL,
        series_ticker TEXT,
        title TEXT,
        reference_price DOUBLE PRECISION NOT NULL,
        terminal_mu DOUBLE PRECISION,
        terminal_sigma DOUBLE PRECISION,
        ml_prob_up DOUBLE PRECISION,
        ml_mu DOUBLE PRECISION,
        structure_mu DOUBLE PRECISION,
        blended_mu DOUBLE PRECISION,
        hours_to_settle DOUBLE PRECISION,
        primary_ticker TEXT,
        primary_type TEXT,
        primary_label TEXT,
        primary_strike_type TEXT,
        primary_floor DOUBLE PRECISION,
        primary_cap DOUBLE PRECISION,
        primary_model_prob DOUBLE PRECISION,
        primary_kalshi_mid DOUBLE PRECISION,
        primary_edge DOUBLE PRECISION,
        primary_signal TEXT,
        most_likely_label TEXT,
        most_likely_prob DOUBLE PRECISION,
        confidence DOUBLE PRECISION,
        expected_move_pct DOUBLE PRECISION,
        direction TEXT,
        method TEXT,
        regime_blocked INTEGER DEFAULT 0,
        regime_notes TEXT,
        prob_15m_avg DOUBLE PRECISION,
        settlement_zone_low DOUBLE PRECISION,
        settlement_zone_high DOUBLE PRECISION,
        range_ml_ticker TEXT,
        range_ml_label TEXT,
        range_ml_prob DOUBLE PRECISION,
        range_ml_signal TEXT,
        range_ml_edge DOUBLE PRECISION,
        range_ml_floor DOUBLE PRECISION,
        range_ml_cap DOUBLE PRECISION,
        range_ml_kalshi_mid DOUBLE PRECISION,
        range_be_ticker TEXT,
        range_be_label TEXT,
        range_be_prob DOUBLE PRECISION,
        range_be_signal TEXT,
        range_be_edge DOUBLE PRECISION,
        range_be_floor DOUBLE PRECISION,
        range_be_cap DOUBLE PRECISION,
        range_be_kalshi_mid DOUBLE PRECISION,
        outcome INTEGER,
        settle_brti DOUBLE PRECISION,
        actual_return DOUBLE PRECISION,
        resolved_at TIMESTAMPTZ,
        asset TEXT NOT NULL DEFAULT 'btc'
      )
    """
    open_ddl = f"""
      CREATE TABLE IF NOT EXISTS hourly_open_snapshots (
        id SERIAL PRIMARY KEY,
        logged_at TIMESTAMPTZ NOT NULL,
        event_ticker TEXT NOT NULL,
        frequency TEXT NOT NULL,
        settle_time TIMESTAMPTZ NOT NULL,
        series_ticker TEXT,
        title TEXT,
        reference_price DOUBLE PRECISION NOT NULL,
        terminal_mu DOUBLE PRECISION,
        terminal_sigma DOUBLE PRECISION,
        ml_prob_up DOUBLE PRECISION,
        ml_mu DOUBLE PRECISION,
        structure_mu DOUBLE PRECISION,
        blended_mu DOUBLE PRECISION,
        hours_to_settle DOUBLE PRECISION,
        primary_ticker TEXT,
        primary_type TEXT,
        primary_label TEXT,
        primary_strike_type TEXT,
        primary_floor DOUBLE PRECISION,
        primary_cap DOUBLE PRECISION,
        primary_model_prob DOUBLE PRECISION,
        primary_kalshi_mid DOUBLE PRECISION,
        primary_edge DOUBLE PRECISION,
        primary_signal TEXT,
        most_likely_label TEXT,
        most_likely_prob DOUBLE PRECISION,
        confidence DOUBLE PRECISION,
        expected_move_pct DOUBLE PRECISION,
        direction TEXT,
        method TEXT,
        regime_blocked INTEGER DEFAULT 0,
        regime_notes TEXT,
        prob_15m_avg DOUBLE PRECISION,
        settlement_zone_low DOUBLE PRECISION,
        settlement_zone_high DOUBLE PRECISION,
        range_ml_ticker TEXT,
        range_ml_label TEXT,
        range_ml_prob DOUBLE PRECISION,
        range_ml_signal TEXT,
        range_ml_edge DOUBLE PRECISION,
        range_ml_floor DOUBLE PRECISION,
        range_ml_cap DOUBLE PRECISION,
        range_ml_kalshi_mid DOUBLE PRECISION,
        range_be_ticker TEXT,
        range_be_label TEXT,
        range_be_prob DOUBLE PRECISION,
        range_be_signal TEXT,
        range_be_edge DOUBLE PRECISION,
        range_be_floor DOUBLE PRECISION,
        range_be_cap DOUBLE PRECISION,
        range_be_kalshi_mid DOUBLE PRECISION,
        range_lean_bands TEXT,
        asset TEXT NOT NULL DEFAULT 'btc',
        UNIQUE(event_ticker, asset)
      )
    """
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute(ddl)
        cur.execute(open_ddl)
        cur.execute(
          "CREATE INDEX IF NOT EXISTS idx_hourly_settle ON hourly_predictions(settle_time)"
        )
        cur.execute(
          "CREATE INDEX IF NOT EXISTS idx_hourly_open_settle ON hourly_open_snapshots(settle_time)"
        )
        self._migrate_pg(cur)
      conn.commit()

  @staticmethod
  def _migrate_pg(cur) -> None:
    cur.execute("""
      SELECT column_name FROM information_schema.columns
      WHERE table_name = 'hourly_predictions'
    """)
    existing = {row[0] for row in cur.fetchall()}
    for col, typ in range_band_migrations_pg():
      if col not in existing:
        cur.execute(f"ALTER TABLE hourly_predictions ADD COLUMN {col} {typ}")
    for col, typ in late_call_migrations_pg():
      if col not in existing:
        cur.execute(f"ALTER TABLE hourly_predictions ADD COLUMN {col} {typ}")
    if "asset" not in existing:
      cur.execute("ALTER TABLE hourly_predictions ADD COLUMN asset TEXT NOT NULL DEFAULT 'btc'")
      cur.execute("UPDATE hourly_predictions SET asset = 'btc' WHERE asset IS NULL")

  def log_prediction(self, row: dict[str, Any], *, force: bool = False) -> int:
    row = {**row, "asset": row.get("asset") or self.asset}
    cols = [
      "logged_at", "event_ticker", "frequency", "settle_time", "series_ticker", "title",
      "reference_price", "terminal_mu", "terminal_sigma", "ml_prob_up", "ml_mu",
      "structure_mu", "blended_mu", "hours_to_settle", "primary_ticker", "primary_type",
      "primary_label", "primary_strike_type", "primary_floor", "primary_cap",
      "primary_model_prob", "primary_kalshi_mid", "primary_edge", "primary_signal",
      "most_likely_label", "most_likely_prob", "confidence", "expected_move_pct",
      "direction", "method", "regime_blocked", "regime_notes", "prob_15m_avg",
      "settlement_zone_low", "settlement_zone_high",
      *RANGE_BAND_LOG_FIELDS,
      "asset",
    ]
    vals = tuple(row.get(c) for c in cols)
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute(
          "SELECT id FROM hourly_predictions WHERE event_ticker = %s AND asset = %s",
          (row["event_ticker"], row["asset"]),
        )
        existing = cur.fetchone()
        if existing and not force:
          return int(existing[0])
        if existing:
          sets = ", ".join(f"{c}=%s" for c in cols if c not in ("event_ticker", "asset"))
          update_vals = tuple(row.get(c) for c in cols if c not in ("event_ticker", "asset")) + (
            row["event_ticker"],
            row["asset"],
          )
          cur.execute(
            f"UPDATE hourly_predictions SET {sets} WHERE event_ticker = %s AND asset = %s",
            update_vals,
          )
          conn.commit()
          return int(existing[0])
        cur.execute(
          f"INSERT INTO hourly_predictions ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
          vals,
        )
        rid = int(cur.fetchone()[0])
      conn.commit()
      return rid

  def get_by_event_ticker(self, event_ticker: str) -> dict[str, Any] | None:
    with self._conn() as conn:
      df = pd.read_sql(
        "SELECT * FROM hourly_predictions WHERE event_ticker = %s AND asset = %s LIMIT 1",
        conn,
        params=(event_ticker, self.asset),
      )
    if df.empty:
      return None
    return df.iloc[0].to_dict()

  def get_pending(self) -> list[dict[str, Any]]:
    with self._conn() as conn:
      df = pd.read_sql(
        "SELECT * FROM hourly_predictions WHERE outcome IS NULL AND asset = %s ORDER BY settle_time",
        conn,
        params=(self.asset,),
      )
    return df.to_dict(orient="records")

  def resolve(self, event_ticker: str, resolution: HourlyResolution) -> bool:
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute(
          """UPDATE hourly_predictions SET
               outcome=%s, settle_brti=%s, actual_return=%s, resolved_at=NOW()
             WHERE event_ticker=%s AND outcome IS NULL AND asset=%s""",
          (resolution.outcome, resolution.settle_brti, resolution.actual_return, event_ticker, self.asset),
        )
        ok = cur.rowcount > 0
      conn.commit()
    return ok

  def load_all(self) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        "SELECT * FROM hourly_predictions WHERE asset = %s ORDER BY logged_at",
        conn,
        params=(self.asset,),
      )

  def load_resolved(self) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        "SELECT * FROM hourly_predictions WHERE outcome IS NOT NULL AND asset = %s ORDER BY settle_time",
        conn,
        params=(self.asset,),
      )

  def load_recent(self, limit: int = 50) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        f"SELECT * FROM hourly_predictions WHERE asset = %s ORDER BY logged_at DESC LIMIT {int(limit)}",
        conn,
        params=(self.asset,),
      )

  def clear_all(self) -> int:
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM hourly_predictions WHERE asset = %s", (self.asset,))
        n = int(cur.fetchone()[0])
        cur.execute("DELETE FROM hourly_predictions WHERE asset = %s", (self.asset,))
      conn.commit()
    return n

  def log_open_snapshot(self, row: dict[str, Any]) -> int:
    row = {**row, "asset": row.get("asset") or self.asset}
    cols = _OPEN_LOG_COLS
    vals = tuple(row.get(c) for c in cols)
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute(
          "SELECT id FROM hourly_open_snapshots WHERE event_ticker = %s AND asset = %s",
          (row["event_ticker"], row["asset"]),
        )
        existing = cur.fetchone()
        if existing:
          sets = ", ".join(f"{c}=%s" for c in cols if c not in ("event_ticker", "asset"))
          update_vals = tuple(row.get(c) for c in cols if c not in ("event_ticker", "asset")) + (
            row["event_ticker"],
            row["asset"],
          )
          cur.execute(
            f"UPDATE hourly_open_snapshots SET {sets} WHERE event_ticker = %s AND asset = %s",
            update_vals,
          )
          conn.commit()
          return int(existing[0])
        cur.execute(
          f"INSERT INTO hourly_open_snapshots ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
          vals,
        )
        rid = int(cur.fetchone()[0])
      conn.commit()
      return rid

  def get_open_snapshot(self, event_ticker: str) -> dict[str, Any] | None:
    with self._conn() as conn:
      df = pd.read_sql(
        "SELECT * FROM hourly_open_snapshots WHERE event_ticker = %s AND asset = %s LIMIT 1",
        conn,
        params=(event_ticker, self.asset),
      )
    if df.empty:
      return None
    return df.iloc[0].to_dict()

  def clear_open_snapshots(self) -> int:
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM hourly_open_snapshots WHERE asset = %s", (self.asset,))
        n = int(cur.fetchone()[0])
        cur.execute("DELETE FROM hourly_open_snapshots WHERE asset = %s", (self.asset,))
      conn.commit()
    return n

  def log_late_call(self, row: dict[str, Any], *, force: bool = False) -> bool:
    event_ticker = row.get("event_ticker")
    if not event_ticker:
      return False
    asset = row.get("asset") or self.asset
    cols = list(LATE_CALL_LOG_FIELDS)
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute(
          "SELECT late_call_logged_at FROM hourly_predictions WHERE event_ticker = %s AND asset = %s",
          (event_ticker, asset),
        )
        existing = cur.fetchone()
        if not existing:
          log.warning("Late call skipped — no :05 row for %s (%s)", event_ticker, asset)
          return False
        if existing[0] and not force:
          return False
        sets = ", ".join(f"{c}=%s" for c in cols)
        vals = tuple(row.get(c) for c in cols) + (event_ticker, asset)
        cur.execute(
          f"UPDATE hourly_predictions SET {sets} WHERE event_ticker = %s AND asset = %s",
          vals,
        )
      conn.commit()
    return True


def create_hourly_store(cfg: dict[str, Any], *, asset: str = "btc") -> HourlyPredictionStore:
  db_url = os.getenv("DATABASE_URL") or cfg.get("database_url")
  if db_url:
    try:
      store = PostgresHourlyStore(db_url, asset=asset)
      store.init()
      log.info("Using PostgreSQL for hourly predictions (%s)", asset)
      return store
    except Exception as e:
      log.warning("PostgreSQL hourly store unavailable (%s), using SQLite", e)
  db_path = str(Path(cfg["paths"]["logs"]) / "hourly_predictions.db")
  store = SqliteHourlyStore(db_path, asset=asset)
  store.init()
  log.info("Using SQLite for hourly predictions at %s (%s)", db_path, asset)
  return store
