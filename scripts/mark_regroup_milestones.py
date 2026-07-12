#!/usr/bin/env python3
from src.config import load_config
from src.trading.pnl_first_health_watchdog import mark_regroup_milestone

cfg = load_config()
items = [
  ("m1_v3_ranking", {"best": "struct_lb4_pull0.35_ub0.70", "delta_vs_fair": -220.85}),
  ("m2_kalshi_epoch", {"kalshi_pnl": -16.78, "closed": 107}),
  ("m3_kalshi_live_report", {"endpoint": "/api/pnl-first/kalshi-live-report"}),
  ("m4_paper_ab", {"endpoint": "/api/pnl-first/paper-ab"}),
  ("m5_health_watchdog", {"endpoint": "/api/pnl-first/health"}),
  ("m6_deployed", {"version": "Beta 5.0.28"}),
]
for mid, detail in items:
  mark_regroup_milestone(cfg, mid, detail=detail)
  print(mid, "ok")
