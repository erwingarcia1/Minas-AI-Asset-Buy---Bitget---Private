#!/usr/bin/env bash
# Open an interactive SSH session on the AWS server (Tokyo EC2).
#
#   bash scripts/server_ssh.sh                 # interactive shell
#   bash scripts/server_ssh.sh 'docker ps'     # run one command and exit
#
# Overrides: SSH_KEY and SERVER env vars, same as scripts/remote_deploy.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_KEY="${SSH_KEY:-$REPO_ROOT/Minas-AI-Guardian-Execution-Key-Tokyo.pem}"
# shellcheck source=scripts/lib/server_host.sh
. "$REPO_ROOT/scripts/lib/server_host.sh"      # SERVER (user@host); override SERVER_HOST / SERVER_USER / SERVER

if [ ! -f "$SSH_KEY" ]; then
    echo "ERROR: SSH key not found: $SSH_KEY (set SSH_KEY=/path/to/key.pem)" >&2
    exit 1
fi

exec ssh -i "$SSH_KEY" -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new "$SERVER" "$@"
