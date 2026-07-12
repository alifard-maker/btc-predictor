"""Fee-aware same-venue Dutch / complementary cover opportunities (Goal 2)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from src.data.sports_markets import SportsEventBook, SportsMarketQuote, kalshi_market_url

# Markets that are almost never a clean exclusive partition with siblings.
# Keep match moneylines only — reject props, spreads, awards, futures, set/period markets.
_PROP_HINTS = (
  "spread",
  "o/u",
  "ou ",
  "over/under",
  " over ",
  " under ",
  "total points",
  "total goals",
  "total runs",
  "will there",
  "will the",
  "exact score",
  "correct score",
  "1st ",
  "2nd ",
  "3rd ",
  "first half",
  "second half",
  "1st half",
  "2nd half",
  "halftime",
  "half-time",
  "player prop",
  "to score",
  "anytime scorer",
  "both teams",
  "btts",
  "corner",
  "yellow card",
  "red card",
  "handicap",
  "margin of",
  "winning margin",
  "wins by",
  "by more than",
  "more than",
  "most ",
  "highest",
  "lowest",
  "leader after",
  "make the cut",
  "top ",
  # Period / set / method props
  "set 1",
  "set 2",
  "set 3",
  "set 4",
  "set 5",
  "set winner",
  "set score",
  "match score",
  "method of victory",
  "shootout",
  "penalty",
  "penalties",
  "extra time",
  "in regulation",
  "to win in",
  # Player / game props
  "outs recorded",
  " outs",
  "strikeout",
  "strikeouts",
  "stolen base",
  "stolen bases",
  " steals",
  "steal ",
  "hits+",
  "runs+",
  "rbis",
  "rbi ",
  "passing yards",
  "rushing yards",
  "receiving yards",
  "home runs",
  "bases",
  "goals",
  "1+",
  "2+",
  "3+",
  "4+",
  "5+",
  "6+",
  "7+",
  "8+",
  "9+",
  "10+",
  "11+",
  "12+",
  "13+",
  "14+",
  "15+",
  "16+",
  "17+",
  "18+",
  "19+",
  "20+",
  "21+",
  "22+",
  "25+",
  "30+",
  "35+",
  "40+",
  "45+",
  "50+",
  # Season / award / futures / destination (not game moneylines)
  "leader",
  "leaders",
  "home run",
  "homer",
  "mvp",
  "cy young",
  "hank aaron",
  "aaron award",
  "award",
  "playoff",
  "playoffs",
  "championship",
  "champion",
  "super bowl",
  "world series",
  "futures",
  "season",
  "make the playoffs",
  "win the ",
  "division",
  "outright",
  "third place",
  "third-place",
  "3rd place",
  "finisher",
  "next team",
  "destination",
  "transfer",
  "to win series",
  "series winner",
)

# Series tickers that are never single-game moneylines (Kalshi).
_SERIES_PROP_REJECT = (
  "LEADER",
  "PLAYOFF",
  "MVP",
  "AWARD",
  "FUTURE",
  "CHAMP",
  "SERIES",
  "CYYOUNG",
  "HOMER",
  "HR",
  "NBAFIN",
  "DIVISION",
  "OUTRIGHT",
  "AARON",
)


@dataclass(frozen=True)
class SportsArbLeg:
  ticker: str
  side: str  # yes | no
  ask: float
  contracts: int
  cost_usd: float
  fee_usd: float

  def to_dict(self) -> dict[str, Any]:
    return {
      "ticker": self.ticker,
      "side": self.side,
      "ask": round(self.ask, 4),
      "contracts": self.contracts,
      "cost_usd": round(self.cost_usd, 4),
      "fee_usd": round(self.fee_usd, 4),
    }


@dataclass(frozen=True)
class SportsArbOpportunity:
  strategy: str
  kind: str  # binary_yes_no | multi_outcome
  event_ticker: str
  series_ticker: str
  title: str
  edge_usd: float
  edge_pct: float
  total_cost_usd: float
  total_fees_usd: float
  payout_usd: float
  legs: list[SportsArbLeg]
  pre_match: bool | None = None

  def to_dict(self) -> dict[str, Any]:
    market = self.legs[0].ticker if self.legs else None
    return {
      "strategy": self.strategy,
      "kind": self.kind,
      "event_ticker": self.event_ticker,
      "series_ticker": self.series_ticker,
      "title": self.title,
      "edge_usd": round(self.edge_usd, 4),
      "edge_pct": round(self.edge_pct, 4),
      "total_cost_usd": round(self.total_cost_usd, 4),
      "total_fees_usd": round(self.total_fees_usd, 4),
      "payout_usd": round(self.payout_usd, 4),
      "legs": [leg.to_dict() for leg in self.legs],
      "pre_match": self.pre_match,
      "venue": "kalshi",
      "venue_url": kalshi_market_url(
        series_ticker=self.series_ticker,
        event_ticker=self.event_ticker,
        market_ticker=market,
      ),
    }


def kalshi_taker_fee_usd(price: float, contracts: int, fee_rate: float) -> float:
  """Approximate Kalshi taker fee: rate * C * p * (1-p)."""
  p = max(0.0, min(1.0, float(price)))
  c = max(0, int(contracts))
  return float(fee_rate) * c * p * (1.0 - p)


def _scale_contracts(max_stake_usd: float, unit_cost: float) -> int:
  if unit_cost <= 0 or max_stake_usd <= 0:
    return 0
  return max(1, int(max_stake_usd // unit_cost))


def is_prop_like_text(*parts: str) -> bool:
  """True when title/subtitle/event text looks like a prop, future, or non-ML market."""
  blob = " " + " ".join(str(p or "") for p in parts).lower() + " "
  blob = re.sub(r"\s+", " ", blob)
  if any(h in blob for h in _PROP_HINTS):
    return True
  # Player-stat thresholds: "19+", "1+", "2.5+" etc.
  if re.search(r"\b\d+(?:\.\d+)?\+\b", blob):
    return True
  return False


def is_prop_like_series(series_ticker: str | None, event_ticker: str | None = None) -> bool:
  series = str(series_ticker or event_ticker or "").upper()
  return bool(series) and any(s in series for s in _SERIES_PROP_REJECT)


def is_prop_like_market(
  market: SportsMarketQuote,
  *,
  event_title: str = "",
) -> bool:
  """True for spreads/totals/props/futures — never trade these as match ML."""
  if is_prop_like_series(market.series_ticker, market.event_ticker):
    return True
  return is_prop_like_text(
    market.title,
    market.subtitle,
    market.ticker,
    event_title,
  )


def is_prop_like_book(book: SportsEventBook) -> bool:
  """True when the event itself is awards/futures/props rather than a match."""
  if is_prop_like_series(book.series_ticker, book.event_ticker):
    return True
  return is_prop_like_text(book.title, book.event_ticker, book.series_ticker)


def _ticker_family(ticker: str) -> str:
  """Strip final outcome suffix so EVENT-OUTCOME → EVENT."""
  t = str(ticker or "")
  if "-" not in t:
    return t
  return t.rsplit("-", 1)[0]


def select_exclusive_outcome_markets(
  book: SportsEventBook,
  *,
  max_outcomes: int = 8,
  min_ask_sum: float = 0.78,
) -> list[SportsMarketQuote] | None:
  """Return markets that plausibly form one exclusive winner partition, else None.

  Heuristics (Kalshi does not expose a hard exclusivity flag):
  - drop prop/futures books and prop-like markets
  - 2..max_outcomes remaining
  - shared ticker family / event prefix
  - sum(yes_ask) in [min_ask_sum, 1) so the book looks nearly complete
  """
  if is_prop_like_book(book):
    return None

  cands: list[SportsMarketQuote] = []
  for m in book.markets:
    if m.yes_ask is None or m.yes_ask <= 0 or m.yes_ask >= 1:
      continue
    if is_prop_like_market(m, event_title=book.title or ""):
      continue
    cands.append(m)

  if len(cands) < 2 or len(cands) > int(max_outcomes):
    return None

  event = str(book.event_ticker or "")
  families = {_ticker_family(m.ticker) for m in cands}
  if len(families) > 1:
    # Allow only if every ticker clearly belongs to this event.
    if not event or not all(
      m.ticker.startswith(event) or event in m.ticker for m in cands
    ):
      return None

  ask_sum = sum(float(m.yes_ask or 0) for m in cands)
  if ask_sum < float(min_ask_sum) or ask_sum >= 1.0:
    return None

  return cands


def find_binary_yes_no_arb(
  market: SportsMarketQuote,
  *,
  fee_rate: float,
  min_edge_usd: float,
  max_stake_usd: float,
  event_title: str = "",
) -> SportsArbOpportunity | None:
  """Buy YES and NO on the same binary market when asks + fees < $1 payout."""
  if is_prop_like_market(market, event_title=event_title):
    return None
  yes_ask = market.yes_ask
  no_ask = market.effective_no_ask
  if yes_ask is None or no_ask is None:
    return None
  if yes_ask <= 0 or no_ask <= 0 or yes_ask >= 1 or no_ask >= 1:
    return None

  unit_cost = float(yes_ask) + float(no_ask)
  unit_fees = kalshi_taker_fee_usd(yes_ask, 1, fee_rate) + kalshi_taker_fee_usd(no_ask, 1, fee_rate)
  unit_edge = 1.0 - unit_cost - unit_fees
  if unit_edge < float(min_edge_usd):
    return None

  n = _scale_contracts(max_stake_usd, unit_cost + unit_fees)
  if n < 1:
    return None

  yes_fee = kalshi_taker_fee_usd(yes_ask, n, fee_rate)
  no_fee = kalshi_taker_fee_usd(no_ask, n, fee_rate)
  yes_cost = float(yes_ask) * n
  no_cost = float(no_ask) * n
  total_cost = yes_cost + no_cost
  total_fees = yes_fee + no_fee
  payout = float(n)
  edge = payout - total_cost - total_fees
  if edge < float(min_edge_usd):
    return None

  return SportsArbOpportunity(
    strategy="dutch_same",
    kind="binary_yes_no",
    event_ticker=market.event_ticker,
    series_ticker=market.series_ticker,
    title=market.title or market.ticker,
    edge_usd=edge,
    edge_pct=(edge / total_cost) if total_cost > 0 else 0.0,
    total_cost_usd=total_cost,
    total_fees_usd=total_fees,
    payout_usd=payout,
    legs=[
      SportsArbLeg(market.ticker, "yes", float(yes_ask), n, yes_cost, yes_fee),
      SportsArbLeg(market.ticker, "no", float(no_ask), n, no_cost, no_fee),
    ],
  )


def find_multi_outcome_dutch(
  book: SportsEventBook,
  *,
  fee_rate: float,
  min_edge_usd: float,
  max_stake_usd: float,
  max_outcomes: int = 8,
  min_ask_sum: float = 0.78,
  max_edge_prob: float = 0.06,
) -> SportsArbOpportunity | None:
  """Buy YES on each outcome of a *verified-looking* exclusive partition.

  Rejects prop soups and incomplete books (the old source of fake +$11 edges).
  """
  partition = select_exclusive_outcome_markets(
    book,
    max_outcomes=max_outcomes,
    min_ask_sum=min_ask_sum,
  )
  if not partition:
    return None

  quotes = [(m, float(m.yes_ask or 0)) for m in partition]
  unit_cost = sum(p for _, p in quotes)
  unit_fees = sum(kalshi_taker_fee_usd(p, 1, fee_rate) for _, p in quotes)
  unit_edge = 1.0 - unit_cost - unit_fees
  if unit_edge < float(min_edge_usd):
    return None
  # Absurd edges ⇒ almost certainly not a true exclusive set.
  if unit_edge > float(max_edge_prob):
    return None

  n = _scale_contracts(max_stake_usd, unit_cost + unit_fees)
  if n < 1:
    return None

  legs: list[SportsArbLeg] = []
  total_cost = 0.0
  total_fees = 0.0
  for m, ask in quotes:
    fee = kalshi_taker_fee_usd(ask, n, fee_rate)
    cost = ask * n
    total_cost += cost
    total_fees += fee
    legs.append(SportsArbLeg(m.ticker, "yes", ask, n, cost, fee))

  payout = float(n)  # exactly one YES settles to $1 *if* partition is valid
  edge = payout - total_cost - total_fees
  if edge < float(min_edge_usd):
    return None
  if (edge / n) > float(max_edge_prob):
    return None

  return SportsArbOpportunity(
    strategy="dutch_same",
    kind="multi_outcome",
    event_ticker=book.event_ticker,
    series_ticker=book.series_ticker,
    title=book.title,
    edge_usd=edge,
    edge_pct=(edge / total_cost) if total_cost > 0 else 0.0,
    total_cost_usd=total_cost,
    total_fees_usd=total_fees,
    payout_usd=payout,
    legs=legs,
  )


def scan_dutch_same_opportunities(
  books: Sequence[SportsEventBook],
  *,
  fee_rate: float = 0.07,
  min_edge_usd: float = 0.01,
  max_stake_usd: float = 5.0,
  include_binary_yes_no: bool = True,
  include_multi_outcome: bool = True,
  multi_max_outcomes: int = 8,
  multi_min_ask_sum: float = 0.78,
  multi_max_edge_prob: float = 0.06,
) -> list[SportsArbOpportunity]:
  """Scan event books for Goal-2 same-venue covers."""
  found, _ = scan_dutch_same_opportunities_with_meta(
    books,
    fee_rate=fee_rate,
    min_edge_usd=min_edge_usd,
    max_stake_usd=max_stake_usd,
    include_binary_yes_no=include_binary_yes_no,
    include_multi_outcome=include_multi_outcome,
    multi_max_outcomes=multi_max_outcomes,
    multi_min_ask_sum=multi_min_ask_sum,
    multi_max_edge_prob=multi_max_edge_prob,
  )
  return found


def scan_dutch_same_opportunities_with_meta(
  books: Sequence[SportsEventBook],
  *,
  fee_rate: float = 0.07,
  min_edge_usd: float = 0.01,
  max_stake_usd: float = 5.0,
  include_binary_yes_no: bool = True,
  include_multi_outcome: bool = True,
  multi_max_outcomes: int = 8,
  multi_min_ask_sum: float = 0.78,
  multi_max_edge_prob: float = 0.06,
) -> tuple[list[SportsArbOpportunity], dict[str, Any]]:
  """Scan + funnel stats (best near-miss edges, partition counts)."""
  found: list[SportsArbOpportunity] = []
  seen_binary: set[str] = set()
  meta: dict[str, Any] = {
    "books": len(books),
    "markets": 0,
    "binary_checked": 0,
    "binary_hits": 0,
    "multi_partitions": 0,
    "multi_hits": 0,
    "best_binary_edge": None,
    "best_binary_detail": None,
  }

  for book in books:
    meta["markets"] = int(meta["markets"]) + len(book.markets)
    book_is_prop = is_prop_like_book(book)
    if include_binary_yes_no and not book_is_prop:
      for m in book.markets:
        if m.ticker in seen_binary:
          continue
        if is_prop_like_market(m, event_title=book.title or ""):
          continue
        yes_ask = m.yes_ask
        no_ask = m.effective_no_ask
        if yes_ask is None or no_ask is None:
          continue
        if yes_ask <= 0 or no_ask <= 0 or yes_ask >= 1 or no_ask >= 1:
          continue
        meta["binary_checked"] = int(meta["binary_checked"]) + 1
        unit_cost = float(yes_ask) + float(no_ask)
        unit_fees = kalshi_taker_fee_usd(yes_ask, 1, fee_rate) + kalshi_taker_fee_usd(
          no_ask, 1, fee_rate
        )
        unit_edge = 1.0 - unit_cost - unit_fees
        prev = meta["best_binary_edge"]
        if prev is None or unit_edge > float(prev):
          meta["best_binary_edge"] = round(unit_edge, 4)
          meta["best_binary_detail"] = (
            f"yes={float(yes_ask):.2f}+no={float(no_ask):.2f} "
            f"edge={unit_edge:+.3f} · {m.ticker}"
          )
        opp = find_binary_yes_no_arb(
          m,
          fee_rate=fee_rate,
          min_edge_usd=min_edge_usd,
          max_stake_usd=max_stake_usd,
          event_title=book.title or "",
        )
        if opp:
          seen_binary.add(m.ticker)
          found.append(opp)
          meta["binary_hits"] = int(meta["binary_hits"]) + 1

    if include_multi_outcome and not book_is_prop:
      partition = select_exclusive_outcome_markets(
        book,
        max_outcomes=multi_max_outcomes,
        min_ask_sum=multi_min_ask_sum,
      )
      if partition:
        meta["multi_partitions"] = int(meta["multi_partitions"]) + 1
      opp = find_multi_outcome_dutch(
        book,
        fee_rate=fee_rate,
        min_edge_usd=min_edge_usd,
        max_stake_usd=max_stake_usd,
        max_outcomes=multi_max_outcomes,
        min_ask_sum=multi_min_ask_sum,
        max_edge_prob=multi_max_edge_prob,
      )
      if opp:
        found.append(opp)
        meta["multi_hits"] = int(meta["multi_hits"]) + 1

  found.sort(key=lambda o: o.edge_usd, reverse=True)
  meta["opportunities"] = len(found)
  return found, meta


def strategy_params_from_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  sports = dict((cfg or {}).get("sports") or {})
  dutch = dict((sports.get("strategies") or {}).get("dutch_same") or {})
  return {
    "enabled": bool(dutch.get("enabled", True)),
    "fee_rate": float(dutch.get("assumed_fee_rate", 0.07)),
    "min_edge_usd": float(dutch.get("min_edge_after_fees_usd", 0.01)),
    "max_stake_usd": float(dutch.get("max_stake_per_opp_usd", 5.0)),
    "include_binary_yes_no": bool(dutch.get("include_binary_yes_no", True)),
    "include_multi_outcome": bool(dutch.get("include_multi_outcome", True)),
    "multi_max_outcomes": int(dutch.get("multi_max_outcomes", 8)),
    "multi_min_ask_sum": float(dutch.get("multi_min_ask_sum", 0.78)),
    "multi_max_edge_prob": float(dutch.get("multi_max_edge_prob", 0.06)),
  }
