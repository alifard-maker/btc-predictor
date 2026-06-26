# Beta 1.2 — flip-to-opposite

**Tag:** `release/beta-1.2` (after deploy)

## What changed

When an open **LONG** or **SHORT** is losing past the stop and reassessment + tape strongly favor the other side, the monitor can recommend **FLIP SHORT** or **FLIP LONG** (exit open leg, bet opposite).

- **One flip per slot** (`max_flips_per_slot: 1`) — no flip-back
- After flip is logged, only **HOLD / TAKE PROFIT / CUT LOSS** on the flipped side
- Separate **flip calibration stats** on the dashboard (like late entry)

Config: `flip:` in `config.yaml`.

## Revert

`git checkout release/beta-1.1`
