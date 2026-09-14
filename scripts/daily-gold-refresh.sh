#!/bin/bash
# Publish the analyser's query indexes, daily, from the seff-data warehouse.
#
# The hourly GitHub Actions workflow rebuilds the rest of this site from the RSM
# API, but it runs on GitHub's runners and the seff-data gold warehouse lives on
# m1mz. So this half has to be pushed from here.
#
# It publishes to main, and the hourly workflow copies what it finds there into
# the deployed site. The deploy branch is force-orphaned on every hourly run, so
# anything pushed straight to it would survive less than an hour; main is the
# only place a daily artefact can wait for the next deploy.
#
# It deliberately does NOT run in your working checkout. It maintains its own
# clone, so a scheduled job can never commit, stash, or rebase over work in
# progress.
#
#   ./scripts/daily-gold-refresh.sh            # export, commit, push
#   ./scripts/daily-gold-refresh.sh --dry-run  # everything except the push
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPORTER="$SCRIPT_DIR/export-prism-index.py"

REPO_URL="https://github.com/spectrumefficiencylimited/sel-current.git"
WORK="${SEL_REFRESH_DIR:-$HOME/R/.sel-current-gold-refresh}"
PYTHON="${SEL_REFRESH_PYTHON:-$HOME/seff-data/venv/bin/python}"
WAREHOUSE="${SEL_WAREHOUSE:-$HOME/seff-data/data/gold.duckdb}"
BRANCH="main"
TARGETS=(silver/analyser-index.json silver/analyser-sites.json silver/prism-meta.json)
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { say "FAILED: $*"; exit 1; }

say "=== daily gold refresh ==="

[ -x "$PYTHON" ]    || die "python not found at $PYTHON"
[ -f "$EXPORTER" ]  || die "exporter not found at $EXPORTER"
[ -f "$WAREHOUSE" ] || die "warehouse not found at $WAREHOUSE (is this m1mz?)"

# The warehouse is maintained by seff-data's own schedule. If that has stalled
# there is nothing new to publish, and quietly re-publishing stale numbers would
# hide the stall rather than surface it.
age_days=$(( ( $(date +%s) - $(stat -f %m "$WAREHOUSE") ) / 86400 ))
say "warehouse last written ${age_days}d ago"
[ "$age_days" -gt 3 ] && say "WARNING: warehouse is ${age_days}d old — check seff-data's schedule"

if [ ! -d "$WORK/.git" ]; then
  say "cloning into $WORK (first run)"
  git clone --depth 50 --branch "$BRANCH" "$REPO_URL" "$WORK" || die "clone failed"
fi
cd "$WORK" || die "cannot enter $WORK"

# Reset hard rather than pull: this clone is disposable and exists only to carry
# generated files. The hourly workflow commits to main constantly, so local state
# here is noise, and an unattended merge conflict is worse than starting from the
# remote every time.
git fetch --depth 50 origin "$BRANCH" || die "fetch failed"
git checkout -q "$BRANCH" 2>/dev/null || git checkout -q -b "$BRANCH" "origin/$BRANCH"
git reset -q --hard "origin/$BRANCH" || die "reset failed"
say "at $(git rev-parse --short HEAD) on $BRANCH"

say "exporting from $WAREHOUSE"
mkdir -p "$WORK/silver"
"$PYTHON" "$EXPORTER" --gold "$WAREHOUSE" --out "$WORK/silver" || die "export failed"

# `git diff` reports nothing for a file git has never seen, so an untracked
# export would look identical to an unchanged one and the first run would
# silently publish nothing. Stage first, then ask.
git add -- "${TARGETS[@]}" || die "git add failed"
if git diff --cached --quiet -- "${TARGETS[@]}"; then
  say "no change — nothing to publish"
  exit 0
fi

if [ "$DRY_RUN" -eq 1 ]; then
  say "DRY RUN — would commit and push:"
  git --no-pager diff --cached --stat -- "${TARGETS[@]}"
  git reset -q HEAD -- "${TARGETS[@]}"
  exit 0
fi

git -c user.name="sel-gold-refresh" \
    -c user.email="spectrum.efficiency.limited@gmail.com" \
    commit -q -m "🗓 Daily analyser index: $(date '+%Y-%m-%d') from seff-data gold" \
    -- "${TARGETS[@]}" || die "commit failed"

git pull -q --rebase origin "$BRANCH" || die "rebase onto origin/$BRANCH failed"
git push -q origin "$BRANCH" || die "push failed"
say "pushed $(git rev-parse --short HEAD)"
say "=== done ==="
