#!/usr/bin/env bash
# Commits the generated data assets and pushes them to the target branch.
#
# Pushes race whenever another run (or a human) lands a commit while this job is
# processing, and a plain `git push` fails with a non-fast-forward error. The
# generated assets are a complete snapshot, so the correct resolution is always
# "rebuild the commit on the new remote tip with our files" rather than a merge.
set -Eeuo pipefail

BRANCH="${TARGET_BRANCH:-main}"
ATTEMPTS="${PUSH_ATTEMPTS:-5}"
read -r -a PATHS <<< "${COMMIT_PATHS:-silver bronze/combined_licences.json index.html}"

log() { printf '%s - commit-and-push: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

git config --local user.email "${GIT_AUTHOR_EMAIL:-action@github.com}"
git config --local user.name "${GIT_AUTHOR_NAME:-GitHub Action}"

git add -- "${PATHS[@]}"
if git diff --staged --quiet; then
    log "no changes to commit."
    exit 0
fi

# Snapshot the generated files: a failed push resets the working tree to the new
# remote tip, which would otherwise discard them.
snapshot=$(mktemp -d)
trap 'rm -rf "$snapshot"' EXIT
for path in "${PATHS[@]}"; do
    [ -e "$path" ] || continue
    mkdir -p "$snapshot/$(dirname "$path")"
    cp -a "$path" "$snapshot/$path"
done

for attempt in $(seq 1 "$ATTEMPTS"); do
    if [ "$attempt" -gt 1 ]; then
        log "push attempt ${attempt}/${ATTEMPTS}: rebuilding on top of origin/${BRANCH}"
        git fetch --depth=1 origin "$BRANCH"
        git reset --hard "origin/${BRANCH}"
        for path in "${PATHS[@]}"; do
            [ -e "$snapshot/$path" ] || continue
            rm -rf "$path"
            mkdir -p "$(dirname "$path")"
            cp -a "$snapshot/$path" "$path"
        done
        git add -- "${PATHS[@]}"
        if git diff --staged --quiet; then
            log "remote already carries this data; nothing to push."
            exit 0
        fi
    fi

    git commit -q -m "📊 Data Update: $(date -u --iso-8601=seconds)"

    if git push origin "HEAD:${BRANCH}"; then
        log "pushed $(git rev-parse --short HEAD) to ${BRANCH}."
        exit 0
    fi

    backoff=$(( attempt * 5 ))
    log "push rejected; retrying in ${backoff}s"
    sleep "$backoff"
done

log "FATAL: could not push to ${BRANCH} after ${ATTEMPTS} attempts." >&2
exit 1
