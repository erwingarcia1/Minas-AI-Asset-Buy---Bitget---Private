#!/usr/bin/env bash
# One-command deploy from your laptop: SSH into the AWS server and run
# scripts/deploy.sh there. All arguments are passed straight through:
#
#   bash scripts/remote_deploy.sh                    # update the live bot
#   bash scripts/remote_deploy.sh --check            # read-only: what would change?
#   bash scripts/remote_deploy.sh platform           # deploy the platform API stack
#   bash scripts/remote_deploy.sh all --force        # both stacks, discard server-local git state
#
# The LOCAL copy of scripts/deploy.sh is streamed over SSH, so this works even
# when the server's checkout doesn't have the script yet (first deploy).
#
# Overrides (env vars):
#   SSH_KEY     path to the .pem   (default: <repo>/Minas-AI-Guardian-Execution-Key-Tokyo.pem)
#   SERVER      user@host          (default: ubuntu@$SERVER_HOST from scripts/lib/server_host.sh)
#   SERVER_HOST the box's address    (default: the Tokyo Elastic IP; the only place it is written)
#   REMOTE_DIR  repo path on server (default: ~/autobuy)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_KEY="${SSH_KEY:-$REPO_ROOT/Minas-AI-Guardian-Execution-Key-Tokyo.pem}"
# shellcheck source=scripts/lib/server_host.sh
. "$REPO_ROOT/scripts/lib/server_host.sh"      # SERVER (user@host); override SERVER_HOST / SERVER_USER / SERVER
REMOTE_DIR="${REMOTE_DIR:-\$HOME/autobuy}"

if [ ! -f "$SSH_KEY" ]; then
    echo "ERROR: SSH key not found: $SSH_KEY" >&2
    echo "       Put the .pem there or point SSH_KEY at it." >&2
    exit 1
fi

# ssh refuses keys readable by group/others — fix quietly if needed.
# (any nonzero group/other digit counts, so compare the last two mode digits)
KEY_MODE="$(stat -c '%a' "$SSH_KEY" 2>/dev/null || stat -f '%Lp' "$SSH_KEY" 2>/dev/null || echo '')"
case "$KEY_MODE" in
    *00|'') : ;;  # already owner-only (or stat unavailable — let ssh decide)
    *)
        echo "==> Tightening key permissions to 600 (ssh refuses open .pem files)"
        chmod 600 "$SSH_KEY"
        ;;
esac

ARGS=""
for a in "$@"; do ARGS="$ARGS $(printf '%q' "$a")"; done

echo "==> Deploying to $SERVER (args: ${*:-bot}) ..."
# shellcheck disable=SC2029  # $REMOTE_DIR and $ARGS expand remotely by design
ssh -i "$SSH_KEY" -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new \
    "$SERVER" "cd $REMOTE_DIR && bash -s --$ARGS" < "$REPO_ROOT/scripts/deploy.sh"
