# Beta 1.1 — dip-and-recover late entry

**Tag:** `release/beta-1.1`  
**Revert to:** `release/beta-1.0` (strict chop guard)

## What changed vs Beta 1.0

Late entry still blocks when price crosses t=0 **more than once**, **unless** all of:

1. Exactly **2** crossings (3+ still blocked)
2. **|gap vs t=0| ≥ 0.20%**
3. Last **4** one-minute closes are **≥75%** on the favorable side of t=0

Config keys under `late_entry`: `recovery_crossings`, `recovery_min_gap_pct`, `recovery_recent_bars`, `recovery_recent_above_pct`.

## Restore Beta 1.0

```bash
git fetch origin
git checkout release/beta-1.0
# redeploy on Railway from this ref
```
