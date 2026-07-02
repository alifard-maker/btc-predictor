"""Hourly V2 (path memory) helpers — isolated from v1 hourly predictor/bot."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.assets import asset_v2_cfg, asset_v2_enabled, asset_v2_runtime_cfg, index_id_for_cfg
from src.calibration.hourly_tracker import HourlyCalibrationTracker
from src.models.path_hourly_predictor import PathHourlyPredictor

if TYPE_CHECKING:
  from src.scheduler.loop import PredictionLoop

log = logging.getLogger(__name__)


def v2_cfg_for_asset(loop: PredictionLoop, asset: str) -> dict[str, Any] | None:
  asset = asset.lower()
  if asset == "btc":
    return loop._btc_v2_cfg
  if asset == "eth":
    return loop._eth_v2_cfg
  return None


def v2_calibration_for_asset(loop: PredictionLoop, asset: str) -> HourlyCalibrationTracker | None:
  asset = asset.lower()
  if asset == "btc":
    return loop.btc_hourly_v2_calibration
  if asset == "eth":
    return loop.eth_hourly_v2_calibration
  return None


def path_predictor_for_asset(loop: PredictionLoop, asset: str) -> PathHourlyPredictor:
  asset = asset.lower()
  if asset not in loop._path_hourly_predictors:
    acfg = v2_cfg_for_asset(loop, asset)
    if acfg is None:
      raise RuntimeError(f"Hourly v2 disabled for {asset}")
    loop._path_hourly_predictors[asset] = PathHourlyPredictor(acfg, asset=asset)
  return loop._path_hourly_predictors[asset]


def init_hourly_v2(loop: PredictionLoop) -> None:
  from src.config import ensure_dirs

  loop._btc_v2_cfg = None
  loop._eth_v2_cfg = None
  loop.btc_hourly_v2_calibration = None
  loop.eth_hourly_v2_calibration = None
  loop._path_hourly_predictors = {}
  loop._hourly_v2_tab_cache: dict[str, tuple[dict[str, Any], float]] = {}
  loop.latest_btc_hourly_v2_prediction = None
  loop.latest_eth_hourly_v2_prediction = None

  if asset_v2_enabled(loop.cfg, "btc"):
    loop._btc_v2_cfg = asset_v2_cfg(loop.cfg, "btc")
    ensure_dirs(loop._btc_v2_cfg)
    loop.btc_hourly_v2_calibration = HourlyCalibrationTracker(loop._btc_v2_cfg, asset="btc")
  if asset_v2_enabled(loop.cfg, "eth"):
    loop._eth_v2_cfg = asset_v2_cfg(loop.cfg, "eth")
    ensure_dirs(loop._eth_v2_cfg)
    loop.eth_hourly_v2_calibration = HourlyCalibrationTracker(loop._eth_v2_cfg, asset="eth")

  for asset in ("btc", "eth"):
    if not asset_v2_enabled(loop.cfg, asset):
      continue
    _seed_v2_bot_settings(loop, asset)


def _seed_v2_bot_settings(loop: PredictionLoop, asset: str) -> None:
  from src.trading.hourly_bot_store import HourlyBotSettings

  acfg = v2_cfg_for_asset(loop, asset)
  if acfg is None:
    return
  bot_cfg = acfg.get("hourly_v2", {}).get("bot") or {}
  store = loop.hourly_bot_store(asset, kind="hourly_v2")
  cur = store.get_settings()
  store.save_settings(
    HourlyBotSettings(
      **{
        **cur.to_dict(),
        "enabled": bool(bot_cfg.get("enabled", cur.enabled)),
        "mode": str(bot_cfg.get("mode", cur.mode)),
        "max_spend_per_hour_usd": float(
          bot_cfg.get("max_spend_per_hour_usd", cur.max_spend_per_hour_usd)
        ),
        "continuous": bool(bot_cfg.get("continuous_enabled", cur.continuous)),
      }
    )
  )


def hourly_v2_tab_prediction(
  loop: PredictionLoop,
  asset: str,
  *,
  include_bot: bool = True,
) -> dict[str, Any]:
  acfg = v2_cfg_for_asset(loop, asset)
  if acfg is None or not acfg.get("hourly_v2", {}).get("enabled", True):
    return {"ok": False, "error": f"{asset.upper()} hourly v2 disabled"}
  if not acfg.get("daily", {}).get("enabled", True):
    return {"ok": False, "error": f"{asset.upper()} Kalshi daily/hourly book disabled"}

  quote = loop.live_price_quote(fresh=False, asset=asset)
  price = quote.price if quote else None
  storage = loop.storage if asset == "btc" else loop.eth_storage()
  if price is None:
    df_1m = storage.load("1m")
    if not df_1m.empty:
      price = float(df_1m["close"].iloc[-1])
  index_label = index_id_for_cfg(acfg)
  if price is None or price <= 0:
    return {"ok": False, "error": f"Live {index_label} unavailable"}

  df_1h = loop._ohlc_1h(storage=storage)
  df_15m = storage.load("15m")
  df_1m = storage.load("1m")
  predictor = path_predictor_for_asset(loop, asset)
  tracker = v2_calibration_for_asset(loop, asset)
  if tracker is None:
    return {"ok": False, "error": "v2 calibration unavailable"}

  lock_price = None
  if tracker:
    pending = tracker.get_pending()
    if pending:
      last = pending[-1]
      if last.get("reference_price"):
        lock_price = float(last["reference_price"])

  live = predictor.predict(
    current_price=float(price),
    df_1h=df_1h,
    df_15m=df_15m if not df_15m.empty else None,
    df_1m=df_1m if not df_1m.empty else None,
    lock_price=lock_price,
  )
  if not live.get("ok"):
    return live

  from src.models.hourly_snapshot import (
    hour_open_prediction_from_row,
    late_call_prediction_from_row,
    locked_prediction_from_row,
  )

  event_ticker = (live.get("event") or {}).get("event_ticker")
  locked = None
  hour_open = None
  late_call = None
  if event_ticker:
    row = tracker.get_logged(event_ticker)
    if row:
      locked = locked_prediction_from_row(row, acfg)
      late_call = late_call_prediction_from_row(row, acfg)
    open_row = tracker.get_hour_open(event_ticker)
    if open_row:
      hour_open = hour_open_prediction_from_row(open_row, acfg, index_label=index_label)

  out = {
    **live,
    "live": live,
    "locked": locked,
    "has_locked": locked is not None,
    "hour_open": hour_open,
    "has_hour_open": hour_open is not None,
    "late_call": late_call,
    "has_late_call": late_call is not None,
    "asset": asset,
    "predictor_version": "v2_path",
  }
  if quote:
    out["brti_live"] = round(quote.price, 2)
    out["brti_source"] = quote.source
    live["brti_live"] = out["brti_live"]
    live["brti_source"] = quote.source
  out["timezone"] = loop.tz
  live["timezone"] = loop.tz
  live["asset"] = asset
  live["index_id"] = index_label
  pf = acfg.get("price_feed") or {}
  out["price_feed"] = pf.get("label", index_label)
  out["settlement_reference"] = pf.get("settlement_reference", index_label)

  if locked and live.get("terminal_mu") is not None and locked.get("terminal_mu") is not None:
    out["live_vs_locked"] = {
      "mu_shift": round(float(live["terminal_mu"]) - float(locked["terminal_mu"]), 2),
      "reference_at_log": locked.get("reference_price"),
      "logged_at": locked.get("logged_at"),
    }
    live["live_vs_locked"] = out["live_vs_locked"]

  from src.trading.hourly_guidance import build_hourly_guidance
  from src.trading.hourly_intrahour_alert import assess_intrahour_opportunity

  out["guidance"] = build_hourly_guidance(
    live, locked, hour_open=hour_open, asset=asset, index_id=index_label, cfg=acfg
  )
  intrahour = assess_intrahour_opportunity(
    live=live,
    locked=locked,
    hour_open=hour_open,
    current_price=float(price) if price else None,
    index_label=index_label,
    cfg=acfg,
  )
  out["intrahour_opportunity"] = intrahour
  out["has_intrahour_opportunity"] = bool(intrahour and intrahour.get("highlight"))

  if asset == "btc":
    loop.latest_btc_hourly_v2_prediction = out
  else:
    loop.latest_eth_hourly_v2_prediction = out

  if include_bot:
    out["bot"] = loop.hourly_bot_status(asset, out, kind="hourly_v2")
  return out


def resolve_hourly_v2_outcomes(loop: PredictionLoop, *, asset: str = "btc") -> None:
  from src.data.kalshi_hourly import try_resolve_pending

  tracker = v2_calibration_for_asset(loop, asset)
  if tracker is None:
    return
  pending = tracker.get_pending()
  if not pending:
    return
  kalshi = loop._kalshi_for(asset)
  resolved = 0
  for row in pending:
    res = try_resolve_pending(kalshi, row)
    if res is None:
      continue
    if tracker.resolve(str(row["event_ticker"]), res):
      resolved += 1
  if resolved:
    log.info("Resolved %d %s hourly v2 predictions via Kalshi", resolved, asset.upper())


def run_hourly_v2_prediction_for_asset(
  loop: PredictionLoop,
  asset: str,
  *,
  force: bool = False,
) -> dict[str, Any] | None:
  acfg = v2_cfg_for_asset(loop, asset)
  if acfg is None:
    return None
  try:
    resolve_hourly_v2_outcomes(loop, asset=asset)
    out = hourly_v2_tab_prediction(loop, asset, include_bot=False)
    if not out.get("ok"):
      return out
    predictor = path_predictor_for_asset(loop, asset)
    row = predictor.to_log_row(out.get("live") or out)
    tracker = v2_calibration_for_asset(loop, asset)
    if tracker and row.get("event_ticker"):
      tracker.log_prediction(row, force=force)
      log.info(
        "%s hourly v2 prediction logged: %s %s %s",
        asset.upper(),
        row.get("event_ticker"),
        row.get("primary_signal"),
        row.get("primary_label"),
      )
    return hourly_v2_tab_prediction(loop, asset)
  except Exception as e:
    log.exception("%s hourly v2 prediction failed: %s", asset.upper(), e)
    loop.last_error = str(e)
    return None


def run_hourly_v2_open_for_asset(loop: PredictionLoop, asset: str) -> dict[str, Any] | None:
  acfg = v2_cfg_for_asset(loop, asset)
  if acfg is None or not acfg.get("hourly_v2", {}).get("hour_open_snapshot", True):
    return None
  try:
    preview = hourly_v2_tab_prediction(loop, asset, include_bot=False)
    if not preview.get("ok"):
      return preview
    predictor = path_predictor_for_asset(loop, asset)
    row = predictor.to_log_row(preview.get("live") or preview)
    tracker = v2_calibration_for_asset(loop, asset)
    if tracker and row.get("event_ticker"):
      tracker.log_open_snapshot(row)
      log.info(
        "%s hourly v2 hour-open snapshot: %s %s",
        asset.upper(),
        row["event_ticker"],
        row.get("primary_signal"),
      )
    return hourly_v2_tab_prediction(loop, asset)
  except Exception as e:
    log.exception("%s hourly v2 hour-open snapshot failed: %s", asset.upper(), e)
    loop.last_error = str(e)
    return None


def run_hourly_v2_late_call_for_asset(
  loop: PredictionLoop,
  asset: str,
  *,
  force: bool = False,
) -> dict[str, Any] | None:
  from datetime import datetime, timezone

  from src.models.hourly_late_call_log import prediction_to_late_call_row

  acfg = v2_cfg_for_asset(loop, asset)
  if acfg is None:
    return None
  try:
    out = hourly_v2_tab_prediction(loop, asset, include_bot=False)
    if not out.get("ok"):
      return out
    live = out.get("live") or out
    event_ticker = (live.get("event") or {}).get("event_ticker")
    if not event_ticker:
      return out
    row = prediction_to_late_call_row(live, logged_at=datetime.now(timezone.utc).isoformat())
    row["asset"] = asset
    tracker = v2_calibration_for_asset(loop, asset)
    if tracker:
      tracker.log_late_call(row, force=force)
    return hourly_v2_tab_prediction(loop, asset)
  except Exception as e:
    log.exception("%s hourly v2 late call failed: %s", asset.upper(), e)
    loop.last_error = str(e)
    return None


def run_hourly_v2_bot_continuous(loop: PredictionLoop, asset: str) -> None:
  from src.backtest.mechanics_profiles import is_hourly_v2_kind

  asset = asset.lower()
  acfg = v2_cfg_for_asset(loop, asset)
  if acfg is None:
    return
  kind = "hourly_v2"
  loop.all_bot_stores()
  store = loop.hourly_bot_store(asset, kind=kind)
  settings = store.get_settings()
  bot_cfg = acfg.get("hourly_v2", {}).get("bot") or {}
  active = settings.enabled and settings.continuous and bot_cfg.get("continuous_enabled", True)
  runtime_cfg = asset_v2_runtime_cfg(acfg)
  tab: dict[str, Any] | None = None
  try:
    if not active:
      if not settings.enabled:
        store.set_last_skip_reason("auto_bet_off")
      return
    tab = loop._cached_tab_if_throttled(loop._hourly_v2_tab_cache, asset)
    if tab is None:
      tab = hourly_v2_tab_prediction(loop, asset, include_bot=False)
      loop._store_tab_cache(loop._hourly_v2_tab_cache, asset, tab)
    if tab.get("ok"):
      loop.hourly_bot(asset, kind=kind).run_continuous_cycle(tab, cfg=runtime_cfg)
  except Exception as e:
    label = "hourly v2" if is_hourly_v2_kind(kind) else kind
    log.exception("%s %s bot continuous cycle failed: %s", asset.upper(), label, e)
  finally:
    store.record_cycle(active=active)
