"""Kalshi Trade API — 15m slot reference (KXBTC15M/KXETH15M), index live price, optional RSA auth."""

from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.calibration.sources import KALSHI_EXIT_SOURCE, KALSHI_REF_SOURCE

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_SERIES = "KXBTC15M"
BRTI_INDEX_ID = "BRTI"
ETH_INDEX_ID = "ETHUSD_RTI"  # Kalshi/CF Benchmarks API id (not "ERTI")
_ETH_INDEX_ALIASES = frozenset({"ERTI", "ETHUSDRTI", "ETHUSD_RTI"})


def v2_book_side(*, side: str, action: str) -> str:
  """Map legacy yes/no + buy/sell to Kalshi V2 bid/ask book side."""
  s = str(side or "").lower()
  a = str(action or "buy").lower()
  if a == "buy":
    return "bid" if s == "yes" else "ask"
  return "ask" if s == "yes" else "bid"


def v2_yes_price_cents(*, side: str, leg_price_cents: int) -> int:
  """V2 order prices are always YES-denominated; invert NO leg prices."""
  p = max(1, min(99, int(leg_price_cents)))
  if str(side or "").lower() == "no":
    return 100 - p
  return p


def parse_v2_order_response(order: dict[str, Any]) -> dict[str, Any]:
  """Normalize create-order V2 response fields."""
  body = order.get("order") if isinstance(order.get("order"), dict) else order
  try:
    fill_count = float(body.get("fill_count") or 0)
  except (TypeError, ValueError):
    fill_count = 0.0
  try:
    remaining_count = float(body.get("remaining_count") or 0)
  except (TypeError, ValueError):
    remaining_count = 0.0
  return {
    "order_id": body.get("order_id") or order.get("order_id"),
    "fill_count": fill_count,
    "remaining_count": remaining_count,
  }


def position_net_from_row(row: dict[str, Any]) -> float:
  """Net YES contracts from a Kalshi market_positions row (negative = NO)."""
  raw = row.get("position_fp")
  if raw is None:
    raw = row.get("position")
  if raw is None:
    return 0.0
  try:
    return float(raw)
  except (TypeError, ValueError):
    return 0.0


def v2_price_dollars(cents: int) -> str:
  """Fixed-point dollar price string for V2 order requests."""
  return f"{int(cents) / 100:.4f}"


def index_slug(index_id: str) -> str:
  """Short slug for source labels (brti, erti, …)."""
  idx = str(index_id or "").upper()
  if idx in _ETH_INDEX_ALIASES:
    return "erti"
  if idx == BRTI_INDEX_ID:
    return "brti"
  return idx.lower().replace("-", "_")


def index_live_source(index_id: str) -> str:
  """Canonical live-tick source for settlement gates and dashboard."""
  return f"{index_slug(index_id)}_live"


@dataclass(frozen=True)
class KalshiSlotSettlement:
  """Kalshi KXBTC15M BRTI open/close for a 15m ET slot."""
  market_ticker: str
  slot_open: datetime
  slot_close: datetime
  open_brti: float
  close_brti: float | None
  status: str

  @property
  def settled(self) -> bool:
    # Kalshi sets expiration_value while status is still "determined" (before "finalized").
    if self.close_brti is None:
      return False
    return self.status not in ("active", "open", "unopened", "inactive")

  @property
  def outcome_up(self) -> bool | None:
    if not self.settled:
      return None
    return self.close_brti >= self.open_brti


@dataclass(frozen=True)
class KalshiPriceQuote:
  """Live BRTI or locked Kalshi slot target."""
  price: float
  source: str
  trade_time: datetime | None = None

  @property
  def age_sec(self) -> float | None:
    if self.trade_time is None:
      return None
    t = self.trade_time
    if t.tzinfo is None:
      t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - t).total_seconds())


@dataclass(frozen=True)
class KalshiSlotMarket:
  """Active Kalshi BTC 15m up/down contract aligned with the current ET slot."""
  market_ticker: str
  event_ticker: str
  title: str
  target_price: float  # floor_strike = BRTI 60s avg before slot open
  open_time: datetime
  close_time: datetime
  yes_bid: float | None
  yes_ask: float | None
  last_price: float | None
  status: str
  rules_primary: str

  def to_dict(self) -> dict[str, Any]:
    return {
      "market_ticker": self.market_ticker,
      "event_ticker": self.event_ticker,
      "title": self.title,
      "target_price": round(self.target_price, 2),
      "open_time": self.open_time.isoformat(),
      "close_time": self.close_time.isoformat(),
      "yes_bid": self.yes_bid,
      "yes_ask": self.yes_ask,
      "last_price": self.last_price,
      "status": self.status,
      "rules_primary": self.rules_primary,
    }


class KalshiClient:
  """Public market data + optional authenticated portfolio access."""

  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    kcfg = cfg.get("kalshi", {})
    self.base_url = (kcfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    self.series_ticker = kcfg.get("series_ticker", DEFAULT_SERIES)
    self.enabled = bool(kcfg.get("enabled", True))
    self.key_id = kcfg.get("key_id", "")
    self._private_key: rsa.RSAPrivateKey | None = None
    self._load_private_key(kcfg)
    self._cache: tuple[KalshiSlotMarket | None, float] | None = None
    self._cache_sec = float(kcfg.get("cache_sec", 15))
    self._brtI_cache: tuple[KalshiPriceQuote | None, float] | None = None
    self._brtI_cache_sec = float(kcfg.get("brti_cache_sec", 0))
    self._index_last_good: dict[str, KalshiPriceQuote] = {}
    self._index_cache: dict[str, tuple[KalshiPriceQuote | None, float]] = {}
    self._brtI_last_good: KalshiPriceQuote | None = None
    self._slot_targets: dict[str, float] = {}
    self._balance_cache: tuple[dict[str, Any] | None, float] | None = None
    self._balance_cache_sec = float(kcfg.get("balance_cache_sec", 30))
    pf = cfg.get("price_feed") or {}
    self._brtI_index = kcfg.get("brti_index_id") or pf.get("index_id") or BRTI_INDEX_ID

  def price_feed_label(self) -> str:
    pf = self.cfg.get("price_feed") or {}
    return str(pf.get("label") or f"Kalshi CF Benchmarks {self._brtI_index}")

  def settlement_reference_label(self) -> str:
    pf = self.cfg.get("price_feed") or {}
    if ref := pf.get("settlement_reference"):
      return str(ref)
    return f"CF Benchmarks {self._brtI_index} (Kalshi {self.series_ticker} settlement)"

  def _index_target_source(self) -> str:
    return f"kalshi_{index_slug(self._brtI_index)}_target"

  def _load_private_key(self, kcfg: dict[str, Any]) -> None:
    pem = kcfg.get("private_key", "")
    path = kcfg.get("private_key_path", "")
    passphrase = kcfg.get("private_key_passphrase") or None
    if not pem and path:
      p = Path(path).expanduser()
      if p.is_file():
        pem = p.read_text()
    if not pem:
      return
    pem = pem.replace("\\n", "\n")
    try:
      key = serialization.load_pem_private_key(
        pem.encode("utf-8"),
        password=passphrase.encode("utf-8") if passphrase else None,
      )
      if isinstance(key, rsa.RSAPrivateKey):
        self._private_key = key
    except Exception as e:
      log.warning("Kalshi private key not loaded: %s", e)

  @property
  def authenticated(self) -> bool:
    return bool(self.key_id and self._private_key)

  def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
    if not self._private_key:
      raise RuntimeError("Kalshi private key not configured")
    sign_path = path.split("?")[0]
    message = f"{timestamp_ms}{method.upper()}{sign_path}".encode("utf-8")
    sig = self._private_key.sign(
      message,
      padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
      hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")

  def _request(
    self,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    auth: bool = False,
    timeout: float = 15,
    critical: bool = False,
  ) -> dict[str, Any]:
    url = f"{self.base_url}{path}"
    headers: dict[str, str] = {"Accept": "application/json"}
    if json_body is not None:
      headers["Content-Type"] = "application/json"
    if auth:
      if not self.authenticated:
        raise RuntimeError("Kalshi API key ID and private key required for this endpoint")
      parsed = urlparse(url)
      # Kalshi signs API root path without query string
      sign_path = parsed.path
      ts = str(int(time.time() * 1000))
      headers.update({
        "KALSHI-ACCESS-KEY": self.key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, sign_path),
      })
    from src.trading.kalshi_circuit import get_circuit_breaker

    circuit = get_circuit_breaker()
    if circuit and not circuit.allows_request(critical=critical):
      raise RuntimeError("Kalshi API circuit breaker open — requests paused")

    try:
      resp = requests.request(
        method, url, params=params, json=json_body, headers=headers, timeout=timeout
      )
      resp.raise_for_status()
      if circuit:
        circuit.record_success()
      return resp.json()
    except Exception as e:
      if circuit:
        err = str(e)
        # Index passthrough errors must not pause discovery/entries for all bots.
        if "/cfbenchmarks/" in path and (
          "400 Client Error" in err or "429" in err or "Too Many Requests" in err
        ):
          log.warning("Kalshi cfbenchmarks error (not tripping circuit): %s", err[:200])
        else:
          circuit.record_failure(err)
      raise

  def get(
    self,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    auth: bool = False,
    critical: bool = False,
  ) -> dict[str, Any]:
    return self._request("GET", path, params=params, auth=auth, critical=critical, timeout=15)

  def post(
    self,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    auth: bool = False,
    critical: bool = False,
  ) -> dict[str, Any]:
    return self._request("POST", path, json_body=json_body, auth=auth, critical=critical, timeout=15)

  def create_order(
    self,
    *,
    ticker: str,
    side: str,
    count: int,
    yes_price: int | None = None,
    no_price: int | None = None,
    order_type: str = "limit",
    action: str = "buy",
    client_order_id: str | None = None,
  ) -> dict[str, Any]:
    """Place a limit order on Kalshi via V2 event-order API (requires auth)."""
    del order_type  # V2 create endpoint is limit-only; kept for caller compatibility.
    price_cents = yes_price if str(side).lower() == "yes" else no_price
    if price_cents is None:
      raise ValueError(f"price required for side={side!r}")
    v2_cents = v2_yes_price_cents(side=side, leg_price_cents=int(price_cents))
    body: dict[str, Any] = {
      "ticker": ticker,
      "client_order_id": client_order_id or str(uuid.uuid4()),
      "side": v2_book_side(side=side, action=action),
      "count": f"{int(count)}.00",
      "price": v2_price_dollars(v2_cents),
      "time_in_force": "good_till_canceled",
      "self_trade_prevention_type": "taker_at_cross",
    }
    critical = str(action).lower() == "sell"
    return self.post("/portfolio/events/orders", json_body=body, auth=True, critical=critical)

  def cancel_order(self, order_id: str) -> dict[str, Any]:
    """Cancel an open order by ID."""
    return self._request(
      "DELETE", f"/portfolio/events/orders/{order_id}", auth=True, critical=True
    )

  def list_resting_orders(self, *, ticker: str | None = None) -> list[dict[str, Any]]:
    """Open resting orders (V1 list endpoint; works for V2 event orders)."""
    if not self.authenticated:
      return []
    params: dict[str, Any] = {"status": "resting", "limit": 200}
    if ticker:
      params["ticker"] = ticker
    try:
      data = self.get("/portfolio/orders", params=params, auth=True)
    except Exception as e:
      log.warning("Kalshi list resting orders failed: %s", e)
      return []
    orders = data.get("orders") if isinstance(data, dict) else None
    return list(orders) if isinstance(orders, list) else []

  def cancel_resting_orders_for_ticker(self, ticker: str) -> int:
    """Cancel all resting orders on a market ticker. Returns count cancelled."""
    cancelled = 0
    for row in self.list_resting_orders(ticker=ticker):
      oid = row.get("order_id")
      if not oid:
        continue
      try:
        self.cancel_order(str(oid))
        cancelled += 1
      except Exception as e:
        log.warning("Cancel resting order %s on %s failed: %s", oid, ticker, e)
    return cancelled

  def get_market_position(self, ticker: str, *, critical: bool = False) -> float | None:
    """Net YES position for one market (negative = NO). None on API error."""
    if not self.authenticated:
      return None
    try:
      data = self.get(
        "/portfolio/positions",
        params={"ticker": str(ticker), "limit": 1},
        auth=True,
        critical=critical,
      )
    except Exception as e:
      log.warning("Kalshi get position for %s failed: %s", ticker, e)
      return None
    rows = data.get("market_positions") if isinstance(data, dict) else None
    if not rows:
      return 0.0
    row = rows[0] if isinstance(rows[0], dict) else None
    if not row:
      return 0.0
    return position_net_from_row(row)

  def list_market_positions(self, *, limit: int = 200, critical: bool = False) -> list[dict[str, Any]]:
    """All non-flat Kalshi market positions."""
    if not self.authenticated:
      return []
    try:
      data = self.get(
        "/portfolio/positions",
        params={"limit": int(limit)},
        auth=True,
        critical=critical,
      )
    except Exception as e:
      log.warning("Kalshi list positions failed: %s", e)
      return []
    rows = data.get("market_positions") if isinstance(data, dict) else None
    if not isinstance(rows, list):
      return []
    out: list[dict[str, Any]] = []
    for row in rows:
      if not isinstance(row, dict):
        continue
      net = position_net_from_row(row)
      if abs(net) < 0.005:
        continue
      out.append(row)
    return out

  def list_fills(
    self,
    *,
    ticker: str | None = None,
    order_id: str | None = None,
    limit: int = 200,
    critical: bool = False,
  ) -> list[dict[str, Any]]:
    """Executed fills for the authenticated member (newest pages first)."""
    if not self.authenticated:
      return []
    params: dict[str, Any] = {"limit": min(int(limit), 200)}
    if ticker:
      params["ticker"] = str(ticker)
    if order_id:
      params["order_id"] = str(order_id)
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while pages < 5:
      if cursor:
        params["cursor"] = cursor
      try:
        data = self.get("/portfolio/fills", params=params, auth=True, critical=critical)
      except Exception as e:
        log.warning("Kalshi list fills failed: %s", e)
        break
      rows = data.get("fills") if isinstance(data, dict) else None
      if not isinstance(rows, list) or not rows:
        break
      out.extend(r for r in rows if isinstance(r, dict))
      cursor = data.get("cursor") if isinstance(data, dict) else None
      pages += 1
      if not cursor or len(out) >= limit:
        break
    return out[:limit]

  def list_settlements(
    self,
    *,
    ticker: str | None = None,
    limit: int = 100,
  ) -> list[dict[str, Any]]:
    """Settlement payouts for expired markets."""
    if not self.authenticated:
      return []
    params: dict[str, Any] = {"limit": min(int(limit), 200)}
    if ticker:
      params["ticker"] = str(ticker)
    try:
      data = self.get("/portfolio/settlements", params=params, auth=True)
    except Exception as e:
      log.warning("Kalshi list settlements failed: %s", e)
      return []
    rows = data.get("settlements") if isinstance(data, dict) else None
    return list(rows) if isinstance(rows, list) else []

  def get_market_ticker(self, ticker: str) -> dict[str, Any] | None:
    """Single market row (status, expiration_value, strikes)."""
    if not self.enabled:
      return None
    try:
      data = self.get(f"/markets/{ticker}")
      row = data.get("market") if isinstance(data, dict) else None
      return row if isinstance(row, dict) else data if isinstance(data, dict) else None
    except Exception as e:
      log.warning("Kalshi get market %s failed: %s", ticker, e)
      return None

  def portfolio_balance(self, *, fresh: bool = False) -> dict[str, Any] | None:
    """Kalshi cash balance (cents); cached to limit /portfolio/balance polling."""
    if not self.authenticated:
      return None
    now_mono = time.monotonic()
    if (
      not fresh
      and self._balance_cache
      and (now_mono - self._balance_cache[1]) < self._balance_cache_sec
    ):
      return self._balance_cache[0]
    try:
      bal = self.get("/portfolio/balance", auth=True)
      self._balance_cache = (bal, now_mono)
      return bal
    except Exception as e:
      log.warning("Kalshi portfolio balance failed: %s", e)
      if self._balance_cache:
        return self._balance_cache[0]
      return None

  @staticmethod
  def balance_cents_from_payload(bal: dict[str, Any] | None) -> int | None:
    if not bal:
      return None
    raw = bal.get("balance")
    if raw is None:
      return None
    try:
      return int(raw)
    except (TypeError, ValueError):
      return None

  @staticmethod
  def balance_usd_from_cents(cents: int | None) -> float | None:
    if cents is None:
      return None
    return round(cents / 100.0, 2)

  @staticmethod
  def _parse_ts(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if ts.tzinfo is None:
      ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)

  @staticmethod
  def _dollars(raw: str | float | None) -> float | None:
    if raw is None or raw == "":
      return None
    return float(raw)

  def _market_from_row(self, row: dict[str, Any]) -> KalshiSlotMarket | None:
    strike = row.get("floor_strike")
    if strike is None:
      return None
    open_time = row.get("open_time")
    close_time = row.get("close_time")
    if not open_time or not close_time:
      return None
    return KalshiSlotMarket(
      market_ticker=str(row.get("ticker", "")),
      event_ticker=str(row.get("event_ticker", "")),
      title=str(row.get("title", "")),
      target_price=float(strike),
      open_time=self._parse_ts(open_time),
      close_time=self._parse_ts(close_time),
      yes_bid=self._dollars(row.get("yes_bid_dollars")),
      yes_ask=self._dollars(row.get("yes_ask_dollars")),
      last_price=self._dollars(row.get("last_price_dollars")),
      status=str(row.get("status", "")),
      rules_primary=str(row.get("rules_primary", "")),
    )

  def active_slot15m_market(self, *, fresh: bool = False) -> KalshiSlotMarket | None:
    """Current open 15m up/down contract (index target at slot open)."""
    if not self.enabled:
      return None
    now_mono = time.monotonic()
    if not fresh and self._cache and (now_mono - self._cache[1]) < self._cache_sec:
      return self._cache[0]

    market: KalshiSlotMarket | None = None
    try:
      data = self.get(
        "/markets",
        params={"series_ticker": self.series_ticker, "status": "open", "limit": 20},
      )
      now = datetime.now(timezone.utc)
      candidates: list[KalshiSlotMarket] = []
      for row in data.get("markets", []):
        m = self._market_from_row(row)
        if m and m.open_time <= now < m.close_time:
          candidates.append(m)
      if candidates:
        market = min(candidates, key=lambda m: m.close_time)
      elif data.get("markets"):
        # Fallback: nearest open market by close time
        parsed = [self._market_from_row(r) for r in data["markets"]]
        parsed = [m for m in parsed if m]
        if parsed:
          market = min(parsed, key=lambda m: abs((m.open_time - now).total_seconds()))
    except Exception as e:
      log.warning("Kalshi market fetch failed: %s", e)

    self._cache = (market, now_mono)
    return market

  def active_btc15m_market(self, *, fresh: bool = False) -> KalshiSlotMarket | None:
    """Backward-compatible alias for active_slot15m_market."""
    return self.active_slot15m_market(fresh=fresh)

  @staticmethod
  def _parse_brti_value(data: dict[str, Any]) -> float | None:
    """Parse CF Benchmarks BRTI from Kalshi passthrough envelope or raw payload."""

    def _as_float(raw: Any) -> float | None:
      if raw is None or raw == "":
        return None
      try:
        return float(raw)
      except (TypeError, ValueError):
        return None

    def _from_obj(obj: Any) -> float | None:
      if isinstance(obj, list):
        for item in reversed(obj):
          v = _from_obj(item)
          if v is not None:
            return v
        return None
      if not isinstance(obj, dict):
        return _as_float(obj)
      for key in ("value", "midPrice", "mid_price", "index_value", "price", "last"):
        v = _as_float(obj.get(key))
        if v is not None:
          return v
      for key in ("payload", "data", "index", "msg"):
        nested = obj.get(key)
        if nested is not None:
          v = _from_obj(nested)
          if v is not None:
            return v
      return None

    return _from_obj(data)

  def fetch_brti_live(self, *, fresh: bool = False) -> KalshiPriceQuote | None:
    """Live CF Benchmarks BRTI via Kalshi passthrough (requires API auth)."""
    return self.fetch_index_live(self._brtI_index, fresh=fresh)

  def fetch_index_live(self, index_id: str | None = None, *, fresh: bool = False) -> KalshiPriceQuote | None:
    """Live CF Benchmarks index (BRTI, ERTI, …) via Kalshi passthrough."""
    if not self.authenticated:
      return None
    idx = (index_id or self._brtI_index).upper()
    source = index_live_source(idx)
    now_mono = time.monotonic()
    cached = self._index_cache.get(idx)
    if not fresh and cached and (now_mono - cached[1]) < self._brtI_cache_sec:
      return cached[0]
    quote: KalshiPriceQuote | None = None
    try:
      data = self.get(
        "/cfbenchmarks/values",
        params={"id": idx},
        auth=True,
      )
      price = self._parse_brti_value(data)
      if price is not None:
        quote = KalshiPriceQuote(price=price, source=source, trade_time=datetime.now(timezone.utc))
        self._index_last_good[idx] = quote
        if idx == self._brtI_index:
          self._brtI_last_good = quote
      elif log.isEnabledFor(logging.DEBUG):
        log.debug("Kalshi %s response had no parseable value: %s", idx, data)
    except Exception as e:
      log.warning("Kalshi %s fetch failed: %s", idx, e)
    self._index_cache[idx] = (quote, now_mono)
    if idx == self._brtI_index:
      self._brtI_cache = (quote, now_mono)
    return quote

  def last_index_quote(self, index_id: str | None = None) -> KalshiPriceQuote | None:
    idx = (index_id or self._brtI_index).upper()
    return self._index_last_good.get(idx) or (self._brtI_last_good if idx == self._brtI_index else None)

  def last_brti_quote(self) -> KalshiPriceQuote | None:
    """Most recent successful BRTI tick (may be stale if fetch is failing)."""
    return self._brtI_last_good

  def _slot_key(self, slot_start: pd.Timestamp) -> str:
    slot = pd.Timestamp(slot_start)
    if slot.tzinfo is None:
      slot = slot.tz_localize("UTC")
    else:
      slot = slot.tz_convert("UTC")
    return slot.isoformat()

  def slot_t0_reference(
    self,
    slot_start: pd.Timestamp | datetime | None = None,
    *,
    fresh: bool = False,
  ) -> tuple[float | None, str]:
    """Kalshi 15m floor_strike — index 60s avg before slot open."""
    target_src = self._index_target_source()
    if slot_start is not None:
      slot_s = pd.Timestamp(slot_start)
      if slot_s.tzinfo is None:
        slot_s = slot_s.tz_localize("UTC")
      else:
        slot_s = slot_s.tz_convert("UTC")
      key = self._slot_key(slot_s)
      if key in self._slot_targets:
        return self._slot_targets[key], target_src

      row = self.market_for_slot(slot_s)
      if row is not None:
        floor = row.get("floor_strike")
        if floor is not None:
          price = float(floor)
          self._slot_targets[key] = price
          return price, target_src

    market = self.active_slot15m_market(fresh=fresh)
    if market is not None:
      now = datetime.now(timezone.utc)
      if market.open_time <= now < market.close_time:
        key = self._slot_key(market.open_time)
        self._slot_targets[key] = market.target_price
        return market.target_price, target_src
      if slot_start is not None:
        slot_s = pd.Timestamp(slot_start)
        if slot_s.tzinfo is None:
          slot_s = slot_s.tz_localize("UTC")
        else:
          slot_s = slot_s.tz_convert("UTC")
        if self._slot_key(market.open_time) == self._slot_key(slot_s):
          key = self._slot_key(slot_s)
          self._slot_targets[key] = market.target_price
          return market.target_price, target_src

    return None, ""

  def live_quote(self, *, fresh: bool = False, allow_target_fallback: bool = False) -> KalshiPriceQuote | None:
    """Current BRTI for P&L. Never uses static t=0 unless allow_target_fallback=True."""
    brti = self.fetch_brti_live(fresh=fresh)
    if brti is not None:
      return brti
    if not fresh and self._brtI_last_good is not None:
      return self._brtI_last_good
    if allow_target_fallback:
      market = self.active_slot15m_market(fresh=fresh)
      if market:
        return KalshiPriceQuote(
          price=market.target_price,
          source=self._index_target_source(),
          trade_time=market.open_time,
        )
    return None

  def lock_current_slot_reference(self, slot_start: pd.Timestamp) -> float | None:
    price, _ = self.slot_t0_reference(slot_start, fresh=True)
    return price

  def market_for_slot(self, slot_start: pd.Timestamp) -> dict[str, Any] | None:
    """KXBTC15M market row whose open_time matches this ET slot (public API)."""
    if not self.enabled:
      return None
    slot_s = pd.Timestamp(slot_start)
    if slot_s.tzinfo is None:
      slot_s = slot_s.tz_localize("UTC")
    else:
      slot_s = slot_s.tz_convert("UTC")
    from src.features.slots import slot_end

    slot_e = slot_end(slot_s)
    close_ts = int(slot_e.timestamp())
    try:
      data = self.get(
        "/markets",
        params={
          "series_ticker": self.series_ticker,
          "min_close_ts": close_ts - 2,
          "max_close_ts": close_ts + 2,
          "limit": 10,
        },
      )
      for row in data.get("markets", []):
        open_time = row.get("open_time")
        if not open_time:
          continue
        ot = self._parse_ts(open_time)
        if abs((ot - slot_s.to_pydatetime()).total_seconds()) <= 60:
          return row
    except Exception as e:
      log.warning("Kalshi market_for_slot failed: %s", e)
    return None

  def slot_settlement(self, slot_start: pd.Timestamp) -> KalshiSlotSettlement | None:
    """BRTI t=0 (floor_strike) and close (expiration_value) for a slot."""
    row = self.market_for_slot(slot_start)
    if not row:
      return None
    floor = row.get("floor_strike")
    if floor is None:
      return None
    exp_raw = row.get("expiration_value")
    close_brti = float(exp_raw) if exp_raw not in (None, "") else None
    return KalshiSlotSettlement(
      market_ticker=str(row.get("ticker", "")),
      slot_open=self._parse_ts(row["open_time"]),
      slot_close=self._parse_ts(row["close_time"]),
      open_brti=float(floor),
      close_brti=close_brti,
      status=str(row.get("status", "")),
    )

  def iter_settled_markets(self, *, limit: int = 200, max_pages: int = 50):
    """Yield settled KXBTC15M markets (newest first)."""
    cursor: str | None = None
    pages = 0
    while pages < max_pages:
      params: dict[str, Any] = {
        "series_ticker": self.series_ticker,
        "status": "settled",
        "limit": min(limit, 200),
      }
      if cursor:
        params["cursor"] = cursor
      try:
        data = self.get("/markets", params=params)
      except Exception as e:
        log.warning("Kalshi settled markets fetch failed: %s", e)
        break
      markets = data.get("markets", [])
      if not markets:
        break
      for row in markets:
        yield row
      cursor = data.get("cursor")
      pages += 1
      if not cursor:
        break

  def resolution_for_entry(
    self,
    entry_price: float,
    settlement: KalshiSlotSettlement,
  ) -> tuple[float, float, int] | None:
    """Return (exit_brti, actual_return, outcome_up_int) when slot is settled."""
    if not settlement.settled or settlement.close_brti is None:
      return None
    open_brti = settlement.open_brti
    close_brti = settlement.close_brti
    if open_brti <= 0:
      return None
    actual_return = (close_brti - open_brti) / open_brti
    outcome = 1 if close_brti >= open_brti else 0
    return close_brti, actual_return, outcome

  def active_market_summary(self) -> dict[str, Any] | None:
    """Compact Kalshi market info for dashboard/bots."""
    active = self.active_slot15m_market()
    if not active:
      return None
    yes_mid = None
    if active.yes_bid is not None and active.yes_ask is not None:
      yes_mid = (active.yes_bid + active.yes_ask) / 2
    elif active.last_price is not None:
      yes_mid = active.last_price
    return {
      "market_ticker": active.market_ticker,
      "yes_bid": round(active.yes_bid, 4) if active.yes_bid is not None else None,
      "yes_ask": round(active.yes_ask, 4) if active.yes_ask is not None else None,
      "yes_mid": round(yes_mid, 4) if yes_mid is not None else None,
      "title": active.title,
    }

  def status(self) -> dict[str, Any]:
    bal = self.portfolio_balance() if self.authenticated else None
    balance_cents = self.balance_cents_from_payload(bal)
    active = self.active_slot15m_market()
    brti = self.fetch_brti_live()
    out: dict[str, Any] = {
      "enabled": self.enabled,
      "authenticated": self.authenticated,
      "series_ticker": self.series_ticker,
      "base_url": self.base_url,
      "connected": active is not None,
      "brti_live": brti.price if brti else None,
      "balance_cents": balance_cents,
      "balance_usd": self.balance_usd_from_cents(balance_cents),
    }
    if active:
      out["active_market"] = active.to_dict()
    return out


def load_kalshi_config(cfg: dict[str, Any]) -> dict[str, Any]:
  """Merge kalshi settings from config.yaml and environment."""
  kcfg = dict(cfg.get("kalshi") or {})
  if os.getenv("KALSHI_ENABLED", "").lower() in ("0", "false", "no"):
    kcfg["enabled"] = False
  elif os.getenv("KALSHI_ENABLED", "").lower() in ("1", "true", "yes"):
    kcfg["enabled"] = True
  if key_id := os.getenv("KALSHI_KEY_ID"):
    kcfg["key_id"] = key_id
  if path := os.getenv("KALSHI_PRIVATE_KEY_PATH"):
    kcfg["private_key_path"] = path
  if pem := os.getenv("KALSHI_PRIVATE_KEY"):
    kcfg["private_key"] = pem
  if pw := os.getenv("KALSHI_PRIVATE_KEY_PASSPHRASE"):
    kcfg["private_key_passphrase"] = pw
  if base := os.getenv("KALSHI_BASE_URL"):
    kcfg["base_url"] = base
  if series := os.getenv("KALSHI_SERIES_TICKER"):
    kcfg["series_ticker"] = series
  return kcfg
