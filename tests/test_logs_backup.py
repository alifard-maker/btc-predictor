"""Tests for log backup (paper vs live separation)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.backup.logs_backup import (
  BACKUP_ASSETS,
  HOURLY_BOT_KINDS_BY_ASSET,
  bot_db_specs,
  on_trade_logged,
  run_full_backup,
  volume_is_persistent,
)
from src.backup.trade_hook import notify_trade_logged, parse_bot_db_meta, should_skip_audit_trade


def _init_bot_db(path: Path) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  conn = sqlite3.connect(path)
  conn.executescript(
    """
    CREATE TABLE bot_trades (
      id TEXT PRIMARY KEY,
      event_ticker TEXT,
      trigger TEXT,
      action TEXT,
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
      status TEXT,
      detail TEXT,
      kalshi_order_id TEXT,
      position_id TEXT,
      entry_bid_cents INTEGER,
      entry_ask_cents INTEGER,
      entry_spread_cents INTEGER,
      created_at TEXT
    );
    """
  )
  conn.execute(
    """
    INSERT INTO bot_trades (
      id, event_ticker, trigger, action, mode, market_ticker, side,
      contracts, price_cents, cost_usd, status, created_at, kalshi_order_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
      "paper-1",
      "EVT",
      "continuous",
      "enter",
      "paper",
      "MKT-A",
      "yes",
      5,
      40,
      2.0,
      "filled",
      "2026-06-29T01:00:00+00:00",
      None,
    ),
  )
  conn.execute(
    """
    INSERT INTO bot_trades (
      id, event_ticker, trigger, action, mode, market_ticker, side,
      contracts, price_cents, cost_usd, status, created_at, kalshi_order_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
      "live-1",
      "EVT",
      "continuous",
      "enter",
      "live",
      "MKT-B",
      "no",
      3,
      55,
      1.65,
      "filled",
      "2026-06-29T02:00:00+00:00",
      "kalshi-order-99",
    ),
  )
  conn.commit()
  conn.close()


def test_bot_db_specs_cover_every_bot():
  cfg = {"paths": {"logs": "data/logs"}}
  specs = bot_db_specs(cfg)
  labels = {f"{asset}_{kind}" for asset, kind, _ in specs}
  for asset, kinds in HOURLY_BOT_KINDS_BY_ASSET.items():
    for kind in kinds:
      assert f"{asset}_{kind}" in labels
  assert "btc_slot15" in labels
  assert "eth_slot15" in labels
  assert "eth_slot15_trial" in labels
  assert set(BACKUP_ASSETS) == {"btc", "eth", "spx", "ndx"}


def test_parse_bot_db_meta_for_index_and_trial_bots():
  assert parse_bot_db_meta(Path("hourly_bot_spx.db")) == ("spx", "hourly")
  assert parse_bot_db_meta(Path("hourly_trial_bot_ndx.db")) == ("ndx", "hourly_trial")
  assert parse_bot_db_meta(Path("hourly_trial_mech_bot_btc.db")) == ("btc", "hourly_trial_mech")
  assert parse_bot_db_meta(Path("hourly_v2_bot_eth.db")) == ("eth", "hourly_v2")
  assert parse_bot_db_meta(Path("slot15_bot_btc.db")) == ("btc", "slot15")
  assert parse_bot_db_meta(Path("slot15_trial_bot_eth.db")) == ("eth", "slot15_trial")


def test_full_backup_exports_index_live_trades():
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    data = root / "data"
    spx_db = data / "spx" / "logs" / "hourly_bot_spx.db"
    _init_bot_db(spx_db)
    conn = sqlite3.connect(spx_db)
    conn.execute(
      """
      INSERT INTO bot_trades (
        id, event_ticker, trigger, action, mode, market_ticker, side,
        contracts, price_cents, cost_usd, status, created_at, kalshi_order_id
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        "spx-live-1",
        "KXINXU-26JUL061000",
        "continuous",
        "enter",
        "live",
        "KXINXU-26JUL061000-T7619.9999",
        "yes",
        2,
        45,
        0.9,
        "filled",
        "2026-07-06T14:00:00+00:00",
        "kalshi-spx-1",
      ),
    )
    conn.commit()
    conn.close()
    cfg = {
      "paths": {"logs": str(data / "logs")},
      "log_backup": {"enabled": True, "backup_dir": str(root / "backups")},
    }
    manifest = run_full_backup(cfg, reason="test")
    live_csv = root / "backups" / "live" / "spx_hourly" / "trades.csv"
    assert live_csv.exists()
    assert "kalshi-spx-1" in live_csv.read_text(encoding="utf-8")
    assert manifest.get("live", {}).get("total_trades", 0) >= 1


def test_full_backup_separates_paper_and_live():
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    data = root / "data"
    logs = data / "logs"
    db = logs / "hourly_bot_btc.db"
    _init_bot_db(db)
    cfg = {
      "paths": {"logs": str(logs)},
      "log_backup": {"enabled": True, "backup_dir": str(root / "backups")},
    }
    manifest = run_full_backup(cfg, reason="test")
    assert manifest.get("paper", {}).get("total_trades") == 1
    assert manifest.get("live", {}).get("total_trades") == 1
    paper_csv = root / "backups" / "paper" / "all_trades.csv"
    live_csv = root / "backups" / "live" / "all_trades.csv"
    assert paper_csv.exists()
    assert live_csv.exists()
    paper_text = paper_csv.read_text(encoding="utf-8")
    live_text = live_csv.read_text(encoding="utf-8")
    assert "paper-1" in paper_text or "MKT-A" in paper_text
    assert "live-1" in live_text or "kalshi-order-99" in live_text
    assert "MKT-B" not in paper_text
    assert "MKT-A" not in live_text


def test_on_trade_logged_appends_audit_jsonl():
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    cfg = {
      "paths": {"logs": str(root / "logs")},
      "log_backup": {"enabled": True, "backup_dir": str(root / "backups")},
    }
    trade = {
      "id": "t-99",
      "event_ticker": "E",
      "action": "enter",
      "mode": "live",
      "market_ticker": "M",
      "side": "yes",
      "contracts": 1,
      "price_cents": 50,
      "status": "filled",
      "created_at": "2026-06-29T03:00:00+00:00",
      "kalshi_order_id": "ord-1",
    }
    on_trade_logged(cfg, kind="hourly", asset="btc", trade=trade)
    audit = root / "backups" / "live" / "audit_trades.jsonl"
    assert audit.exists()
    lines = [json.loads(ln) for ln in audit.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["trade"]["kalshi_order_id"] == "ord-1"
    on_trade_logged(cfg, kind="hourly", asset="btc", trade=trade)
    lines2 = [json.loads(ln) for ln in audit.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines2) == 1


def test_should_skip_audit_trade_for_pytest_fixtures():
  assert should_skip_audit_trade(
    Path("/tmp/pytest-123/hourly_bot_btc.db"),
    {"event_ticker": "EV1", "market_ticker": "M"},
  )
  assert should_skip_audit_trade(
    Path("/data/logs/hourly_bot_btc.db"),
    {"event_ticker": "EV1", "market_ticker": "KXBTC15M-OLD"},
  )
  assert not should_skip_audit_trade(
    Path("/data/logs/hourly_bot_eth.db"),
    {"event_ticker": "KXETH-26JUN291200", "market_ticker": "KXETH-26JUN291200-T1610"},
  )


def test_notify_trade_hook_from_db_path():
  with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    logs = root / "logs"
    db = logs / "slot15_bot_eth.db"
    _init_bot_db(db)
    cfg = {
      "paths": {"logs": str(logs)},
      "log_backup": {"enabled": True, "backup_dir": str(root / "backups")},
    }
    import src.backup.trade_hook as hook

    hook._CFG = cfg
    notify_trade_logged(
      db,
      trade={
        "id": "hook-1",
        "event_ticker": "S",
        "action": "exit",
        "mode": "paper",
        "market_ticker": "M",
        "side": "yes",
        "contracts": 2,
        "price_cents": 60,
        "pnl_usd": 0.5,
        "status": "filled",
        "created_at": "2026-06-29T04:00:00+00:00",
      },
    )
    audit = root / "backups" / "paper" / "audit_trades.jsonl"
    assert audit.exists()


def test_volume_is_persistent_requires_railway_env(monkeypatch):
  monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
  monkeypatch.delenv("RAILWAY_VOLUME_NAME", raising=False)
  monkeypatch.delenv("RAILWAY_VOLUME_ID", raising=False)
  assert volume_is_persistent("/data") is False
  monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")
  assert volume_is_persistent("/data") is True
