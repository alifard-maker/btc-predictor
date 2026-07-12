"""Goal 3 — value vs sharp fair line (Kalshi + Polymarket paper)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from src.data.odds_api import SharpEvent
from src.data.polymarket import PolyMoneylineQuote
from src.data.sports_markets import SportsEventBook, SportsMarketQuote, kalshi_market_url
from src.trading.sports_arb_engine import (
  is_prop_like_book,
  is_prop_like_market,
  is_prop_like_series,
  is_prop_like_text,
  kalshi_taker_fee_usd,
)

# Extra Goal-3 rejects beyond shared prop hints (Odds API h2h is game ML only).
_VALUE_EXTRA_REJECT = (
  "to nil",
  "clean sheet",
  "win to nil",
  "season long",
  "regular season",
)

# Series tickers that are never single-game moneylines vs Odds API h2h.
_SERIES_REJECT_SUBSTR = (
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
  "SB",
  "DIVISION",
  "OUTRIGHT",
  "AARON",
)

_FIFA_SPORT_KEYS = frozenset({"soccer_fifa_world_cup"})
_DRAW_MARKET_RE = re.compile(
  r"\b(?:reg(?:ulation)?\s*time\s*[:\-]?\s*)?(?:draw|tie)\b",
  re.I,
)

# Odds API sport_key → allowed Kalshi series ticker prefixes (uppercase).
_SPORT_SERIES_ALLOW = {
  "baseball_mlb": ("KXMLB", "MLB"),
  "americanfootball_nfl": ("KXNFL", "NFL"),
  "americanfootball_ncaaf": ("KXNCAAF", "NCAAF", "CFB"),
  "basketball_nba": ("KXNBA", "NBA"),
  "basketball_ncaab": ("KXNCAAB", "NCAAB", "CBB"),
  "basketball_wnba": ("KXWNBA", "WNBA"),
  "soccer_epl": ("KXEPL", "EPL", "SOCCER"),
  "soccer_fifa_world_cup": ("KXFIFA", "FIFA", "WC"),
  "icehockey_nhl": ("KXNHL", "NHL"),
  "tennis_atp": ("KXTENNIS", "KXATP", "ATP", "TENNIS"),
  "tennis_wta": ("KXTENNIS", "KXWTA", "WTA", "TENNIS"),
}


def is_moneyline_like_market(
  market: SportsMarketQuote,
  *,
  event_title: str = "",
) -> bool:
  """True for match/outright winner markets suitable for h2h sharp comparison."""
  if is_prop_like_market(market, event_title=event_title):
    return False
  blob = f" {market.title} {market.subtitle} {market.ticker} {event_title} ".lower()
  if any(x in blob for x in _VALUE_EXTRA_REJECT):
    return False
  if is_prop_like_series(market.series_ticker, market.event_ticker):
    return False
  series = str(market.series_ticker or market.event_ticker or "").upper()
  if any(s in series for s in _SERIES_REJECT_SUBSTR):
    return False
  # Exact scorelines like "3-2" / "2 – 1"
  if re.search(r"\b\d\s*[-–]\s*\d\b", blob):
    return False
  # Standalone draw/tie markets — blocked except FIFA draw legs (handled separately).
  if _DRAW_MARKET_RE.search(blob) and not _is_fifa_series(
    str(market.series_ticker or market.event_ticker or "")
  ):
    if not re.search(r"\b(win|winner|moneyline|ml)\b", blob):
      return False
  return True


def _is_fifa_series(series: str) -> bool:
  s = str(series or "").upper()
  return "FIFA" in s or "KXFIFA" in s


def is_fifa_sport_key(sport_key: str) -> bool:
  return str(sport_key or "") in _FIFA_SPORT_KEYS


def is_fifa_book(book: SportsEventBook) -> bool:
  return _is_fifa_series(str(book.series_ticker or book.event_ticker or ""))


def is_fifa_draw_leg_market(
  market: SportsMarketQuote,
  *,
  event_title: str = "",
  series_ticker: str = "",
) -> bool:
  """Kalshi FIFA regulation-time tie leg (3-way soccer draw)."""
  if not _is_fifa_series(str(series_ticker or market.series_ticker or market.event_ticker or "")):
    return False
  blob = f" {market.title} {market.subtitle} {event_title} ".lower()
  if not _DRAW_MARKET_RE.search(blob):
    return False
  if is_prop_like_market(market, event_title=event_title):
    return False
  return True


def _book_looks_like_game(book: SportsEventBook) -> bool:
  """Reject leaderboards / season fields (many outcomes) and futures titles."""
  if is_prop_like_book(book):
    return False
  title = f" {book.title or ''} {book.event_ticker or ''} {book.series_ticker or ''} ".lower()
  if any(x in title for x in _VALUE_EXTRA_REJECT):
    return False
  if is_prop_like_text(book.title or "", book.event_ticker or ""):
    return False
  series = str(book.series_ticker or book.event_ticker or "").upper()
  if any(s in series for s in _SERIES_REJECT_SUBSTR):
    return False
  ml = [
    m
    for m in book.markets
    if is_moneyline_like_market(m, event_title=book.title or "")
  ]
  # Game ML: typically 2 sides (or 3 with soccer draw). Big fields = awards/leaders.
  if len(ml) < 2 or len(ml) > 3:
    return False
  return True


def _series_matches_sport(sport_key: str, book: SportsEventBook) -> bool:
  series = str(book.series_ticker or book.event_ticker or "").upper()
  if not series:
    return False
  if any(s in series for s in _SERIES_REJECT_SUBSTR):
    return False
  key = str(sport_key or "")
  if key.startswith("tennis_atp"):
    key = "tennis_atp"
  elif key.startswith("tennis_wta"):
    key = "tennis_wta"
  allow = _SPORT_SERIES_ALLOW.get(key)
  if not allow:
    return "GAME" in series or "MATCH" in series
  if not any(p in series for p in allow):
    return False
  # Major US leagues: Odds h2h maps to *GAME* series, not season leaders/futures
  if key in (
    "baseball_mlb",
    "americanfootball_nfl",
    "basketball_nba",
    "basketball_wnba",
    "icehockey_nhl",
  ):
    return "GAME" in series or "MATCH" in series
  return True


def _norm(s: str) -> str:
  s = (s or "").lower()
  s = re.sub(r"[^a-z0-9\s]", " ", s)
  # Drop only true noise — never "united" (Man United / Newcastle United).
  drop = {"fc", "sc", "the", "vs", "v", "and", "club", "cf", "afc"}
  toks = [t for t in s.split() if t and t not in drop]
  return " ".join(toks)


def _tokens(s: str) -> set[str]:
  return set(_norm(s).split())


def team_match_score(a: str, b: str) -> float:
  """0..1 fuzzy score between two team / title strings."""
  na, nb = _norm(a), _norm(b)
  if not na or not nb:
    return 0.0
  if na == nb:
    return 1.0
  if na in nb or nb in na:
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    # Avoid city-only hits: "miami" ⊂ "miami marlins" (NFL Miami ≠ Marlins)
    if " " not in shorter and shorter != longer.split()[-1]:
      pass  # fall through to token / nickname logic
    else:
      return 0.92
  ta, tb = _tokens(a), _tokens(b)
  if not ta or not tb:
    return 0.0
  inter = len(ta & tb)
  last_a = na.split()[-1]
  last_b = nb.split()[-1]
  # Ambiguous last tokens ("City", "Athletic") need a first-token assist —
  # otherwise Manchester City ↔ Kansas City Royals.
  _weak_last = {"city", "town", "athletic", "sporting", "hotspur", "rovers"}
  nick = 0.0
  # Only the last token of a multi-word name is a nickname candidate
  if " " in na and len(last_a) >= 4 and last_a in tb:
    if last_a in _weak_last:
      first_a = na.split()[0]
      if first_a in tb or any(t.startswith(first_a) or first_a.startswith(t) for t in tb):
        nick = 0.85
    else:
      nick = 0.85
  if " " in nb and len(last_b) >= 4 and last_b in ta:
    if last_b in _weak_last:
      first_b = nb.split()[0]
      if first_b in ta or any(t.startswith(first_b) or first_b.startswith(t) for t in ta):
        nick = max(nick, 0.85)
    else:
      nick = max(nick, 0.85)
  if last_a == last_b and len(last_a) >= 4:
    first_a = na.split()[0]
    first_b = nb.split()[0]
    if last_a in _weak_last and first_a != first_b and not (
      first_a.startswith(first_b) or first_b.startswith(first_a)
    ):
      pass  # Manchester City ≠ Kansas City
    elif (
      first_a.startswith(first_b)
      or first_b.startswith(first_a)
      or min(len(first_a), len(first_b)) <= 3
    ):
      nick = max(nick, 0.88)
    else:
      nick = max(nick, 0.8)
  if inter == 0:
    return nick
  union = len(ta | tb)
  jacc = inter / union
  shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
  if shorter <= longer:
    # "tigers" ⊂ {detroit,tigers} is good; "miami" ⊂ {miami,marlins} is not
    if len(shorter) == 1:
      only = next(iter(shorter))
      longer_name = nb if len(tb) >= len(ta) else na
      if only != longer_name.split()[-1]:
        return max(jacc, nick)
    return max(jacc, nick, 0.85)
  return max(jacc, nick)


def event_pair_score(home: str, away: str, title: str) -> float:
  """Score whether title mentions both sides of a matchup."""
  th = team_match_score(home, title)
  ta = team_match_score(away, title)
  return min(th, ta) * 0.35 + max(th, ta) * 0.15 + (th + ta) / 2 * 0.5


@dataclass(frozen=True)
class ValueOpportunity:
  strategy: str
  kind: str  # kalshi_value | poly_value
  venue: str
  event_ticker: str
  series_ticker: str
  title: str
  selection: str
  fair_prob: float
  venue_ask: float
  edge_prob: float
  edge_usd: float
  edge_pct: float
  total_cost_usd: float
  total_fees_usd: float
  payout_usd: float
  contracts: int
  sharp_book: str
  sharp_event_id: str
  legs: list[dict[str, Any]]
  pre_match: bool | None = None
  match_score: float | None = None

  def to_dict(self) -> dict[str, Any]:
    market = None
    if self.legs:
      market = str(self.legs[0].get("ticker") or "") or None
    venue_url = None
    if self.venue == "kalshi":
      venue_url = kalshi_market_url(
        series_ticker=self.series_ticker,
        event_ticker=self.event_ticker,
        market_ticker=market,
      )
    elif self.venue == "polymarket" and self.event_ticker:
      # event_ticker holds slug for poly paper quotes
      slug = str(self.event_ticker).lstrip("/")
      venue_url = f"https://polymarket.com/event/{slug}"
    return {
      "strategy": self.strategy,
      "kind": self.kind,
      "venue": self.venue,
      "event_ticker": self.event_ticker,
      "series_ticker": self.series_ticker,
      "title": self.title,
      "selection": self.selection,
      "fair_prob": round(self.fair_prob, 4),
      "venue_ask": round(self.venue_ask, 4),
      "edge_prob": round(self.edge_prob, 4),
      "edge_usd": round(self.edge_usd, 4),
      "edge_pct": round(self.edge_pct, 4),
      "total_cost_usd": round(self.total_cost_usd, 4),
      "total_fees_usd": round(self.total_fees_usd, 4),
      "payout_usd": round(self.payout_usd, 4),
      "contracts": self.contracts,
      "sharp_book": self.sharp_book,
      "sharp_event_id": self.sharp_event_id,
      "legs": self.legs,
      "pre_match": self.pre_match,
      "match_score": round(self.match_score, 3) if self.match_score is not None else None,
      "venue_url": venue_url,
    }


def value_params_from_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  sports = dict((cfg or {}).get("sports") or {})
  raw = dict((sports.get("strategies") or {}).get("value_sharp") or {})
  return {
    "enabled": bool(raw.get("enabled", False)),
    "min_edge_prob": float(raw.get("min_edge_prob", 0.03)),
    "max_stake_usd": float(raw.get("max_stake_usd", 5.0)),
    "assumed_fee_rate": float(raw.get("assumed_fee_rate", 0.07)),
    "poly_fee_rate": float(raw.get("poly_fee_rate", 0.0)),
    "allow_kalshi_live": bool(raw.get("allow_kalshi_live", False)),
    "min_match_score": float(raw.get("min_match_score", 0.55)),
    "max_edge_prob": float(raw.get("max_edge_prob", 0.12)),
    "sport_keys": list(raw.get("sport_keys") or ["baseball_mlb", "soccer_epl"]),
    "tennis_max_atp": int(raw.get("tennis_max_atp", 2)),
    "tennis_max_wta": int(raw.get("tennis_max_wta", 2)),
    "fifa_draw_legs": bool(raw.get("fifa_draw_legs", True)),
  }


def _market_blob(market: SportsMarketQuote) -> str:
  return f"{market.title} {market.subtitle}".strip()


def _best_kalshi_market_for_team(
  book: SportsEventBook,
  team: str,
  *,
  min_score: float,
) -> tuple[SportsMarketQuote, float] | None:
  best: tuple[SportsMarketQuote, float] | None = None
  for m in book.markets:
    if is_fifa_draw_leg_market(
      m,
      event_title=book.title or "",
      series_ticker=book.series_ticker or "",
    ):
      continue
    if not is_moneyline_like_market(m, event_title=book.title or ""):
      continue
    blob = _market_blob(m)
    score = max(
      team_match_score(team, blob),
      team_match_score(team, m.title or ""),
      team_match_score(team, m.subtitle or ""),
    )
    if m.yes_ask is None or m.yes_ask <= 0.01 or m.yes_ask >= 0.99:
      continue
    if score < min_score:
      continue
    if best is None or score > best[1]:
      best = (m, score)
  return best


def _draw_selection_label(market: SportsMarketQuote) -> str:
  sub = (market.subtitle or market.title or "").strip()
  return sub or "Draw"


def _best_kalshi_market_for_draw(
  book: SportsEventBook,
) -> SportsMarketQuote | None:
  """FIFA-only: regulation-time tie market on a matched 3-way book."""
  if not is_fifa_book(book):
    return None
  for m in book.markets:
    if not is_fifa_draw_leg_market(
      m,
      event_title=book.title or "",
      series_ticker=book.series_ticker or "",
    ):
      continue
    if m.yes_ask is None or m.yes_ask <= 0.01 or m.yes_ask >= 0.99:
      continue
    return m
  return None


def _append_kalshi_value_opp(
  opps: list[ValueOpportunity],
  *,
  book: SportsEventBook,
  sharp: SharpEvent,
  market: SportsMarketQuote,
  selection: str,
  fair: float,
  ask: float,
  fee_rate: float,
  min_edge_prob: float,
  max_edge_prob: float,
  max_stake_usd: float,
  meta: dict[str, Any],
  meta_hit_key: str,
  match_score: float | None = None,
) -> None:
  fee_1 = kalshi_taker_fee_usd(ask, 1, fee_rate)
  edge_prob = float(fair) - ask - fee_1
  prev_best = meta["best_edge_prob"]
  if prev_best is None or edge_prob > float(prev_best):
    meta["best_edge_prob"] = round(edge_prob, 4)
    meta["best_edge_detail"] = (
      f"{selection} fair={float(fair):.2f} ask={ask:.2f} edge={edge_prob:+.3f} · {book.title}"
    )
  if edge_prob < float(min_edge_prob):
    meta["below_min_edge"] = int(meta["below_min_edge"]) + 1
    return
  if edge_prob > float(max_edge_prob):
    meta["absurd_edge_rejects"] = int(meta["absurd_edge_rejects"]) + 1
    return
  unit_cost = ask + fee_1
  n = max(1, int(max_stake_usd // unit_cost)) if unit_cost > 0 else 0
  if n < 1:
    return
  fees = kalshi_taker_fee_usd(ask, n, fee_rate)
  cost = ask * n + fees
  edge_usd = float(fair) * n - cost
  if edge_usd < 0.01:
    meta["below_min_edge"] = int(meta["below_min_edge"]) + 1
    return
  meta[meta_hit_key] = int(meta.get(meta_hit_key, 0)) + 1
  opps.append(
    ValueOpportunity(
      strategy="value_sharp",
      kind="kalshi_value",
      venue="kalshi",
      event_ticker=book.event_ticker,
      series_ticker=book.series_ticker,
      title=f"{book.title} · {selection}",
      selection=selection,
      fair_prob=float(fair),
      venue_ask=ask,
      edge_prob=edge_prob,
      edge_usd=edge_usd,
      edge_pct=(edge_usd / cost) if cost else 0.0,
      total_cost_usd=cost,
      total_fees_usd=fees,
      payout_usd=float(n),
      contracts=n,
      sharp_book=sharp.bookmaker,
      sharp_event_id=sharp.event_id,
      match_score=match_score,
      legs=[{
        "ticker": market.ticker,
        "side": "yes",
        "ask": ask,
        "contracts": n,
        "cost_usd": round(ask * n, 4),
        "fee_usd": round(fees, 4),
        "venue": "kalshi",
      }],
    )
  )


def _book_covers_matchup(
  book: SportsEventBook,
  home: str,
  away: str,
  *,
  min_score: float,
  sport_key: str | None = None,
) -> float:
  """Best score that this Kalshi event is the sharp matchup.

  Kalshi often titles the event weakly but lists each team as its own market
  subtitle — treat two team-market hits as a strong event match.
  """
  if not _book_looks_like_game(book):
    return 0.0
  if sport_key and not _series_matches_sport(sport_key, book):
    return 0.0

  best = event_pair_score(home, away, book.title or "")
  for m in book.markets:
    blob = f"{book.title} {_market_blob(m)}"
    best = max(best, event_pair_score(home, away, blob))

  home_hit = 0.0
  away_hit = 0.0
  for m in book.markets:
    if is_fifa_draw_leg_market(
      m,
      event_title=book.title or "",
      series_ticker=book.series_ticker or "",
    ):
      continue
    if not is_moneyline_like_market(m, event_title=book.title or ""):
      continue
    blob = _market_blob(m)
    home_hit = max(
      home_hit,
      team_match_score(home, blob),
      team_match_score(home, m.subtitle or ""),
    )
    away_hit = max(
      away_hit,
      team_match_score(away, blob),
      team_match_score(away, m.subtitle or ""),
    )
  # Require BOTH sides of the sharp game to appear — stops "Miami" alone
  # matching Marlins into an NFL playoff field.
  if home_hit >= min_score and away_hit >= min_score:
    best = max(best, 0.5 * (home_hit + away_hit))
  elif home_hit < min_score or away_hit < min_score:
    # Title-only match still OK if both names appear in the event title
    if event_pair_score(home, away, book.title or "") < min_score:
      return 0.0
  return best


def _match_sharp_to_kalshi_books(
  sharp: SharpEvent,
  books: Sequence[SportsEventBook],
  *,
  min_score: float,
) -> SportsEventBook | None:
  best: tuple[SportsEventBook, float] | None = None
  for b in books:
    score = _book_covers_matchup(
      b,
      sharp.home_team,
      sharp.away_team,
      min_score=min_score,
      sport_key=str(sharp.sport_key or ""),
    )
    if score < min_score:
      continue
    if best is None or score > best[1]:
      best = (b, score)
  return best[0] if best else None


def scan_kalshi_value(
  sharp_events: Sequence[SharpEvent],
  books: Sequence[SportsEventBook],
  *,
  min_edge_prob: float,
  max_stake_usd: float,
  fee_rate: float,
  min_match_score: float,
  max_edge_prob: float = 0.12,
  fifa_draw_legs: bool = True,
) -> tuple[list[ValueOpportunity], dict[str, Any]]:
  opps: list[ValueOpportunity] = []
  meta: dict[str, Any] = {
    "sharp_events": len(sharp_events),
    "books": len(books),
    "event_matches": 0,
    "team_market_hits": 0,
    "draw_market_hits": 0,
    "below_min_edge": 0,
    "absurd_edge_rejects": 0,
    "opportunities": 0,
    "best_edge_prob": None,
    "best_edge_detail": None,
  }
  for sharp in sharp_events:
    book = _match_sharp_to_kalshi_books(sharp, books, min_score=min_match_score)
    if book is None:
      continue
    meta["event_matches"] = int(meta["event_matches"]) + 1
    for team in (sharp.home_team, sharp.away_team):
      fair = sharp.fair_for(team)
      if fair is None or fair <= 0:
        continue
      hit = _best_kalshi_market_for_team(book, team, min_score=min_match_score)
      if hit is None:
        continue
      market, mscore = hit
      _append_kalshi_value_opp(
        opps,
        book=book,
        sharp=sharp,
        market=market,
        selection=team,
        fair=float(fair),
        ask=float(market.yes_ask or 0),
        fee_rate=fee_rate,
        min_edge_prob=min_edge_prob,
        max_edge_prob=max_edge_prob,
        max_stake_usd=max_stake_usd,
        meta=meta,
        meta_hit_key="team_market_hits",
        match_score=mscore,
      )
    if fifa_draw_legs and is_fifa_sport_key(sharp.sport_key) and is_fifa_book(book):
      fair_draw = sharp.fair_for_draw()
      draw_market = _best_kalshi_market_for_draw(book)
      if fair_draw is not None and fair_draw > 0 and draw_market is not None:
        _append_kalshi_value_opp(
          opps,
          book=book,
          sharp=sharp,
          market=draw_market,
          selection=_draw_selection_label(draw_market),
          fair=float(fair_draw),
          ask=float(draw_market.yes_ask or 0),
          fee_rate=fee_rate,
          min_edge_prob=min_edge_prob,
          max_edge_prob=max_edge_prob,
          max_stake_usd=max_stake_usd,
          meta=meta,
          meta_hit_key="draw_market_hits",
          match_score=1.0,
        )
  opps.sort(key=lambda o: o.edge_prob, reverse=True)
  meta["opportunities"] = len(opps)
  return opps, meta


def _poly_sport_compatible(sharp_key: str, quote_key: str | None) -> bool:
  """Reject cross-sport matches (soccer sharp ↔ mlb-* Polymarket slug)."""
  if not quote_key:
    return True
  a = str(sharp_key or "")
  b = str(quote_key or "")
  if a.startswith("tennis_atp"):
    a = "tennis_atp"
  elif a.startswith("tennis_wta"):
    a = "tennis_wta"
  if b.startswith("tennis_atp"):
    b = "tennis_atp"
  elif b.startswith("tennis_wta"):
    b = "tennis_wta"
  if a == b:
    return True
  # Same family prefixes (soccer_*, baseball_*)
  af = a.split("_", 1)[0]
  bf = b.split("_", 1)[0]
  return bool(af and af == bf)


def _match_poly_quote(
  sharp: SharpEvent,
  team: str,
  quotes: Sequence[PolyMoneylineQuote],
  *,
  min_score: float,
) -> PolyMoneylineQuote | None:
  best: tuple[PolyMoneylineQuote, float] | None = None
  for q in quotes:
    if not _poly_sport_compatible(str(sharp.sport_key or ""), q.sport_key):
      continue
    # Both sides of the matchup must appear in the Poly event title
    event_score = event_pair_score(sharp.home_team, sharp.away_team, q.title)
    if event_score < min_score:
      continue
    sel_score = max(
      team_match_score(team, q.outcome),
      team_match_score(team, q.title),
    )
    if sel_score < min_score:
      continue
    score = 0.45 * event_score + 0.55 * sel_score
    if best is None or score > best[1]:
      best = (q, score)
  return best[0] if best else None


def scan_poly_value(
  sharp_events: Sequence[SharpEvent],
  quotes: Sequence[PolyMoneylineQuote],
  *,
  min_edge_prob: float,
  max_stake_usd: float,
  fee_rate: float,
  min_match_score: float,
  max_edge_prob: float = 0.12,
  min_ask: float = 0.05,
) -> tuple[list[ValueOpportunity], dict[str, Any]]:
  opps: list[ValueOpportunity] = []
  meta: dict[str, Any] = {
    "poly_quotes": len(quotes),
    "team_hits": 0,
    "below_min_edge": 0,
    "absurd_edge_rejects": 0,
    "penny_ask_rejects": 0,
    "opportunities": 0,
    "best_edge_prob": None,
  }
  for sharp in sharp_events:
    for team in (sharp.home_team, sharp.away_team):
      fair = sharp.fair_for(team)
      if fair is None or fair <= 0:
        continue
      q = _match_poly_quote(sharp, team, quotes, min_score=min_match_score)
      if q is None:
        continue
      meta["team_hits"] = int(meta["team_hits"]) + 1
      ask = float(q.ask)
      # Penny / dust asks are almost always stale, illiquid, or wrong market
      if ask < float(min_ask):
        meta["penny_ask_rejects"] = int(meta["penny_ask_rejects"]) + 1
        continue
      fee_1 = ask * float(fee_rate)
      edge_prob = float(fair) - ask - fee_1
      prev_best = meta["best_edge_prob"]
      if prev_best is None or edge_prob > float(prev_best):
        meta["best_edge_prob"] = round(edge_prob, 4)
      if edge_prob < float(min_edge_prob):
        meta["below_min_edge"] = int(meta["below_min_edge"]) + 1
        continue
      if edge_prob > float(max_edge_prob):
        meta["absurd_edge_rejects"] = int(meta["absurd_edge_rejects"]) + 1
        continue
      unit_cost = ask + fee_1
      n = max(1, int(max_stake_usd // unit_cost)) if unit_cost > 0 else 0
      if n < 1:
        continue
      fees = unit_cost * n - ask * n
      cost = ask * n + fees
      edge_usd = float(fair) * n - cost
      if edge_usd < 0.01:
        meta["below_min_edge"] = int(meta["below_min_edge"]) + 1
        continue
      opps.append(
        ValueOpportunity(
          strategy="value_sharp",
          kind="poly_value",
          venue="polymarket",
          event_ticker=q.event_slug or q.event_id,
          series_ticker=q.sport_key or "polymarket",
          title=f"{q.title} · {team}",
          selection=team,
          fair_prob=float(fair),
          venue_ask=ask,
          edge_prob=edge_prob,
          edge_usd=edge_usd,
          edge_pct=(edge_usd / cost) if cost else 0.0,
          total_cost_usd=cost,
          total_fees_usd=fees,
          payout_usd=float(n),
          contracts=n,
          sharp_book=sharp.bookmaker,
          sharp_event_id=sharp.event_id,
          legs=[{
            "ticker": q.market_id,
            "side": "yes",
            "outcome": q.outcome,
            "ask": ask,
            "contracts": n,
            "cost_usd": round(ask * n, 4),
            "fee_usd": round(fees, 4),
            "venue": "polymarket",
            "token_id": q.token_id,
          }],
        )
      )
  opps.sort(key=lambda o: o.edge_prob, reverse=True)
  meta["opportunities"] = len(opps)
  return opps, meta


def scan_value_opportunities(
  sharp_events: Sequence[SharpEvent],
  *,
  kalshi_books: Sequence[SportsEventBook] | None = None,
  poly_quotes: Sequence[PolyMoneylineQuote] | None = None,
  min_edge_prob: float = 0.03,
  max_stake_usd: float = 5.0,
  kalshi_fee_rate: float = 0.07,
  poly_fee_rate: float = 0.0,
  min_match_score: float = 0.55,
  max_edge_prob: float = 0.12,
  fifa_draw_legs: bool = True,
) -> list[ValueOpportunity]:
  opps, _ = scan_value_opportunities_with_meta(
    sharp_events,
    kalshi_books=kalshi_books,
    poly_quotes=poly_quotes,
    min_edge_prob=min_edge_prob,
    max_stake_usd=max_stake_usd,
    kalshi_fee_rate=kalshi_fee_rate,
    poly_fee_rate=poly_fee_rate,
    min_match_score=min_match_score,
    max_edge_prob=max_edge_prob,
    fifa_draw_legs=fifa_draw_legs,
  )
  return opps


def scan_value_opportunities_with_meta(
  sharp_events: Sequence[SharpEvent],
  *,
  kalshi_books: Sequence[SportsEventBook] | None = None,
  poly_quotes: Sequence[PolyMoneylineQuote] | None = None,
  min_edge_prob: float = 0.03,
  max_stake_usd: float = 5.0,
  kalshi_fee_rate: float = 0.07,
  poly_fee_rate: float = 0.0,
  min_match_score: float = 0.55,
  max_edge_prob: float = 0.12,
  fifa_draw_legs: bool = True,
) -> tuple[list[ValueOpportunity], dict[str, Any]]:
  opps: list[ValueOpportunity] = []
  meta: dict[str, Any] = {"kalshi": {}, "poly": {}}
  if kalshi_books:
    k_opps, k_meta = scan_kalshi_value(
      sharp_events,
      kalshi_books,
      min_edge_prob=min_edge_prob,
      max_stake_usd=max_stake_usd,
      fee_rate=kalshi_fee_rate,
      min_match_score=min_match_score,
      max_edge_prob=max_edge_prob,
      fifa_draw_legs=fifa_draw_legs,
    )
    opps.extend(k_opps)
    meta["kalshi"] = k_meta
  if poly_quotes:
    p_opps, p_meta = scan_poly_value(
      sharp_events,
      poly_quotes,
      min_edge_prob=min_edge_prob,
      max_stake_usd=max_stake_usd,
      fee_rate=poly_fee_rate,
      min_match_score=min_match_score,
      max_edge_prob=max_edge_prob,
    )
    opps.extend(p_opps)
    meta["poly"] = p_meta
  opps.sort(key=lambda o: o.edge_prob, reverse=True)
  meta["opportunities"] = len(opps)
  meta["matched"] = len(opps)
  k = meta.get("kalshi") or {}
  meta["event_matches"] = k.get("event_matches", 0)
  meta["team_market_hits"] = k.get("team_market_hits", 0)
  meta["draw_market_hits"] = k.get("draw_market_hits", 0)
  meta["below_min_edge"] = k.get("below_min_edge", 0)
  meta["absurd_edge_rejects"] = k.get("absurd_edge_rejects", 0)
  meta["best_edge_prob"] = k.get("best_edge_prob")
  meta["best_edge_detail"] = k.get("best_edge_detail")
  if meta["best_edge_prob"] is None and (meta.get("poly") or {}).get("best_edge_prob") is not None:
    meta["best_edge_prob"] = meta["poly"]["best_edge_prob"]
  return opps, meta
