from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pandas as pd


def normalize_database_url(url: str) -> str:
  """Railway/Heroku may provide postgres:// — psycopg2 expects postgresql://."""
  if url.startswith("postgres://"):
    return url.replace("postgres://", "postgresql://", 1)
  return url


def _strip_sslmode(url: str) -> str:
  """Remove sslmode from URL; we pass sslmode via connect_kwargs instead."""
  parsed = urlparse(url)
  if not parsed.query:
    return url
  params = parse_qs(parsed.query, keep_blank_values=True)
  params.pop("sslmode", None)
  new_query = urlencode({k: v[0] for k, v in params.items()})
  return urlunparse(parsed._replace(query=new_query))


def _py(val: Any) -> Any:
  """Cast numpy scalars to native Python types for Postgres."""
  if hasattr(val, "item"):
    return val.item()
  return val


class PredictionStore(ABC):
  @abstractmethod
  def init(self) -> None: ...

  @abstractmethod
  def log_prediction(
    self, timestamp: str, price: float, prob_up: float, prob_down: float,
    confidence: float, signal: str, expected_move: float,
  ) -> int: ...

  @abstractmethod
  def get_pending(self) -> list[tuple[int, str, float]]: ...

  @abstractmethod
  def resolve_with_prices(self, price_lookup: dict[str, tuple[float, float]]) -> int: ...

  @abstractmethod
  def load_resolved(self) -> pd.DataFrame: ...

  @abstractmethod
  def load_recent(self, limit: int = 50) -> pd.DataFrame: ...

  @abstractmethod
  def latest(self) -> dict[str, Any] | None: ...


class SqlitePredictionStore(PredictionStore):
  def __init__(self, db_path: str):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)

  def _conn(self):
    import sqlite3
    return sqlite3.connect(self.db_path)

  def init(self) -> None:
    with self._conn() as conn:
      conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          timestamp TEXT NOT NULL,
          price REAL NOT NULL,
          prob_up REAL NOT NULL,
          prob_down REAL NOT NULL,
          confidence REAL NOT NULL,
          signal TEXT NOT NULL,
          expected_move REAL,
          outcome INTEGER,
          actual_return REAL,
          resolved_at TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
      """)
      conn.execute("CREATE INDEX IF NOT EXISTS idx_pred_ts ON predictions(timestamp)")
      conn.execute("CREATE INDEX IF NOT EXISTS idx_pred_outcome ON predictions(outcome)")

  def log_prediction(self, timestamp, price, prob_up, prob_down, confidence, signal, expected_move) -> int:
    price, prob_up, prob_down, confidence, expected_move = map(
      _py, (price, prob_up, prob_down, confidence, expected_move)
    )
    ts = pd.Timestamp(timestamp).isoformat()
    with self._conn() as conn:
      existing = conn.execute(
        "SELECT id FROM predictions WHERE timestamp = ? LIMIT 1",
        (ts,),
      ).fetchone()
      if existing:
        return existing[0]
      cur = conn.execute(
        """INSERT INTO predictions
           (timestamp, price, prob_up, prob_down, confidence, signal, expected_move)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ts, price, prob_up, prob_down, confidence, signal, expected_move),
      )
      return cur.lastrowid

  def get_pending(self) -> list[tuple[int, str, float]]:
    with self._conn() as conn:
      return conn.execute(
        "SELECT id, timestamp, price FROM predictions WHERE outcome IS NULL ORDER BY timestamp"
      ).fetchall()

  def resolve_with_prices(self, price_lookup: dict[str, tuple[float, float]]) -> int:
    count = 0
    with self._conn() as conn:
      for ts, (_, actual_return) in price_lookup.items():
        outcome = 1 if actual_return > 0 else 0
        conn.execute(
          """UPDATE predictions SET outcome=?, actual_return=?, resolved_at=datetime('now')
             WHERE timestamp=? AND outcome IS NULL""",
          (outcome, actual_return, ts),
        )
        count += conn.total_changes
    return count

  def load_resolved(self) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        "SELECT * FROM predictions WHERE outcome IS NOT NULL ORDER BY timestamp", conn
      )

  def load_recent(self, limit: int = 50) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        f"SELECT * FROM predictions ORDER BY created_at DESC LIMIT {int(limit)}", conn
      )

  def latest(self) -> dict[str, Any] | None:
    df = self.load_recent(1)
    if df.empty:
      return None
    return df.iloc[0].to_dict()


class PostgresPredictionStore(PredictionStore):
  def __init__(self, database_url: str):
    import psycopg2
    self.database_url = _strip_sslmode(normalize_database_url(database_url))
    self._psycopg2 = psycopg2

  def _conn(self):
    return self._psycopg2.connect(self.database_url, sslmode="require")

  def init(self) -> None:
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          CREATE TABLE IF NOT EXISTS predictions (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ NOT NULL,
            price DOUBLE PRECISION NOT NULL,
            prob_up DOUBLE PRECISION NOT NULL,
            prob_down DOUBLE PRECISION NOT NULL,
            confidence DOUBLE PRECISION NOT NULL,
            signal TEXT NOT NULL,
            expected_move DOUBLE PRECISION,
            outcome INTEGER,
            actual_return DOUBLE PRECISION,
            resolved_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW()
          )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pred_ts ON predictions(timestamp)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pred_outcome ON predictions(outcome)")
      conn.commit()

  def log_prediction(self, timestamp, price, prob_up, prob_down, confidence, signal, expected_move) -> int:
    price, prob_up, prob_down, confidence, expected_move = map(
      _py, (price, prob_up, prob_down, confidence, expected_move)
    )
    ts = pd.Timestamp(timestamp).isoformat()
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute(
          "SELECT id FROM predictions WHERE timestamp = %s::timestamptz LIMIT 1",
          (ts,),
        )
        row = cur.fetchone()
        if row:
          return row[0]
        cur.execute(
          """INSERT INTO predictions
             (timestamp, price, prob_up, prob_down, confidence, signal, expected_move)
             VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
          (ts, price, prob_up, prob_down, confidence, signal, expected_move),
        )
        row_id = cur.fetchone()[0]
      conn.commit()
      return row_id

  def get_pending(self) -> list[tuple[int, str, float]]:
    with self._conn() as conn:
      with conn.cursor() as cur:
        cur.execute(
          "SELECT id, timestamp, price FROM predictions WHERE outcome IS NULL ORDER BY timestamp"
        )
        rows = cur.fetchall()
    return [(r[0], r[1].isoformat(), r[2]) for r in rows]

  def resolve_with_prices(self, price_lookup: dict[str, tuple[float, float]]) -> int:
    count = 0
    with self._conn() as conn:
      with conn.cursor() as cur:
        for ts, (_, actual_return) in price_lookup.items():
          outcome = 1 if actual_return > 0 else 0
          cur.execute(
            """UPDATE predictions SET outcome=%s, actual_return=%s, resolved_at=NOW()
               WHERE timestamp=%s AND outcome IS NULL""",
            (outcome, actual_return, ts),
          )
          count += cur.rowcount
      conn.commit()
    return count

  def load_resolved(self) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        "SELECT * FROM predictions WHERE outcome IS NOT NULL ORDER BY timestamp", conn
      )

  def load_recent(self, limit: int = 50) -> pd.DataFrame:
    with self._conn() as conn:
      return pd.read_sql(
        f"SELECT * FROM predictions ORDER BY created_at DESC LIMIT {int(limit)}", conn
      )

  def latest(self) -> dict[str, Any] | None:
    df = self.load_recent(1)
    if df.empty:
      return None
    row = df.iloc[0].to_dict()
    for k, v in row.items():
      if hasattr(v, "isoformat"):
        row[k] = v.isoformat()
    return row


def create_prediction_store(cfg: dict[str, Any]) -> PredictionStore:
  log = logging.getLogger(__name__)
  db_url = os.getenv("DATABASE_URL") or cfg.get("database_url")
  if db_url:
    try:
      store = PostgresPredictionStore(db_url)
      store.init()
      log.info("Using PostgreSQL for predictions")
      return store
    except Exception as e:
      log.warning("PostgreSQL unavailable (%s), falling back to SQLite", e)
  store = SqlitePredictionStore(cfg["paths"]["db"])
  store.init()
  log.info("Using SQLite for predictions at %s", cfg["paths"]["db"])
  return store
