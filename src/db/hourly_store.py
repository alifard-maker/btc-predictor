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
  def log_prediction(self, row: dict[str, Any]) -> int: ...

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
  outcome INTEGER,
  settle_brti REAL,
  actual_return REAL,
  resolved_at TEXT
"""

_HOURLY_COLS_PG = _HOURLY_COLS.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY").replace(
  "INTEGER DEFAULT 0", "INTEGER DEFAULT 0"
)


class SqliteHourlyStore(HourlyPredictionStore):
  def __init__(self, db_path: str):
    self.db_path = Path(db_path)

  def _conn(self):
    import sqlite3
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(self.db_path)

  def init(self) -> None:
    with self._conn() as conn:
      conn.execute(f"CREATE TABLE IF NOT EXISTS hourly_predictions ({_HOURLY_COLS})")
      conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hourly_settle ON hourly_predictions(settle_time)"
      )

  def log_prediction(self, row: dict[str, Any]) -> int:
    cols = [
      "logged_at", "event_ticker", "frequency", "settle_time", "series_ticker", "title",
      "reference_price", "terminal_mu", "terminal_sigma", "ml_prob_up", "ml_mu",
      "structure_mu", "blended_mu", "hours_to_settle", "primary_ticker", "primary_type",
      "primary_label", "primary_strike_type", "primary_floor", "primary_cap",
      "primary_model_prob", "primary_kalshi_mid", "primary_edge", "primary_signal",
      "most_likely_label", "most_likely_prob", "confidence", "expected_move_pct",
      "direction", "method", "regime_blocked", "regime_notes", "prob_15m_avg",
    ]
    vals = [row.get(c) for c in cols]
    with self._conn() as conn:
      existing = conn.execute(
        "SELECT id FROM hourly_predictions WHERE event_ticker = ?",
        (row["event_ticker"],),
      ).fetchone()
      if existing:
        placeholders = ", ".join(f"{c}=?" for c in cols if c != "event_ticker")
        update_vals = [row.get(c) for c in cols if c != "event_ticker"] + [row["event_ticker"]]
        conn.execute(
          f"UPDATE hourly_predictions SET {placeholders} WHERE event_ticker = ?",
          update_vals,
        )
        return int(existing[0])
      cur = conn.execute(
        f"INSERT INTO hourly_predictions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        vals,
      )
      return int(cur.lastrowid)

  def get_pending(self) -> list[dict[str, Any]]:
    with self._conn() as conn:
      conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
      rows = conn.execute(
        "SELECT * FROM hourly_predictions WHERE outcome IS NULL ORDER BY settle_time"
      ).fetchall()
      return list(rows)

  def resolve(self, event_ticker: str, resolution: HourlyResolution) -> bool:
    with self._conn() as conn:
      cur = conn.execute(
        """UPDATE hourly_predictions SET
             outcome=?, settle_brti=?, actual_return=?, resolved_at=datetime('now')
           WHERE event_ticker=? AND outcome IS NULL""",
        (resolution.outcome, resolution.settle_brti, resolution.actual_return, event_ticker),
      )
      return cur.rowcount > 0

  def load_all(self) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql("SELECT * FROM hourly_predictions ORDER BY logged_at", conn)

  def load_resolved(self) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        "SELECT * FROM hourly_predictions WHERE outcome IS NOT NULL ORDER BY settle_time", conn
      )

  def load_recent(self, limit: int = 50) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        f"SELECT * FROM hourly_predictions ORDER BY logged_at DESC LIMIT {int(limit)}", conn
      )

  def clear_all(self) -> int:
    with self._conn() as conn:
      n = int(conn.execute("SELECT COUNT(*) FROM hourly_predictions").fetchone()[0])
      conn.execute("DELETE FROM hourly_predictions")
    return n


def _strip_sslmode(url: str) -> str:
  parsed = urlparse(url)
  if not parsed.query:
    return url
  params = parse_qs(parsed.query, keep_blank_values=True)
  params.pop("sslmode", None)
  new_query = urlencode({k: v[0] for k, v in params.items()})
  return urlunparse(parsed._replace(query=new_query))


class PostgresHourlyStore(HourlyPredictionStore):
  def __init__(self, database_url: str):
    import psycopg2
    self.database_url = _strip_sslmode(normalize_database_url(database_url))
    self._psycopg2 = psycopg2

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
        outcome INTEGER,
        settle_brti DOUBLE PRECISION,
        actual_return DOUBLE PRECISION,
        resolved_at TIMESTAMPTZ
      )
    """
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute(ddl)
        cur.execute(
          "CREATE INDEX IF NOT EXISTS idx_hourly_settle ON hourly_predictions(settle_time)"
        )
      conn.commit()

  def log_prediction(self, row: dict[str, Any]) -> int:
    cols = [
      "logged_at", "event_ticker", "frequency", "settle_time", "series_ticker", "title",
      "reference_price", "terminal_mu", "terminal_sigma", "ml_prob_up", "ml_mu",
      "structure_mu", "blended_mu", "hours_to_settle", "primary_ticker", "primary_type",
      "primary_label", "primary_strike_type", "primary_floor", "primary_cap",
      "primary_model_prob", "primary_kalshi_mid", "primary_edge", "primary_signal",
      "most_likely_label", "most_likely_prob", "confidence", "expected_move_pct",
      "direction", "method", "regime_blocked", "regime_notes", "prob_15m_avg",
    ]
    vals = tuple(row.get(c) for c in cols)
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute("SELECT id FROM hourly_predictions WHERE event_ticker = %s", (row["event_ticker"],))
        existing = cur.fetchone()
        if existing:
          sets = ", ".join(f"{c}=%s" for c in cols if c != "event_ticker")
          update_vals = tuple(row.get(c) for c in cols if c != "event_ticker") + (row["event_ticker"],)
          cur.execute(f"UPDATE hourly_predictions SET {sets} WHERE event_ticker = %s", update_vals)
          conn.commit()
          return int(existing[0])
        cur.execute(
          f"INSERT INTO hourly_predictions ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
          vals,
        )
        rid = int(cur.fetchone()[0])
      conn.commit()
      return rid

  def get_pending(self) -> list[dict[str, Any]]:
    with self._conn() as conn:
      df = pd.read_sql(
        "SELECT * FROM hourly_predictions WHERE outcome IS NULL ORDER BY settle_time", conn
      )
    return df.to_dict(orient="records")

  def resolve(self, event_ticker: str, resolution: HourlyResolution) -> bool:
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute(
          """UPDATE hourly_predictions SET
               outcome=%s, settle_brti=%s, actual_return=%s, resolved_at=NOW()
             WHERE event_ticker=%s AND outcome IS NULL""",
          (resolution.outcome, resolution.settle_brti, resolution.actual_return, event_ticker),
        )
        ok = cur.rowcount > 0
      conn.commit()
    return ok

  def load_all(self) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql("SELECT * FROM hourly_predictions ORDER BY logged_at", conn)

  def load_resolved(self) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        "SELECT * FROM hourly_predictions WHERE outcome IS NOT NULL ORDER BY settle_time", conn
      )

  def load_recent(self, limit: int = 50) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        f"SELECT * FROM hourly_predictions ORDER BY logged_at DESC LIMIT {int(limit)}", conn
      )

  def clear_all(self) -> int:
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM hourly_predictions")
        n = int(cur.fetchone()[0])
        cur.execute("DELETE FROM hourly_predictions")
      conn.commit()
    return n


def create_hourly_store(cfg: dict[str, Any]) -> HourlyPredictionStore:
  db_url = os.getenv("DATABASE_URL") or cfg.get("database_url")
  if db_url:
    try:
      store = PostgresHourlyStore(db_url)
      store.init()
      log.info("Using PostgreSQL for hourly predictions")
      return store
    except Exception as e:
      log.warning("PostgreSQL hourly store unavailable (%s), using SQLite", e)
  db_path = str(Path(cfg["paths"]["logs"]) / "hourly_predictions.db")
  store = SqliteHourlyStore(db_path)
  store.init()
  log.info("Using SQLite for hourly predictions at %s", db_path)
  return store
