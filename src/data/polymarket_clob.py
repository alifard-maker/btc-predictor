"""Polymarket CLOB trading client — L1/L2 auth ready; orders gated by allow_live."""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_CHAIN_ID = 137


def _poly_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  return dict((dict((cfg or {}).get("sports") or {}).get("polymarket") or {}))


def _env(name: str) -> str:
  return (os.getenv(name) or "").strip()


def polymarket_private_key() -> str:
  # Prefer namespaced key; accept PRIVATE_KEY as fallback for local scripts.
  return _env("POLYMARKET_PRIVATE_KEY") or _env("PRIVATE_KEY")


def polymarket_key_configured() -> bool:
  return bool(polymarket_private_key()) or bool(
    _env("POLY_CLOB_API_KEY") and _env("POLY_CLOB_SECRET") and _env("POLY_CLOB_PASSPHRASE")
  )


class PolymarketClobClient:
  """Authenticated CLOB wrapper. Never places orders unless allow_live is true."""

  def __init__(self, cfg: dict[str, Any] | None = None):
    self.cfg = cfg or {}
    poly = _poly_cfg(self.cfg)
    self.enabled = bool(poly.get("enabled", False))
    self.mode = str(poly.get("mode") or "paper").lower()
    self.allow_live = bool(poly.get("allow_live", False))
    self.host = str(poly.get("clob_host") or DEFAULT_CLOB_HOST).rstrip("/")
    self.chain_id = int(poly.get("chain_id") or DEFAULT_CHAIN_ID)
    self.signature_type = int(
      _env("POLY_SIGNATURE_TYPE") or poly.get("signature_type") or 0
    )
    self.funder = _env("POLY_FUNDER_ADDRESS") or str(poly.get("funder") or "").strip() or None

    self._client: Any = None
    self._authenticated = False
    self._auth_error: str | None = None
    self._creds_source: str | None = None

    if self.enabled:
      self._try_authenticate()

  @property
  def paper_only(self) -> bool:
    return (not self.allow_live) or self.mode != "live"

  @property
  def authenticated(self) -> bool:
    return bool(self._authenticated and self._client is not None)

  @property
  def live_ready(self) -> bool:
    """True when CLOB auth works — does NOT mean live trading is armed."""
    return self.authenticated and self._auth_error is None

  def _try_authenticate(self) -> None:
    try:
      self._client = self._build_client()
      self._authenticated = self._client is not None
      if self._authenticated:
        # Soft health probe — ignore failures (network) but keep client.
        try:
          self._client.get_ok()
        except Exception as exc:
          log.info("polymarket CLOB get_ok probe: %s", exc)
    except Exception as exc:
      self._client = None
      self._authenticated = False
      self._auth_error = str(exc)[:240]
      log.warning("polymarket CLOB auth failed: %s", exc)

  def _build_client(self) -> Any:
    try:
      from py_clob_client_v2 import ApiCreds, ClobClient
    except ImportError as exc:
      raise RuntimeError("py-clob-client-v2 not installed") from exc

    pk = polymarket_private_key()
    api_key = _env("POLY_CLOB_API_KEY")
    api_secret = _env("POLY_CLOB_SECRET")
    api_pass = _env("POLY_CLOB_PASSPHRASE")

    creds = None
    if api_key and api_secret and api_pass:
      creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass)
      self._creds_source = "env_l2"
    elif pk:
      # L1 → derive L2 (in-memory only; never log secret fields)
      temp = ClobClient(host=self.host, chain_id=self.chain_id, key=pk)
      creds = temp.create_or_derive_api_key()
      self._creds_source = "derived_l1"
    else:
      self._auth_error = "missing_POLYMARKET_PRIVATE_KEY_or_L2_creds"
      return None

    kwargs: dict[str, Any] = {
      "host": self.host,
      "chain_id": self.chain_id,
      "creds": creds,
      "signature_type": self.signature_type,
    }
    if pk:
      kwargs["key"] = pk
    if self.funder:
      kwargs["funder"] = self.funder
    return ClobClient(**kwargs)

  def place_buy(
    self,
    *,
    token_id: str,
    price: float,
    size: float,
    order_type: str = "FOK",
  ) -> dict[str, Any]:
    """Place a BUY on CLOB. Hard-blocked unless allow_live and mode=live."""
    if not self.allow_live or self.mode != "live":
      return {
        "ok": False,
        "action": "live_skip",
        "reason": "poly_live_disabled",
        "allow_live": self.allow_live,
        "mode": self.mode,
      }
    if not self.authenticated or self._client is None:
      return {"ok": False, "action": "live_skip", "reason": "poly_not_authenticated"}

    token_id = str(token_id or "").strip()
    price = float(price)
    size = float(size)
    if not token_id or price <= 0 or size <= 0:
      return {"ok": False, "action": "live_skip", "reason": "bad_order_args"}

    try:
      from py_clob_client_v2 import OrderType
      from py_clob_client_v2.clob_types import OrderArgsV2
    except ImportError as exc:
      return {"ok": False, "action": "live_failed", "error": str(exc)}

    ot = getattr(OrderType, str(order_type).upper(), OrderType.FOK)
    order_args = OrderArgsV2(
      token_id=token_id,
      price=max(0.01, min(0.99, price)),
      size=size,
      side="BUY",
    )
    try:
      resp = self._client.create_and_post_order(order_args, order_type=ot)
      return {"ok": True, "action": "live_submitted", "response": resp}
    except Exception as exc:
      log.exception("polymarket CLOB place_buy failed: %s", exc)
      return {"ok": False, "action": "live_failed", "error": str(exc)[:300]}

  def status(self) -> dict[str, Any]:
    return {
      "enabled": self.enabled,
      "mode": self.mode,
      "allow_live": self.allow_live,
      "paper_only": self.paper_only,
      "clob_host": self.host,
      "chain_id": self.chain_id,
      "signature_type": self.signature_type,
      "funder_set": bool(self.funder),
      "key_configured": polymarket_key_configured(),
      "authenticated": self.authenticated,
      "live_ready": self.live_ready,
      "creds_source": self._creds_source,
      "auth_error": self._auth_error,
      "note": (
        "CLOB ready — live orders still gated (allow_live=false)"
        if self.live_ready and not self.allow_live
        else (
          "Polymarket CLOB live armed"
          if self.live_ready and self.allow_live
          else "Set POLYMARKET_PRIVATE_KEY (or L2 creds) for CLOB auth"
        )
      ),
    }
