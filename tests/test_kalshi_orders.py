"""Kalshi V2 order API helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.data.kalshi import KalshiClient, parse_v2_order_response, v2_action_side_from_book, v2_book_side, v2_price_dollars, v2_yes_price_cents


def test_v2_book_side_buy_yes():
  assert v2_book_side(side="yes", action="buy") == "bid"


def test_v2_book_side_buy_no():
  assert v2_book_side(side="no", action="buy") == "ask"


def test_v2_book_side_sell_yes():
  assert v2_book_side(side="yes", action="sell") == "ask"


def test_v2_book_side_sell_no():
  assert v2_book_side(side="no", action="sell") == "bid"


def test_v2_action_side_from_book_round_trip():
  for side, action in (("yes", "buy"), ("no", "buy"), ("yes", "sell"), ("no", "sell")):
    book = v2_book_side(side=side, action=action)
    got = v2_action_side_from_book(book_side=book, outcome_side=side)
    assert got == (action, side)


def test_v2_price_dollars():
  assert v2_price_dollars(40) == "0.4000"
  assert v2_price_dollars(63) == "0.6300"


def test_v2_yes_price_cents_inverts_no_leg():
  assert v2_yes_price_cents(side="yes", leg_price_cents=40) == 40
  assert v2_yes_price_cents(side="no", leg_price_cents=74) == 26


def test_create_order_uses_v2_events_endpoint():
  client = KalshiClient({"kalshi": {"key_id": "k", "private_key": ""}})
  client._private_key = MagicMock()
  assert client.authenticated
  with patch.object(client, "post", return_value={"order_id": "ord-1"}) as post:
    resp = client.create_order(
      ticker="KXBTCD-26JUN2923-T60000",
      side="yes",
      count=5,
      yes_price=18,
      client_order_id="test-client-id",
    )
  assert resp["order_id"] == "ord-1"
  post.assert_called_once()
  path, kwargs = post.call_args[0][0], post.call_args[1]
  assert path == "/portfolio/events/orders"
  body = kwargs["json_body"]
  assert body["ticker"] == "KXBTCD-26JUN2923-T60000"
  assert body["client_order_id"] == "test-client-id"
  assert body["side"] == "bid"
  assert body["count"] == "5.00"
  assert body["price"] == "0.1800"
  assert body["time_in_force"] == "good_till_canceled"


def test_create_order_buy_no_uses_inverted_yes_price():
  client = KalshiClient({"kalshi": {"key_id": "k", "private_key": ""}})
  client._private_key = MagicMock()
  with patch.object(client, "post", return_value={"order_id": "ord-2"}) as post:
    client.create_order(
      ticker="KXBTCD-26JUN2923-T59699.99",
      side="no",
      count=2,
      no_price=74,
    )
  body = post.call_args[1]["json_body"]
  assert body["side"] == "ask"
  assert body["price"] == "0.2600"


def test_cancel_order_uses_v2_events_endpoint():
  client = KalshiClient({"kalshi": {"key_id": "k", "private_key": ""}})
  client._private_key = MagicMock()
  with patch.object(client, "_request", return_value={"order_id": "ord-1"}) as req:
    client.cancel_order("ord-1")
  req.assert_called_once_with(
    "DELETE", "/portfolio/events/orders/ord-1", auth=True, critical=True
  )


def test_position_net_from_row_prefers_fp():
  from src.data.kalshi import position_net_from_row

  assert position_net_from_row({"position_fp": "-5.93"}) == -5.93
  assert position_net_from_row({"position": -2}) == -2.0


def test_parse_v2_order_response_includes_status():
  parsed = parse_v2_order_response({
    "order": {
      "order_id": "ord-1",
      "fill_count": 2,
      "remaining_count": 0,
      "status": "executed",
    },
  })
  assert parsed["status"] == "executed"
  assert parsed["fill_count"] == 2.0


def test_get_order_reads_events_endpoint():
  client = KalshiClient({"kalshi": {"key_id": "k", "private_key": ""}})
  client._private_key = MagicMock()
  with patch.object(
    client,
    "get",
    return_value={"order": {"order_id": "ord-1", "status": "executed"}},
  ) as get:
    row = client.get_order("ord-1")
  assert row["status"] == "executed"
  get.assert_called_once_with(
    "/portfolio/events/orders/ord-1", auth=True, critical=False,
  )


def test_get_market_position_reads_portfolio_positions():
  client = KalshiClient({"kalshi": {"key_id": "k", "private_key": ""}})
  client._private_key = MagicMock()
  with patch.object(client, "get", return_value={"market_positions": [{"ticker": "T1", "position_fp": "2.00"}]}) as get:
    net = client.get_market_position("T1")
  assert net == 2.0
  get.assert_called_once()
  assert get.call_args[0][0] == "/portfolio/positions"
