"""Plain-language guidance for the Kalshi hourly/daily tab."""

from __future__ import annotations

from typing import Any


def _pick_safest(live: dict[str, Any], locked: dict[str, Any] | None) -> dict[str, Any] | None:
  """Highest-probability near-forecast outcome — range band mass or ATM threshold."""
  ref = locked or live
  range_row = (ref.get("strategy_range") or live.get("strategy_range") or {}).get("most_likely")
  thresh_row = (ref.get("strategy_threshold") or live.get("strategy_threshold") or {}).get("most_likely")
  candidates: list[tuple[float, dict[str, Any], str]] = []
  if range_row and range_row.get("model_prob") is not None:
    candidates.append((float(range_row["model_prob"]), range_row, "range"))
  if thresh_row and thresh_row.get("model_prob") is not None:
    candidates.append((float(thresh_row["model_prob"]), thresh_row, "threshold"))
  if not candidates:
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
        "the stall band where BRTI is most likely to finish."
        if kind == "range"
        else "the strike closest to forecast μ (not the deepest ITM leg)."
      )
    ),
  }


def _pick_locked(locked: dict[str, Any] | None) -> dict[str, Any] | None:
  if not locked:
    return {
      "tier": "locked",
      "title": "Official prediction (not locked yet)",
      "label": "—",
      "reason": "Wait for :05 ET — this is the pick scored against actual settle BRTI.",
    }
  pick = locked.get("primary_pick") or {}
  zone = locked.get("most_likely") or {}
  return {
    "tier": "locked",
    "title": "Official scored pick @ lock",
    "pick_type": pick.get("contract_type", "threshold"),
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
  be_t = (live.get("strategy_threshold") or {}).get("best_edge")
  be_r = (live.get("strategy_range") or {}).get("best_edge")
  candidates = [c for c in (be_t, be_r) if c and c.get("edge") is not None]
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
        "Highest model % that BRTI finishes inside this band — best when price is consolidating.",
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
    "summary": "Strategy 2 bets BRTI lands inside a price band at settle — often the safest directional bet type when chop dominates.",
    "locked": locked_sr,
    "live": live_sr,
    "recommendations": recs,
    "stall_note": stall_note,
    "locked_vs_live": {
      "locked": "Most-likely band + best band edge at :05 ET.",
      "live": "Band odds refresh with BRTI — compare to locked pick, do not confuse with scored forecast.",
    },
  }


def build_hourly_guidance(
  live: dict[str, Any],
  locked: dict[str, Any] | None = None,
) -> dict[str, Any]:
  ev = live.get("event") or {}
  freq = str(ev.get("frequency") or "hourly").lower()
  hours = float(live.get("hours_to_settle") or 0)
  series = ev.get("series_ticker") or ""

  if freq == "daily":
    interval = {
      "type": "daily",
      "badge": "DAILY",
      "series": series or "BTCD",
      "settles_in": f"{hours:.1f}h",
      "summary": (
        "Kalshi daily threshold book — settles once per day (typically 4 PM ET). "
        "More time for price to drift; uncertainty scales with time to settle."
      ),
      "predictability": "higher",
      "predictability_note": "Usually smoother than hourly — better when you want a slower, wider window.",
    }
  else:
    interval = {
      "type": "hourly",
      "badge": "HOURLY",
      "series": series or "KXBTCD",
      "settles_in": f"{hours:.1f}h",
      "summary": (
        "Kalshi hourly threshold book (KXBTCD) — settles at the top of each hour. "
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
      "detail": "Where the model expected BRTI at lock time. Compare actual settle to this band.",
      "risk": "low",
    },
    {
      "name": "Range band (Strategy 2)",
      "best_for": "Most predictable *bet type* when price is consolidating",
      "detail": "Bet BRTI finishes inside a narrow band — highest model mass when stall box is tight.",
      "risk": "low–medium",
    },
    {
      "name": "Near-ATM threshold (Strategy 1)",
      "best_for": "Directional lean with moderate confidence",
      "detail": "Strikes close to current BRTI — model % often 40–70%, not lottery-ticket tails.",
      "risk": "medium",
    },
    {
      "name": "Far OTM threshold + edge",
      "best_for": "Mispricing hunters only",
      "detail": "Tiny model % (e.g. 2%) with big Kalshi gap — low predictability despite 'edge'.",
      "risk": "high",
    },
  ]

  recs = [r for r in (_pick_safest(live, locked), _pick_locked(locked), _pick_edge(live)) if r]

  return {
    "interval": interval,
    "which_tab": {
      "use_15m": "KXBTC15M — 15-minute up/down slots at :00, :15, :30, :45 ET → use the 15m tab.",
      "use_this_tab": f"Kalshi {interval['badge']} threshold + range books for this event → this tab.",
    },
    "locked_vs_live": {
      "locked": "Primary signal + settlement range at :05 ET — used for accuracy stats.",
      "live": "Updates every refresh — for monitoring only until next hour's lock.",
    },
    "strategies": strategies,
    "recommendations": recs,
    "strategy_range": build_range_strategy_guidance(live, locked),
    "regime_blocked": not regime.get("allow_trade", True),
    "regime_note": (
      "Regime filter blocked LEAN signals — safest action is NEUTRAL / watch locked range only."
      if not regime.get("allow_trade", True) and primary_sig in (None, "NEUTRAL")
      else None
    ),
  }
