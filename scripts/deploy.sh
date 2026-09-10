#!/usr/bin/env bash
# Deploy/update this repo on the server (run ON the server, from the repo root).
#
#   bash scripts/deploy.sh [bot|platform|all] [--check] [--force] [--branch NAME]
#
# Normally you run this from your laptop via scripts/remote_deploy.sh, which
# streams this script over SSH — so first-time deploys work even before the
# server's checkout contains it.
#
# Safe by design:
#   * git updates are fast-forward-only unless --force, and refuse to run if
#     the pushed commit is missing the files the bot stack needs;
#   * the new bot image must pass a --once --dry-run smoke test BEFORE the
#     live containers are touched, and the old image is kept tagged
#     bitget-autobuy:rollback (automatic rollback if the new one crash-loops);
#   * .env, data/ (client registry + bot config) and secrets/ are gitignored
#     and never written;
#   * the platform stack runs under a separate compose project
#     ("autobuy-platform") so it can never remove the bot's containers.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash scripts/deploy.sh [bot|platform|all] [--check] [--force] [--branch NAME]

  bot       (default) update + restart the live trading bot and admin web UI
            (docker-compose.legacy.yml — the containers currently in production)
  platform  deploy the FastAPI platform stack (docker-compose.yml, prod
            profile: Postgres + Redis + API on :8000). Refuses to start
            without SECRET_KEY / ENCRYPTION_KEY in .env.
  all       both of the above

  --check   read-only: fetch and report what a deploy would change, then exit
  --force   discard local commits/changes on the server and hard-reset to
            origin (never touches .env, data/, secrets/ — those are gitignored)
  --branch  deploy a specific branch (default: the branch checked out here)
EOF
}

# --- locate the repo root (works both as a file and piped via `bash -s`) ----
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    cd "$(dirname "${BASH_SOURCE[0]}")/.."
fi
if [ ! -f autobuy.py ] || [ ! -d .git ]; then
    echo "ERROR: run from the repo root (autobuy.py + .git not found in $PWD)" >&2
    echo "       e.g.  cd ~/autobuy && bash scripts/deploy.sh" >&2
    exit 1
fi

# --- parse args -------------------------------------------------------------
TARGET=bot
CHECK=0
FORCE=0
BRANCH=""
while [ $# -gt 0 ]; do
    case "$1" in
        bot|platform|all) TARGET="$1" ;;
        --check)  CHECK=1 ;;
        --force)  FORCE=1 ;;
        --branch) BRANCH="${2:?--branch needs a value}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
    shift
done
if [ -z "$BRANCH" ]; then
    BRANCH="$(git rev-parse --abbrev-ref HEAD)"
    if [ "$BRANCH" = "HEAD" ]; then
        echo "ERROR: this checkout is on a detached HEAD — pass --branch NAME explicitly." >&2
        exit 1
    fi
fi

# --- pick how to call docker (sudo needed for the socket and root-owned .env)
SUDO=""
if ! docker info >/dev/null 2>&1 || { [ -e .env ] && [ ! -r .env ]; }; then
    if sudo -n true 2>/dev/null; then
        SUDO="sudo"
    else
        echo "ERROR: this deploy needs root (Docker socket and/or the root-owned .env)" >&2
        echo "       but passwordless sudo is unavailable. Run it as a user that can" >&2
        echo "       'sudo -n' (the ubuntu account on the server can)." >&2
        exit 1
    fi
fi
compose() { $SUDO docker compose "$@"; }

echo "==> Deploy target: $TARGET   branch: $BRANCH   $( [ "$CHECK" = 1 ] && echo '(CHECK ONLY — no changes)' )"

# --- make sure origin uses the deploy key (SSH), not HTTPS ------------------
SSH_URL="git@github.com:erwingarcia1/Minas-AI-Asset-Buy---Bitget---Private.git"
CURRENT_URL="$(git remote get-url origin)"
FETCH_FROM="origin"
case "$CURRENT_URL" in
    https://*)
        if [ "$CHECK" = 1 ]; then
            echo "==> Would switch origin from HTTPS to the SSH deploy key URL: $SSH_URL"
            # fetch through the deploy key anyway, without changing the remote
            FETCH_FROM="$SSH_URL"
        else
            echo "==> Switching origin to the SSH deploy key URL"
            git remote set-url origin "$SSH_URL"
        fi
        ;;
esac

# --- fetch + report ---------------------------------------------------------
export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new"
echo "==> Fetching $BRANCH ..."
git fetch "$FETCH_FROM" "$BRANCH"

LOCAL_REV="$(git rev-parse HEAD)"
REMOTE_REV="$(git rev-parse FETCH_HEAD)"
if [ "$LOCAL_REV" = "$REMOTE_REV" ]; then
    echo "==> Already up to date ($(git log -1 --oneline))"
else
    echo "==> Updates available:"
    git log --oneline "HEAD..$REMOTE_REV" | sed 's/^/      /'
fi

# --- refuse to deploy a commit that cannot run the bot stack ----------------
# (catches a partial push: the compose/requirements/Dockerfile set must land
#  together, or the rebuilt webapp image would crash-loop on missing flask)
if [ "$TARGET" != platform ]; then
    PUSH_PROBLEM=""
    for f in docker-compose.legacy.yml requirements-legacy.txt; do
        git cat-file -e "$REMOTE_REV:$f" 2>/dev/null || PUSH_PROBLEM="$f is missing from the pushed commit"
    done
    if [ -z "$PUSH_PROBLEM" ] && ! git show "$REMOTE_REV:Dockerfile" | grep -q requirements-legacy.txt; then
        PUSH_PROBLEM="the pushed Dockerfile still installs requirements.txt (no flask — webapp would crash-loop)"
    fi
    if [ -n "$PUSH_PROBLEM" ]; then
        if [ "$CHECK" = 1 ]; then
            echo "==> WARNING: $PUSH_PROBLEM — a bot deploy would refuse to run."
        else
            echo "ERROR: $PUSH_PROBLEM." >&2
            echo "       Commit docker-compose.legacy.yml, requirements-legacy.txt, the" >&2
            echo "       Dockerfile edit and scripts/ together, push, then re-run." >&2
            exit 1
        fi
    fi
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "==> NOTE: server checkout has local modifications:"
    git status --porcelain | sed 's/^/      /'
fi

if [ "$CHECK" = 1 ]; then
    echo "==> Current containers:"
    $SUDO docker ps --format '      {{.Names}}\t{{.Status}}' || true
    echo "==> Check complete — nothing was changed."
    exit 0
fi

# --- update the working tree ------------------------------------------------
if [ "$LOCAL_REV" != "$REMOTE_REV" ]; then
    if [ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
        if [ "$FORCE" = 1 ]; then
            git checkout -qf "$BRANCH" 2>/dev/null || git checkout -qf -b "$BRANCH" "$REMOTE_REV"
        else
            git checkout -q "$BRANCH" 2>/dev/null || git checkout -qb "$BRANCH" "$REMOTE_REV"
        fi
    fi
    if [ "$FORCE" = 1 ]; then
        echo "==> Hard-resetting to origin/$BRANCH (--force)"
        git reset --hard "$REMOTE_REV"
    else
        echo "==> Fast-forwarding to origin/$BRANCH"
        if ! git merge --ff-only "$REMOTE_REV"; then
            echo "ERROR: cannot fast-forward — the server has local commits or the branch" >&2
            echo "       was rewritten. Re-run with --force to discard the server's local" >&2
            echo "       git state (config/.env/data are gitignored and stay untouched)." >&2
            exit 1
        fi
    fi
    echo "==> Now at: $(git log -1 --oneline)"
fi

# --- sanity checks shared by both stacks ------------------------------------
if ! $SUDO test -f .env; then
    echo "ERROR: .env not found in $PWD — the bot's API keys / the platform's" >&2
    echo "       secrets live there. Create it first (see README)." >&2
    exit 1
fi

LEGACY="-f docker-compose.legacy.yml"

deploy_bot() {
    echo "==> [bot] Keeping the current image as bitget-autobuy:rollback ..."
    $SUDO docker tag bitget-autobuy:latest bitget-autobuy:rollback 2>/dev/null \
        || echo "      (no existing image to keep — first build on this box)"

    echo "==> [bot] Building the new image ..."
    # shellcheck disable=SC2086
    compose $LEGACY build

    echo "==> [bot] Smoke test: one --dry-run pass with the NEW image (live bot untouched) ..."
    # shellcheck disable=SC2086
    if ! compose $LEGACY run --rm autobuy --once --dry-run; then
        echo "ERROR: the new image failed its dry-run pass — live containers were NOT touched." >&2
        exit 1
    fi

    echo "==> [bot] Restarting bot + web UI on the new image ..."
    # shellcheck disable=SC2086
    compose $LEGACY up -d

    echo "==> [bot] Watching for crash loops (30s) ..."
    local _round name state restarts
    for _round in 1 2 3 4 5 6; do
        sleep 5
        for name in bitget-autobuy bitget-autobuy-webapp; do
            state="$($SUDO docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)"
            restarts="$($SUDO docker inspect -f '{{.RestartCount}}' "$name" 2>/dev/null || echo 999)"
            if [ "$state" != running ] || [ "$restarts" != 0 ]; then
                echo "ERROR: $name is $state (restarts: $restarts). Recent logs:" >&2
                # shellcheck disable=SC2086
                compose $LEGACY logs --tail 30 >&2 || true
                if $SUDO docker image inspect bitget-autobuy:rollback >/dev/null 2>&1; then
                    echo "==> [bot] Rolling back to the previous image ..." >&2
                    $SUDO docker tag bitget-autobuy:rollback bitget-autobuy:latest
                    # shellcheck disable=SC2086
                    compose $LEGACY up -d --force-recreate
                    echo "==> [bot] Rollback done — investigate before redeploying." >&2
                fi
                exit 1
            fi
        done
    done
    echo "==> [bot] Healthy. Last log lines:"
    # shellcheck disable=SC2086
    compose $LEGACY logs --tail 5 autobuy 2>/dev/null | sed 's/^/      /' || true
}

deploy_platform() {
    echo "==> [platform] Checking the server .env (API_ENV must be exactly production, secrets, email, links) ..."
    # shellcheck source=scripts/lib/platform_env_checks.sh
    . "$(dirname "${BASH_SOURCE[0]:-$0}")/lib/platform_env_checks.sh" 2>/dev/null || . scripts/lib/platform_env_checks.sh
    PLATFORM_ENV_SUDO="$SUDO" check_platform_env .env || exit 1
    echo "==> [platform] Validating docker-compose.yml ..."
    if ! compose -p autobuy-platform --profile prod config -q; then
        echo "ERROR: docker-compose.yml failed validation — fix it before deploying." >&2
        exit 1
    fi
    echo "==> [platform] Building + starting Postgres, Redis and the API (project: autobuy-platform) ..."
    compose -p autobuy-platform --profile prod up -d --build
    echo "==> [platform] Waiting for the API to pass /health (migrations run at boot) ..."
    local _try
    for _try in $(seq 1 30); do
        if curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:8000/health; then
            echo "==> [platform] API is healthy: http://127.0.0.1:8000/health"
            return 0
        fi
        sleep 4
    done
    echo "ERROR: API did not become healthy within 2 minutes. Recent logs:" >&2
    compose -p autobuy-platform --profile prod logs --tail 40 api >&2 || true
    exit 1
}

case "$TARGET" in
    bot)      deploy_bot ;;
    platform) deploy_platform ;;
    all)      deploy_bot; deploy_platform ;;
esac

echo "==> Pruning dangling images older than 48h (rollback image is tagged, so it survives) ..."
$SUDO docker image prune -f --filter "until=48h" >/dev/null || true

echo "==> Deploy complete. Containers:"
$SUDO docker ps --format '      {{.Names}}\t{{.Status}}'
