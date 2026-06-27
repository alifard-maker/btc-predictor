"""Plain-language guidance for the Kalshi hourly/daily tab."""

from __future__ import annotations

from typing import Any


def _index_id(live: dict[str, Any], index_id: str | None) -> str:
  return str(index_id or live.get("index_id") or live.get("settlement_reference") or "BRTI")


def _forecast_mu_sigma(data: dict[str, Any]) -> tuple[float | None, float | None]:
  mu = data.get("terminal_mu") or data.get("blended_mu")
  sigma = data.get("terminal_sigma")
  if mu is None or sigma is None:
    return None, None
  return float(mu), float(sigma)


def _near_forecast(
  row: dict[str, Any] | None,
  mu: float | None,
  sigma: float | None,
  *,
  window_mult: float = 2.5,
) -> bool:
  if not row or mu is None or sigma is None:
    return False
  window = max(sigma * window_mult, mu * 0.003)
  if row.get("contract_type") == "range":
    lo, hi = row.get("floor_strike"), row.get("cap_strike")
    if lo is not None and hi is not None:
      lo_f, hi_f = float(lo), float(hi)
      return lo_f <= mu <= hi_f or abs((lo_f + hi_f) / 2 - mu) <= window
    return False
  strike = row.get("floor_strike") or row.get("cap_strike")
  if strike is None:
    return False
  return abs(float(strike) - mu) <= window


def _pick_safest(
  live: dict[str, Any],
  locked: dict[str, Any] | None,
  *,
  index_id: str,
) -> dict[str, Any] | None:
  """Highest-probability outcome near forecast μ — never a far OTM tail."""
  ref = locked or live
  mu, sigma = _forecast_mu_sigma(ref if locked else live)
  range_row = (ref.get("strategy_range") or live.get("strategy_range") or {}).get("most_likely")
  thresh_row = (ref.get("strategy_threshold") or live.get("strategy_threshold") or {}).get("most_likely")
  candidates: list[tuple[float, dict[str, Any], str]] = []
  if range_row and range_row.get("model_prob") is not None and _near_forecast(range_row, mu, sigma):
    candidates.append((float(range_row["model_prob"]), range_row, "range"))
  if (
    thresh_row
    and thresh_row.get("model_prob") is not None
    and float(thresh_row["model_prob"]) >= 0.08
    and _near_forecast(thresh_row, mu, sigma)
  ):
    candidates.append((float(thresh_row["model_prob"]), thresh_row, "threshold"))
  if not candidates:
    zone = (ref.get("most_likely") or live.get("most_likely") or {})
    if zone.get("settlement_zone_low") is not None:
      return {
        "tier": "safest",
        "title": "Most predictable outcome",
        "pick_type": "zone",
        "label": (
          f"{index_id} ${zone['settlement_zone_low']:,.0f}–${zone['settlement_zone_high']:,.0f}"
        ),
        "model_prob": None,
        "signal": "—",
        "settlement_zone": (
          f"${zone['settlement_zone_low']:,.0f}–${zone['settlement_zone_high']:,.0f}"
        ),
        "reason": (
          "No Kalshi contract brackets forecast μ — use the settlement zone, not far OTM strikes."
        ),
      }
    return None
  prob, row, kind = max(candidates, key=lambda x: x[0])
  return {
    "tier": "safest",
    "title": "Most predictable outcome",
    "pick_type": kind,
    "label": row.get("label"),
    "model_prob": prob,
    "signal": row.get("signal", "—"),
    "reason": (
      "Highest model probability near forecast μ — "
      + (
        f"the stall band where {index_id} is most likely to finish."
        if kind == "range"
        else "the strike closest to forecast μ (not the deepest ITM leg)."
      )
    ),
  }


def _pick_locked(locked: dict[str, Any] | None, *, index_id: str) -> dict[str, Any] | None:
  if not locked:
    return {
      "tier": "locked",
      "title": "Official prediction (not locked yet)",
      "label": "—",
      "reason": f"Wait for :05 ET — this is the pick scored against actual settle {index_id}.",
    }
  zone = locked.get("most_likely") or {}
  mu, sigma = _forecast_mu_sigma(locked)
  pick = locked.get("primary_pick") or {}
  range_pick = (locked.get("strategy_range") or {}).get("most_likely")
  pick_type = pick.get("contract_type", "threshold")
  if range_pick and not _near_forecast(pick, mu, sigma):
    pick = range_pick
    pick_type = "range"
  elif not _near_forecast(pick, mu, sigma):
    pick = {
      "label": (
        f"{index_id} ${zone.get('settlement_zone_low'):,.0f}–${zone.get('settlement_zone_high'):,.0f}"
        if zone.get("settlement_zone_low") is not None
        else "Settlement zone"
      ),
      "signal": pick.get("signal", "NEUTRAL"),
      "model_prob": None,
      "contract_type": "zone",
    }
    pick_type = "zone"
  return {
    "tier": "locked",
    "title": "Official scored pick @ lock",
    "pick_type": pick_type,
    "label": pick.get("label") or "—",
    "signal": pick.get("signal", "NEUTRAL"),
    "model_prob": pick.get("model_prob"),
    "settlement_zone": (
      f"${zone.get('settlement_zone_low'):,.0f}–${zone.get('settlement_zone_high'):,.0f}"
      if zone.get("settlement_zone_low") is not None
      else None
    ),
    "logged_at": locked.get("logged_at"),
    "reason": (
      "Frozen at lock time — calibration and Brier use this, not the live reassessment."
    ),
  }


def _pick_edge(live: dict[str, Any]) -> dict[str, Any] | None:
  mu, sigma = _forecast_mu_sigma(live)
  be_t = (live.get("strategy_threshold") or {}).get("best_edge")
  be_r = (live.get("strategy_range") or {}).get("best_edge")
  candidates = [
    c
    for c in (be_t, be_r)
    if c and c.get("edge") is not None and _near_forecast(c, mu, sigma)
  ]
  if not candidates:
    return None
  row = max(candidates, key=lambda c: abs(float(c["edge"])))
  prob = float(row.get("model_prob") or 0)
  return {
    "tier": "edge",
    "title": "Best edge vs Kalshi (higher risk)",
    "pick_type": row.get("contract_type", "threshold"),
    "label": row.get("label"),
    "signal": row.get("signal", "NEUTRAL"),
    "model_prob": prob,
    "edge": row.get("edge"),
    "reason": (
      "Largest model − Kalshi gap right now. "
      + (
        "Low model % — treat as speculative, not a high-confidence direction."
        if prob < 0.15 or prob > 0.85
        else "Use only if you accept mispricing risk; edge can mean far OTM."
      )
    ),
  }


def build_range_strategy_guidance(
  live: dict[str, Any],
  locked: dict[str, Any] | None = None,
  *,
  index_id: str = "BRTI",
) -> dict[str, Any]:
  live_sr = live.get("strategy_range") or {}
  locked_sr = (locked or {}).get("strategy_range") or {}
  live_ml = live_sr.get("most_likely")
  live_be = live_sr.get("best_edge")
  locked_ml = locked_sr.get("most_likely")
  locked_be = locked_sr.get("best_edge")

  def _band_card(tier: str, title: str, row: dict[str, Any] | None, reason: str) -> dict[str, Any] | None:
    if not row:
      return None
    return {
      "tier": tier,
      "title": title,
      "label": row.get("label"),
      "model_prob": row.get("model_prob"),
      "signal": row.get("signal", "NEUTRAL"),
      "edge": row.get("edge"),
      "reason": reason,
    }

  locked_card = (
    _band_card(
      "locked",
      "Locked range pick @ :05",
      locked_ml,
      "Frozen at lock — use this band for honest range-band scoring (not the live table below).",
    )
    if locked
    else {
      "tier": "locked",
      "title": "Locked range pick (not yet)",
      "label": "—",
      "reason": "Range band locks at :05 ET with the threshold forecast.",
    }
  )

  recs = [
    r
    for r in (
      _band_card(
        "safest",
        "Most predictable stall band",
        locked_ml or live_ml,
        f"Highest model % that {index_id} finishes inside this band — best when price is consolidating.",
      ),
      locked_card,
      _band_card(
        "edge",
        "Best range edge now (live)",
        live_be,
        "Largest Kalshi mispricing among bands right now — can be a thin-probability tail band.",
      ),
    )
    if r
  ]

  consolidation = (live.get("structure") or {}).get("consolidation")
  stall_note = None
  if consolidation:
    stall_note = (
      f"1h stall box ${consolidation.get('low'):,.0f}–${consolidation.get('high'):,.0f} "
      f"(tightness {consolidation.get('tightness')}) — range bands work best here."
    )
  elif locked_ml or live_ml:
    stall_note = "No tight stall box — range bands are less reliable; prefer locked settlement range or thresholds."

  return {
    "summary": (
      f"Strategy 2 bets {index_id} lands inside a price band at settle — "
      "each row is one band contract; BUY YES/NO is the Kalshi leg to take."
    ),
    "locked": locked_sr,
    "live": live_sr,
    "recommendations": recs,
    "stall_note": stall_note,
    "locked_vs_live": {
      "locked": "Most-likely band + best band edge at :05 ET.",
      "live": f"Band odds refresh with {index_id} — compare to locked pick, do not confuse with scored forecast.",
    },
  }


def build_hourly_guidance(
  live: dict[str, Any],
  locked: dict[str, Any] | None = None,
  *,
  asset: str = "btc",
  index_id: str | None = None,
) -> dict[str, Any]:
  ev = live.get("event") or {}
  freq = str(ev.get("frequency") or "hourly").lower()
  hours = float(live.get("hours_to_settle") or 0)
  series = ev.get("series_ticker") or ""
  idx = _index_id(live, index_id)
  asset = str(asset or live.get("asset") or "btc").lower()

  default_hourly_series = "KXETHD" if asset == "eth" else "KXBTCD"
  default_daily_series = "ETHD" if asset == "eth" else "BTCD"

  if freq == "daily":
    interval = {
      "type": "daily",
      "badge": "DAILY",
      "series": series or default_daily_series,
      "settles_in": f"{hours:.1f}h",
      "summary": (
        f"Kalshi daily threshold book ({series or default_daily_series}) — settles once per day. "
        "More time for price to drift; uncertainty scales with time to settle."
      ),
      "predictability": "higher",
      "predictability_note": "Usually smoother than hourly — better when you want a slower, wider window.",
    }
  else:
    interval = {
      "type": "hourly",
      "badge": "HOURLY",
      "series": series or default_hourly_series,
      "settles_in": f"{hours:.1f}h",
      "summary": (
        f"Kalshi hourly book ({series or default_hourly_series}) — settles at the top of each hour. "
        "Official forecast locks at :05 ET; live section updates with the market."
      ),
      "predictability": "moderate",
      "predictability_note": "Faster settle than daily — use locked range for scoring, live for monitoring.",
    }

  regime = (locked or live).get("regime") or live.get("regime") or {}
  primary_sig = (locked or {}).get("primary_pick", {}).get("signal") if locked else None

  strategies = [
    {
      "name": "Locked settlement range",
      "best_for": "Safest read — no contract required",
      "detail": f"Where the model expected {idx} at lock time. Compare actual settle to this band.",
      "risk": "low",
    },
    {
      "name": "Range band (Strategy 2)",
      "best_for": "Band contracts when price is consolidating",
      "detail": (
        f"Each row is “{idx} settles inside this $20 band.” "
        "BUY YES = take Yes on that band; BUY NO = take No (even on neighbors of the most-likely band)."
      ),
      "risk": "low–medium",
    },
    {
      "name": "Near-ATM threshold (Strategy 1)",
      "best_for": "Explicit BUY YES/NO when model and edge agree",
      "detail": (
        f"Strikes close to current {idx} — BUY YES/NO when direction and mispricing align. "
        "VALUE YES / FADE YES on tails."
      ),
      "risk": "medium",
    },
    {
      "name": "Far OTM threshold + edge",
      "best_for": "Mispricing hunters only",
      "detail": "Tiny model % (e.g. 2%) with big Kalshi gap — low predictability despite 'edge'.",
      "risk": "high",
    },
  ]

  recs = [
    r
    for r in (
      _pick_safest(live, locked, index_id=idx),
      _pick_locked(locked, index_id=idx),
      _pick_edge(live),
    )
    if r
  ]

  book_note = None
  if live.get("forecast_covers_book") is False:
    book_note = (
      "Kalshi book does not bracket forecast μ — contract tables may be empty; "
      "trust the settlement zone and re-lock at :05 after deploy."
    )

  use_15m = (
    "KXBTC15M — 15-minute up/down slots at :00, :15, :30, :45 ET → use the 15m tab."
    if asset == "btc"
    else "15m tab is BTC slot LONG/SHORT only — ETH hourly Kalshi contracts live on this tab."
  )

  return {
    "interval": interval,
    "which_tab": {
      "use_15m": use_15m,
      "use_this_tab": f"Kalshi {interval['badge']} threshold + range books for this event → this tab.",
    },
    "locked_vs_live": {
      "locked": "Primary signal + settlement range at :05 ET — used for accuracy stats.",
      "live": "Updates every refresh — for monitoring only until next hour's lock.",
    },
    "strategies": strategies,
    "signal_legend": {
      "buy_yes": "Buy Yes on this contract — model says Kalshi underprices it.",
      "buy_no": "Buy No on this contract — model says Kalshi overprices Yes.",
      "value_yes": "Cheap OTM tail — speculative Yes, not a high-confidence direction.",
      "fade_yes": "Kalshi overpriced vs model on a likely leg.",
      "range_note": "Neighboring bands are separate contracts; BUY NO on a side band means fade that band, not “away from forecast.”",
    },
    "recommendations": recs,
    "strategy_range": build_range_strategy_guidance(live, locked, index_id=idx),
    "regime_blocked": not regime.get("allow_trade", True),
    "regime_note": (
      book_note
      or (
        "Regime filter blocked BUY YES/NO signals — safest action is NEUTRAL / watch locked range only."
        if not regime.get("allow_trade", True) and primary_sig in (None, "NEUTRAL")
        else None
      )
    ),
    "book_note": book_note,
    "index_id": idx,
    "asset": asset,
  }
