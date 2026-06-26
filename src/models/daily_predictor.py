"""Daily / hourly Kalshi threshold + range-band probability engine."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from src.data.kalshi_daily import DailyEventBook, KalshiContractMarket, KalshiDailyMarkets
from src.features.levels import (
  ConsolidationBox,
  PriceLevel,
  consolidation_box,
  detect_levels,
  levels_to_dict,
)


def _norm_cdf(x: float) -> float:
  return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class ContractOdds:
  ticker: str
  contract_type: str  # threshold | range
  label: str
  model_prob: float
  kalshi_mid: float | None
  edge: float | None
  signal: str  # LEAN YES | LEAN NO | NEUTRAL
  strike_type: str
  floor_strike: float | None
  cap_strike: float | None
  notes: list[str]

  def to_dict(self) -> dict[str, Any]:
    return {
      "ticker": self.ticker,
      "contract_type": self.contract_type,
      "label": self.label,
      "model_prob": round(self.model_prob, 4),
      "kalshi_mid": round(self.kalshi_mid, 4) if self.kalshi_mid is not None else None,
      "edge": round(self.edge, 4) if self.edge is not None else None,
      "signal": self.signal,
      "strike_type": self.strike_type,
      "floor_strike": self.floor_strike,
      "cap_strike": self.cap_strike,
      "notes": self.notes,
    }


class DailyPredictor:
  """Map chart structure → terminal BRTI odds for Kalshi daily/hourly books."""

  def __init__(self, cfg: dict[str, Any]):
    self.cfg = cfg
    self.markets = KalshiDailyMarkets(cfg)
    dcfg = cfg.get("daily", {})
    self.min_edge = float(dcfg.get("min_edge", 0.05))
    self.nearby_strikes = int(dcfg.get("nearby_strikes", 7))
    self.top_bands = int(dcfg.get("top_bands", 8))
    self.vol_lookback_1h = int(dcfg.get("vol_lookback_1h", 24))
    self.drift_lookback_1h = int(dcfg.get("drift_lookback_1h", 6))

  def _terminal_params(
    self,
    current_price: float,
    df_1h: pd.DataFrame | None,
    hours_left: float,
  ) -> tuple[float, float]:
    """Return (mu, sigma) for BRTI at settle in price units."""
    if current_price <= 0:
      return 0.0, current_price * 0.01

    drift_pct = 0.0
    vol_pct = 0.35  # default ~0.35% hourly-ish

    if df_1h is not None and not df_1h.empty:
      df = df_1h.copy()
      df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
      closes = df["close"].astype(float)
      rets = closes.pct_change().dropna()
      if len(rets) >= 4:
        vol_pct = float(rets.tail(self.vol_lookback_1h).std() * 100) or vol_pct
        tail = closes.tail(self.drift_lookback_1h)
        if len(tail) >= 2 and float(tail.iloc[0]) > 0:
          drift_pct = (float(tail.iloc[-1]) - float(tail.iloc[0])) / float(tail.iloc[0]) * 100

    # Scale drift to remaining horizon (diminishing)
    horizon_scale = min(1.0, math.sqrt(max(0.25, hours_left) / max(self.drift_lookback_1h, 1)))
    mu = current_price * (1.0 + (drift_pct / 100.0) * horizon_scale * 0.6)
    sigma = current_price * (vol_pct / 100.0) * math.sqrt(max(0.25, hours_left))
    return mu, max(sigma, current_price * 0.0008)

  def _prob_above(self, strike: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
      return 1.0 if mu >= strike else 0.0
    z = (strike - mu) / sigma
    return float(np.clip(1.0 - _norm_cdf(z), 0.02, 0.98))

  def _prob_below(self, strike: float, mu: float, sigma: float) -> float:
    return 1.0 - self._prob_above(strike, mu, sigma)

  def _prob_between(self, low: float, high: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
      return 1.0 if low <= mu <= high else 0.0
    z_lo = (low - mu) / sigma
    z_hi = (high - mu) / sigma
    return float(np.clip(_norm_cdf(z_hi) - _norm_cdf(z_lo), 0.01, 0.95))

  def _level_nudge(
    self,
    prob: float,
    strike: float,
    levels: list[PriceLevel],
    *,
    above: bool,
  ) -> tuple[float, list[str]]:
    notes: list[str] = []
    for lv in levels:
      dist_pct = abs(lv.price - strike) / strike * 100 if strike > 0 else 999
      if dist_pct > 0.35:
        continue
      bump = 0.04 * lv.strength
      if lv.level_type == "support" and above:
        prob += bump
        notes.append(f"Support ~${lv.price:,.0f} ({lv.touches} touches, wick {lv.wick_score:.0%})")
      elif lv.level_type == "resistance" and not above:
        prob += bump
        notes.append(f"Resistance ~${lv.price:,.0f} ({lv.touches} touches)")
      elif lv.level_type == "resistance" and above:
        prob -= bump * 0.7
      elif lv.level_type == "support" and not above:
        prob -= bump * 0.7
      if lv.volume_confirmed:
        notes.append("Volume confirmed at level")
    return float(np.clip(prob, 0.02, 0.98)), notes[:3]

  def _signal(self, model_p: float, kalshi_mid: float | None) -> tuple[str, float | None]:
    edge = None
    if kalshi_mid is not None:
      edge = model_p - kalshi_mid
      if edge >= self.min_edge:
        return "LEAN YES", edge
      if edge <= -self.min_edge:
        return "LEAN NO", edge
    return "NEUTRAL", edge

  def predict(
    self,
    *,
    current_price: float,
    df_1h: pd.DataFrame | None,
    book: DailyEventBook | None = None,
    override_mu: float | None = None,
    override_sigma: float | None = None,
  ) -> dict[str, Any]:
    book = book or self.markets.active_book()
    now = datetime.now(timezone.utc)

    if book is None:
      return {"ok": False, "error": "No open Kalshi daily/hourly BTC event found"}

    hours_left = max(0.1, (book.close_time - now).total_seconds() / 3600.0)
    mu, sigma = self._terminal_params(current_price, df_1h, hours_left)
    if override_mu is not None:
      mu = float(override_mu)
    if override_sigma is not None:
      sigma = float(override_sigma)
    levels = detect_levels(df_1h, current_price) if df_1h is not None else []
    box = consolidation_box(df_1h) if df_1h is not None else None

    # --- Strategy 1: threshold (above/below) ---
    threshold_rows: list[ContractOdds] = []
    greater = [m for m in book.threshold_markets if m.strike_type == "greater" and m.floor_strike]
    greater.sort(key=lambda m: abs((m.floor_strike or 0) - current_price))
    for m in greater[: self.nearby_strikes]:
      strike = float(m.floor_strike)
      p = self._prob_above(strike, mu, sigma)
      p, notes = self._level_nudge(p, strike, levels, above=True)
      sig, edge = self._signal(p, m.yes_mid)
      threshold_rows.append(
        ContractOdds(
          ticker=m.ticker,
          contract_type="threshold",
          label=m.subtitle or f"≥ ${strike:,.0f}",
          model_prob=p,
          kalshi_mid=m.yes_mid,
          edge=edge,
          signal=sig,
          strike_type="greater",
          floor_strike=strike,
          cap_strike=None,
          notes=notes,
        )
      )

    # --- Strategy 2: range bands ---
    range_rows: list[ContractOdds] = []
    bands = [m for m in book.range_markets if m.strike_type == "between"]
    band_scores: list[tuple[float, KalshiContractMarket, float, list[str]]] = []

    for m in bands:
      low = float(m.floor_strike or 0)
      high = float(m.cap_strike or low)
      if low <= 0 or high <= low:
        continue
      p = self._prob_between(low, high, mu, sigma)
      notes: list[str] = []
      if box and box.low <= (low + high) / 2 <= box.high:
        p = min(0.92, p + 0.08 * max(0, 1.0 - box.tightness))
        notes.append(
          f"Inside {box.hours:.0f}h consolidation ${box.low:,.0f}–${box.high:,.0f}"
        )
      mid = (low + high) / 2
      dist = abs(mid - current_price) / current_price * 100
      score = p - dist * 0.002
      band_scores.append((score, m, p, notes))

    band_scores.sort(key=lambda x: x[0], reverse=True)
    for _score, m, p, notes in band_scores[: self.top_bands]:
      low = float(m.floor_strike or 0)
      high = float(m.cap_strike or low)
      sig, edge = self._signal(p, m.yes_mid)
      range_rows.append(
        ContractOdds(
          ticker=m.ticker,
          contract_type="range",
          label=m.subtitle or f"${low:,.0f}–${high:,.0f}",
          model_prob=p,
          kalshi_mid=m.yes_mid,
          edge=edge,
          signal=sig,
          strike_type="between",
          floor_strike=low,
          cap_strike=high,
          notes=notes,
        )
      )

    best_threshold = max(threshold_rows, key=lambda r: abs(r.edge or 0), default=None)
    most_likely_threshold = max(threshold_rows, key=lambda r: r.model_prob, default=None)
    most_likely_range = max(range_rows, key=lambda r: r.model_prob, default=None)

    zone_lo = mu - sigma * 0.45
    zone_hi = mu + sigma * 0.45
    ml_parts: list[str] = []
    if most_likely_threshold:
      ml_parts.append(
        f"{most_likely_threshold.label} ({most_likely_threshold.model_prob * 100:.0f}% model)"
      )
    if most_likely_range:
      ml_parts.append(
        f"band {most_likely_range.label} ({most_likely_range.model_prob * 100:.0f}% model)"
      )

    supports = [l for l in levels if l.level_type == "support"][:3]
    resists = [l for l in levels if l.level_type == "resistance"][:3]

    return {
      "ok": True,
      "method": "daily_structure",
      "current_price": round(current_price, 2),
      "terminal_mu": round(mu, 2),
      "terminal_sigma": round(sigma, 2),
      "hours_to_settle": round(hours_left, 2),
      "event": {
        "event_ticker": book.event_ticker,
        "series_ticker": book.series_ticker,
        "frequency": book.frequency,
        "title": book.title,
        "close_time": book.close_time.isoformat(),
      },
      "structure": {
        "support_levels": levels_to_dict(supports),
        "resistance_levels": levels_to_dict(resists),
        "consolidation": {
          "low": round(box.low, 2),
          "high": round(box.high, 2),
          "hours": round(box.hours, 1),
          "tightness": round(box.tightness, 2),
        }
        if box
        else None,
      },
      "most_likely": {
        "settlement_zone_low": round(zone_lo, 2),
        "settlement_zone_high": round(zone_hi, 2),
        "summary": (
          f"BRTI likely ${zone_lo:,.0f}–${zone_hi:,.0f} at settle"
          + (f"; best threshold: {ml_parts[0]}" if ml_parts else "")
          + (f"; best band: {ml_parts[1]}" if len(ml_parts) > 1 else "")
        ),
        "threshold": most_likely_threshold.to_dict() if most_likely_threshold else None,
        "range": most_likely_range.to_dict() if most_likely_range else None,
      },
      "strategy_threshold": {
        "summary": (
          f"Best edge (mispricing): {best_threshold.label} ({best_threshold.signal})"
          if best_threshold and best_threshold.edge is not None
          else "Threshold odds vs Kalshi YES mid"
        ),
        "contracts": [c.to_dict() for c in threshold_rows],
        "best_edge": best_threshold.to_dict() if best_threshold else None,
        "most_likely": most_likely_threshold.to_dict() if most_likely_threshold else None,
      },
      "strategy_range": {
        "summary": (
          f"Most likely stall band: {most_likely_range.label} ({most_likely_range.model_prob * 100:.0f}% model)"
          if most_likely_range
          else "Range band odds"
        ),
        "contracts": [c.to_dict() for c in range_rows],
        "best_edge": max(range_rows, key=lambda r: abs(r.edge or 0), default=None).to_dict()
        if range_rows
        else None,
        "most_likely": most_likely_range.to_dict() if most_likely_range else None,
      },
    }
