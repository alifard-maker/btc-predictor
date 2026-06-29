"""Tests for historical NO exit P&L backfill."""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.trading.bot_pnl_backfill import (
  backfill_bot_db,
  correct_no_exit_pnl_usd,
  is_inverted_no_exit,
  wrong_no_exit_pnl_usd,
)
from src.trading.hourly_bot_store import HourlyBotSettings, HourlyBotStore
from src.trading.paper_bankroll import apply_paper_exit_pnl, get_paper_state, reset_paper_bankroll


def _insert_no_exit(
  store: HourlyBotStore,
  *,
  entry_cents: int,
  exit_cents: int,
  contracts: int,
  pnl_usd: float,
) -> None:
  now = datetime.now(timezone.utc).isoformat()
  with store._connect() as conn:
    conn.execute(
      """
      INSERT INTO bot_trades (
        id, event_ticker, trigger, action, mode, market_ticker, side, contracts,
        price_cents, entry_price_cents, exit_price_cents, cost_usd, pnl_usd,
        signal, label, actionable_headline, status, detail, kalshi_order_id,
        position_id, entry_bid_cents, entry_ask_cents, entry_spread_cents,
        entry_settings_json, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        str(uuid.uuid4()),
        "KXTEST-1H",
        "continuous",
        "exit",
        "paper",
        "MKT-1",
        "no",
        contracts,
        exit_cents,
        entry_cents,
        exit_cents,
        0.0,
        pnl_usd,
        None,
        None,
        None,
        "filled",
        "test exit",
        None,
        None,
        None,
        None,
        None,
        None,
        now,
      ),
    )


def test_detect_inverted_no_exit():
  entry, exit_c, contracts = 68, 80, 36
  wrong = wrong_no_exit_pnl_usd(
    entry_price_cents=entry,
    exit_price_cents=exit_c,
    contracts=contracts,
  )
  correct = correct_no_exit_pnl_usd(
    entry_price_cents=entry,
    exit_price_cents=exit_c,
    contracts=contracts,
  )
  assert wrong == -4.32
  assert correct == 4.32
  row = {
    "side": "no",
    "action": "exit",
    "status": "filled",
    "entry_price_cents": entry,
    "exit_price_cents": exit_c,
    "contracts": contracts,
    "pnl_usd": wrong,
  }
  assert is_inverted_no_exit(row)
  row["pnl_usd"] = correct
  assert not is_inverted_no_exit(row)


def test_backfill_corrects_pnl_and_paper_bankroll_idempotent():
  entry, exit_c, contracts = 68, 80, 36
  wrong = wrong_no_exit_pnl_usd(
    entry_price_cents=entry,
    exit_price_cents=exit_c,
    contracts=contracts,
  )
  correct = correct_no_exit_pnl_usd(
    entry_price_cents=entry,
    exit_price_cents=exit_c,
    contracts=contracts,
  )

  with tempfile.TemporaryDirectory() as tmp:
    db_path = Path(tmp) / "hourly_bot_btc.db"
    store = HourlyBotStore(db_path)
    store.save_settings(HourlyBotSettings(mode="paper", max_spend_per_hour_usd=25.0))
    with store._connect() as conn:
      reset_paper_bankroll(conn, 25.0)
      apply_paper_exit_pnl(conn, wrong, 25.0)
    _insert_no_exit(
      store,
      entry_cents=entry,
      exit_cents=exit_c,
      contracts=contracts,
      pnl_usd=wrong,
    )

    first = backfill_bot_db(db_path, dry_run=False, data_dir=Path(tmp))
    assert first["fixed"] == 1
    assert first["paper_pnl_delta_usd"] == round(correct - wrong, 2)

    with store._connect() as conn:
      row = conn.execute(
        "SELECT pnl_usd FROM bot_trades WHERE action = 'exit' AND side = 'no'",
      ).fetchone()
      paper = get_paper_state(conn)
    assert float(row[0]) == correct
    assert paper is not None
    assert paper.paper_bankroll_usd == round(25.0 + correct, 2)
    assert paper.paper_realized_all_time_usd == correct

    second = backfill_bot_db(db_path, dry_run=False, data_dir=Path(tmp))
    assert second["fixed"] == 0
    assert second["paper_pnl_delta_usd"] == 0.0
