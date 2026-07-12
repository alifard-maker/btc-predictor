"""Compare bot-tracked live legs vs Kalshi positions and resting orders."""

from __future__ import annotations

from typing import Any

from src.trading.bot_position_mode import normalize_position_mode
from src.data.kalshi import position_net_from_row
from src.trading.live_position_sync import _position_contracts, kalshi_sellable_contracts


def _leg_key(ticker: str, side: str) -> str:
  return f"{ticker}:{str(side).lower()}"


def _hourly_event_time_suffix(event_ticker: str) -> str | None:
  from src.trading.hourly_event_time import hourly_event_time_suffix

  return hourly_event_time_suffix(event_ticker)


def _ticker_belongs_to_event(ticker: str, event_ticker: str | None) -> bool:
  if not event_ticker:
    return True
  from src.trading.hourly_event_time import is_kalshi_hourly_event, ticker_belongs_to_hourly_event

  t = str(ticker)
  e = str(event_ticker)
  if t == e or t.startswith(f"{e}-"):
    return True
  if is_kalshi_hourly_event(e):
    return ticker_belongs_to_hourly_event(t, e)
  return False


def _market_exposure_usd(row: dict[str, Any]) -> float:
  val = row.get("market_exposure_dollars") or row.get("market_exposure")
  try:
    return float(val or 0)
  except (TypeError, ValueError):
    return 0.0


def _kalshi_contracts_for_side(net: float, side: str) -> float:
  s = str(side).lower()
  if s == "yes":
    return max(0.0, float(net))
  return max(0.0, -float(net))


def _aggregate_bot_legs(positions: list[dict[str, Any]], *, live_only: bool = True) -> dict[str, dict[str, Any]]:
  out: dict[str, dict[str, Any]] = {}
  for pos in positions:
    if live_only and normalize_position_mode(pos.get("mode")) != "live":
      continue
    ticker = str(pos.get("market_ticker") or "")
    side = str(pos.get("side") or "").lower()
    if not ticker or side not in ("yes", "no"):
      continue
    key = _leg_key(ticker, side)
    row = out.setdefault(
      key,
      {
        "ticker": ticker,
        "side": side,
        "contracts": 0,
        "cost_usd": 0.0,
        "labels": [],
        "position_ids": [],
      },
    )
    row["contracts"] = round(row["contracts"] + _position_contracts(pos), 2)
    row["cost_usd"] = round(row["cost_usd"] + float(pos.get("cost_usd") or 0), 2)
    label = pos.get("label")
    if label and label not in row["labels"]:
      row["labels"].append(str(label))
    pid = pos.get("id")
    if pid:
      row["position_ids"].append(str(pid))
  return out


def _aggregate_kalshi_positions(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
  out: dict[str, dict[str, Any]] = {}
  for row in rows:
    ticker = str(row.get("ticker") or "")
    if not ticker:
      continue
    try:
      net = position_net_from_row(row)
    except (TypeError, ValueError):
      continue
    if abs(net) < 0.005:
      continue
    side = "yes" if net > 0 else "no"
    key = _leg_key(ticker, side)
    out[key] = {
      "ticker": ticker,
      "side": side,
      "contracts": round(abs(net), 2),
      "net_position": net,
      "market_exposure": row.get("market_exposure_dollars") or row.get("market_exposure"),
    }
  return out


def _resting_sells_by_ticker(kalshi: Any) -> dict[str, list[dict[str, Any]]]:
  out: dict[str, list[dict[str, Any]]] = {}
  if not kalshi or not getattr(kalshi, "authenticated", False):
    return out
  for row in kalshi.list_resting_orders():
    if str(row.get("action") or "").lower() != "sell":
      continue
    ticker = str(row.get("ticker") or "")
    if not ticker:
      continue
    out.setdefault(ticker, []).append(row)
  return out


def build_live_reconcile_report(
  *,
  bot_positions: list[dict[str, Any]],
  kalshi: Any,
  event_ticker: str | None = None,
  market_tickers: set[str] | None = None,
  asset: str | None = None,
) -> dict[str, Any]:
  """Summarize bot vs exchange alignment for live hourly debugging.

  Always scopes Kalshi inventory to the bot asset when provided so sports
  (and other account positions) never appear as ETH/BTC mismatches.
  """
  bot = _aggregate_bot_legs(bot_positions, live_only=True)
  kalshi_rows = kalshi.list_market_positions() if kalshi else []
  if asset:
    from src.trading.hourly_event_time import hourly_fill_belongs_to_asset

    kalshi_rows = [
      row for row in kalshi_rows
      if hourly_fill_belongs_to_asset(str(row.get("ticker") or ""), asset)
    ]
  if market_tickers and event_ticker:
    allowed = {str(t) for t in market_tickers}
    kalshi_rows = [
      row for row in kalshi_rows
      if str(row.get("ticker") or "") in allowed
      or _ticker_belongs_to_event(str(row.get("ticker") or ""), event_ticker)
    ]
  elif market_tickers:
    allowed = {str(t) for t in market_tickers}
    kalshi_rows = [
      row for row in kalshi_rows
      if str(row.get("ticker") or "") in allowed
    ]
  elif event_ticker:
    kalshi_rows = [
      row for row in kalshi_rows
      if _ticker_belongs_to_event(str(row.get("ticker") or ""), event_ticker)
    ]
  # If we know the asset but have no event/markets yet, asset filter above is enough.
  # Never fall through to unscoped account-wide inventory for crypto bots.
  kalshi_legs = _aggregate_kalshi_positions(kalshi_rows)
  resting_sells = _resting_sells_by_ticker(kalshi)

  matched: list[dict[str, Any]] = []
  mismatches: list[dict[str, Any]] = []
  bot_only: list[dict[str, Any]] = []
  kalshi_only: list[dict[str, Any]] = []

  all_keys = set(bot) | set(kalshi_legs)
  for key in sorted(all_keys):
    b = bot.get(key)
    k = kalshi_legs.get(key)
    if b and k:
      bot_ct = float(b["contracts"])
      kalshi_ct = float(k["contracts"])
      if abs(bot_ct - kalshi_ct) < 0.25:
        matched.append({**b, "kalshi_contracts": kalshi_ct, "status": "ok"})
      else:
        mismatches.append({
          **b,
          "kalshi_contracts": kalshi_ct,
          "delta": round(bot_ct - kalshi_ct, 2),
          "status": "count_mismatch",
        })
    elif b:
      sellable = kalshi_sellable_contracts(kalshi, b["ticker"], b["side"]) if kalshi else None
      bot_only.append({**b, "kalshi_sellable": sellable, "status": "bot_only"})
    elif k:
      kalshi_only.append({**k, "status": "kalshi_only"})

  orphan_sells: list[dict[str, Any]] = []
  bot_tickers = {b["ticker"] for b in bot.values()}
  allowed_tickers = {str(t) for t in market_tickers} if market_tickers else None
  for ticker, orders in resting_sells.items():
    if asset:
      from src.trading.hourly_event_time import hourly_fill_belongs_to_asset

      if not hourly_fill_belongs_to_asset(ticker, asset):
        continue
    if allowed_tickers is not None and ticker not in allowed_tickers:
      continue
    if event_ticker and allowed_tickers is None:
      if not _ticker_belongs_to_event(ticker, event_ticker):
        continue
    if ticker in bot_tickers:
      continue
    for o in orders:
      orphan_sells.append({
        "ticker": ticker,
        "order_id": o.get("order_id"),
        "side": o.get("side"),
        "remaining_count": o.get("remaining_count"),
        "yes_price": o.get("yes_price"),
      })

  aligned = not mismatches and not bot_only and not kalshi_only and not orphan_sells
  bot_live_exposure_usd = round(sum(float(v.get("cost_usd") or 0) for v in bot.values()), 2)
  kalshi_exposure_usd = round(
    sum(_market_exposure_usd(k) for k in kalshi_legs.values()),
    2,
  )
  return {
    "ok": aligned,
    "event_ticker": event_ticker,
    "asset": asset,
    "bot_live_legs": len(bot),
    "kalshi_legs": len(kalshi_legs),
    "bot_live_contracts": sum(int(v["contracts"]) for v in bot.values()),
    "kalshi_contracts": sum(int(v["contracts"]) for v in kalshi_legs.values()),
    "bot_live_exposure_usd": bot_live_exposure_usd,
    "kalshi_exposure_usd": kalshi_exposure_usd,
    "matched": matched,
    "mismatches": mismatches,
    "bot_only": bot_only,
    "kalshi_only": kalshi_only,
    "orphan_resting_sells": orphan_sells,
    "resting_sell_count": sum(len(v) for v in resting_sells.values()),
  }


def _pick_label_from_tab(tab: dict[str, Any] | None, market_ticker: str) -> str | None:
  if not tab:
    return None
  from src.trading.hourly_bot import _find_contract_in_live

  live = tab.get("live") or tab
  pick = _find_contract_in_live(live, market_ticker)
  if pick and pick.get("label"):
    return str(pick["label"])
  return None


def merge_kalshi_hourly_open_positions(
  bot_positions: list[dict[str, Any]],
  kalshi: Any,
  event_ticker: str,
  *,
  tab: dict[str, Any] | None = None,
  asset: str | None = None,
) -> list[dict[str, Any]]:
  """
  Merge bot-tracked open legs with Kalshi inventory for this hourly window.

  Includes Strategy-2 range bands (KXBTC-/KXETH-* siblings) that exist on Kalshi but
  were never adopted into the bot log — so the dashboard open-positions panel
  matches what you see on Kalshi.
  """
  if not kalshi or not getattr(kalshi, "authenticated", False) or not event_ticker:
    return list(bot_positions)

  from src.trading.hourly_event_time import ticker_belongs_to_hourly_event
  from src.trading.live_position_sync import kalshi_position_leg
  from src.trading.live_range_guards import is_range_market_ticker

  bot_live = [
    p for p in bot_positions
    if normalize_position_mode(p.get("mode")) == "live"
    and ticker_belongs_to_hourly_event(str(p.get("market_ticker") or ""), event_ticker)
  ]
  bot_keys = {
    _leg_key(str(p.get("market_ticker") or ""), str(p.get("side") or ""))
    for p in bot_live
  }

  kalshi_rows = kalshi.list_market_positions() if kalshi else []
  if asset:
    from src.trading.hourly_event_time import hourly_fill_belongs_to_asset

    kalshi_rows = [
      r for r in kalshi_rows
      if hourly_fill_belongs_to_asset(str(r.get("ticker") or ""), asset)
    ]
  kalshi_rows = [
    r for r in kalshi_rows
    if _ticker_belongs_to_event(str(r.get("ticker") or ""), event_ticker)
  ]
  kalshi_agg = _aggregate_kalshi_positions(kalshi_rows)

  merged = [dict(p) for p in bot_live]
  for key, krow in kalshi_agg.items():
    if key in bot_keys:
      continue
    ticker = str(krow["ticker"])
    side = str(krow["side"])
    snap = kalshi_position_leg(kalshi, ticker, side) or {}
    entry_c = int(snap.get("entry_price_cents") or 0)
    contracts = float(krow["contracts"])
    if contracts < 0.05:
      continue
    cost_usd = snap.get("cost_usd")
    if cost_usd is None:
      try:
        cost_usd = float(krow.get("market_exposure") or 0)
      except (TypeError, ValueError):
        cost_usd = 0.0
    if entry_c <= 0 and cost_usd and contracts > 0:
      entry_c = max(1, min(99, int(round(100.0 * float(cost_usd) / contracts))))
    label = _pick_label_from_tab(tab, ticker)
    is_range = is_range_market_ticker(ticker)
    merged.append({
      "id": f"kalshi-only:{key}",
      "event_ticker": event_ticker,
      "market_ticker": ticker,
      "side": side,
      "contracts": max(1, int(round(contracts))),
      "contracts_fp": contracts,
      "entry_price_cents": entry_c or None,
      "cost_usd": round(float(cost_usd or 0), 2),
      "label": label or ticker.rsplit("-", 1)[-1],
      "mode": "live",
      "entry_source": "kalshi_only",
      "kalshi_only": True,
      "leg_strategy": "s2_range" if is_range else "s1_threshold",
    })

  for row in merged:
    if "leg_strategy" not in row:
      ticker = str(row.get("market_ticker") or "")
      row["leg_strategy"] = "s2_range" if is_range_market_ticker(ticker) else "s1_threshold"

  return merged
