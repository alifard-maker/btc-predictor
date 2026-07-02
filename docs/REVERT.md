# Reverting to a known-good release

## Backup tag (ETH hourly live + asset-scoped Kalshi sync)

| Item | Value |
|------|-------|
| Tag | `backup/2026-07-02-eth-hourly-live` |
| Commit | `f2d3af6` |
| Version | Beta 4.0.15 |
| Notes | ETH hourly live; Kalshi fill sync scoped by asset (no BTC→ETH contamination); foreign-asset phantom purge; Railway volume resizing to 20GB |

## Backup tag (post disk-resize, pre ghost-position fix)

| Item | Value |
|------|-------|
| Tag | `backup/2026-07-02-post-resize` |
| Commit | `bd510ce` |
| Version | Beta 4.0.12 |
| Notes | Kalshi V2 fill sync working; Railway volume resized to 20GB |

## Redeploy on Railway

1. In your repo: `git fetch --tags`
2. Checkout the tag: `git checkout backup/2026-07-02-post-resize`
3. Push to a deploy branch or trigger Railway deploy from that commit:
   - Railway dashboard → Service → Settings → point branch to a branch containing this commit, or
   - `git push origin HEAD:deploy/backup-2026-07-02` and select that branch in Railway

## Local checkout

```bash
git fetch --tags
git checkout backup/2026-07-02-post-resize
```

To return to latest main:

```bash
git checkout main
git pull
```

## List all backup tags

```bash
git tag -l 'backup/*'
```
