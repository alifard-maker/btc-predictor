"""Tests for Polymarket CLOB client — auth scaffolding + live gate."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.data.polymarket_clob import PolymarketClobClient


def _cfg(**poly_overrides):
  poly = {
    "enabled": True,
    "mode": "paper",
    "allow_live": False,
    "clob_host": "https://clob.polymarket.com",
    "chain_id": 137,
    "signature_type": 0,
  }
  poly.update(poly_overrides)
  return {"sports": {"polymarket": poly}}


def test_place_buy_blocked_when_allow_live_false():
  with patch.dict("os.environ", {}, clear=False):
    client = PolymarketClobClient(_cfg(allow_live=False, mode="paper"))
  # Force a fake authenticated client — gate must still block
  client._client = MagicMock()
  client._authenticated = True
  client.allow_live = False
  out = client.place_buy(token_id="tok", price=0.4, size=5)
  assert out["ok"] is False
  assert out["reason"] == "poly_live_disabled"


def test_place_buy_blocked_when_mode_paper_even_if_allow_live():
  client = PolymarketClobClient(_cfg(allow_live=True, mode="paper"))
  client._client = MagicMock()
  client._authenticated = True
  client.allow_live = True
  client.mode = "paper"
  out = client.place_buy(token_id="tok", price=0.4, size=5)
  assert out["ok"] is False
  assert out["reason"] == "poly_live_disabled"


def test_status_missing_key():
  with patch.dict(
    "os.environ",
    {
      "POLYMARKET_PRIVATE_KEY": "",
      "PRIVATE_KEY": "",
      "POLY_CLOB_API_KEY": "",
      "POLY_CLOB_SECRET": "",
      "POLY_CLOB_PASSPHRASE": "",
    },
    clear=False,
  ):
    client = PolymarketClobClient(_cfg())
  st = client.status()
  assert st["allow_live"] is False
  assert st["authenticated"] is False
  assert st["live_ready"] is False
  assert st["key_configured"] is False


def test_l2_env_builds_client():
  fake_creds = MagicMock()
  fake_clob = MagicMock()
  fake_clob.get_ok.return_value = "OK"

  with patch.dict(
    "os.environ",
    {
      "POLYMARKET_PRIVATE_KEY": "0xabc",
      "POLY_CLOB_API_KEY": "k",
      "POLY_CLOB_SECRET": "s",
      "POLY_CLOB_PASSPHRASE": "p",
    },
    clear=False,
  ):
    with patch("py_clob_client_v2.ApiCreds", return_value=fake_creds) as creds_cls:
      with patch("py_clob_client_v2.ClobClient", return_value=fake_clob) as clob_cls:
        client = PolymarketClobClient(_cfg())
        assert client.authenticated
        assert client.live_ready
        assert client._creds_source == "env_l2"
        creds_cls.assert_called_once()
        assert clob_cls.called
        st = client.status()
        assert st["live_ready"] is True
        assert "gated" in (st["note"] or "").lower() or "allow_live" in (st["note"] or "")


def test_derive_l1_when_no_l2_env():
  derived = MagicMock()
  temp = MagicMock()
  temp.create_or_derive_api_key.return_value = derived
  full = MagicMock()
  full.get_ok.return_value = "OK"

  with patch.dict(
    "os.environ",
    {
      "POLYMARKET_PRIVATE_KEY": "0xdead",
      "POLY_CLOB_API_KEY": "",
      "POLY_CLOB_SECRET": "",
      "POLY_CLOB_PASSPHRASE": "",
    },
    clear=False,
  ):
    with patch("py_clob_client_v2.ClobClient", side_effect=[temp, full]) as clob_cls:
      client = PolymarketClobClient(_cfg())
      assert client.authenticated
      assert client._creds_source == "derived_l1"
      temp.create_or_derive_api_key.assert_called_once()
      assert clob_cls.call_count == 2


def test_place_buy_calls_clob_when_armed():
  client = PolymarketClobClient(_cfg(allow_live=True, mode="live"))
  mock = MagicMock()
  mock.create_and_post_order.return_value = {"orderID": "1"}
  client._client = mock
  client._authenticated = True
  client.allow_live = True
  client.mode = "live"

  with patch("py_clob_client_v2.OrderType") as OT:
    OT.FOK = "FOK"
    with patch("py_clob_client_v2.clob_types.OrderArgsV2") as OA:
      OA.return_value = MagicMock()
      out = client.place_buy(token_id="tok123", price=0.42, size=3.0, order_type="FOK")
  assert out["ok"] is True
  assert out["action"] == "live_submitted"
  mock.create_and_post_order.assert_called_once()
