#!/usr/bin/env bash
# Replaces the history of `main` with a single commit holding the current tree.
#
# WHY: every run used to commit ~80 MB of regenerated data, growing the
# repository to 10.6 GB. The pipeline no longer does that, but the existing
# history still carries every old snapshot. Truncating makes those objects
# unreachable so GitHub can garbage-collect them.
#
# THIS IS DESTRUCTIVE AND IRREVERSIBLE. Read docs/RUNBOOK-history-truncation.md
# before running it. In particular, do not run it until the new pipeline is
# merged to main and has completed at least one green run.
set -Eeuo pipefail

BRANCH="${TARGET_BRANCH:-main}"
REMOTE="${REMOTE:-origin}"
BUNDLE_PATH="${BUNDLE_PATH:-}"

log() { printf '%s - truncate-history: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ABORTED: $*" >&2; exit 1; }

[ "${CONFIRM:-}" = "yes-truncate-main" ] || die \
    "refusing to run without CONFIRM=yes-truncate-main. See docs/RUNBOOK-history-truncation.md."

log "fetching ${REMOTE}/${BRANCH}..."
git fetch "$REMOTE" "$BRANCH"

TIP=$(git rev-parse "${REMOTE}/${BRANCH}")
TREE=$(git rev-parse "${REMOTE}/${BRANCH}^{tree}")
log "current tip: ${TIP}"

# Guard: the whole point is to truncate a history that will not grow back. If
# the old pipeline is still on main it would start re-committing the datasets
# on the next hourly run, and the repository would bloat again from zero.
WORKFLOW=$(git show "${REMOTE}/${BRANCH}:.github/workflows/fetch-data.yml" 2>/dev/null || true)
case "$WORKFLOW" in
    *COMMIT_PATHS*) log "verified: main carries the pipeline that keeps datasets out of git." ;;
    "") die "could not read .github/workflows/fetch-data.yml on ${REMOTE}/${BRANCH}." ;;
    *) die "main still has the old workflow, which commits the full datasets. Merge the new pipeline and let it run green first, or the history will grow straight back." ;;
esac

# Guard: a git-tracked dataset means the tree we are about to freeze still
# contains the very files this exercise is meant to remove.
for path in bronze/combined_licences.json silver/combined_licences.json \
            silver/combined_licences.csv silver/combined_licences.duckdb; do
    if git cat-file -e "${TREE}:${path}" 2>/dev/null; then
        die "${path} is still tracked on ${BRANCH}; the truncated tree would keep it. Merge the removal first."
    fi
done

if [ -n "$BUNDLE_PATH" ]; then
    git rev-parse --is-shallow-repository | grep -q false \
        || die "BUNDLE_PATH needs a full clone; this one is shallow. Re-clone without --depth, or unset BUNDLE_PATH to skip the backup."
    log "writing backup bundle to ${BUNDLE_PATH} (this is large and slow)..."
    git bundle create "$BUNDLE_PATH" "${REMOTE}/${BRANCH}"
    log "backup written: $(du -h "$BUNDLE_PATH" | cut -f1)"
else
    log "NOTE: no BUNDLE_PATH set, so no backup of the old history is being taken."
fi

MESSAGE="${TRUNCATE_MESSAGE:-Repository history restarted

The full datasets are build outputs published to the live site, not source.
Committing them added roughly 80 MB of git history per hour and grew this
repository to 10.6 GB. History before this commit held those regenerated
snapshots and has been dropped; the pipeline and the current data are intact.

Downloads are unchanged:
  https://spectrumefficiencylimited.github.io/sel-current/silver/

Previous tip: ${TIP}}"

NEW_COMMIT=$(git commit-tree "$TREE" -m "$MESSAGE")
log "created parentless commit ${NEW_COMMIT} with the exact tree of ${TIP}"

log "force-pushing ${NEW_COMMIT} to ${REMOTE}/${BRANCH}..."
git push --force-with-lease="${BRANCH}:${TIP}" "$REMOTE" "${NEW_COMMIT}:refs/heads/${BRANCH}"

log "done. ${BRANCH} is now a single commit."
cat <<'NEXT'

Next steps (the repository will NOT shrink on its own):

  1. Let the workflow run once. peaceiris publishes gh-pages with
     force_orphan, so that branch also collapses to a single commit and its
     old objects become unreachable too.
  2. Delete any other refs still holding old objects: stale branches, tags,
     and open PRs that reference pre-truncation commits.
  3. Ask GitHub Support (https://support.github.com/) to run garbage
     collection on the repository. Unreachable objects are not reclaimed
     promptly on their own, so the reported size stays high until they do.
     Mention that history was intentionally rewritten to drop large
     regenerated data blobs.
  4. Tell anyone with a clone to re-clone. Their old history no longer
     shares any commit with the remote.
NEXT
