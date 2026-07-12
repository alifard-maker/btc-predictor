"""The Odds API client — sharp fair lines for sports value (Goal 3).

API key via ODDS_API_KEY env (never commit). Free-tier quota is tiny; cache hard.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.the-odds-api.com/v4"
# Prefer Pinnacle; fall back across these if missing on an event.
PREFERRED_BOOKS = ("pinnacle", "betfair_ex_eu", "betfair_ex_uk", "matchbook")


@dataclass(frozen=True)
class SharpOutcome:
  name: str
  decimal_odds: float
  implied_prob: float
  fair_prob: float


@dataclass(frozen=True)
class SharpEvent:
  sport_key: str
  event_id: str
  home_team: str
  away_team: str
  commence_time: str | None
  bookmaker: str
  outcomes: tuple[SharpOutcome, ...]

  def fair_for(self, team: str) -> float | None:
    target = _norm(team)
    for o in self.outcomes:
      if _norm(o.name) == target or target in _norm(o.name) or _norm(o.name) in target:
        return o.fair_prob
    return None

  def fair_for_draw(self) -> float | None:
    """Fair probability for soccer 3-way draw/tie (Odds API h2h third outcome)."""
    for label in ("Draw", "Tie"):
      fair = self.fair_for(label)
      if fair is not None and fair > 0:
        return fair
    home_n = _norm(self.home_team)
    away_n = _norm(self.away_team)
    for o in self.outcomes:
      name_n = _norm(o.name)
      if not name_n or name_n in (home_n, away_n):
        continue
      if name_n in ("draw", "tie") or "draw" in name_n:
        return o.fair_prob
    return None

  def to_dict(self) -> dict[str, Any]:
    return {
      "sport_key": self.sport_key,
      "event_id": self.event_id,
      "home_team": self.home_team,
      "away_team": self.away_team,
      "commence_time": self.commence_time,
      "bookmaker": self.bookmaker,
      "outcomes": [
        {
          "name": o.name,
          "decimal_odds": o.decimal_odds,
          "implied_prob": round(o.implied_prob, 6),
          "fair_prob": round(o.fair_prob, 6),
        }
        for o in self.outcomes
      ],
    }


def _norm(s: str) -> str:
  return " ".join("".join(c if c.isalnum() or c.isspace() else " " for c in (s or "").lower()).split())


def multiplicative_devig(decimal_odds: list[float]) -> list[float]:
  """Convert decimal odds → fair probs by normalizing implied probs (multiplicative)."""
  implied = []
  for o in decimal_odds:
    if o is None or float(o) <= 1.0:
      implied.append(0.0)
    else:
      implied.append(1.0 / float(o))
  total = sum(implied)
  if total <= 0:
    return [0.0] * len(implied)
  return [p / total for p in implied]


def odds_api_key(cfg: dict[str, Any] | None = None) -> str:
  env = (os.getenv("ODDS_API_KEY") or "").strip()
  if env:
    return env
  sports = dict((cfg or {}).get("sports") or {})
  value = dict((sports.get("strategies") or {}).get("value_sharp") or {})
  return str(value.get("api_key") or sports.get("odds_api_key") or "").strip()


class OddsApiClient:
  def __init__(self, cfg: dict[str, Any] | None = None):
    self.cfg = cfg or {}
    sports = dict(self.cfg.get("sports") or {})
    value = dict((sports.get("strategies") or {}).get("value_sharp") or {})
    self.base = str(value.get("base_url") or DEFAULT_BASE).rstrip("/")
    self.cache_sec = float(value.get("odds_cache_sec", 21600))  # 6h default (free quota)
    self.regions = str(value.get("regions") or "us")
    if "bookmakers" in value:
      books = value.get("bookmakers") or []
    else:
      books = list(PREFERRED_BOOKS)
    if isinstance(books, str):
      books = [b.strip() for b in books.split(",") if b.strip()]
    self.bookmakers = [str(b) for b in books]
    self.sport_keys = [str(s) for s in (value.get("sport_keys") or ["baseball_mlb", "soccer_epl"])]
    self._cache: dict[str, tuple[Any, float]] = {}
    self.last_remaining: int | None = None
    self.last_used: int | None = None

  @property
  def configured(self) -> bool:
    return bool(odds_api_key(self.cfg))

  def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
    key = odds_api_key(self.cfg)
    if not key:
      raise RuntimeError("ODDS_API_KEY not set")
    q = dict(params or {})
    q["apiKey"] = key
    url = f"{self.base}{path}"
    resp = requests.get(url, params=q, timeout=25)
    rem = resp.headers.get("x-requests-remaining")
    used = resp.headers.get("x-requests-used")
    try:
      self.last_remaining = int(rem) if rem is not None else self.last_remaining
      self.last_used = int(used) if used is not None else self.last_used
    except ValueError:
      pass
    if resp.status_code != 200:
      raise RuntimeError(f"odds_api_{resp.status_code}:{resp.text[:200]}")
    return resp.json()

  def _cached(self, cache_key: str, fetcher, cache_sec: float | None = None):
    ttl = self.cache_sec if cache_sec is None else float(cache_sec)
    hit = self._cache.get(cache_key)
    if hit and (time.monotonic() - hit[1]) < ttl:
      return hit[0]
    data = fetcher()
    self._cache[cache_key] = (data, time.monotonic())
    return data

  def list_sports(self) -> list[dict[str, Any]]:
    # /sports does not count against quota
    return self._cached("sports", lambda: self._get("/sports"), cache_sec=max(self.cache_sec, 3600))

  def discover_active_tennis_keys(self, *, max_atp: int = 2, max_wta: int = 2) -> list[str]:
    """Odds API tennis is tournament-scoped — pick active ATP/WTA keys (quota-safe)."""
    try:
      sports = self.list_sports()
    except Exception as exc:
      log.warning("odds_api list_sports failed: %s", exc)
      return []
    atp: list[str] = []
    wta: list[str] = []
    for s in sports:
      if not isinstance(s, dict) or not s.get("active"):
        continue
      key = str(s.get("key") or "")
      if key.startswith("tennis_atp_") and "doubles" not in key:
        atp.append(key)
      elif key.startswith("tennis_wta_") and "doubles" not in key:
        wta.append(key)
    return atp[: max(0, int(max_atp))] + wta[: max(0, int(max_wta))]

  def resolve_sport_keys(self, sport_keys: list[str] | None = None) -> list[str]:
    keys = list(sport_keys if sport_keys is not None else self.sport_keys)
    out: list[str] = []
    seen: set[str] = set()
    for k in keys:
      if k == "tennis_auto" or k in ("tennis_atp_wta", "tennis"):
        for tk in self.discover_active_tennis_keys(
          max_atp=int(self._value_cfg().get("tennis_max_atp", 2)),
          max_wta=int(self._value_cfg().get("tennis_max_wta", 2)),
        ):
          if tk not in seen:
            seen.add(tk)
            out.append(tk)
        continue
      if k not in seen:
        seen.add(k)
        out.append(k)
    return out

  def _value_cfg(self) -> dict[str, Any]:
    sports = dict(self.cfg.get("sports") or {})
    return dict((sports.get("strategies") or {}).get("value_sharp") or {})

  def fetch_h2h_odds(self, sport_key: str) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
      "regions": self.regions,
      "markets": "h2h",
      "oddsFormat": "decimal",
    }
    # US region rarely has Pinnacle — filtering then retrying burns 2× credits.
    # Only apply bookmaker filter when it is likely to hit (non-us regions).
    regions_l = {r.strip().lower() for r in self.regions.split(",") if r.strip()}
    use_book_filter = bool(self.bookmakers) and not regions_l.issubset({"us"})
    if use_book_filter:
      params["bookmakers"] = ",".join(self.bookmakers[:4])

    def _fetch():
      raw = self._get(f"/sports/{sport_key}/odds", params)
      return raw if isinstance(raw, list) else []

    data = self._cached(f"odds:{sport_key}:{self.regions}", _fetch)
    return data if isinstance(data, list) else []

  def _pick_book(self, bookmakers: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_key = {str(b.get("key") or ""): b for b in bookmakers if isinstance(b, dict)}
    for pref in list(self.bookmakers) + list(PREFERRED_BOOKS):
      if pref in by_key:
        return by_key[pref]
    # US sharp-ish preference when Pinnacle absent
    for pref in ("draftkings", "fanduel", "betonlineag", "bovada", "williamhill_us"):
      if pref in by_key:
        return by_key[pref]
    return bookmakers[0] if bookmakers else None

  def parse_sharp_event(self, sport_key: str, raw: dict[str, Any]) -> SharpEvent | None:
    books = list(raw.get("bookmakers") or [])
    book = self._pick_book(books)
    if not book:
      return None
    h2h = None
    for m in book.get("markets") or []:
      if str(m.get("key") or "") == "h2h":
        h2h = m
        break
    if not h2h:
      return None
    outcomes_raw = list(h2h.get("outcomes") or [])
    if len(outcomes_raw) < 2:
      return None
    names = [str(o.get("name") or "") for o in outcomes_raw]
    prices = [float(o.get("price") or 0) for o in outcomes_raw]
    fair = multiplicative_devig(prices)
    outs = tuple(
      SharpOutcome(
        name=names[i],
        decimal_odds=prices[i],
        implied_prob=(1.0 / prices[i]) if prices[i] > 1 else 0.0,
        fair_prob=fair[i],
      )
      for i in range(len(names))
    )
    return SharpEvent(
      sport_key=sport_key,
      event_id=str(raw.get("id") or ""),
      home_team=str(raw.get("home_team") or ""),
      away_team=str(raw.get("away_team") or ""),
      commence_time=raw.get("commence_time"),
      bookmaker=str(book.get("key") or ""),
      outcomes=outs,
    )

  def fetch_sharp_events(self, sport_keys: list[str] | None = None) -> list[SharpEvent]:
    keys = self.resolve_sport_keys(sport_keys)
    out: list[SharpEvent] = []
    for sk in keys:
      try:
        raw_list = self.fetch_h2h_odds(sk)
      except Exception as exc:
        log.warning("odds_api fetch %s failed: %s", sk, exc)
        continue
      for raw in raw_list:
        if not isinstance(raw, dict):
          continue
        ev = self.parse_sharp_event(sk, raw)
        if ev:
          out.append(ev)
    return out

  def quota_status(self) -> dict[str, Any]:
    return {
      "configured": self.configured,
      "requests_remaining": self.last_remaining,
      "requests_used": self.last_used,
      "cache_sec": self.cache_sec,
      "regions": self.regions,
      "sport_keys": list(self.sport_keys),
      "resolved_sport_keys": self.resolve_sport_keys() if self.configured else [],
    }
