#!/usr/bin/env bash
# push_pi_data.sh — publish the Pi-only operational data to the `pi-data` branch.
#
# The data files (decisions.jsonl, daily_energy.jsonl, agent_decisions.log,
# energy_log.db) are gitignored on main and exist only on the Pi. This script
# snapshots them into a dedicated orphan branch so any remote session (Mac,
# Claude Code web, phone) can read them with:
#
#   git fetch origin pi-data
#   git show origin/pi-data:decisions.jsonl
#
# The branch is kept flat — a single commit, force-pushed each run. The JSONL
# files are append-only and the DB snapshot is full state, so no history is
# lost by replacing the commit.
#
# Cron (hourly, 5 past — after the :00/:30 agent cycles):
#   5 * * * * $HOME/home-energy-agent/agent/push_pi_data.sh >> /tmp/push_pi_data.log 2>&1

set -euo pipefail

REPO="${REPO:-$HOME/home-energy-agent}"
AGENT_DIR="$REPO/agent"
WORKTREE="${WORKTREE:-$HOME/.pi-data-worktree}"
BRANCH="pi-data"

export GIT_AUTHOR_NAME="energypi" GIT_AUTHOR_EMAIL="energypi@local"
export GIT_COMMITTER_NAME="energypi" GIT_COMMITTER_EMAIL="energypi@local"

# Single-instance guard — cron overlap protection
exec 9>/tmp/push_pi_data.lock
flock -n 9 || { echo "$(date -Iseconds) another run in progress, skipping"; exit 0; }

cd "$REPO"

# --- Bootstrap: ensure a local pi-data branch exists -------------------------
git fetch origin "$BRANCH" 2>/dev/null || true
if ! git rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null; then
    if git rev-parse --verify --quiet "refs/remotes/origin/$BRANCH" >/dev/null; then
        git branch "$BRANCH" "origin/$BRANCH"
    else
        # Orphan branch from the empty tree — no code history, data only
        EMPTY_TREE=$(git mktree </dev/null)
        INIT=$(git commit-tree "$EMPTY_TREE" -m "pi-data: initial empty commit")
        git branch "$BRANCH" "$INIT"
    fi
fi

# --- Worktree: pi-data checked out beside the main repo ----------------------
git worktree prune
if [ ! -d "$WORKTREE" ]; then
    git worktree add "$WORKTREE" "$BRANCH"
fi

# --- Copy data files ----------------------------------------------------------
for f in decisions.jsonl daily_energy.jsonl agent_decisions.log; do
    [ -f "$AGENT_DIR/$f" ] && cp "$AGENT_DIR/$f" "$WORKTREE/$f"
done

# Consistent SQLite snapshot (DB is in WAL mode — plain cp can tear)
if [ -f "$AGENT_DIR/energy_log.db" ]; then
    SRC_DB="$AGENT_DIR/energy_log.db" DST_DB="$WORKTREE/energy_log.db" python3 - <<'PY'
import os, sqlite3
src = sqlite3.connect(os.environ["SRC_DB"])
dst = sqlite3.connect(os.environ["DST_DB"])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
fi

date -Iseconds > "$WORKTREE/last_updated.txt"

# --- Commit (amend keeps the branch at one commit) and force-push ------------
cd "$WORKTREE"
git add -A
git commit --amend --quiet -m "pi-data snapshot $(date -Iseconds)"

for delay in 2 4 8 16 0; do
    if git push -f -u origin "$BRANCH" 2>&1; then
        echo "$(date -Iseconds) pushed $BRANCH"
        exit 0
    fi
    [ "$delay" -eq 0 ] && break
    echo "$(date -Iseconds) push failed, retrying in ${delay}s"
    sleep "$delay"
done

echo "$(date -Iseconds) ERROR: push failed after retries" >&2
exit 1
