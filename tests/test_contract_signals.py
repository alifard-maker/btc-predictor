"""Contract signal helpers."""

from src.trading.contract_signals import (
  BUY_NO,
  BUY_YES,
  is_buy_no,
  is_buy_yes,
  primary_pick_correct,
  signal_correct_for_outcome,
)


def test_buy_yes_no_labels():
  assert is_buy_yes(BUY_YES)
  assert is_buy_no(BUY_NO)
  assert is_buy_yes("LEAN YES")
  assert is_buy_no("LEAN NO")


def test_signal_correct_band_outcome():
  assert signal_correct_for_outcome(BUY_YES, 1, 0.6)
  assert not signal_correct_for_outcome(BUY_YES, 0, 0.6)
  assert signal_correct_for_outcome(BUY_NO, 0, 0.1)
  assert not signal_correct_for_outcome(BUY_NO, 1, 0.1)
  assert signal_correct_for_outcome("LEAN YES", 1, 0.6)


def test_primary_pick_correct():
  assert primary_pick_correct(BUY_YES, 1, 0.6)
  assert not primary_pick_correct(BUY_NO, 1, 0.4)
  assert primary_pick_correct("LEAN NO", 0, 0.3)
