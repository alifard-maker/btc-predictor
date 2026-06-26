from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path | None = None) -> dict[str, Any]:
  config_path = path or PROJECT_ROOT / "config.yaml"
  with open(config_path) as f:
    cfg = yaml.safe_load(f)

  # Railway / cloud: persistent data dir (mount a volume at /data)
  data_dir = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))

  default_paths = {
    "candles": data_dir / "candles",
    "models": data_dir / "models",
    "logs": data_dir / "logs",
    "db": data_dir / "logs" / "predictions.db",
  }
  for key, default in default_paths.items():
    cfg.setdefault("paths", {})
    cfg["paths"][key] = os.getenv(f"PATH_{key.upper()}", str(default))

  # Env overrides for trading config
  if symbol := os.getenv("SYMBOL"):
    cfg["symbol"] = symbol
  if exchange := os.getenv("EXCHANGE"):
    cfg["exchange"] = exchange
  if os.getenv("EXCHANGE_FALLBACKS"):
    cfg["exchange_fallbacks"] = os.getenv("EXCHANGE_FALLBACKS", "").split(",")

  if db_url := os.getenv("DATABASE_URL"):
    cfg["database_url"] = db_url

  cfg["admin_api_key"] = os.getenv("ADMIN_API_KEY", "")
  cfg["enable_scheduler"] = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"

  if os.getenv("TIMEZONE"):
    cfg["timezone"] = os.getenv("TIMEZONE")

  if live_feed := os.getenv("PRICE_FEED_LIVE"):
    cfg.setdefault("price_feed", {})
    cfg["price_feed"]["live"] = live_feed

  cfg["app_password"] = os.getenv("APP_PASSWORD", cfg.get("app_password") or "")

  from src.data.kalshi import load_kalshi_config
  cfg["kalshi"] = load_kalshi_config(cfg)

  return cfg


def ensure_dirs(cfg: dict[str, Any]) -> None:
  for p in cfg.get("paths", {}).values():
    Path(p).parent.mkdir(parents=True, exist_ok=True)
  candles_base = Path(cfg["paths"]["candles"])
  candles_base.mkdir(parents=True, exist_ok=True)
  for interval in cfg.get("intervals", []):
    (candles_base / interval).mkdir(parents=True, exist_ok=True)
  Path(cfg["paths"]["models"]).mkdir(parents=True, exist_ok=True)
  Path(cfg["paths"]["logs"]).mkdir(parents=True, exist_ok=True)
