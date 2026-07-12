"""Fee-aware same-venue Dutch / complementary cover opportunities (Goal 2)."""

from __future__ import annotations

import math
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


def execution_ask_decimal(ask: float) -> float:
  """Kalshi live FOK uses ceiling cents — size covers on executable prices."""
  return max(0.01, min(0.99, math.ceil(float(ask) * 100 - 1e-9) / 100.0))


def _scale_contracts(max_stake_usd: float, unit_cost: float) -> int:
  if unit_cost <= 0 or max_stake_usd <= 0:
    return 0
  return max(1, int(max_stake_usd // unit_cost))


def _size_equal_cover_contracts(
  leg_asks: Sequence[float],
  *,
  fee_rate: float,
  max_stake_usd: float,
  min_edge_usd: float,
) -> int:
  """Pick one equal contract count for every leg.

  Goal-2 covers must pay $n on any outcome. Every leg must use the same n;
  per-leg sizing (e.g. max_stake on each side separately) can leave the
  cheaper-favorite payout below total cost.
  """
  if not leg_asks or max_stake_usd <= 0:
    return 0
  exec_asks = [execution_ask_decimal(a) for a in leg_asks]

  def totals(n: int) -> tuple[float, float, float]:
    cost = sum(a * n for a in exec_asks)
    fees = sum(kalshi_taker_fee_usd(a, n, fee_rate) for a in exec_asks)
    payout = float(n)
    return payout, cost, fees

  unit_exec = sum(exec_asks)
  unit_fees = sum(kalshi_taker_fee_usd(a, 1, fee_rate) for a in exec_asks)
  n_cap = _scale_contracts(max_stake_usd, unit_exec + unit_fees)
  if n_cap < 1:
    return 0

  for n in range(n_cap, 0, -1):
    payout, cost, fees = totals(n)
    edge = payout - cost - fees
    if edge + 1e-9 >= float(min_edge_usd) and payout + 1e-9 >= cost + fees:
      return n
  return 0


def validate_dutch_cover_opportunity(opp: dict[str, Any], *, min_edge_usd: float = 0.0) -> tuple[bool, str | None]:
  """Pre-flight check before live FOK — catches unequal legs and payout < cost."""
  legs = list(opp.get("legs") or [])
  if len(legs) < 2:
    return False, "need_2_legs"
  counts = [int(leg.get("contracts") or 0) for leg in legs]
  if any(c < 1 for c in counts):
    return False, "bad_contracts"
  if len(set(counts)) != 1:
    return False, "unequal_leg_contracts"
  n = counts[0]
  payout = float(opp.get("payout_usd") or n)
  cost = float(opp.get("total_cost_usd") or 0)
  fees = float(opp.get("total_fees_usd") or 0)
  if payout + 1e-9 < cost + fees:
    return False, "payout_lt_cost"
  edge = payout - cost - fees
  if edge + 1e-9 < float(min_edge_usd):
    return False, "edge_below_min"
  return True, None


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

  exec_yes = execution_ask_decimal(float(yes_ask))
  exec_no = execution_ask_decimal(float(no_ask))
  unit_cost = exec_yes + exec_no
  unit_fees = kalshi_taker_fee_usd(exec_yes, 1, fee_rate) + kalshi_taker_fee_usd(exec_no, 1, fee_rate)
  unit_edge = 1.0 - unit_cost - unit_fees
  if unit_edge < float(min_edge_usd):
    return None

  n = _size_equal_cover_contracts(
    [exec_yes, exec_no],
    fee_rate=fee_rate,
    max_stake_usd=max_stake_usd,
    min_edge_usd=min_edge_usd,
  )
  if n < 1:
    return None

  yes_fee = kalshi_taker_fee_usd(exec_yes, n, fee_rate)
  no_fee = kalshi_taker_fee_usd(exec_no, n, fee_rate)
  yes_cost = exec_yes * n
  no_cost = exec_no * n
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
      SportsArbLeg(market.ticker, "yes", exec_yes, n, yes_cost, yes_fee),
      SportsArbLeg(market.ticker, "no", exec_no, n, no_cost, no_fee),
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
  exec_asks = [execution_ask_decimal(p) for _, p in quotes]
  unit_cost = sum(exec_asks)
  unit_fees = sum(kalshi_taker_fee_usd(a, 1, fee_rate) for a in exec_asks)
  unit_edge = 1.0 - unit_cost - unit_fees
  if unit_edge < float(min_edge_usd):
    return None
  # Absurd edges ⇒ almost certainly not a true exclusive set.
  if unit_edge > float(max_edge_prob):
    return None

  n = _size_equal_cover_contracts(
    exec_asks,
    fee_rate=fee_rate,
    max_stake_usd=max_stake_usd,
    min_edge_usd=min_edge_usd,
  )
  if n < 1:
    return None

  legs: list[SportsArbLeg] = []
  total_cost = 0.0
  total_fees = 0.0
  for (m, _raw_ask), ask in zip(quotes, exec_asks):
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


def _near_multi_outcome_edge(
  book: SportsEventBook,
  *,
  fee_rate: float,
  max_outcomes: int = 8,
) -> dict[str, Any] | None:
  """Best-effort 2+ outcome cover edge for funnel stats (even when gates reject)."""
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
  asks = [float(m.yes_ask or 0) for m in cands]
  exec_asks = [execution_ask_decimal(a) for a in asks]
  unit_cost = sum(exec_asks)
  unit_fees = sum(kalshi_taker_fee_usd(a, 1, fee_rate) for a in exec_asks)
  unit_edge = 1.0 - unit_cost - unit_fees
  labels = " + ".join(
    f"{(m.title or m.ticker)[:18]}@{execution_ask_decimal(float(m.yes_ask or 0)):.2f}"
    for m in cands[:3]
  )
  if len(cands) > 3:
    labels += f" +{len(cands) - 3} more"
  return {
    "unit_edge": round(unit_edge, 4),
    "unit_cost": round(unit_cost, 4),
    "ask_sum": round(sum(asks), 4),
    "outcomes": len(cands),
    "event_ticker": book.event_ticker,
    "detail": f"sum={unit_cost:.2f} edge={unit_edge:+.3f} · {book.event_ticker} · {labels}",
    "reject_reason": (
      "ask_sum_ge_1" if unit_cost >= 1.0
      else ("edge_lt_min" if unit_edge < 0 else None)
    ),
  }


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
    "best_multi_edge": None,
    "best_multi_detail": None,
    "multi_near_miss": 0,
    "multi_reject_sum_ge_1": 0,
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
      near = _near_multi_outcome_edge(
        book,
        fee_rate=fee_rate,
        max_outcomes=multi_max_outcomes,
      )
      if near:
        prev_multi = meta["best_multi_edge"]
        if prev_multi is None or float(near["unit_edge"]) > float(prev_multi):
          meta["best_multi_edge"] = near["unit_edge"]
          meta["best_multi_detail"] = near["detail"]
        if not partition:
          meta["multi_near_miss"] = int(meta["multi_near_miss"]) + 1
          if near.get("reject_reason") == "ask_sum_ge_1":
            meta["multi_reject_sum_ge_1"] = int(meta["multi_reject_sum_ge_1"]) + 1
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
