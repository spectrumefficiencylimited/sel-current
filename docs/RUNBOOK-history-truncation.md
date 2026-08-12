# Runbook: truncating `main` history

## Why

Every workflow run used to commit the regenerated datasets — roughly **80 MB of
new git objects per hour**. Over about a year that grew the repository to
**10.6 GB** (GitHub's reported size), which means slow clones and very little
headroom before GitHub's size limits.

The pipeline no longer commits those files: they are build outputs, published to
the live site on every run. That stops the growth, but the existing history
still holds every old snapshot. Truncating `main` makes those objects
unreachable so GitHub can garbage-collect them.

## What is lost

The hourly full-dataset snapshots in git history — roughly 8,400 copies of a
dataset whose only useful version is the current one.

**Not** lost: the pipeline, the current data, the site, the repository URL, the
Pages deployment, stars, forks, issues, or PRs — and, importantly, **not the
history's analytical value**. Before truncating, `scripts/backfill-history.py`
mined the summary files out of those 8,400 commits into `gold/`, so the record
back to 2025-07-18 survives as a 568 KB time series that is already committed.

If you want to re-mine or extend that back-fill — for example to pull richer
dimensions out of the old raw snapshots — **do it before step 3**, because after
truncation those commits are gone:

```bash
git clone --filter=blob:limit=10k --no-checkout --single-branch --branch main \
    https://github.com/spectrumefficiencylimited/sel-current.git hist
./scripts/backfill-history.py --repo hist --output gold/
```

The blob filter keeps that clone at a few megabytes instead of 10.6 GB.

### Checking whether an engineer dimension is possible

Aggregating by approved radio engineer needs a field the `/licences` endpoint
does not return. Before truncating is a good moment to check whether another
endpoint carries it, since the raw snapshots are still around to compare against:

```bash
curl -s -H "Ocp-Apim-Subscription-Key: $RSM_API_KEY" \
  "https://api.business.govt.nz/gateway/radio-spectrum-management/v1/licences/441849" | jq 'keys'
```

If a licence detail response includes an engineer or certifier, the pipeline can
be extended to enrich a daily sample rather than every record.

## Order of operations

Do these in order. Step 3 is irreversible.

### 1. Merge the new pipeline to `main`

The truncation script refuses to run until `main` carries the workflow that
keeps datasets out of git. If you truncate while the old workflow is still on
`main`, the very next hourly run starts re-committing 80 MB and the repository
bloats again from zero.

### 2. Confirm one green run

Wait for a scheduled run (or trigger one from the Actions tab) and confirm:

- the run is green,
- <https://spectrumefficiencylimited.github.io/sel-current/> loads and shows a
  current timestamp,
- the CSV, JSON and DuckDB links download,
- the commit it pushed to `main` touches only the three small files.

Until this passes, the old history is your fallback. Do not skip it.

### 3. Truncate

Branch protection on `main`, if enabled, must be lifted for the force-push and
can be restored immediately afterwards.

```bash
# Optional but recommended: a backup of the old history. Needs a FULL clone,
# which means downloading ~10.6 GB, and produces a bundle of similar size.
git clone https://github.com/spectrumefficiencylimited/sel-current.git full-clone
cd full-clone
export BUNDLE_PATH=~/sel-current-history-$(date +%F).bundle

# A shallow clone is fine if you are skipping the backup.
CONFIRM=yes-truncate-main ./scripts/truncate-history.sh
```

The script fetches `main`, verifies the new pipeline is in place and the large
files are untracked, optionally writes the backup bundle, then creates a
parentless commit with the *exact current tree* and force-pushes it with a
lease. The working tree is never modified, and no files change — only the
history behind them.

### 4. Reclaim the space

The repository will **not** shrink on its own.

1. Let the workflow run once. `peaceiris/actions-gh-pages` is configured with
   `force_orphan: true`, so `gh-pages` collapses to a single commit and its old
   objects become unreachable too.
2. Delete stale branches and tags that still reference pre-truncation commits,
   and close or rebase any open PR against the old history.
3. Ask [GitHub Support](https://support.github.com/) to run garbage collection,
   explaining that history was intentionally rewritten to drop large regenerated
   data blobs. The reported size stays at 10.6 GB until they do.

### 5. Tell consumers

Anyone with an existing clone must re-clone: their history shares no commit with
the remote any more. Data consumers using the published download URLs are
unaffected — those URLs and their contents do not change.

## Rollback

Before step 3, there is nothing to roll back. After step 3 and before GitHub's
garbage collection, the old tip is recoverable if you kept it:

```bash
git push --force origin <old-tip-sha>:refs/heads/main
```

The script prints the previous tip SHA and records it in the truncation commit
message. Once garbage collection has run, recovery is only possible from the
backup bundle.
