"""P&L-first Phase 0 pipeline milestone — live gate-stack exercise, not PnL-gated fills."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.trading.pnl_first_gates import pnl_first_active
from src.trading.pnl_first_railway_manager import load_manager_state, save_manager_state

REQUIRED_SESSION_GATES = frozenset({"regime", "edge", "taker", "preflight"})


def _default_pipeline_state() -> dict[str, Any]:
  return {
    "current_hour": None,
    "completed_hours": [],
    "session_gates_seen": [],
    "consecutive_pipeline_hours": 0,
    "streak": [],
    "milestone_achieved_at": None,
  }


def _pipeline_bucket(cfg: dict[str, Any] | None) -> dict[str, Any]:
  state = load_manager_state(cfg)
  pm = state.get("pipeline_milestone")
  if not isinstance(pm, dict):
    pm = _default_pipeline_state()
    state["pipeline_milestone"] = pm
  return pm


def _persist_pipeline(cfg: dict[str, Any] | None, pm: dict[str, Any]) -> None:
  state = load_manager_state(cfg)
  state["pipeline_milestone"] = pm
  save_manager_state(state, cfg)


def classify_skip_reason(reason: str | None, *, entry_filled: bool = False) -> set[str]:
  """Map bot skip / entry outcome to pipeline gate families exercised."""
  if entry_filled:
    return {"cycle", "regime_clear", "edge_clear", "taker", "s1", "entry_fill"}

  gates: set[str] = {"cycle"}
  if not reason:
    gates.add("entry_path")
    return gates

  raw = str(reason)
  low = raw.lower()

  if low.startswith("pnl_first_regime_blocked") or low.startswith("regime_blocked"):
    gates.add("regime")
    return gates

  if low.startswith("ask_edge_too_low") or low.startswith("tail_ask_edge"):
    gates.update({"regime_clear", "edge"})
    return gates

  if low.startswith("pnl_first_s2_blocked"):
    gates.update({"regime_clear", "s1"})
    return gates

  if low.startswith("pnl_first_taker_only") or low.startswith("pnl_first_no_entry_price"):
    gates.update({"regime_clear", "edge_clear", "taker"})
    return gates

  if low.startswith("pnl_first_live_ev"):
    gates.update({"regime_clear", "edge_clear", "taker"})
    return gates

  if "too_late" in low or "too_early" in low or "min_hours_to_settle" in low:
    gates.update({"regime_clear", "time_window"})
    return gates

  if low.startswith("pnl_first_no_s1"):
    gates.update({"regime_clear", "s1"})
    return gates

  # Any later-stage skip implies regime was evaluated and cleared.
  if any(
    low.startswith(p)
    for p in (
      "hour_budget",
      "fully_deployed",
      "no_buy",
      "no_entry",
      "hour_momentum",
      "adaptive_",
      "whipsaw",
      "leg_stop",
      "correlation",
      "tail_entry",
    )
  ):
    gates.add("regime_clear")
    if "ask_edge" in low:
      gates.add("edge")
    return gates

  gates.add("entry_path")
  return gates


def _map_session_gates(flags: set[str]) -> set[str]:
  """Collapse cycle flags into session-level gate coverage."""
  out: set[str] = set()
  if "regime" in flags or "regime_clear" in flags:
    out.add("regime")
  if "edge" in flags or "edge_clear" in flags or "entry_fill" in flags:
    out.add("edge")
  if "taker" in flags or "entry_fill" in flags:
    out.add("taker")
  if "s1" in flags or "entry_fill" in flags:
    out.add("s1")
  if "time_window" in flags:
    out.add("time_window")
  return out


def _new_hour_record(event_ticker: str, *, live: bool) -> dict[str, Any]:
  return {
    "event_ticker": event_ticker,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "cycles": 0,
    "gates_seen": [],
    "live": live,
    "preflight_ok": False,
    "entry_fills": 0,
  }


def record_pipeline_cycle(
  cfg: dict[str, Any] | None,
  *,
  event_ticker: str,
  skip_reason: str | None,
  mode: str,
  kind: str,
  asset: str,
  entry_filled: bool = False,
) -> None:
  """Record one BTC hourly live cycle for pipeline milestone tracking."""
  if asset != "btc" or kind != "hourly" or str(mode).lower() != "live":
    return
  if not pnl_first_active(cfg, kind=kind, mode=mode):
    return

  pm = _pipeline_bucket(cfg)
  current = pm.get("current_hour")
  if not isinstance(current, dict) or current.get("event_ticker") != event_ticker:
    if isinstance(current, dict) and current.get("event_ticker"):
      _finalize_current_hour(cfg, pm, preflight_ok=bool(current.get("preflight_ok")))
    session_gates = set(pm.get("session_gates_seen") or [])
    hour = _new_hour_record(event_ticker, live=True)
    pm["current_hour"] = hour

  current = pm["current_hour"]
  current["cycles"] = int(current.get("cycles") or 0) + 1
  flags = classify_skip_reason(skip_reason, entry_filled=entry_filled)
  if entry_filled:
    current["entry_fills"] = int(current.get("entry_fills") or 0) + 1

  hour_gates = set(current.get("gates_seen") or [])
  hour_gates |= flags
  current["gates_seen"] = sorted(hour_gates)

  session_gates = set(pm.get("session_gates_seen") or [])
  session_gates |= _map_session_gates(flags)
  pm["session_gates_seen"] = sorted(session_gates)

  _persist_pipeline(cfg, pm)


def note_pipeline_preflight(cfg: dict[str, Any] | None, *, ok: bool) -> None:
  """Manager preflight tick — counts toward session gate coverage."""
  pm = _pipeline_bucket(cfg)
  if ok:
    session_gates = set(pm.get("session_gates_seen") or [])
    session_gates.add("preflight")
    pm["session_gates_seen"] = sorted(session_gates)
  current = pm.get("current_hour")
  if isinstance(current, dict) and ok:
    current["preflight_ok"] = True
  _persist_pipeline(cfg, pm)


def finalize_pipeline_hour(
  cfg: dict[str, Any] | None,
  event_ticker: str,
  *,
  live: bool,
) -> None:
  """Finalize a completed hourly event (hour rollover)."""
  pm = _pipeline_bucket(cfg)
  current = pm.get("current_hour")
  if not isinstance(current, dict) or current.get("event_ticker") != event_ticker:
    return
  _finalize_current_hour(cfg, pm, preflight_ok=bool(current.get("preflight_ok")), live=live)


def sync_pipeline_hour_boundary(cfg: dict[str, Any] | None, event_ticker: str) -> None:
  """Finalize the open hour when Kalshi rolls to a new event ticker."""
  pm = _pipeline_bucket(cfg)
  current = pm.get("current_hour")
  if not isinstance(current, dict):
    return
  prev = str(current.get("event_ticker") or "")
  if prev and prev != event_ticker:
    _finalize_current_hour(cfg, pm, preflight_ok=bool(current.get("preflight_ok")))


def _hour_pipeline_ok(hour: dict[str, Any], *, live: bool) -> bool:
  if not live:
    return False
  if int(hour.get("cycles") or 0) < 1:
    return False
  if not hour.get("preflight_ok"):
    return False
  return True


def _finalize_current_hour(
  cfg: dict[str, Any] | None,
  pm: dict[str, Any],
  *,
  preflight_ok: bool,
  live: bool | None = None,
) -> None:
  current = pm.get("current_hour")
  if not isinstance(current, dict):
    return

  if live is None:
    live = bool(current.get("live"))
  current["preflight_ok"] = preflight_ok or bool(current.get("preflight_ok"))
  current["ended_at"] = datetime.now(timezone.utc).isoformat()
  ok = _hour_pipeline_ok(current, live=live)

  completed = list(pm.get("completed_hours") or [])
  completed.append({
    "event": current.get("event_ticker"),
    "ok": ok,
    "cycles": int(current.get("cycles") or 0),
    "gates_seen": list(current.get("gates_seen") or []),
    "entry_fills": int(current.get("entry_fills") or 0),
    "started_at": current.get("started_at"),
    "ended_at": current.get("ended_at"),
  })
  pm["completed_hours"] = completed[-200:]

  streak = int(pm.get("consecutive_pipeline_hours") or 0)
  streak_rows = list(pm.get("streak") or [])
  if ok:
    streak += 1
    streak_rows.append({
      "event": current.get("event_ticker"),
      "cycles": int(current.get("cycles") or 0),
      "gates_seen": list(current.get("gates_seen") or []),
      "entry_fills": int(current.get("entry_fills") or 0),
    })
  else:
    streak = 0
    streak_rows = []

  pm["consecutive_pipeline_hours"] = streak
  pm["streak"] = streak_rows[-target_pipeline_hours(cfg):]
  pm["current_hour"] = None
  _persist_pipeline(cfg, pm)


def target_pipeline_hours(cfg: dict[str, Any] | None) -> int:
  pf = dict((cfg or {}).get("pnl_first") or {})
  return int(pf.get("milestone_pipeline_hours") or pf.get("milestone_positive_hours", 20))


def reset_pipeline_milestone(cfg: dict[str, Any] | None = None, *, reason: str = "manual_reset") -> dict[str, Any]:
  """Clear pipeline hour streak — use when restarting Phase 0–1 on a new model profile."""
  pm = _default_pipeline_state()
  pm["reset_reason"] = reason
  pm["reset_at"] = datetime.now(timezone.utc).isoformat()
  _persist_pipeline(cfg, pm)
  return compute_pipeline_milestone(cfg)


def compute_pipeline_milestone(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
  """Evaluate pipeline milestone from persisted manager state."""
  pm = _pipeline_bucket(cfg)
  target = target_pipeline_hours(cfg)
  streak = int(pm.get("consecutive_pipeline_hours") or 0)
  session_gates = set(pm.get("session_gates_seen") or [])
  missing = sorted(REQUIRED_SESSION_GATES - session_gates)
  coverage_ok = not missing
  achieved = streak >= target and coverage_ok

  if achieved and not pm.get("milestone_achieved_at"):
    pm["milestone_achieved_at"] = datetime.now(timezone.utc).isoformat()
    _persist_pipeline(cfg, pm)

  current = pm.get("current_hour") if isinstance(pm.get("current_hour"), dict) else None

  return {
    "ts": datetime.now(timezone.utc).isoformat(),
    "milestone_mode": "pipeline",
    "target_pipeline_hours": target,
    "consecutive_pipeline_hours": streak,
    "milestone_achieved": achieved,
    "session_gate_coverage": sorted(session_gates),
    "required_session_gates": sorted(REQUIRED_SESSION_GATES),
    "missing_session_gates": missing,
    "session_gate_coverage_ok": coverage_ok,
    "streak": list(pm.get("streak") or [])[:target],
    "completed_pipeline_hours": len(pm.get("completed_hours") or []),
    "current_hour": current,
    # Legacy aliases for dashboard/health
    "target_positive_hours": target,
    "consecutive_positive_hours": streak,
  }
