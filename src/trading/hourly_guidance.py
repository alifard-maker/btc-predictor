"""Plain-language guidance for the Kalshi hourly/daily tab."""

from __future__ import annotations

from typing import Any


def _pick_safest(live: dict[str, Any], locked: dict[str, Any] | None) -> dict[str, Any] | None:
  """Highest model-probability outcome — most predictable landing zone."""
  ref = locked or live
  ml = ref.get("most_likely") or live.get("most_likely") or {}
  candidates: list[tuple[float, dict[str, Any], str]] = []
  for kind, row in (("range", ml.get("range")), ("threshold", ml.get("threshold"))):
    if row and row.get("model_prob") is not None:
      candidates.append((float(row["model_prob"]), row, kind))
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
      "Highest model probability — where BRTI is most likely to finish "
      f"({'inside this band' if kind == 'range' else 'relative to this strike'})."
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
    "regime_blocked": not regime.get("allow_trade", True),
    "regime_note": (
      "Regime filter blocked LEAN signals — safest action is NEUTRAL / watch locked range only."
      if not regime.get("allow_trade", True) and primary_sig in (None, "NEUTRAL")
      else None
    ),
  }
