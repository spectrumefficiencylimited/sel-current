#!/bin/bash
# Refresh gold/seff-aggregates.json from the seff-data warehouse and push it.
#
# The hourly GitHub Actions workflow rebuilds everything else in this repo from
# the RSM API, but it runs on GitHub's runners and the seff-data DuckDB
# warehouse lives on m1mz. So this half of the data has to be pushed from here,
# which is what this script is: the weekly counterpart to the hourly workflow.
#
# It deliberately does NOT run in your working checkout. It maintains its own
# clone, so a scheduled job can never commit, stash, or rebase over work in
# progress. The only file it is allowed to touch is the export.
#
#   ./scripts/weekly-gold-refresh.sh            # refresh, commit, push
#   ./scripts/weekly-gold-refresh.sh --dry-run  # do everything except push
#
set -uo pipefail

# The exporter is resolved next to this script, in the checkout you actually
# work in — not inside the throwaway clone. The clone exists to carry the
# result to the remote, nothing more, and this way the job never depends on the
# remote already having the version of the exporter it is about to run.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPORTER="$SCRIPT_DIR/export-seff-aggregates.py"

REPO_URL="https://github.com/spectrumefficiencylimited/sel-current.git"
WORK="${SEL_REFRESH_DIR:-$HOME/R/.sel-current-gold-refresh}"
PYTHON="${SEL_REFRESH_PYTHON:-$HOME/seff-data/venv/bin/python}"
WAREHOUSE="${SEL_WAREHOUSE:-$HOME/seff-data/data/gold.duckdb}"
TARGET="gold/seff-aggregates.json"
BRANCH="main"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { say "FAILED: $*"; exit 1; }

say "=== weekly gold refresh ==="

[ -x "$PYTHON" ]    || die "python not found at $PYTHON"
[ -f "$EXPORTER" ]  || die "exporter not found at $EXPORTER"
[ -f "$WAREHOUSE" ] || die "warehouse not found at $WAREHOUSE (is this m1mz?)"

# The warehouse is maintained by seff-data's own cron. If that has stalled there
# is nothing new to publish, and quietly re-committing week-old numbers would
# hide the stall rather than surface it.
age_days=$(( ( $(date +%s) - $(stat -f %m "$WAREHOUSE") ) / 86400 ))
say "warehouse last written ${age_days}d ago"
[ "$age_days" -gt 14 ] && say "WARNING: warehouse is over two weeks old — check seff-data's cron"

# --- the dedicated clone ----------------------------------------------------
if [ ! -d "$WORK/.git" ]; then
  say "cloning into $WORK (first run)"
  git clone --depth 50 --branch "$BRANCH" "$REPO_URL" "$WORK" || die "clone failed"
fi
cd "$WORK" || die "cannot enter $WORK"

# Reset hard rather than pull: this clone is disposable and exists only to carry
# one generated file. The hourly workflow commits to main constantly, so any
# local state here is noise, and a merge conflict in an unattended job is worse
# than starting from the remote every time.
git fetch --depth 50 origin "$BRANCH" || die "fetch failed"
git checkout -q "$BRANCH" 2>/dev/null || git checkout -q -b "$BRANCH" "origin/$BRANCH"
git reset -q --hard "origin/$BRANCH" || die "reset failed"
say "at $(git rev-parse --short HEAD) on $BRANCH"

# --- regenerate --------------------------------------------------------------
say "exporting from $WAREHOUSE"
"$PYTHON" "$EXPORTER" --db "$WAREHOUSE" --output "$WORK/$TARGET" \
  || die "export script failed"

# `git diff` reports nothing for a file git has never seen, so an untracked
# export would look identical to an unchanged one and the first run would
# silently publish nothing. Stage first, then ask.
git add -- "$TARGET" || die "git add failed"
if git diff --cached --quiet -- "$TARGET"; then
  say "no change — nothing to publish"
  exit 0
fi
say "$TARGET changed"
# Pathspec-scoped commit: even if something else were somehow dirty in this
# clone, only the export can be committed.
MSG="🗓 Weekly gold refresh: $(date '+%Y-%m-%d') from seff-data warehouse"

if [ "$DRY_RUN" -eq 1 ]; then
  say "DRY RUN — would commit and push:"
  git --no-pager diff --cached --stat -- "$TARGET"
  git reset -q HEAD -- "$TARGET"
  exit 0
fi

git -c user.name="sel-gold-refresh" \
    -c user.email="spectrum.efficiency.limited@gmail.com" \
    commit -q -m "$MSG" -- "$TARGET" || die "commit failed"

# The hourly workflow may have pushed between our reset and now; rebase our one
# commit on top rather than forcing over it.
git pull -q --rebase origin "$BRANCH" || die "rebase onto origin/$BRANCH failed"
git push -q origin "$BRANCH" || die "push failed"
say "pushed $(git rev-parse --short HEAD)"
say "=== done ==="
