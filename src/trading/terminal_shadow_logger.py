"""Track B Phase B0: terminal window shadow logger (no orders)."""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_LAST_EVENT_BY_ASSET: dict[str, str] = {}
_LAST_LOG_MONO_BY_KEY: dict[str, float] = {}
_FINALIZED_EVENTS: set[str] = set()


def _norm_cdf(x: float) -> float:
  return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def track_b_shadow_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  from src.trading.pnl_first_gates import _pnl_first_cfg

  return dict(_pnl_first_cfg(cfg).get("track_b_shadow") or {})


def track_b_shadow_active(cfg: dict[str, Any] | None) -> bool:
  return bool(track_b_shadow_cfg(cfg).get("enabled"))


def track_b_epoch_iso(cfg: dict[str, Any] | None) -> str | None:
  if not track_b_shadow_active(cfg):
    return None
  block = track_b_shadow_cfg(cfg)
  raw = block.get("stats_epoch_at") or block.get("started_at")
  return str(raw) if raw else None


def shadow_assets(cfg: dict[str, Any] | None) -> tuple[str, ...]:
  block = track_b_shadow_cfg(cfg)
  raw = block.get("assets") or ["eth"]
  return tuple(str(a).lower() for a in raw if str(a).strip())


def shadow_log_dir(cfg: dict[str, Any] | None = None) -> Path:
  del cfg
  base = Path(os.getenv("DATA_DIR", "data"))
  d = base / "logs" / "terminal_shadow"
  d.mkdir(parents=True, exist_ok=True)
  return d


def prob_above_strike(
  spot: float,
  strike: float,
  sigma_terminal: float,
  hours_left: float,
) -> float:
  """GBM terminal prob spot finishes above strike."""
  if spot <= 0 or strike <= 0:
    return 0.5
  hl = max(1.0 / 3600.0, float(hours_left))
  sigma = max(float(sigma_terminal) * math.sqrt(hl), spot * 0.0005)
  z = (float(strike) - float(spot)) / sigma
  return max(0.0, min(1.0, 1.0 - _norm_cdf(z)))


def _pick_shadow_contract(live: dict[str, Any]) -> dict[str, Any] | None:
  strat = live.get("strategy_threshold") or {}
  pick = strat.get("best_edge")
  if isinstance(pick, dict) and pick.get("yes_ask") is not None:
    return pick
  contracts = strat.get("contracts") or []
  with_ask = [c for c in contracts if c.get("yes_ask") is not None]
  if not with_ask:
    return None
  spot = float(live.get("current_price") or 0)

  def _dist(c: dict[str, Any]) -> float:
    strike = c.get("floor_strike") or c.get("cap_strike")
    if strike is None:
      return 1e9
    return abs(float(strike) - spot)

  return min(with_ask, key=_dist)


def _strike_value(pick: dict[str, Any]) -> float | None:
  if pick.get("floor_strike") is not None:
    return float(pick["floor_strike"])
  if pick.get("cap_strike") is not None:
    return float(pick["cap_strike"])
  return None


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def _parse_iso(raw: str | None) -> datetime | None:
  if not raw:
    return None
  try:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
  except ValueError:
    return None


def _epoch_ok(ts_iso: str, epoch_iso: str | None) -> bool:
  if not epoch_iso:
    return True
  ts = _parse_iso(ts_iso)
  epoch = _parse_iso(epoch_iso)
  if ts is None or epoch is None:
    return True
  return ts >= epoch


def _taker_fee_usd(ask_prob: float, fee_rate: float) -> float:
  return max(0.0, float(ask_prob) * float(fee_rate))


def _shadow_would_pnl_yes(
  *,
  settled_yes: bool,
  yes_ask: float,
  fee_rate: float,
) -> float:
  fee = _taker_fee_usd(yes_ask, fee_rate)
  cost = float(yes_ask) + fee
  if settled_yes:
    return 1.0 - cost
  return -cost


def maybe_log_terminal_shadow(
  tab: dict[str, Any],
  cfg: dict[str, Any] | None,
  *,
  asset: str,
) -> dict[str, Any] | None:
  """Append one shadow sample when in terminal window. No orders."""
  if not track_b_shadow_active(cfg):
    return None
  asset_key = str(asset).lower()
  if asset_key not in shadow_assets(cfg):
    return None
  if not tab.get("ok"):
    return None

  block = track_b_shadow_cfg(cfg)
  live = tab.get("live") or tab
  event_ticker = str((tab.get("event") or live.get("event") or {}).get("event_ticker") or "")
  if not event_ticker:
    return None

  prev = _LAST_EVENT_BY_ASSET.get(asset_key)
  if prev and prev != event_ticker:
    finalize_terminal_shadow_event(prev, cfg, asset=asset_key)
  _LAST_EVENT_BY_ASSET[asset_key] = event_ticker

  hours_left = float(live.get("hours_to_settle") or 0)
  max_h = float(block.get("max_hours_to_settle", 0.25))
  min_h = float(block.get("min_hours_to_settle", 0.0))
  if hours_left > max_h or hours_left < min_h:
    return {"ok": True, "skipped": True, "reason": "outside_terminal_window", "hours_to_settle": hours_left}

  cadence = max(5, int(block.get("cadence_seconds", 10)))
  cadence_key = f"{asset_key}:{event_ticker}"
  now_mono = time.monotonic()
  last_mono = _LAST_LOG_MONO_BY_KEY.get(cadence_key)
  if last_mono is not None and (now_mono - last_mono) < cadence:
    return {"ok": True, "skipped": True, "reason": "cadence"}

  pick = _pick_shadow_contract(live)
  if pick is None:
    return {"ok": True, "skipped": True, "reason": "no_book"}

  strike = _strike_value(pick)
  spot = float(live.get("current_price") or live.get("brti_live") or tab.get("brti_live") or 0)
  sigma = float(live.get("terminal_sigma") or spot * 0.004)
  spot_implied = prob_above_strike(spot, float(strike or spot), sigma, hours_left)
  yes_mid = pick.get("kalshi_mid")
  yes_ask = float(pick.get("yes_ask") or 0)
  fee_rate = float(block.get("taker_fee_rate", 0.07))
  fee_1 = _taker_fee_usd(yes_ask, fee_rate)
  edge_prob = spot_implied - yes_ask - fee_1
  edge_cents = round(edge_prob * 100.0, 2)

  ts = datetime.now(timezone.utc).isoformat()
  row = {
    "type": "sample",
    "ts": ts,
    "track_b_epoch_at": track_b_epoch_iso(cfg),
    "asset": asset_key,
    "event_ticker": event_ticker,
    "market_ticker": pick.get("ticker"),
    "strike": strike,
    "spot": round(spot, 4),
    "time_left_s": round(hours_left * 3600.0, 1),
    "spot_implied_prob": round(spot_implied, 4),
    "kalshi_yes_mid": yes_mid,
    "kalshi_yes_ask": round(yes_ask, 4),
    "edge_cents": edge_cents,
    "moneyness_usd": round(spot - float(strike or spot), 4),
    "realized_vol_1h": round(sigma, 4),
    "plan": "4k_week",
  }
  path = shadow_log_dir(cfg) / f"{event_ticker}.jsonl"
  _append_jsonl(path, row)
  _LAST_LOG_MONO_BY_KEY[cadence_key] = now_mono
  return {"ok": True, "logged": True, "event_ticker": event_ticker, "edge_cents": edge_cents}


def finalize_terminal_shadow_event(
  event_ticker: str,
  cfg: dict[str, Any] | None,
  *,
  asset: str,
  settle_spot: float | None = None,
) -> dict[str, Any] | None:
  """Append settlement summary for a closed hourly event (shadow only)."""
  if not track_b_shadow_active(cfg):
    return None
  key = f"{asset}:{event_ticker}"
  if key in _FINALIZED_EVENTS:
    return {"ok": True, "skipped": True, "reason": "already_finalized"}
  path = shadow_log_dir(cfg) / f"{event_ticker}.jsonl"
  if not path.exists():
    return {"ok": True, "skipped": True, "reason": "no_samples"}

  epoch_iso = track_b_epoch_iso(cfg)
  samples: list[dict[str, Any]] = []
  for line in path.read_text(encoding="utf-8").splitlines():
    if not line.strip():
      continue
    try:
      row = json.loads(line)
    except json.JSONDecodeError:
      continue
    if row.get("type") == "settlement":
      _FINALIZED_EVENTS.add(key)
      return {"ok": True, "skipped": True, "reason": "settlement_exists"}
    if row.get("type") != "sample":
      continue
    if not _epoch_ok(str(row.get("ts") or ""), epoch_iso):
      continue
    samples.append(row)

  if not samples:
    return {"ok": True, "skipped": True, "reason": "no_epoch_samples"}

  final_10m = [s for s in samples if float(s.get("time_left_s") or 9999) <= 600.0]
  use = final_10m or samples
  edges = [float(s["edge_cents"]) for s in use if s.get("edge_cents") is not None]
  median_edge = round(statistics.median(edges), 2) if edges else None
  last = use[-1]
  strike = last.get("strike")
  fee_rate = float(track_b_shadow_cfg(cfg).get("taker_fee_rate", 0.07))
  yes_ask = float(last.get("kalshi_yes_ask") or 0)

  settled_yes: bool | None = None
  shadow_pnl: float | None = None
  if settle_spot is not None and strike is not None:
    settled_yes = float(settle_spot) >= float(strike)
    shadow_pnl = round(_shadow_would_pnl_yes(settled_yes=settled_yes, yes_ask=yes_ask, fee_rate=fee_rate), 4)

  settlement = {
    "type": "settlement",
    "ts": datetime.now(timezone.utc).isoformat(),
    "track_b_epoch_at": epoch_iso,
    "asset": asset,
    "event_ticker": event_ticker,
    "samples": len(samples),
    "samples_final_10m": len(final_10m),
    "median_edge_cents": median_edge,
    "median_edge_cents_final_10m": median_edge if final_10m else None,
    "last_yes_ask": yes_ask,
    "strike": strike,
    "settle_spot": settle_spot,
    "settled_yes": settled_yes,
    "shadow_would_pnl_usd": shadow_pnl,
    "plan": "4k_week",
  }
  _append_jsonl(path, settlement)
  _FINALIZED_EVENTS.add(key)
  return {"ok": True, "finalized": True, "event_ticker": event_ticker, "settlement": settlement}


def summarize_track_b_shadow(
  cfg: dict[str, Any] | None,
  *,
  asset: str = "eth",
) -> dict[str, Any]:
  """Aggregate shadow JSONL since track_b epoch."""
  block = track_b_shadow_cfg(cfg)
  epoch_iso = track_b_epoch_iso(cfg)
  if not track_b_shadow_active(cfg):
    return {"ok": False, "enabled": False, "asset": asset}

  root = shadow_log_dir(cfg)
  events = 0
  samples = 0
  final_edges: list[float] = []
  would_pnls: list[float] = []
  last_sample_at: str | None = None
  promotion = {
    "min_events": int(block.get("promotion_min_events", 50)),
    "min_median_edge_cents_final_10m": float(block.get("promotion_min_median_edge_cents", 8.0)),
    "min_positive_would_pnl_events": int(block.get("promotion_min_would_pnl_events", 100)),
  }

  for path in sorted(root.glob("*.jsonl")):
    event_samples: list[dict[str, Any]] = []
    settlement_row: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
      if not line.strip():
        continue
      try:
        row = json.loads(line)
      except json.JSONDecodeError:
        continue
      if str(row.get("asset") or "").lower() != str(asset).lower():
        continue
      if row.get("type") == "settlement":
        if _epoch_ok(str(row.get("ts") or ""), epoch_iso):
          settlement_row = row
        continue
      if row.get("type") != "sample":
        continue
      if not _epoch_ok(str(row.get("ts") or ""), epoch_iso):
        continue
      event_samples.append(row)

    if not event_samples:
      continue
    events += 1
    samples += len(event_samples)
    if event_samples:
      ts = str(event_samples[-1].get("ts") or "")
      if last_sample_at is None or ts > last_sample_at:
        last_sample_at = ts
    final_10m = [s for s in event_samples if float(s.get("time_left_s") or 9999) <= 600.0]
    use = final_10m or event_samples
    for s in use:
      if s.get("edge_cents") is not None:
        final_edges.append(float(s["edge_cents"]))
    if settlement_row and settlement_row.get("shadow_would_pnl_usd") is not None:
      would_pnls.append(float(settlement_row["shadow_would_pnl_usd"]))

  median_edge = round(statistics.median(final_edges), 2) if final_edges else None
  total_would_pnl = round(sum(would_pnls), 2) if would_pnls else 0.0
  return {
    "ok": True,
    "enabled": True,
    "lane": "track_b_shadow",
    "label": "Track B · terminal shadow (hourly final 15m)",
    "asset": asset,
    "stats_epoch_at": epoch_iso,
    "events_logged": events,
    "samples": samples,
    "median_edge_cents": median_edge,
    "settled_events": len(would_pnls),
    "shadow_would_pnl_usd": total_would_pnl,
    "last_sample_at": last_sample_at,
    "promotion": promotion,
    "promotion_ready": bool(
      events >= promotion["min_events"]
      and median_edge is not None
      and median_edge >= promotion["min_median_edge_cents_final_10m"]
    ),
    "note": "Shadow only — no orders. Hypothetical P&L uses last-sample ask when settle_spot known.",
  }
