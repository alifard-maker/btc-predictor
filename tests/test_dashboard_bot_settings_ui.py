"""Tests for dashboard bot toggle sync helpers (mirrors bot_settings_ui.js)."""

from __future__ import annotations

BOT_SETTING_FIELDS = (
  "enabled",
  "mode",
  "allow_strong",
  "allow_actionable",
  "use_accumulated_profit",
  "profit_use_pct",
  "paper_auto_refill",
  "live_auto_refill_hour_budget",
)


def bot_ui_key(kind: str, asset: str) -> str:
  return f"{kind}-{asset}"


def normalize_bot_settings(raw: dict | None, max_key: str) -> dict | None:
  if not raw:
    return None
  return {
    "enabled": bool(raw.get("enabled")),
    "mode": "live" if raw.get("mode") == "live" else "paper",
    max_key: float(raw.get(max_key, 10)),
    "allow_strong": bool(raw.get("allow_strong")),
    "allow_actionable": bool(raw.get("allow_actionable")),
    "use_accumulated_profit": bool(raw.get("use_accumulated_profit", False)),
    "profit_use_pct": float(raw.get("profit_use_pct", 100)),
    "paper_auto_refill": raw.get("paper_auto_refill", True) is not False,
    "live_auto_refill_hour_budget": bool(raw.get("live_auto_refill_hour_budget", False)),
  }


def bot_settings_equal(a: dict | None, b: dict | None, max_key: str) -> bool:
  na = normalize_bot_settings(a, max_key)
  nb = normalize_bot_settings(b, max_key)
  if na is None or nb is None:
    return na is nb
  for field in BOT_SETTING_FIELDS:
    if na[field] != nb[field]:
      return False
  return na[max_key] == nb[max_key]


def should_update_settings_from_server(
  *,
  server: dict,
  dom: dict,
  last_known: dict | None,
  pending,
  patch_confirmed: dict | None = None,
  max_key: str,
) -> bool:
  if pending:
    return False
  srv = normalize_bot_settings(server, max_key)
  if not srv:
    return False
  if not dom:
    return True
  dom_norm = normalize_bot_settings(dom, max_key)
  if dom_norm and srv.get("mode") and dom_norm["mode"] != srv["mode"]:
    return True
  known = normalize_bot_settings(last_known, max_key) if last_known else dom_norm
  if patch_confirmed and patch_confirmed.get("at") and patch_confirmed.get("settings"):
    age = __import__("time").time() * 1000 - patch_confirmed["at"]
    if 0 <= age < 120000:
      conf = normalize_bot_settings(patch_confirmed["settings"], max_key)
      if conf:
        if srv["enabled"] != conf["enabled"]:
          return False
        if srv[max_key] != conf[max_key]:
          return False
        if srv["live_auto_refill_hour_budget"] != conf["live_auto_refill_hour_budget"]:
          return False
        if srv["paper_auto_refill"] != conf["paper_auto_refill"]:
          return False
  if dom_norm and known and dom_norm["enabled"] == known["enabled"] and dom_norm["enabled"] != srv["enabled"]:
    return False
  if (
    dom_norm and known
    and dom_norm["live_auto_refill_hour_budget"] == known["live_auto_refill_hour_budget"]
    and dom_norm["live_auto_refill_hour_budget"] != srv["live_auto_refill_hour_budget"]
  ):
    return False
  if (
    dom_norm and known
    and dom_norm["paper_auto_refill"] == known["paper_auto_refill"]
    and dom_norm["paper_auto_refill"] != srv["paper_auto_refill"]
  ):
    return False
  if dom_norm and srv and dom_norm[max_key] != srv[max_key]:
    return False
  if not known or not bot_settings_equal(srv, known, max_key):
    return True
  return not bot_settings_equal(srv, dom_norm, max_key)


def test_normalize_defaults():
  n = normalize_bot_settings({"enabled": 1, "mode": "live", "allow_strong": 0}, "max_spend_per_hour_usd")
  assert n is not None
  assert n["enabled"] is True
  assert n["mode"] == "live"
  assert n["allow_strong"] is False
  assert n["use_accumulated_profit"] is False
  assert n["profit_use_pct"] == 100.0
  assert n["paper_auto_refill"] is True
  assert n["max_spend_per_hour_usd"] == 10.0


def test_equal_ignores_extra_keys():
  a = {
    "enabled": True,
    "mode": "paper",
    "max_spend_per_hour_usd": 30,
    "allow_strong": True,
    "allow_actionable": False,
  }
  b = {**a, "continuous": False}
  assert bot_settings_equal(a, b, "max_spend_per_hour_usd")


def test_pending_blocks_server_update():
  server = {"enabled": False, "mode": "paper", "max_spend_per_hour_usd": 25}
  dom = {"enabled": True, "mode": "paper", "max_spend_per_hour_usd": 25}
  pending = normalize_bot_settings(dom, "max_spend_per_hour_usd")
  assert not should_update_settings_from_server(
    server=server,
    dom=dom,
    last_known=dom,
    pending=pending,
    max_key="max_spend_per_hour_usd",
  )


def test_stale_poll_does_not_clobber_dom_auto_bet_off():
  server = {"enabled": True, "mode": "paper", "max_spend_per_hour_usd": 25}
  dom = {"enabled": False, "mode": "paper", "max_spend_per_hour_usd": 25}
  last_known = normalize_bot_settings(dom, "max_spend_per_hour_usd")
  assert not should_update_settings_from_server(
    server=server,
    dom=dom,
    last_known=last_known,
    pending=None,
    max_key="max_spend_per_hour_usd",
  )


def test_stale_poll_does_not_clobber_dom_auto_bet_on():
  server = {"enabled": False, "mode": "paper", "max_spend_per_hour_usd": 25}
  dom = {"enabled": True, "mode": "paper", "max_spend_per_hour_usd": 25}
  last_known = normalize_bot_settings(dom, "max_spend_per_hour_usd")
  assert not should_update_settings_from_server(
    server=server,
    dom=dom,
    last_known=last_known,
    pending=None,
    max_key="max_spend_per_hour_usd",
  )


def test_stale_poll_does_not_clobber_dom_cap():
  server = {"enabled": True, "mode": "paper", "max_spend_per_hour_usd": 15}
  dom = {"enabled": True, "mode": "paper", "max_spend_per_hour_usd": 30}
  last_known = normalize_bot_settings({"enabled": True, "mode": "paper", "max_spend_per_hour_usd": 15}, "max_spend_per_hour_usd")
  assert not should_update_settings_from_server(
    server=server,
    dom=dom,
    last_known=last_known,
    pending=None,
    max_key="max_spend_per_hour_usd",
  )


def test_stale_poll_does_not_clobber_dom_auto_refill():
  server = {
    "enabled": True,
    "mode": "live",
    "max_spend_per_hour_usd": 15,
    "live_auto_refill_hour_budget": False,
  }
  dom = {**server, "live_auto_refill_hour_budget": True}
  last_known = normalize_bot_settings(dom, "max_spend_per_hour_usd")
  assert not should_update_settings_from_server(
    server=server,
    dom=dom,
    last_known=last_known,
    pending=None,
    max_key="max_spend_per_hour_usd",
  )


def test_patch_confirmed_cap_blocks_stale_server():
  server = {"enabled": True, "mode": "live", "max_spend_per_hour_usd": 15}
  dom = {"enabled": True, "mode": "live", "max_spend_per_hour_usd": 30}
  last_known = normalize_bot_settings(dom, "max_spend_per_hour_usd")
  patch_confirmed = {
    "at": __import__("time").time() * 1000,
    "settings": dom,
  }
  assert not should_update_settings_from_server(
    server=server,
    dom=dom,
    last_known=last_known,
    pending=None,
    patch_confirmed=patch_confirmed,
    max_key="max_spend_per_hour_usd",
  )
