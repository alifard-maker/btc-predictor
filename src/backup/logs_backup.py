"""Backup bot trade logs and calibration files — paper and live kept separate.

Live-mode exports are append-audited for future tax filings.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_BACKUP_LOCK = threading.Lock()
_LAST_RUN: dict[str, Any] = {}

TRADE_COLUMNS = [
  "id",
  "event_ticker",
  "trigger",
  "action",
  "mode",
  "market_ticker",
  "side",
  "contracts",
  "price_cents",
  "entry_price_cents",
  "exit_price_cents",
  "cost_usd",
  "pnl_usd",
  "signal",
  "label",
  "actionable_headline",
  "status",
  "detail",
  "kalshi_order_id",
  "position_id",
  "entry_bid_cents",
  "entry_ask_cents",
  "entry_spread_cents",
  "entry_settings_json",
  "created_at",
]


def backup_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
  raw = (cfg or {}).get("log_backup") or {}
  data_dir = Path((cfg or {}).get("paths", {}).get("logs", "data/logs")).parent
  default_root = data_dir / "backups"
  backup_dir = raw.get("backup_dir") or os.getenv("BACKUP_DIR", "")
  root = Path(backup_dir) if backup_dir else default_root
  return {
    "enabled": bool(raw.get("enabled", True)),
    "interval_minutes": int(raw.get("interval_minutes", 15)),
    "keep_snapshot_days": int(raw.get("keep_snapshot_days", 90)),
    "root": root,
    "data_dir": data_dir,
  }


def volume_is_persistent(data_dir: str | Path | None = None) -> bool:
  """True when Railway has a volume mounted at /data (not just DATA_DIR=/data in env)."""
  mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
  if mount == "/data":
    return True
  # Legacy marker only trusted when Railway volume env is also present in some form
  if os.getenv("RAILWAY_VOLUME_NAME") or os.getenv("RAILWAY_VOLUME_ID"):
    return True
  return False


def touch_persistent_marker(data_dir: str | Path) -> None:
  """Write marker once a volume-backed data dir is in use."""
  for base in (Path("/data"), Path(data_dir)):
    try:
      base.mkdir(parents=True, exist_ok=True)
      (base / ".persistent_volume").write_text(
        datetime.now(timezone.utc).isoformat(), encoding="utf-8"
      )
    except OSError:
      continue


HOURLY_BOT_KINDS_BY_ASSET: dict[str, tuple[str, ...]] = {
  "btc": (
    "hourly",
    "hourly_trial",
    "hourly_trial_rally",
    "hourly_trial_soft",
    "hourly_trial_mech",
    "hourly_v2",
  ),
  "eth": ("hourly", "hourly_trial", "hourly_v2"),
  "spx": ("hourly", "hourly_trial"),
  "ndx": ("hourly", "hourly_trial"),
}
SLOT15_ASSETS = ("btc", "eth")
BACKUP_ASSETS = tuple(HOURLY_BOT_KINDS_BY_ASSET.keys())
SLOT15_TRIAL_KIND = "slot15_trial"
HUMAN_TAX_ASSETS = ("btc", "eth")


def human_tax_bot_label(asset: str) -> str:
  return f"{asset.lower()}_hourly_human"


def human_trade_db_specs(cfg: dict[str, Any]) -> list[tuple[str, Path]]:
  """(asset, db_path) for dashboard manual hourly trade stores."""
  data_dir = Path(cfg["paths"]["logs"]).parent
  specs: list[tuple[str, Path]] = []
  for asset in HUMAN_TAX_ASSETS:
    logs = _asset_logs_dir(data_dir, asset)
    specs.append((asset, logs / f"human_trades_{asset}.db"))
  return specs


HUMAN_TRADE_COLUMNS = [
  "id",
  "event_ticker",
  "action",
  "mode",
  "market_ticker",
  "side",
  "contracts",
  "price_cents",
  "entry_price_cents",
  "exit_price_cents",
  "cost_usd",
  "pnl_usd",
  "signal",
  "label",
  "status",
  "detail",
  "kalshi_order_id",
  "position_id",
  "entry_bid_cents",
  "entry_ask_cents",
  "entry_spread_cents",
  "entry_context_json",
  "created_at",
]


def _asset_logs_dir(data_dir: Path, asset: str) -> Path:
  if asset == "btc":
    return data_dir / "logs"
  return data_dir / asset / "logs"


def _hourly_bot_db_filename(kind: str, asset: str) -> str:
  if kind == "hourly_trial":
    return f"hourly_trial_bot_{asset}.db"
  if kind == "hourly_trial_rally":
    return f"hourly_trial_rally_bot_{asset}.db"
  if kind == "hourly_trial_soft":
    return f"hourly_trial_soft_bot_{asset}.db"
  if kind == "hourly_trial_mech":
    return f"hourly_trial_mech_bot_{asset}.db"
  if kind == "hourly_v2":
    return f"hourly_v2_bot_{asset}.db"
  return f"hourly_bot_{asset}.db"


def bot_db_specs(cfg: dict[str, Any]) -> list[tuple[str, str, Path]]:
  """(asset, kind, db_path) for every bot store — live tax exports include all bots."""
  data_dir = Path(cfg["paths"]["logs"]).parent
  specs: list[tuple[str, str, Path]] = []
  for asset, kinds in HOURLY_BOT_KINDS_BY_ASSET.items():
    logs = _asset_logs_dir(data_dir, asset)
    for kind in kinds:
      specs.append((asset, kind, logs / _hourly_bot_db_filename(kind, asset)))
  for asset in SLOT15_ASSETS:
    logs = _asset_logs_dir(data_dir, asset)
    specs.append((asset, "slot15", logs / f"slot15_bot_{asset}.db"))
    specs.append((asset, SLOT15_TRIAL_KIND, logs / f"slot15_trial_bot_{asset}.db"))
  return specs


def calibration_file_specs(cfg: dict[str, Any]) -> list[tuple[str, Path]]:
  """(label, path) for calibration / prediction logs under each asset scope."""
  data_dir = Path(cfg["paths"]["logs"]).parent
  names = (
    "predictions.db",
    "predictions.jsonl",
    "postmortems.jsonl",
    "stats_archive.json",
    "stats_epoch.json",
    "hourly_predictions.db",
    "hourly_stats_archive.json",
    "hourly_stats_epoch.json",
  )
  out: list[tuple[str, Path]] = []
  for asset in BACKUP_ASSETS:
    logs = _asset_logs_dir(data_dir, asset)
    prefix = f"{asset}/"
    for name in names:
      out.append((prefix + name, logs / name))
  return out


def _sqlite_backup(src: Path, dst: Path) -> None:
  dst.parent.mkdir(parents=True, exist_ok=True)
  if not src.exists():
    return
  src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
  try:
    dst_conn = sqlite3.connect(dst)
    try:
      src_conn.backup(dst_conn)
    finally:
      dst_conn.close()
  finally:
    src_conn.close()


def _file_sha256(path: Path) -> str | None:
  if not path.exists() or not path.is_file():
    return None
  h = hashlib.sha256()
  with open(path, "rb") as f:
    for chunk in iter(lambda: f.read(65536), b""):
      h.update(chunk)
  return h.hexdigest()


def _rows_from_human_db(db_path: Path, mode: str) -> list[dict[str, Any]]:
  if not db_path.exists():
    return []
  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  conn.row_factory = sqlite3.Row
  try:
    rows = conn.execute(
      "SELECT * FROM human_trades WHERE mode = ? ORDER BY created_at ASC",
      (mode,),
    ).fetchall()
    return [dict(r) for r in rows]
  except sqlite3.Error:
    return []
  finally:
    conn.close()


def _human_trade_counts(db_path: Path) -> dict[str, int]:
  if not db_path.exists():
    return {}
  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  try:
    rows = conn.execute(
      "SELECT mode, COUNT(*) FROM human_trades GROUP BY mode",
    ).fetchall()
    return {str(m): int(c) for m, c in rows}
  except sqlite3.Error:
    return {}
  finally:
    conn.close()


def _write_human_csv(rows: list[dict[str, Any]], path: Path) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with open(path, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=HUMAN_TRADE_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for row in rows:
      w.writerow({k: row.get(k) for k in HUMAN_TRADE_COLUMNS})


def _export_human_log_trades(
  cfg: dict[str, Any],
  *,
  mode: str,
  dest: Path,
  csv_name: str = "human_log_trades.csv",
  all_csv_name: str | None = None,
) -> dict[str, Any]:
  """Export human manual trade SQLite logs (strategy ledger; live taxes use trades.csv)."""
  all_rows: list[dict[str, Any]] = []
  per_bot: dict[str, int] = {}
  for asset, db_path in human_trade_db_specs(cfg):
    rows = _rows_from_human_db(db_path, mode)
    label = human_tax_bot_label(asset)
    per_bot[label] = len(rows)
    if rows:
      _write_human_csv(rows, dest / label / csv_name)
      all_rows.extend(rows)
  if all_rows:
    all_rows.sort(key=lambda r: str(r.get("created_at") or ""))
    _write_human_csv(all_rows, dest / (all_csv_name or "all_human_log_trades.csv"))
  return {"mode": mode, "total_trades": len(all_rows), "per_bot": per_bot, "source": "human_log"}


def _rows_from_db(db_path: Path, mode: str) -> list[dict[str, Any]]:
  if not db_path.exists():
    return []
  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  conn.row_factory = sqlite3.Row
  try:
    rows = conn.execute(
      "SELECT * FROM bot_trades WHERE mode = ? ORDER BY created_at ASC",
      (mode,),
    ).fetchall()
    return [dict(r) for r in rows]
  except sqlite3.Error:
    return []
  finally:
    conn.close()


def _trade_counts(db_path: Path) -> dict[str, int]:
  if not db_path.exists():
    return {}
  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  try:
    rows = conn.execute(
      "SELECT mode, COUNT(*) FROM bot_trades GROUP BY mode",
    ).fetchall()
    return {str(m): int(c) for m, c in rows}
  except sqlite3.Error:
    return {}
  finally:
    conn.close()


def _write_csv(rows: list[dict[str, Any]], path: Path) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with open(path, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=TRADE_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for row in rows:
      w.writerow({k: row.get(k) for k in TRADE_COLUMNS})


def _append_audit_jsonl(
  path: Path,
  record: dict[str, Any],
  *,
  dedupe_key: str | None = None,
) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  if dedupe_key and path.exists():
    try:
      with open(path, encoding="utf-8") as f:
        for line in f:
          if not line.strip():
            continue
          try:
            prev = json.loads(line)
            if prev.get("dedupe_key") == dedupe_key:
              return
          except json.JSONDecodeError:
            continue
    except OSError:
      pass
  with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(record, default=str) + "\n")


def _optional_kalshi_client(cfg: dict[str, Any] | None = None) -> Any | None:
  from src.backup.kalshi_tax_export import kalshi_client_for_backup

  return kalshi_client_for_backup(cfg)


def _export_bot_log_trades(
  cfg: dict[str, Any],
  *,
  mode: str,
  dest: Path,
  csv_name: str = "trades.csv",
  all_csv_name: str | None = None,
) -> dict[str, Any]:
  """Export bot SQLite trade log CSVs for one mode."""
  all_rows: list[dict[str, Any]] = []
  per_bot: dict[str, int] = {}
  for asset, kind, db_path in bot_db_specs(cfg):
    rows = _rows_from_db(db_path, mode)
    label = f"{asset}_{kind}"
    per_bot[label] = len(rows)
    if rows:
      _write_csv(rows, dest / label / csv_name)
      all_rows.extend(rows)
  if all_rows:
    all_rows.sort(key=lambda r: str(r.get("created_at") or ""))
    _write_csv(all_rows, dest / (all_csv_name or "all_trades.csv"))
  return {"mode": mode, "total_trades": len(all_rows), "per_bot": per_bot, "source": "bot_log"}


def _export_mode_trades(
  cfg: dict[str, Any],
  *,
  mode: str,
  dest: Path,
  kalshi: Any | None = None,
) -> dict[str, Any]:
  """Refresh per-bot CSVs and consolidated CSV for one mode."""
  bot_log_stats = _export_bot_log_trades(
    cfg,
    mode=mode,
    dest=dest,
    csv_name="bot_log_trades.csv",
    all_csv_name="all_bot_log_trades.csv",
  )
  # Paper + live: persist human SQLite ledger as CSV for later adopt/analyze.
  human_log_stats = _export_human_log_trades(
    cfg,
    mode=mode,
    dest=dest,
    csv_name="human_log_trades.csv",
    all_csv_name="all_human_log_trades.csv",
  )
  if mode == "live":
    from src.backup.kalshi_tax_export import export_kalshi_wallet_live_trades, write_tax_readme

    write_tax_readme(dest)
    kalshi_stats = export_kalshi_wallet_live_trades(
      cfg,
      kalshi or _optional_kalshi_client(cfg),
      dest,
    )
    return {**kalshi_stats, "bot_log": bot_log_stats, "human_log": human_log_stats}

  return {"bot_log": bot_log_stats, "human_log": human_log_stats}


def _copy_calibration_files(cfg: dict[str, Any], dest: Path) -> list[str]:
  copied: list[str] = []
  for label, src in calibration_file_specs(cfg):
    if not src.exists():
      continue
    dst = dest / label
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    copied.append(label)
  return copied


def _prune_snapshots(root: Path, keep_days: int) -> int:
  snap_root = root / "snapshots"
  if not snap_root.exists() or keep_days <= 0:
    return 0
  cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
  removed = 0
  for child in snap_root.iterdir():
    if not child.is_dir():
      continue
    try:
      ts = datetime.strptime(child.name, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
      continue
    if ts < cutoff:
      shutil.rmtree(child, ignore_errors=True)
      removed += 1
  return removed


def run_full_backup(cfg: dict[str, Any], *, reason: str = "scheduled", kalshi: Any | None = None) -> dict[str, Any]:
  """Full backup: sqlite snapshots + paper/live CSV exports + calibration files."""
  bcfg = backup_cfg(cfg)
  if not bcfg["enabled"]:
    return {"ok": False, "reason": "backup_disabled"}

  root: Path = bcfg["root"]
  data_dir = bcfg["data_dir"]
  stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
  snap_dir = root / "snapshots" / stamp

  with _BACKUP_LOCK:
    try:
      touch_persistent_marker(data_dir)

      paper_dir = root / "paper"
      live_dir = root / "live"
      paper_stats = _export_mode_trades(cfg, mode="paper", dest=paper_dir)
      live_stats = _export_mode_trades(cfg, mode="live", dest=live_dir, kalshi=kalshi or _optional_kalshi_client(cfg))

      full_dir = snap_dir / "full"
      for asset, kind, db_path in bot_db_specs(cfg):
        if db_path.exists():
          rel = f"{asset}/logs/{db_path.name}"
          _sqlite_backup(db_path, full_dir / rel)
      for asset, db_path in human_trade_db_specs(cfg):
        if db_path.exists():
          rel = f"{asset}/logs/{db_path.name}"
          _sqlite_backup(db_path, full_dir / rel)

      cal_dest = snap_dir / "calibration"
      cal_files = _copy_calibration_files(cfg, cal_dest)
      _copy_calibration_files(cfg, root / "calibration")

      for mode_name, mode_dir in (("paper", paper_dir), ("live", live_dir)):
        mode_snap = snap_dir / mode_name
        if mode_dir.exists():
          shutil.copytree(mode_dir, mode_snap, dirs_exist_ok=True)

      bot_counts: dict[str, dict[str, int]] = {}
      for asset, kind, db_path in bot_db_specs(cfg):
        bot_counts[f"{asset}_{kind}"] = _trade_counts(db_path)
      for asset, db_path in human_trade_db_specs(cfg):
        bot_counts[human_tax_bot_label(asset)] = _human_trade_counts(db_path)

      manifest = {
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "data_dir": str(data_dir),
        "backup_root": str(root),
        "volume_persistent": volume_is_persistent(data_dir),
        "paper": paper_stats,
        "live": live_stats,
        "bot_trade_counts": bot_counts,
        "calibration_files": cal_files,
        "snapshot_dir": str(snap_dir),
      }
      for name, payload in (
        ("manifest.json", manifest),
        (f"paper/manifest.json", {**manifest, "scope": "paper", **paper_stats}),
        (f"live/manifest.json", {**manifest, "scope": "live", **live_stats}),
        (f"snapshots/{stamp}/manifest.json", manifest),
      ):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")

      pruned = _prune_snapshots(root, bcfg["keep_snapshot_days"])
      manifest["snapshots_pruned"] = pruned

      _LAST_RUN.clear()
      _LAST_RUN.update({"ok": True, **manifest})
      log.info(
        "Log backup (%s): paper=%s live=%s → %s",
        reason,
        paper_stats.get("total_trades"),
        live_stats.get("total_trades"),
        root,
      )
      return manifest
    except Exception as e:
      log.exception("Log backup failed: %s", e)
      err = {"ok": False, "reason": str(e), "at": datetime.now(timezone.utc).isoformat()}
      _LAST_RUN.clear()
      _LAST_RUN.update(err)
      return err


def on_settings_saved(
  cfg: dict[str, Any] | None,
  *,
  asset: str,
  bot_type: str,
  old_settings: dict[str, Any],
  new_settings: dict[str, Any],
  source: str = "internal",
) -> None:
  """Append settings change to mode-specific audit log (paper vs live)."""
  if not cfg or old_settings == new_settings:
    return
  bcfg = backup_cfg(cfg)
  if not bcfg["enabled"]:
    return
  mode = str(new_settings.get("mode") or old_settings.get("mode") or "paper")
  mode_dir = bcfg["root"] / ("live" if mode == "live" else "paper")
  audit_path = mode_dir / "settings_audit.jsonl"
  ts = datetime.now(timezone.utc).isoformat()
  dedupe = f"{asset}:{bot_type}:{ts}:{hash(json.dumps(new_settings, sort_keys=True, default=str))}"
  record = {
    "dedupe_key": dedupe,
    "timestamp": ts,
    "asset": asset,
    "bot_type": bot_type,
    "source": source,
    "old_settings": old_settings,
    "new_settings": new_settings,
  }
  try:
    _append_audit_jsonl(audit_path, record, dedupe_key=None)
  except Exception as e:
    log.warning("Settings audit backup skipped: %s", e)


def on_trade_logged(
  cfg: dict[str, Any] | None,
  *,
  kind: str,
  asset: str,
  trade: dict[str, Any],
) -> None:
  """Append trade to mode-specific audit log; live trades also trigger quick export."""
  if not cfg:
    return
  bcfg = backup_cfg(cfg)
  if not bcfg["enabled"]:
    return

  mode = str(trade.get("mode") or "paper")
  root = bcfg["root"]
  mode_dir = root / ("live" if mode == "live" else "paper")
  audit_path = mode_dir / "audit_trades.jsonl"
  dedupe = str(trade.get("id") or "")
  record = {
    "dedupe_key": dedupe,
    "logged_at": datetime.now(timezone.utc).isoformat(),
    "bot_kind": kind,
    "asset": asset,
    "trade": {k: trade.get(k) for k in TRADE_COLUMNS},
  }
  try:
    _append_audit_jsonl(audit_path, record, dedupe_key=dedupe or None)
    if mode == "live":
      stats = _export_mode_trades(cfg, mode="live", dest=mode_dir, kalshi=_optional_kalshi_client(cfg))
      live_manifest = mode_dir / "manifest.json"
      live_manifest.write_text(
        json.dumps(
          {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reason": "live_trade",
            "last_trade_id": dedupe,
            "scope": "live",
            "pnl_source": "kalshi_wallet",
            "tax_export": stats,
            "note": "Per-bot trades.csv in subfolders — see TAX_README.txt",
          },
          indent=2,
        ),
        encoding="utf-8",
      )
  except Exception as e:
    log.warning("Trade audit backup skipped: %s", e)


def last_backup_status() -> dict[str, Any]:
  return dict(_LAST_RUN)


def tax_export_status(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
  """Summarize Kalshi-wallet tax CSVs on disk (for admin status + sync UI)."""
  bcfg = backup_cfg(cfg or {})
  live_dir = bcfg["root"] / "live"
  full_cfg = cfg or {"paths": {"logs": str(bcfg["data_dir"] / "logs")}}
  if "paths" not in full_cfg:
    full_cfg = {**full_cfg, "paths": {"logs": str(bcfg["data_dir"] / "logs")}}
  per_bot: dict[str, Any] = {}
  if live_dir.is_dir():
    for asset, kind, _db_path in bot_db_specs(full_cfg):
      label = f"{asset}_{kind}"
      tax_csv = live_dir / label / "trades.csv"
      bot_log = live_dir / label / "bot_log_trades.csv"
      row_count = 0
      if tax_csv.exists():
        try:
          with open(tax_csv, encoding="utf-8") as f:
            row_count = max(0, sum(1 for _ in f) - 1)
        except OSError:
          pass
      per_bot[label] = {
        "trades_csv": tax_csv.exists(),
        "bot_log_csv": bot_log.exists(),
        "kalshi_wallet_rows": row_count,
      }
    for asset, _db_path in human_trade_db_specs(full_cfg):
      label = human_tax_bot_label(asset)
      tax_csv = live_dir / label / "trades.csv"
      human_log = live_dir / label / "human_log_trades.csv"
      row_count = 0
      if tax_csv.exists():
        try:
          with open(tax_csv, encoding="utf-8") as f:
            row_count = max(0, sum(1 for _ in f) - 1)
        except OSError:
          pass
      per_bot[label] = {
        "trades_csv": tax_csv.exists(),
        "human_log_csv": human_log.exists(),
        "kalshi_wallet_rows": row_count,
        "actor": "dashboard_manual",
      }
    other = live_dir / "kalshi_other" / "trades.csv"
    other_rows = 0
    if other.exists():
      try:
        with open(other, encoding="utf-8") as f:
          other_rows = max(0, sum(1 for _ in f) - 1)
      except OSError:
        pass
    per_bot["kalshi_other"] = {
      "trades_csv": other.exists(),
      "kalshi_wallet_rows": other_rows,
    }
  tax_manifest = live_dir / "tax_manifest.json"
  manifest_payload: dict[str, Any] | None = None
  if tax_manifest.exists():
    try:
      manifest_payload = json.loads(tax_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
      manifest_payload = None
  return {
    "backup_root": str(live_dir.parent),
    "live_dir": str(live_dir),
    "tax_readme": (live_dir / "TAX_README.txt").exists(),
    "tax_manifest": manifest_payload,
    "all_trades_csv": (live_dir / "all_trades.csv").exists(),
    "per_bot": per_bot,
    "pnl_source": "kalshi_wallet",
  }


def build_backup_archive(cfg: dict[str, Any] | None, mode: str) -> bytes:
  """Zip paper/ or live/ backup tree for download to local Mac."""
  if mode not in ("paper", "live"):
    raise ValueError("mode must be paper or live")
  bcfg = backup_cfg(cfg or {})
  mode_dir = bcfg["root"] / mode
  if not mode_dir.is_dir():
    raise FileNotFoundError(f"Backup directory missing: {mode_dir}")
  buf = io.BytesIO()
  with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for path in sorted(mode_dir.rglob("*")):
      if not path.is_file():
        continue
      arcname = str(path.relative_to(bcfg["root"]))
      zf.write(path, arcname=arcname)
  return buf.getvalue()


def backup_summary(cfg: dict[str, Any]) -> dict[str, Any]:
  bcfg = backup_cfg(cfg)
  root = bcfg["root"]
  out: dict[str, Any] = {
    "enabled": bcfg["enabled"],
    "backup_root": str(root),
    "volume_persistent": volume_is_persistent(bcfg["data_dir"]),
    "last_run": last_backup_status(),
  }
  for mode in ("paper", "live"):
    mode_dir = root / mode
    audit = mode_dir / "audit_trades.jsonl"
    all_csv = mode_dir / "all_trades.csv"
    out[mode] = {
      "audit_lines": sum(1 for _ in open(audit, encoding="utf-8")) if audit.exists() else 0,
      "all_trades_csv": all_csv.exists(),
      "manifest": (mode_dir / "manifest.json").exists(),
    }
  if (root / "live").is_dir():
    out["tax_export"] = tax_export_status(cfg)
  snap_root = root / "snapshots"
  out["snapshot_count"] = len(list(snap_root.iterdir())) if snap_root.exists() else 0
  return out
