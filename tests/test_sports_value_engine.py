"""Unit tests for Goal-3 value_sharp engine + Odds helpers."""

from src.data.odds_api import SharpEvent, SharpOutcome, multiplicative_devig
from src.data.polymarket import PolyMoneylineQuote
from src.data.sports_markets import SportsEventBook, SportsMarketQuote
from src.trading.sports_value_engine import (
  event_pair_score,
  is_fifa_draw_leg_market,
  scan_value_opportunities,
  scan_value_opportunities_with_meta,
  team_match_score,
)


def test_multiplicative_devig_sums_to_one():
  fair = multiplicative_devig([1.90, 2.00])
  assert abs(sum(fair) - 1.0) < 1e-9
  assert fair[0] > fair[1]


def test_team_match_nickname():
  assert team_match_score("Detroit Tigers", "Tigers") >= 0.75
  assert team_match_score("Philadelphia Phillies", "Phillies") >= 0.75
  assert event_pair_score(
    "Detroit Tigers",
    "Philadelphia Phillies",
    "Philadelphia Phillies vs. Detroit Tigers",
  ) >= 0.55


def _sharp() -> SharpEvent:
  # Fair ~53% Tigers / 47% Phillies after devig of 1.88 / 2.04
  fair = multiplicative_devig([1.88, 2.04])
  return SharpEvent(
    sport_key="baseball_mlb",
    event_id="ev1",
    home_team="Detroit Tigers",
    away_team="Philadelphia Phillies",
    commence_time="2026-07-10T22:40:00Z",
    bookmaker="pinnacle",
    outcomes=(
      SharpOutcome("Detroit Tigers", 1.88, 1 / 1.88, fair[0]),
      SharpOutcome("Philadelphia Phillies", 2.04, 1 / 2.04, fair[1]),
    ),
  )


def _mlb_game_book(
  *,
  tigers_ask: float,
  phillies_ask: float = 0.48,
) -> SportsEventBook:
  m1 = SportsMarketQuote(
    ticker="KXMLBGAME-26JUL10DETPHI-DET",
    event_ticker="KXMLBGAME-26JUL10DETPHI",
    series_ticker="KXMLBGAME",
    title="Detroit Tigers",
    subtitle="Winner",
    yes_bid=max(0.01, tigers_ask - 0.02),
    yes_ask=tigers_ask,
    no_bid=0.55,
    no_ask=0.58,
    close_time=None,
    status="open",
  )
  m2 = SportsMarketQuote(
    ticker="KXMLBGAME-26JUL10DETPHI-PHI",
    event_ticker="KXMLBGAME-26JUL10DETPHI",
    series_ticker="KXMLBGAME",
    title="Philadelphia Phillies",
    subtitle="Winner",
    yes_bid=max(0.01, phillies_ask - 0.02),
    yes_ask=phillies_ask,
    no_bid=0.50,
    no_ask=0.52,
    close_time=None,
    status="open",
  )
  return SportsEventBook(
    "KXMLBGAME-26JUL10DETPHI",
    "KXMLBGAME",
    "Phillies at Tigers",
    None,
    [m1, m2],
  )


def test_kalshi_value_detects_edge():
  sharp = _sharp()
  book = _mlb_game_book(tigers_ask=0.42)  # fair ~0.52 → clear value
  opps = scan_value_opportunities(
    [sharp],
    kalshi_books=[book],
    poly_quotes=[],
    min_edge_prob=0.03,
    max_stake_usd=5.0,
    kalshi_fee_rate=0.0,
    min_match_score=0.5,
  )
  assert opps
  assert opps[0].kind == "kalshi_value"
  assert opps[0].selection == "Detroit Tigers"
  assert opps[0].edge_prob > 0.03


def test_poly_value_detects_edge():
  sharp = _sharp()
  q = PolyMoneylineQuote(
    event_id="1",
    event_slug="mlb-phi-det-2026-07-10",
    title="Philadelphia Phillies vs. Detroit Tigers",
    market_id="m1",
    question="Philadelphia Phillies vs. Detroit Tigers",
    outcome="Detroit Tigers",
    ask=0.40,
    bid=0.39,
    token_id="tok",
    sport_key="baseball_mlb",
  )
  opps = scan_value_opportunities(
    [sharp],
    kalshi_books=[],
    poly_quotes=[q],
    min_edge_prob=0.03,
    max_stake_usd=5.0,
    poly_fee_rate=0.0,
    min_match_score=0.5,
  )
  assert opps
  assert opps[0].kind == "poly_value"
  assert opps[0].venue == "polymarket"


def test_rejects_no_edge():
  sharp = _sharp()
  fair_tigers = sharp.fair_for("Detroit Tigers") or 0.5
  book = _mlb_game_book(tigers_ask=fair_tigers + 0.02)
  opps = scan_value_opportunities(
    [sharp],
    kalshi_books=[book],
    min_edge_prob=0.03,
    kalshi_fee_rate=0.0,
    min_match_score=0.5,
  )
  assert opps == []


def test_value_skips_scoreline_props():
  from src.trading.sports_value_engine import is_moneyline_like_market

  sharp = _sharp()
  prop = SportsMarketQuote(
    ticker="KX-TIG-32",
    event_ticker="KXMLBGAME-26JUL10DETPHI",
    series_ticker="KXMLBGAME",
    title="Detroit Tigers win 3-2",
    subtitle="",
    yes_bid=0.10,
    yes_ask=0.12,
    no_bid=0.85,
    no_ask=0.88,
    close_time=None,
    status="open",
  )
  assert not is_moneyline_like_market(prop)
  book = SportsEventBook(
    "KXMLBGAME-26JUL10DETPHI",
    "KXMLBGAME",
    "Phillies at Tigers",
    None,
    [prop],
  )
  opps = scan_value_opportunities(
    [sharp],
    kalshi_books=[book],
    min_edge_prob=0.03,
    kalshi_fee_rate=0.0,
    min_match_score=0.5,
  )
  assert opps == []


def test_rejects_hr_leader_and_nfl_playoff_mismatches():
  """Season leaders / cross-sport futures must not match Odds API game h2h."""
  from src.trading.sports_value_engine import is_moneyline_like_market

  sharp = _sharp()
  leader = SportsMarketQuote(
    ticker="KXLEADERMLBHR-26-JSOT",
    event_ticker="KXLEADERMLBHR-26",
    series_ticker="KXLEADERMLBHR",
    title="Juan Soto",
    subtitle="Home Runs Leader",
    yes_bid=0.02,
    yes_ask=0.03,
    no_bid=0.95,
    no_ask=0.97,
    close_time=None,
    status="open",
  )
  assert not is_moneyline_like_market(leader)
  leader_book = SportsEventBook(
    "KXLEADERMLBHR-26",
    "KXLEADERMLBHR",
    "Pro Baseball Home Runs Leader",
    None,
    [leader] * 5,
  )
  playoff = SportsMarketQuote(
    ticker="KXNFLPLAYOFF-27-MIA",
    event_ticker="KXNFLPLAYOFF-27",
    series_ticker="KXNFLPLAYOFF",
    title="Miami",
    subtitle="Make playoffs",
    yes_bid=0.08,
    yes_ask=0.10,
    no_bid=0.88,
    no_ask=0.90,
    close_time=None,
    status="open",
  )
  playoff_book = SportsEventBook(
    "KXNFLPLAYOFF-27",
    "KXNFLPLAYOFF",
    "NFL Make Playoffs",
    None,
    [playoff],
  )
  opps = scan_value_opportunities(
    [sharp],
    kalshi_books=[leader_book, playoff_book],
    min_edge_prob=0.03,
    kalshi_fee_rate=0.0,
    min_match_score=0.5,
  )
  assert opps == []


def test_city_only_does_not_match_full_team_name():
  # "miami" alone must not score like "Miami Marlins"
  assert team_match_score("Miami Marlins", "Miami") < 0.75


def test_united_not_stripped_from_team_names():
  assert team_match_score("Manchester United", "Man United") >= 0.75
  assert "united" in __import__("src.trading.sports_value_engine", fromlist=["_norm"])._norm(
    "Manchester United"
  )


def test_poly_rejects_penny_ask_fake_edge():
  """1¢ ask vs 65¢ fair is not a real opportunity — dust quotes are rejected."""
  sharp = _sharp()
  q = PolyMoneylineQuote(
    event_id="1",
    event_slug="mlb-phi-det-2026-07-10",
    title="Philadelphia Phillies vs. Detroit Tigers",
    market_id="m1",
    question="Philadelphia Phillies vs. Detroit Tigers",
    outcome="Detroit Tigers",
    ask=0.01,
    bid=0.01,
    token_id="tok",
    sport_key="baseball_mlb",
  )
  opps, meta = scan_value_opportunities_with_meta(
    [sharp],
    kalshi_books=[],
    poly_quotes=[q],
    min_edge_prob=0.03,
    max_stake_usd=5.0,
    poly_fee_rate=0.0,
    min_match_score=0.5,
    max_edge_prob=0.12,
  )
  assert opps == []
  assert int((meta.get("poly") or {}).get("penny_ask_rejects", 0)) >= 1


def test_poly_rejects_man_city_on_mlb_royals():
  """Soccer 'Manchester City' must not match MLB Kansas City Royals via 'City'."""
  fair = multiplicative_devig([1.70, 2.20])
  sharp = SharpEvent(
    sport_key="baseball_mlb",
    event_id="mlb1",
    home_team="Baltimore Orioles",
    away_team="Kansas City Royals",
    commence_time="2026-07-11T23:05:00Z",
    bookmaker="pinnacle",
    outcomes=(
      SharpOutcome("Baltimore Orioles", 1.70, 1 / 1.70, fair[0]),
      SharpOutcome("Kansas City Royals", 2.20, 1 / 2.20, fair[1]),
    ),
  )
  q = PolyMoneylineQuote(
    event_id="2",
    event_slug="mlb-kc-bal-2026-07-11",
    title="Kansas City Royals vs. Baltimore Orioles",
    market_id="m2",
    question="Kansas City Royals vs. Baltimore Orioles",
    outcome="Manchester City",
    ask=0.42,
    bid=0.40,
    token_id="tok2",
    sport_key="baseball_mlb",
  )
  assert team_match_score("Kansas City Royals", "Manchester City") < 0.75
  opps = scan_value_opportunities(
    [sharp],
    kalshi_books=[],
    poly_quotes=[q],
    min_edge_prob=0.03,
    max_stake_usd=5.0,
    poly_fee_rate=0.0,
    min_match_score=0.55,
  )
  assert opps == []


def test_value_rejects_set_winner_and_outs_props():
  from src.trading.sports_value_engine import is_moneyline_like_market

  set_m = SportsMarketQuote(
    ticker="KX-SET2",
    event_ticker="KXTENNIS-1",
    series_ticker="KXTENNIS",
    title="Linda Noskova",
    subtitle="",
    yes_bid=0.45,
    yes_ask=0.49,
    no_bid=0.50,
    no_ask=0.52,
    close_time=None,
    status="open",
  )
  assert not is_moneyline_like_market(
    set_m, event_title="Karolina Muchova vs Linda Noskova: Set 2 Winner"
  )
  outs = SportsMarketQuote(
    ticker="KX-OUTS",
    event_ticker="KXMLBGAME-1",
    series_ticker="KXMLBGAME",
    title="Robbie Ray: 19+",
    subtitle="",
    yes_bid=0.35,
    yes_ask=0.40,
    no_bid=0.55,
    no_ask=0.60,
    close_time=None,
    status="open",
  )
  assert not is_moneyline_like_market(
    outs, event_title="Colorado vs San Francisco (Outs Recorded)"
  )


def test_matches_when_teams_are_separate_markets():
  """Kalshi often has weak event titles but one market per team subtitle."""
  fair = multiplicative_devig([1.88, 2.04])
  sharp = SharpEvent(
    sport_key="soccer_epl",
    event_id="epl1",
    home_team="Manchester United",
    away_team="Liverpool",
    commence_time="2026-07-10T22:40:00Z",
    bookmaker="pinnacle",
    outcomes=(
      SharpOutcome("Manchester United", 1.88, 1 / 1.88, fair[0]),
      SharpOutcome("Liverpool", 2.04, 1 / 2.04, fair[1]),
    ),
  )
  m1 = SportsMarketQuote(
    ticker="KX-MU",
    event_ticker="EVT-EPL",
    series_ticker="KXEPL",
    title="Winner",
    subtitle="Manchester United",
    yes_bid=0.40,
    yes_ask=0.42,
    no_bid=0.55,
    no_ask=0.58,
    close_time=None,
    status="open",
  )
  m2 = SportsMarketQuote(
    ticker="KX-LIV",
    event_ticker="EVT-EPL",
    series_ticker="KXEPL",
    title="Winner",
    subtitle="Liverpool",
    yes_bid=0.45,
    yes_ask=0.48,
    no_bid=0.50,
    no_ask=0.52,
    close_time=None,
    status="open",
  )
  book = SportsEventBook("EVT-EPL", "KXEPL", "EPL Game", None, [m1, m2])
  from src.trading.sports_value_engine import scan_value_opportunities_with_meta

  opps, meta = scan_value_opportunities_with_meta(
    [sharp],
    kalshi_books=[book],
    min_edge_prob=0.03,
    kalshi_fee_rate=0.0,
    min_match_score=0.55,
  )
  assert meta["event_matches"] >= 1
  assert opps
  assert opps[0].selection == "Manchester United"


def _fifa_sharp() -> SharpEvent:
  fair = multiplicative_devig([2.10, 3.40, 3.60])
  return SharpEvent(
    sport_key="soccer_fifa_world_cup",
    event_id="fifa1",
    home_team="Argentina",
    away_team="Switzerland",
    commence_time="2026-07-11T19:00:00Z",
    bookmaker="pinnacle",
    outcomes=(
      SharpOutcome("Argentina", 2.10, 1 / 2.10, fair[0]),
      SharpOutcome("Draw", 3.40, 1 / 3.40, fair[1]),
      SharpOutcome("Switzerland", 3.60, 1 / 3.60, fair[2]),
    ),
  )


def _fifa_game_book(*, tie_ask: float) -> SportsEventBook:
  m_home = SportsMarketQuote(
    ticker="KXFIFAGAME-26JUL11ARGSUI-ARG",
    event_ticker="KXFIFAGAME-26JUL11ARGSUI",
    series_ticker="KXFIFAGAME",
    title="Argentina",
    subtitle="Reg Time: Argentina",
    yes_bid=max(0.01, 0.46 - 0.02),
    yes_ask=0.46,
    no_bid=0.50,
    no_ask=0.52,
    close_time=None,
    status="open",
  )
  m_away = SportsMarketQuote(
    ticker="KXFIFAGAME-26JUL11ARGSUI-SUI",
    event_ticker="KXFIFAGAME-26JUL11ARGSUI",
    series_ticker="KXFIFAGAME",
    title="Switzerland",
    subtitle="Reg Time: Switzerland",
    yes_bid=max(0.01, 0.28 - 0.02),
    yes_ask=0.28,
    no_bid=0.68,
    no_ask=0.70,
    close_time=None,
    status="open",
  )
  m_tie = SportsMarketQuote(
    ticker="KXFIFAGAME-26JUL11ARGSUI-TIE",
    event_ticker="KXFIFAGAME-26JUL11ARGSUI",
    series_ticker="KXFIFAGAME",
    title="Tie",
    subtitle="Reg Time: Tie",
    yes_bid=max(0.01, tie_ask - 0.02),
    yes_ask=tie_ask,
    no_bid=0.68,
    no_ask=0.70,
    close_time=None,
    status="open",
  )
  return SportsEventBook(
    "KXFIFAGAME-26JUL11ARGSUI",
    "KXFIFAGAME",
    "Argentina vs Switzerland: Regulation Time Moneyline",
    None,
    [m_home, m_away, m_tie],
  )


def test_fifa_draw_leg_market_detection():
  tie = _fifa_game_book(tie_ask=0.28).markets[2]
  assert is_fifa_draw_leg_market(
    tie,
    event_title="Argentina vs Switzerland: Regulation Time Moneyline",
    series_ticker="KXFIFAGAME",
  )


def test_fifa_draw_value_detects_edge():
  sharp = _fifa_sharp()
  fair_draw = sharp.fair_for_draw() or 0
  book = _fifa_game_book(tie_ask=max(0.05, fair_draw - 0.06))
  opps, meta = scan_value_opportunities_with_meta(
    [sharp],
    kalshi_books=[book],
    min_edge_prob=0.03,
    kalshi_fee_rate=0.0,
    min_match_score=0.55,
    fifa_draw_legs=True,
  )
  draw_opps = [o for o in opps if "Tie" in o.selection or "Draw" in o.selection]
  assert meta["draw_market_hits"] >= 1
  assert draw_opps
  assert draw_opps[0].edge_prob > 0.03


def test_fifa_draw_disabled_skips_tie():
  sharp = _fifa_sharp()
  fair_draw = sharp.fair_for_draw() or 0
  book = _fifa_game_book(tie_ask=max(0.05, fair_draw - 0.06))
  opps, meta = scan_value_opportunities_with_meta(
    [sharp],
    kalshi_books=[book],
    min_edge_prob=0.03,
    kalshi_fee_rate=0.0,
    min_match_score=0.55,
    fifa_draw_legs=False,
  )
  assert meta.get("draw_market_hits", 0) == 0
  assert not [o for o in opps if "Tie" in o.selection]


def test_epl_draw_not_scanned_without_fifa_flag():
  fair = multiplicative_devig([2.10, 3.40, 3.60])
  sharp = SharpEvent(
    sport_key="soccer_epl",
    event_id="epl-draw",
    home_team="Manchester United",
    away_team="Liverpool",
    commence_time="2026-07-10T22:40:00Z",
    bookmaker="pinnacle",
    outcomes=(
      SharpOutcome("Manchester United", 2.10, 1 / 2.10, fair[0]),
      SharpOutcome("Draw", 3.40, 1 / 3.40, fair[1]),
      SharpOutcome("Liverpool", 3.60, 1 / 3.60, fair[2]),
    ),
  )
  tie = SportsMarketQuote(
    ticker="KXEPLGAME-1-TIE",
    event_ticker="KXEPLGAME-1",
    series_ticker="KXEPLGAME",
    title="Draw",
    subtitle="Reg Time: Tie",
    yes_bid=0.20,
    yes_ask=0.22,
    no_bid=0.75,
    no_ask=0.78,
    close_time=None,
    status="open",
  )
  home = SportsMarketQuote(
    ticker="KXEPLGAME-1-MU",
    event_ticker="KXEPLGAME-1",
    series_ticker="KXEPLGAME",
    title="Manchester United",
    subtitle="Winner",
    yes_bid=0.40,
    yes_ask=0.42,
    no_bid=0.55,
    no_ask=0.58,
    close_time=None,
    status="open",
  )
  away = SportsMarketQuote(
    ticker="KXEPLGAME-1-LIV",
    event_ticker="KXEPLGAME-1",
    series_ticker="KXEPLGAME",
    title="Liverpool",
    subtitle="Winner",
    yes_bid=0.30,
    yes_ask=0.32,
    no_bid=0.65,
    no_ask=0.68,
    close_time=None,
    status="open",
  )
  book = SportsEventBook("KXEPLGAME-1", "KXEPLGAME", "Man U vs Liverpool", None, [home, away, tie])
  opps, meta = scan_value_opportunities_with_meta(
    [sharp],
    kalshi_books=[book],
    min_edge_prob=0.03,
    kalshi_fee_rate=0.0,
    min_match_score=0.55,
    fifa_draw_legs=True,
  )
  assert meta.get("draw_market_hits", 0) == 0
  assert not [o for o in opps if "Tie" in o.selection]
