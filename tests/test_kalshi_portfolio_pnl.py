from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.trading.kalshi_portfolio_pnl import (
  build_daily_history,
  build_kalshi_portfolio_pnl_report,
  build_weekly_history,
  categorize_ticker,
  day_window_et,
  invalidate_kalshi_portfolio_pnl_cache,
  parse_stats_epoch,
  summarize_window,
  week_window_et,
)
from src.trading.kalshi_portfolio_pnl_store import KalshiPortfolioPnlStore

ET = ZoneInfo("America/New_York")


def test_categorize_ticker():
  assert categorize_ticker("KXBTC15M-26JUL131200") == "BTC 15m"
  assert categorize_ticker("KXETHD-26JUL1316-T1800") == "ETH hourly"
  assert categorize_ticker("KXMLBGAME-26JUL13BOSNYY") == "MLB sports"
  assert categorize_ticker("KXATP-FOO") == "Tennis (ATP)"
  assert categorize_ticker("KXWCGAME-26JUL13ARGSWIT-TIE") == "Other sports (WC tie)"


def test_day_window_et_boundaries():
  now = datetime(2026, 7, 13, 16, 30, tzinfo=ET)
  start, end, label = day_window_et(now)
  assert start == datetime(2026, 7, 13, 0, 1, 0, tzinfo=ET)
  assert end == datetime(2026, 7, 13, 23, 59, 59, tzinfo=ET)
  assert "Jul 13" in label


def test_week_window_et_starts_sunday():
  monday = datetime(2026, 7, 13, 10, 0, tzinfo=ET)
  start, end, label = week_window_et(monday)
  assert start == datetime(2026, 7, 12, 0, 1, 0, tzinfo=ET)
  assert end == datetime(2026, 7, 18, 23, 59, 59, tzinfo=ET)
  assert "Sun Jul 12" in label
  assert "Sat Jul 18" in label


def test_summarize_window_investment_and_rates():
  day_start = datetime(2026, 7, 13, 0, 1, 0, tzinfo=ET)
  day_end = datetime(2026, 7, 13, 23, 59, 59, tzinfo=ET)
  now_et = datetime(2026, 7, 13, 13, 0, tzinfo=ET)
  inside = datetime(2026, 7, 13, 12, 0, tzinfo=ET).astimezone(timezone.utc)
  outside = datetime(2026, 7, 12, 12, 0, tzinfo=ET).astimezone(timezone.utc)
  closed = [
    {
      "category": "BTC hourly",
      "pnl_usd": 2.0,
      "exit_at": inside,
      "cost_usd": 5.0,
    },
    {
      "category": "ETH hourly",
      "pnl_usd": -0.5,
      "exit_at": outside,
      "cost_usd": 3.0,
    },
  ]
  entries = [
    {"category": "BTC hourly", "cost_usd": 5.0, "bought_at": inside},
    {"category": "ETH hourly", "cost_usd": 3.0, "bought_at": outside},
    {"category": "MLB sports", "cost_usd": 2.0, "bought_at": inside},
  ]
  block = summarize_window(
    closed,
    entries,
    start_et=day_start,
    end_et=day_end,
    label="today",
    now_et=now_et,
  )
  assert block["closed_legs"] == 1
  assert block["entries"] == 2
  assert block["total_pnl_usd"] == 2.0
  assert block["invested_usd"] == 5.0
  assert block["entry_cost_usd"] == 5.0
  assert block["buy_volume_usd"] == 7.0
  assert block["pnl_per_leg_usd"] == 2.0
  assert block["roi_pct"] == round(100 * 2 / 5, 1)
  assert block["pnl_per_hour_usd"] is not None


def test_store_persists_and_epoch_filters(tmp_path):
  store = KalshiPortfolioPnlStore(tmp_path / "k.db")
  inside = datetime(2026, 7, 13, 12, 0, tzinfo=ET).astimezone(timezone.utc)
  outside = datetime(2026, 7, 10, 12, 0, tzinfo=ET).astimezone(timezone.utc)
  store.upsert_closed_legs([
    {
      "fingerprint": "a",
      "ticker": "KXBTC-1",
      "side": "yes",
      "category": "BTC hourly",
      "contracts": 2,
      "entry_cents": 40,
      "exit_cents": 60,
      "cost_usd": 0.8,
      "pnl_usd": 0.4,
      "buy_at": inside.isoformat(),
      "exit_at": inside.isoformat(),
      "exit_type": "SELL",
    },
    {
      "fingerprint": "b",
      "ticker": "KXETH-1",
      "side": "no",
      "category": "ETH hourly",
      "contracts": 1,
      "entry_cents": 50,
      "exit_cents": 20,
      "cost_usd": 0.5,
      "pnl_usd": -0.3,
      "buy_at": outside.isoformat(),
      "exit_at": outside.isoformat(),
      "exit_type": "SETTLEMENT",
    },
  ])
  store.upsert_entries([
    {
      "fingerprint": "e1",
      "order_id": "o1",
      "ticker": "KXBTC-1",
      "side": "yes",
      "category": "BTC hourly",
      "contracts": 2,
      "price_cents": 40,
      "cost_usd": 0.8,
      "bought_at": inside.isoformat(),
    },
  ])
  assert len(store.list_closed_legs()) == 2
  epoch = store.set_stats_epoch_now()
  assert parse_stats_epoch(epoch) is not None
  assert store.runtime()["clean_sheets"] == 1


def test_daily_history_uses_buy_volume_not_closed_stake():
  """Daily buys $ should reflect new spend that day, not cost basis of legs closed."""
  now_et = datetime(2026, 7, 13, 15, 0, tzinfo=ET)
  bought_prior = datetime(2026, 7, 12, 10, 0, tzinfo=ET).astimezone(timezone.utc)
  closed_today = datetime(2026, 7, 13, 10, 0, tzinfo=ET).astimezone(timezone.utc)
  bought_today = datetime(2026, 7, 13, 9, 0, tzinfo=ET).astimezone(timezone.utc)
  closed = [
    {"category": "BTC hourly", "pnl_usd": -0.5, "exit_at": closed_today, "cost_usd": 5.0},
  ]
  entries = [
    {"category": "BTC hourly", "cost_usd": 5.0, "bought_at": bought_prior},
    {"category": "BTC hourly", "cost_usd": 2.5, "bought_at": bought_today},
  ]
  days = build_daily_history(closed, entries, now_et=now_et)
  today = next(d for d in days if d["is_today"])
  assert today["entry_cost_usd"] == 5.0
  assert today["buy_volume_usd"] == 2.5


def test_daily_and_weekly_history():
  now_et = datetime(2026, 7, 13, 15, 0, tzinfo=ET)
  d1 = datetime(2026, 7, 13, 10, 0, tzinfo=ET).astimezone(timezone.utc)
  d0 = datetime(2026, 7, 12, 10, 0, tzinfo=ET).astimezone(timezone.utc)
  closed = [
    {"category": "BTC hourly", "pnl_usd": 1.0, "exit_at": d1, "cost_usd": 2.0},
    {"category": "BTC hourly", "pnl_usd": 0.5, "exit_at": d0, "cost_usd": 1.0},
  ]
  entries = [
    {"category": "BTC hourly", "cost_usd": 2.0, "bought_at": d1},
    {"category": "BTC hourly", "cost_usd": 1.0, "bought_at": d0},
  ]
  days = build_daily_history(closed, entries, now_et=now_et)
  assert len(days) == 2
  assert days[0]["is_today"] is True
  weeks = build_weekly_history(closed, entries, now_et=now_et)
  assert len(weeks) == 1
  assert weeks[0]["is_current_week"] is True


def test_settlement_net_pnl_matches_kalshi_ui():
  from src.trading.kalshi_portfolio_pnl import _settlement_net_pnl_usd

  row = {
    "market_result": "yes",
    "value": 100,
    "yes_count_fp": "4.00",
    "no_count_fp": "4.00",
    "yes_total_cost_dollars": "3.560000",
    "no_total_cost_dollars": "2.560000",
    "fee_cost": "0.092100",
  }
  assert _settlement_net_pnl_usd(row) == -2.21


def test_wallet_runway_kpi():
  from src.trading.kalshi_portfolio_pnl import TARGET_WEEKLY_USD, wallet_runway_kpi

  block = {
    "label": "Sun Jul 12 – Sat Jul 18",
    "total_pnl_usd": 100.0,
    "closed_legs": 10,
    "pnl_per_hour_usd": 4.0,
    "pnl_per_leg_usd": 10.0,
    "win_rate": 0.5,
  }
  kpi = wallet_runway_kpi(block)
  assert kpi["week_pnl_usd"] == 100.0
  assert kpi["gap_usd"] == TARGET_WEEKLY_USD - 100.0
  assert kpi["pnl_source"] == "kalshi_wallet"


def test_build_report_without_kalshi_auth(tmp_path):
  store = KalshiPortfolioPnlStore(tmp_path / "k.db")

  class FakeKalshi:
    authenticated = False

  report = build_kalshi_portfolio_pnl_report(FakeKalshi(), store=store)
  assert report["ok"] is False


def test_build_report_cached(monkeypatch, tmp_path):
  invalidate_kalshi_portfolio_pnl_cache()
  store = KalshiPortfolioPnlStore(tmp_path / "k.db")

  class FakeKalshi:
    authenticated = True

    def list_market_positions(self, **kwargs):
      return []

    def portfolio_balance(self, **kwargs):
      return {"balance": 10000}

    def list_fills(self, **kwargs):
      return []

    def list_settlements(self, **kwargs):
      return []

    def list_orders(self, **kwargs):
      return []

    @staticmethod
    def balance_cents_from_payload(bal):
      return int(bal.get("balance") or 0)

    @staticmethod
    def balance_usd_from_cents(cents):
      return round(cents / 100.0, 2)

  calls = {"n": 0}
  real_build = build_kalshi_portfolio_pnl_report

  def counting_build(kalshi, **kwargs):
    calls["n"] += 1
    return real_build(kalshi, store=store)

  monkeypatch.setattr(
    "src.trading.kalshi_portfolio_pnl.build_kalshi_portfolio_pnl_report",
    counting_build,
  )
  from src.trading.kalshi_portfolio_pnl import build_kalshi_portfolio_pnl_report_cached

  first = build_kalshi_portfolio_pnl_report_cached(FakeKalshi(), {"paths": {"logs": str(tmp_path)}}, store=store, ttl_sec=60.0)
  second = build_kalshi_portfolio_pnl_report_cached(FakeKalshi(), {"paths": {"logs": str(tmp_path)}}, store=store, ttl_sec=60.0)
  assert first["cached"] is False
  assert second["cached"] is True
  assert calls["n"] == 1
