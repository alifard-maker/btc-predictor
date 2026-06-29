"""Backup bot trade logs and calibration files — paper and live kept separate.

Live-mode exports are append-audited for future tax filings.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
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


def bot_db_specs(cfg: dict[str, Any]) -> list[tuple[str, str, Path]]:
  """(asset, kind, db_path) for all bot stores."""
  data_dir = Path(cfg["paths"]["logs"]).parent
  specs: list[tuple[str, str, Path]] = []
  for asset in ("btc", "eth"):
    logs = data_dir / "logs" if asset == "btc" else data_dir / asset / "logs"
    specs.append((asset, "hourly", logs / f"hourly_bot_{asset}.db"))
    specs.append((asset, "slot15", logs / f"slot15_bot_{asset}.db"))
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
  for asset in ("btc", "eth"):
    logs = data_dir / "logs" if asset == "btc" else data_dir / asset / "logs"
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


def _export_mode_trades(
  cfg: dict[str, Any],
  *,
  mode: str,
  dest: Path,
) -> dict[str, Any]:
  """Refresh per-bot CSVs and consolidated CSV for one mode."""
  all_rows: list[dict[str, Any]] = []
  per_bot: dict[str, int] = {}
  for asset, kind, db_path in bot_db_specs(cfg):
    rows = _rows_from_db(db_path, mode)
    label = f"{asset}_{kind}"
    per_bot[label] = len(rows)
    if rows:
      _write_csv(rows, dest / label / "trades.csv")
      all_rows.extend(rows)
  if all_rows:
    all_rows.sort(key=lambda r: str(r.get("created_at") or ""))
    _write_csv(all_rows, dest / "all_trades.csv")
  return {"mode": mode, "total_trades": len(all_rows), "per_bot": per_bot}


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


def run_full_backup(cfg: dict[str, Any], *, reason: str = "scheduled") -> dict[str, Any]:
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
      live_stats = _export_mode_trades(cfg, mode="live", dest=live_dir)

      full_dir = snap_dir / "full"
      for asset, kind, db_path in bot_db_specs(cfg):
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
      _export_mode_trades(cfg, mode="live", dest=mode_dir)
      live_manifest = mode_dir / "manifest.json"
      live_manifest.write_text(
        json.dumps(
          {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reason": "live_trade",
            "last_trade_id": dedupe,
            "scope": "live",
            "note": "Tax-relevant live trade log — do not delete",
          },
          indent=2,
        ),
        encoding="utf-8",
      )
  except Exception as e:
    log.warning("Trade audit backup skipped: %s", e)


def last_backup_status() -> dict[str, Any]:
  return dict(_LAST_RUN)


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
  snap_root = root / "snapshots"
  out["snapshot_count"] = len(list(snap_root.iterdir())) if snap_root.exists() else 0
  return out
