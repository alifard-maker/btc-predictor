"""Daily / hourly Kalshi threshold + range-band probability engine."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
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

  def __init__(self, cfg: dict[str, Any], *, daily_cfg: dict[str, Any] | None = None):
    self.cfg = cfg
    dcfg = daily_cfg if daily_cfg is not None else cfg.get("daily", {})
    self.markets = KalshiDailyMarkets(cfg, daily_cfg=dcfg)
    self.min_edge = float(dcfg.get("min_edge", 0.05))
    self.nearby_strikes = int(dcfg.get("nearby_strikes", 30))
    self.top_bands = int(dcfg.get("top_bands", 30))
    self.strike_sigma_window = float(dcfg.get("strike_sigma_window", 2.5))
    self.band_sigma_window = float(dcfg.get("band_sigma_window", 2.5))
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
    """Range-band mispricing vs Kalshi YES mid."""
    edge = None
    if kalshi_mid is not None:
      edge = model_p - kalshi_mid
      if edge >= self.min_edge:
        return "LEAN YES", edge
      if edge <= -self.min_edge:
        return "LEAN NO", edge
    return "NEUTRAL", edge

  def _signal_threshold(
    self,
    model_p: float,
    kalshi_mid: float | None,
    strike_type: str,
  ) -> tuple[str, float | None]:
    """Threshold signal — LEAN only when model direction and edge agree.

    ≥ strike: model >50% + cheap YES → LEAN YES; model <50% + rich YES → LEAN NO.
    Otherwise mispricing is tagged VALUE YES (cheap tail) or FADE YES (rich ITM).
    """
    if kalshi_mid is None:
      return "NEUTRAL", None
    edge = model_p - kalshi_mid
    if abs(edge) < self.min_edge:
      return "NEUTRAL", edge
    favors_yes = model_p >= 0.5
    if edge > 0:
      return ("LEAN YES" if favors_yes else "VALUE YES"), edge
    return ("LEAN NO" if not favors_yes else "FADE YES"), edge

  @staticmethod
  def _threshold_strike(m: KalshiContractMarket) -> float | None:
    if m.strike_type == "greater" and m.floor_strike is not None:
      return float(m.floor_strike)
    if m.strike_type == "less" and m.cap_strike is not None:
      return float(m.cap_strike)
    return None

  @staticmethod
  def _band_bounds(m: KalshiContractMarket) -> tuple[float, float] | None:
    if m.strike_type != "between":
      return None
    low = float(m.floor_strike or 0)
    high = float(m.cap_strike or low)
    if low <= 0 or high <= low:
      return None
    return low, high

  def _mu_window(self, mu: float, sigma: float, sigma_mult: float) -> float:
    return max(sigma * sigma_mult, mu * 0.003)

  def _build_threshold_odds(
    self,
    m: KalshiContractMarket,
    mu: float,
    sigma: float,
    levels: list[PriceLevel],
  ) -> ContractOdds | None:
    if m.strike_type == "greater" and m.floor_strike is not None:
      strike = float(m.floor_strike)
      p = self._prob_above(strike, mu, sigma)
      p, notes = self._level_nudge(p, strike, levels, above=True)
      label = m.subtitle or f"≥ ${strike:,.0f}"
      strike_type = "greater"
      floor_strike, cap_strike = strike, None
    elif m.strike_type == "less" and m.cap_strike is not None:
      strike = float(m.cap_strike)
      p = self._prob_below(strike, mu, sigma)
      p, notes = self._level_nudge(p, strike, levels, above=False)
      label = m.subtitle or f"< ${strike:,.0f}"
      strike_type = "less"
      floor_strike, cap_strike = None, strike
    else:
      return None
    sig, edge = self._signal_threshold(p, m.yes_mid, strike_type)
    return ContractOdds(
      ticker=m.ticker,
      contract_type="threshold",
      label=label,
      model_prob=p,
      kalshi_mid=m.yes_mid,
      edge=edge,
      signal=sig,
      strike_type=strike_type,
      floor_strike=floor_strike,
      cap_strike=cap_strike,
      notes=notes,
    )

  def _build_range_odds(
    self,
    m: KalshiContractMarket,
    mu: float,
    sigma: float,
    box: ConsolidationBox | None,
  ) -> ContractOdds | None:
    bounds = self._band_bounds(m)
    if bounds is None:
      return None
    low, high = bounds
    p = self._prob_between(low, high, mu, sigma)
    notes: list[str] = []
    if box and box.low <= (low + high) / 2 <= box.high:
      p = min(0.92, p + 0.08 * max(0, 1.0 - box.tightness))
      notes.append(
        f"Inside {box.hours:.0f}h consolidation ${box.low:,.0f}–${box.high:,.0f}"
      )
    sig, edge = self._signal(p, m.yes_mid)
    return ContractOdds(
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

  def _near_threshold_markets(
    self,
    markets: list[KalshiContractMarket],
    mu: float,
    sigma: float,
  ) -> list[KalshiContractMarket]:
    """Nearest strikes to forecast μ (up to nearby_strikes)."""
    scored: list[tuple[float, KalshiContractMarket]] = []
    for m in markets:
      if m.strike_type not in ("greater", "less"):
        continue
      strike = self._threshold_strike(m)
      if strike is None:
        continue
      scored.append((abs(strike - mu), m))
    scored.sort(key=lambda x: x[0])
    return [m for _, m in scored[: self.nearby_strikes]]

  def _near_band_markets(
    self,
    markets: list[KalshiContractMarket],
    mu: float,
    sigma: float,
  ) -> list[KalshiContractMarket]:
    """Nearest range bands to forecast μ (up to top_bands)."""
    scored: list[tuple[float, KalshiContractMarket]] = []
    for m in markets:
      bounds = self._band_bounds(m)
      if bounds is None:
        continue
      low, high = bounds
      mid = (low + high) / 2
      scored.append((abs(mid - mu), m))
    scored.sort(key=lambda x: x[0])
    return [m for _, m in scored[: self.top_bands]]

  def _most_likely_threshold(
    self,
    markets: list[KalshiContractMarket],
    mu: float,
    sigma: float,
    levels: list[PriceLevel],
  ) -> ContractOdds | None:
    """ATM threshold within forecast window — None if book doesn't cover μ."""
    window = self._mu_window(mu, sigma, self.strike_sigma_window)
    best: ContractOdds | None = None
    best_dist = float("inf")
    for m in markets:
      strike = self._threshold_strike(m)
      if strike is None:
        continue
      dist = abs(strike - mu)
      if dist > window:
        continue
      if dist >= best_dist:
        continue
      row = self._build_threshold_odds(m, mu, sigma, levels)
      if row is None:
        continue
      best = row
      best_dist = dist
    return best

  def _most_likely_range(
    self,
    markets: list[KalshiContractMarket],
    mu: float,
    sigma: float,
    box: ConsolidationBox | None,
  ) -> ContractOdds | None:
    window = self._mu_window(mu, sigma, self.band_sigma_window)
    best: ContractOdds | None = None
    best_p = -1.0
    for m in markets:
      bounds = self._band_bounds(m)
      if bounds is None:
        continue
      low, high = bounds
      mid = (low + high) / 2
      contains = low <= mu <= high
      if not contains and abs(mid - mu) > window:
        continue
      row = self._build_range_odds(m, mu, sigma, box)
      if row is None:
        continue
      if row.model_prob > best_p:
        best_p = row.model_prob
        best = row
    return best

  def _row_near_forecast(
    self,
    row: dict[str, Any] | ContractOdds,
    mu: float,
    sigma: float,
  ) -> bool:
    window = self._mu_window(mu, sigma, self.strike_sigma_window)
    if isinstance(row, ContractOdds):
      data = row.to_dict()
    else:
      data = row
    if data.get("contract_type") == "range":
      lo, hi = data.get("floor_strike"), data.get("cap_strike")
      if lo is not None and hi is not None:
        lo_f, hi_f = float(lo), float(hi)
        return lo_f <= mu <= hi_f or abs((lo_f + hi_f) / 2 - mu) <= window
      return False
    strike = data.get("floor_strike") or data.get("cap_strike")
    if strike is None:
      return False
    return abs(float(strike) - mu) <= window

  def _book_covers_forecast(
    self,
    book: DailyEventBook,
    mu: float,
    sigma: float,
  ) -> bool:
    window = self._mu_window(mu, sigma, self.strike_sigma_window)
    strikes = self._threshold_strike_values(book.threshold_markets)
    if strikes and min(abs(s - mu) for s in strikes) <= window:
      return True
    for m in book.range_markets:
      bounds = self._band_bounds(m)
      if bounds is None:
        continue
      low, high = bounds
      if low <= mu <= high or abs((low + high) / 2 - mu) <= window:
        return True
    return False

  @staticmethod
  def _threshold_strike_values(markets: list[KalshiContractMarket]) -> list[float]:
    out: list[float] = []
    for m in markets:
      if m.strike_type == "greater" and m.floor_strike is not None:
        out.append(float(m.floor_strike))
      elif m.strike_type == "less" and m.cap_strike is not None:
        out.append(float(m.cap_strike))
    return out

  @staticmethod
  def _threshold_strike_from_row(row: ContractOdds) -> float | None:
    if row.strike_type == "greater":
      return row.floor_strike
    if row.strike_type == "less":
      return row.cap_strike
    return None

  def predict(
    self,
    *,
    current_price: float,
    df_1h: pd.DataFrame | None,
    book: DailyEventBook | None = None,
    override_mu: float | None = None,
    override_sigma: float | None = None,
  ) -> dict[str, Any]:
    book = book or self.markets.active_book(reference_price=current_price)
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

    # --- Strategy 1: threshold (above/below) near forecast μ ---
    threshold_rows: list[ContractOdds] = []
    for m in self._near_threshold_markets(book.threshold_markets, mu, sigma):
      row = self._build_threshold_odds(m, mu, sigma, levels)
      if row:
        threshold_rows.append(row)
    threshold_rows.sort(
      key=lambda r: (self._threshold_strike_from_row(r) or mu),
    )

    # --- Strategy 2: range bands with highest mass near μ ---
    range_rows: list[ContractOdds] = []
    for m in self._near_band_markets(book.range_markets, mu, sigma):
      row = self._build_range_odds(m, mu, sigma, box)
      if row:
        range_rows.append(row)
    range_rows.sort(key=lambda r: r.model_prob, reverse=True)

    near_threshold_rows = [r for r in threshold_rows if self._row_near_forecast(r, mu, sigma)]
    near_range_rows = [r for r in range_rows if self._row_near_forecast(r, mu, sigma)]

    best_threshold = max(near_threshold_rows, key=lambda r: abs(r.edge or 0), default=None)
    most_likely_threshold = self._most_likely_threshold(
      book.threshold_markets, mu, sigma, levels
    )
    most_likely_range = self._most_likely_range(book.range_markets, mu, sigma, box)

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

    covers = self._book_covers_forecast(book, mu, sigma)

    return {
      "ok": True,
      "method": "daily_structure",
      "current_price": round(current_price, 2),
      "terminal_mu": round(mu, 2),
      "terminal_sigma": round(sigma, 2),
      "hours_to_settle": round(hours_left, 2),
      "forecast_covers_book": covers,
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
          else (
            "No threshold near forecast μ — see range bands or settlement zone."
            if not covers
            else "Threshold odds vs Kalshi YES mid"
          )
        ),
        "contracts": [c.to_dict() for c in threshold_rows],
        "best_edge": best_threshold.to_dict() if best_threshold else None,
        "most_likely": most_likely_threshold.to_dict() if most_likely_threshold else None,
      },
      "strategy_range": {
        "summary": (
          f"Most likely stall band: {most_likely_range.label} ({most_likely_range.model_prob * 100:.0f}% model)"
          if most_likely_range
          else (
            "No range band near forecast μ — Kalshi book may not bracket BRTI."
            if not covers
            else "Range band odds"
          )
        ),
        "contracts": [c.to_dict() for c in range_rows],
        "best_edge": (
          max(near_range_rows, key=lambda r: abs(r.edge or 0), default=None).to_dict()
          if near_range_rows
          else None
        ),
        "most_likely": most_likely_range.to_dict() if most_likely_range else None,
      },
    }
