"""Kalshi Trade API — KXBTC15M market context and optional RSA auth."""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_SERIES = "KXBTC15M"


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
    kcfg = cfg.get("kalshi", {})
    self.base_url = (kcfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    self.series_ticker = kcfg.get("series_ticker", DEFAULT_SERIES)
    self.enabled = bool(kcfg.get("enabled", True))
    self.key_id = kcfg.get("key_id", "")
    self._private_key: rsa.RSAPrivateKey | None = None
    self._load_private_key(kcfg)
    self._cache: tuple[KalshiSlotMarket | None, float] | None = None
    self._cache_sec = float(kcfg.get("cache_sec", 15))

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
    auth: bool = False,
    timeout: float = 15,
  ) -> dict[str, Any]:
    url = f"{self.base_url}{path}"
    headers: dict[str, str] = {"Accept": "application/json"}
    if auth:
      if not self.authenticated:
        raise RuntimeError("Kalshi API key ID and private key required for this endpoint")
      parsed = urlparse(url)
      sign_path = parsed.path
      if parsed.query:
        sign_path = f"{parsed.path}?{parsed.query}"
      ts = str(int(time.time() * 1000))
      headers.update({
        "KALSHI-ACCESS-KEY": self.key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, sign_path),
      })
    resp = requests.request(method, url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

  def get(self, path: str, *, params: dict[str, Any] | None = None, auth: bool = False) -> dict[str, Any]:
    return self._request("GET", path, params=params, auth=auth)

  def portfolio_balance(self) -> dict[str, Any] | None:
    """Verify API credentials; returns balance dict or None if not configured."""
    if not self.authenticated:
      return None
    try:
      return self.get("/portfolio/balance", auth=True)
    except Exception as e:
      log.warning("Kalshi portfolio balance failed: %s", e)
      return None

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

  def active_btc15m_market(self, *, fresh: bool = False) -> KalshiSlotMarket | None:
    """Current open KXBTC15M contract (BRTI target at slot open)."""
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

  def status(self) -> dict[str, Any]:
    bal = self.portfolio_balance() if self.authenticated else None
    active = self.active_btc15m_market()
    out: dict[str, Any] = {
      "enabled": self.enabled,
      "authenticated": self.authenticated,
      "series_ticker": self.series_ticker,
      "base_url": self.base_url,
      "connected": active is not None,
      "balance_cents": bal.get("balance") if bal else None,
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
