"""Audit auto-tune direction vs Kalshi wallet week P&L."""

from src.trading.bot_auto_tuning import audit_tuning_vs_kalshi_wallet


def test_audit_warns_loosening_while_wallet_negative():
  proposal = {
    "total_pnl_usd": 5.0,
    "changes": ["Lowered min_ask_edge to 12¢ (win rate 58%, profitable)."],
  }
  wallet = {"ok": True, "week_pnl_usd": -3.5}
  audit = audit_tuning_vs_kalshi_wallet(proposal, wallet)
  assert audit["loosening"] is True
  assert audit["warning"] is not None
  assert "loosening" in audit["warning"].lower()


def test_audit_warns_tightening_on_stale_bot_log_while_wallet_positive():
  proposal = {
    "total_pnl_usd": -4.0,
    "changes": ["Raised min_ask_edge to 16¢ (win rate 42%)."],
  }
  wallet = {"ok": True, "week_pnl_usd": 8.0}
  audit = audit_tuning_vs_kalshi_wallet(proposal, wallet)
  assert audit["tightening"] is True
  assert audit["warning"] is not None
  assert "stale" in audit["warning"].lower() or "bot log" in audit["warning"].lower()


def test_audit_aligned_tightening_while_wallet_negative():
  proposal = {
    "total_pnl_usd": -6.0,
    "changes": ["Raised min_ask_edge to 16¢ (win rate 42%)."],
  }
  wallet = {"ok": True, "week_pnl_usd": -5.0}
  audit = audit_tuning_vs_kalshi_wallet(proposal, wallet)
  assert audit["tightening"] is True
  assert audit["aligned"] is True
  assert audit["warning"] is None
