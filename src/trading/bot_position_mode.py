"""Paper vs live tagging for open bot positions."""

from __future__ import annotations

from typing import Any


def normalize_position_mode(mode: str | None) -> str:
  m = str(mode or "paper").lower()
  return m if m in ("paper", "live") else "paper"


def exposure_by_mode(positions: list[dict[str, Any]]) -> tuple[float, float, float]:
  """Return (paper_usd, live_usd, total_usd) for open legs."""
  paper = live = 0.0
  for pos in positions:
    cost = float(pos.get("cost_usd") or 0)
    if normalize_position_mode(pos.get("mode")) == "live":
      live += cost
    else:
      paper += cost
  return round(paper, 2), round(live, 2), round(paper + live, 2)


def backfill_position_modes(conn: Any) -> None:
  """Infer position mode from matching enter trade rows (one-time migration)."""
  conn.execute(
    """
    UPDATE bot_positions
    SET mode = (
      SELECT t.mode FROM bot_trades t
      WHERE t.position_id = bot_positions.id AND t.action = 'enter'
      ORDER BY t.created_at DESC
      LIMIT 1
    )
    WHERE EXISTS (
      SELECT 1 FROM bot_trades t
      WHERE t.position_id = bot_positions.id AND t.action = 'enter'
    )
    """
  )
