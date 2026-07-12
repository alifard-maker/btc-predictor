"""Unit tests for Goal-2 sports Dutch / cover arb engine."""

from datetime import datetime, timezone

from src.data.sports_markets import SportsEventBook, SportsMarketQuote
from src.trading.sports_arb_engine import (
  _size_equal_cover_contracts,
  execution_ask_decimal,
  find_binary_yes_no_arb,
  find_multi_outcome_dutch,
  is_prop_like_market,
  kalshi_taker_fee_usd,
  scan_dutch_same_opportunities,
  select_exclusive_outcome_markets,
  validate_dutch_cover_opportunity,
)


def _m(**kwargs) -> SportsMarketQuote:
  base = dict(
    ticker="M1",
    event_ticker="EVT1",
    series_ticker="KXTEST",
    title="Test",
    subtitle="",
    yes_bid=0.40,
    yes_ask=0.45,
    no_bid=0.50,
    no_ask=0.55,
    close_time=datetime(2026, 7, 11, tzinfo=timezone.utc),
    status="open",
  )
  base.update(kwargs)
  return SportsMarketQuote(**base)


def test_kalshi_taker_fee_peaks_at_mid():
  mid = kalshi_taker_fee_usd(0.5, 1, 0.07)
  low = kalshi_taker_fee_usd(0.1, 1, 0.07)
  assert mid > low
  assert abs(mid - 0.0175) < 1e-9


def test_binary_yes_no_detects_cover():
  # yes_ask + no_ask = 0.90 → clear edge even after fees
  m = _m(yes_ask=0.40, no_ask=0.50, yes_bid=0.48)
  opp = find_binary_yes_no_arb(m, fee_rate=0.07, min_edge_usd=0.01, max_stake_usd=5.0)
  assert opp is not None
  assert opp.kind == "binary_yes_no"
  assert opp.edge_usd > 0
  assert len(opp.legs) == 2


def test_binary_yes_no_rejects_no_edge():
  m = _m(yes_ask=0.52, no_ask=0.52, yes_bid=0.48)
  opp = find_binary_yes_no_arb(m, fee_rate=0.07, min_edge_usd=0.01, max_stake_usd=5.0)
  assert opp is None


def test_binary_uses_effective_no_ask_from_yes_bid():
  m = _m(yes_ask=0.40, no_ask=None, yes_bid=0.55)  # effective no_ask = 0.45
  opp = find_binary_yes_no_arb(m, fee_rate=0.0, min_edge_usd=0.01, max_stake_usd=5.0)
  assert opp is not None
  assert abs(opp.legs[1].ask - 0.45) < 1e-9


def test_multi_outcome_dutch_tight_partition():
  # Near-complete 3-way book: sum asks = 0.96 → ~4¢ edge
  markets = [
    _m(ticker="EVT1-A", yes_ask=0.31, title="Team A"),
    _m(ticker="EVT1-B", yes_ask=0.32, title="Team B"),
    _m(ticker="EVT1-C", yes_ask=0.33, title="Team C"),
  ]
  book = SportsEventBook("EVT1", "KXTEST", "Match", markets[0].close_time, markets)
  opp = find_multi_outcome_dutch(book, fee_rate=0.0, min_edge_usd=0.01, max_stake_usd=5.0)
  assert opp is not None
  assert opp.kind == "multi_outcome"
  assert len(opp.legs) == 3
  # unit edge ≈ 4¢; scaled edge depends on contracts
  assert abs((opp.edge_usd / opp.payout_usd) - 0.04) < 1e-6
  assert (opp.edge_usd / opp.payout_usd) <= 0.06


def test_multi_rejects_prop_soup():
  markets = [
    _m(ticker="EVT1-A", yes_ask=0.20, title="Team A"),
    _m(ticker="EVT1-B", yes_ask=0.20, title="Team B"),
    _m(ticker="EVT1-OU", yes_ask=0.20, title="O/U 2.5"),
    _m(ticker="EVT1-SP", yes_ask=0.20, title="Spread: Team A (-1.5)"),
  ]
  book = SportsEventBook("EVT1", "KXTEST", "Match", markets[0].close_time, markets)
  # After dropping props, only 2 markets remain with ask sum 0.40 < min_ask_sum
  assert select_exclusive_outcome_markets(book) is None
  assert find_multi_outcome_dutch(book, fee_rate=0.0, min_edge_usd=0.01, max_stake_usd=5.0) is None


def test_multi_rejects_incomplete_ask_sum():
  # Looks like winners but book is incomplete (sum asks too low) — fake huge edge
  markets = [
    _m(ticker="EVT1-A", yes_ask=0.10, title="Player A"),
    _m(ticker="EVT1-B", yes_ask=0.10, title="Player B"),
    _m(ticker="EVT1-C", yes_ask=0.10, title="Player C"),
  ]
  book = SportsEventBook("EVT1", "KXTEST", "Tournament", markets[0].close_time, markets)
  assert find_multi_outcome_dutch(book, fee_rate=0.0, min_edge_usd=0.01, max_stake_usd=5.0) is None


def test_multi_rejects_absurd_edge():
  markets = [
    _m(ticker="EVT1-A", yes_ask=0.40, title="A"),
    _m(ticker="EVT1-B", yes_ask=0.40, title="B"),
  ]
  # sum=0.80 passes min_ask_sum but edge=0.20 > max_edge_prob 0.06
  book = SportsEventBook("EVT1", "KXTEST", "Match", markets[0].close_time, markets)
  assert find_multi_outcome_dutch(
    book, fee_rate=0.0, min_edge_usd=0.01, max_stake_usd=5.0, max_edge_prob=0.06
  ) is None


def test_is_prop_like():
  assert is_prop_like_market(_m(title="Spread: PHI (-1.5)"))
  assert is_prop_like_market(_m(title="Will there be a run in the 1st?"))
  assert not is_prop_like_market(_m(title="Philadelphia Phillies"))


def test_rejects_screenshot_non_match_markets():
  """Positions like awards/futures/props/set/spreads must never be tradeable."""
  from src.trading.sports_arb_engine import is_prop_like_book, is_prop_like_text

  cases = [
    ("Jayson Tatum's Next Team", "Cleveland"),
    ("World Cup: Third-Place Finisher", "Spain"),
    ("Norway vs England: Method of Victory", "England to win in Penalty Shootout"),
    ("NL Hank Aaron Award Winner?", "Drake Baldwin"),
    ("AL Cy Young Winner?", "Drew Rasmussen"),
    ("AL West Division Winner", "Seattle"),
    ("Karolina Muchova vs Linda Noskova: Set 2 Winner", "Linda Noskova"),
    ("Guadalajara vs Toluca", "Guadalajara wins by more than 1.5 goals"),
    ("Colorado vs San Francisco (Outs Recorded)", "Robbie Ray: 19+"),
    ("Toronto vs San Diego: Stolen Bases", "Fernando Tatis Jr.: 1+"),
    ("The Amundi Evian Championship Winner", "Na Rin An"),
  ]
  for event_title, market_title in cases:
    assert is_prop_like_text(event_title, market_title), (event_title, market_title)
    m = _m(title=market_title, yes_ask=0.40, no_ask=0.50)
    book = SportsEventBook("EVT", "KXTEST", event_title, m.close_time, [m])
    assert is_prop_like_book(book) or is_prop_like_market(m, event_title=event_title)
    opps = scan_dutch_same_opportunities(
      [book], fee_rate=0.0, min_edge_usd=0.01, max_stake_usd=5.0
    )
    assert opps == [], (event_title, market_title, opps)


def test_match_moneyline_still_allowed():
  m = _m(title="Los Angeles Dodgers", yes_ask=0.40, no_ask=0.50)
  book = SportsEventBook(
    "KXMLBGAME-1",
    "KXMLBGAME",
    "Arizona Diamondbacks vs. Los Angeles Dodgers",
    m.close_time,
    [m],
  )
  opps = scan_dutch_same_opportunities(
    [book], fee_rate=0.0, min_edge_usd=0.01, max_stake_usd=5.0, include_multi_outcome=False
  )
  assert opps
  assert opps[0].kind == "binary_yes_no"


def test_scan_sorts_by_edge():
  books = [
    SportsEventBook(
      "E1",
      "S",
      "t",
      datetime(2026, 7, 11, tzinfo=timezone.utc),
      [_m(ticker="X", yes_ask=0.40, no_ask=0.50)],
    ),
    SportsEventBook(
      "E2",
      "S",
      "t",
      datetime(2026, 7, 11, tzinfo=timezone.utc),
      [_m(ticker="Y", event_ticker="E2", yes_ask=0.30, no_ask=0.40)],
    ),
  ]
  opps = scan_dutch_same_opportunities(
    books, fee_rate=0.0, min_edge_usd=0.01, max_stake_usd=5.0, include_multi_outcome=False
  )
  assert len(opps) >= 2
  assert opps[0].edge_usd >= opps[1].edge_usd


def test_equal_contracts_on_multi_outcome_legs():
  markets = [
    _m(ticker="EVT1-A", yes_ask=0.48, title="Cleveland"),
    _m(ticker="EVT1-B", yes_ask=0.47, title="Miami"),
  ]
  book = SportsEventBook("EVT1", "KXMLBGAME", "Cleveland vs Miami", markets[0].close_time, markets)
  opp = find_multi_outcome_dutch(book, fee_rate=0.0, min_edge_usd=0.01, max_stake_usd=5.0)
  assert opp is not None
  counts = [leg.contracts for leg in opp.legs]
  assert len(set(counts)) == 1
  assert opp.payout_usd >= opp.total_cost_usd + opp.total_fees_usd


def test_unequal_per_leg_sizing_would_lose_on_favorite():
  """Per-leg $2 caps (3 vs 6) lose when the cheaper side wins — equal n must not."""
  favorite_ask = 0.587
  dog_ask = 0.315
  bad_counts = (3, 6)
  cost = bad_counts[0] * favorite_ask + bad_counts[1] * dog_ask
  assert bad_counts[0] < cost  # favorite win pays $3, cost ~$3.65

  markets = [
    _m(ticker="EVT1-A", yes_ask=favorite_ask, title="Chicago C"),
    _m(ticker="EVT1-B", yes_ask=dog_ask, title="Cincinnati"),
  ]
  book = SportsEventBook("EVT1", "KXMLBGAME", "Chicago C vs Cincinnati", markets[0].close_time, markets)
  opp = find_multi_outcome_dutch(book, fee_rate=0.0, min_edge_usd=0.01, max_stake_usd=2.0)
  if opp:
    assert len({leg.contracts for leg in opp.legs}) == 1
    assert opp.payout_usd >= opp.total_cost_usd


def test_validate_rejects_unequal_leg_contracts():
  opp = {
    "payout_usd": 4.0,
    "total_cost_usd": 3.5,
    "total_fees_usd": 0.0,
    "legs": [
      {"contracts": 3},
      {"contracts": 4},
    ],
  }
  ok, reason = validate_dutch_cover_opportunity(opp, min_edge_usd=0.01)
  assert not ok
  assert reason == "unequal_leg_contracts"


def test_size_uses_execution_ask_ceiling():
  assert execution_ask_decimal(0.401) == 0.41
  n = _size_equal_cover_contracts(
    [0.401, 0.401],
    fee_rate=0.0,
    max_stake_usd=5.0,
    min_edge_usd=0.01,
  )
  assert n >= 1
  cost = 0.41 * 2 * n
  assert float(n) >= cost

