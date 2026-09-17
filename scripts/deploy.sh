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
Usage: bash scripts/deploy.sh [bot|platform|all] [--check] [--force] [--branch NAME] [--web DIR]

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
  --web DIR platform: ship a prebuilt web bundle (a `frontend/dist` built on a
            laptop with `npm run build` and copied to DIR on this server) into
            frontend/dist before starting the API. Without --web the deploy
            refuses to complete unless frontend/dist was built from this
            checkout's frontend/ tree (frontend/dist/BUILD_INFO), so the served
            SPA can never silently be an older release.
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
WEB_BUNDLE=""
while [ $# -gt 0 ]; do
    case "$1" in
        bot|platform|all) TARGET="$1" ;;
        --check)  CHECK=1 ;;
        --force)  FORCE=1 ;;
        --branch) BRANCH="${2:?--branch needs a value}"; shift ;;
        --web)    WEB_BUNDLE="${2:?--web needs a directory}"; shift ;;
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
# The compose profile that carries the platform API. docker-compose.yml declares
# profiles: ["prod"]; anything else (e.g. "production") silently deploys nothing.
PLATFORM_PROFILE="prod"

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
    if ! compose -p autobuy-platform --profile "$PLATFORM_PROFILE" config -q; then
        echo "ERROR: docker-compose.yml failed validation — fix it before deploying." >&2
        exit 1
    fi
    # A profile that matches nothing makes `up -d` a silent no-op ("Container
    # autobuy_postgres Running", exit 0) while the old API keeps serving stale
    # code and env. Refuse unless the api service is part of the resolved config.
    if ! compose -p autobuy-platform --profile "$PLATFORM_PROFILE" config --services 2>/dev/null | grep -qx api; then
        echo "ERROR: compose profile '$PLATFORM_PROFILE' does not include the api service — nothing would be deployed." >&2
        echo "       docker-compose.yml declares the API under profiles: [\"prod\"]." >&2
        exit 1
    fi

    local before_id before_created expected_commit expected_tree
    before_id="$($SUDO docker inspect --format '{{.Id}}' autobuy_api 2>/dev/null || echo none)"
    before_created="$($SUDO docker inspect --format '{{.Created}}' autobuy_api 2>/dev/null || echo none)"
    expected_commit="$(git rev-parse HEAD)"

    # 0. the web bundle: shipped from --web, or already on disk and built from this checkout.
    #    Checked BEFORE the API moves, so a stale SPA never rides along with a new API.
    # shellcheck source=scripts/lib/platform_web_checks.sh
    . "$(dirname "${BASH_SOURCE[0]:-$0}")/lib/platform_web_checks.sh" 2>/dev/null || . scripts/lib/platform_web_checks.sh
    expected_tree="$(frontend_tree_of .)"
    if [ -n "$WEB_BUNDLE" ]; then
        echo "==> [platform] Checking the web bundle at $WEB_BUNDLE against this checkout's frontend/ tree ${expected_tree:0:12} ..."
        check_web_bundle "$expected_tree" "$WEB_BUNDLE" || exit 1
        if [ -d frontend/dist ]; then
            rm -rf frontend/dist.prev && mv frontend/dist frontend/dist.prev
        fi
        mkdir -p frontend/dist
        cp -a "$WEB_BUNDLE"/. frontend/dist/
        echo "==> [platform] Web bundle installed into frontend/dist (previous kept at frontend/dist.prev)"
    else
        echo "==> [platform] Checking that frontend/dist was built from this checkout's frontend/ tree ${expected_tree:0:12} ..."
        if ! check_web_bundle "$expected_tree" frontend/dist; then
            echo "       The served web app would be an older release than the API. Build on a laptop" >&2
            echo "       (cd frontend && npm ci && npm run build), copy frontend/dist/ to this server, and" >&2
            echo "       rerun with --web /path/to/dist. Nothing was deployed." >&2
            exit 1
        fi
    fi

    echo "==> [platform] Building + starting Postgres, Redis and the API (project: autobuy-platform, commit ${expected_commit:0:7}) ..."
    GIT_COMMIT="$expected_commit" compose -p autobuy-platform --profile "$PLATFORM_PROFILE" up -d --build

    # 1. the api container must have been (re)created — same id and created-at means nothing moved
    local after_id after_created
    after_id="$($SUDO docker inspect --format '{{.Id}}' autobuy_api 2>/dev/null || echo none)"
    after_created="$($SUDO docker inspect --format '{{.Created}}' autobuy_api 2>/dev/null || echo none)"
    if [ "$after_id" = "none" ]; then
        echo "ERROR: no autobuy_api container after 'up -d' — the deploy did not start the API." >&2
        exit 1
    fi
    if [ "$after_id" = "$before_id" ] && [ "$after_created" = "$before_created" ]; then
        echo "ERROR: the autobuy_api container was not recreated (same id ${after_id:0:12}, created $after_created)." >&2
        echo "       The old build is still running with its old environment. Nothing was deployed." >&2
        exit 1
    fi
    echo "==> [platform] api container recreated: ${before_id:0:12} -> ${after_id:0:12} (created $after_created)"

    echo "==> [platform] Waiting for the API to pass /health (migrations run at boot) ..."
    local _try health
    for _try in $(seq 1 30); do
        if health="$(curl -fsS --max-time 3 http://127.0.0.1:8000/health 2>/dev/null)"; then
            # 2. the running code must be the commit we just deployed
            local running_commit
            running_commit="$(printf '%s' "$health" | sed -n 's/.*"commit": *"\([^"]*\)".*/\1/p')"
            if [ "$running_commit" != "$expected_commit" ]; then
                echo "ERROR: /health reports commit '${running_commit:-<none>}' but the checkout is $expected_commit." >&2
                echo "       The container that answered is not running the deployed code (stale image or another process on :8000)." >&2
                exit 1
            fi
            echo "==> [platform] API is healthy and running commit ${running_commit:0:7}: http://127.0.0.1:8000/health"
            # 3. the web app nginx actually serves must be that same bundle (bind mount, no restart needed)
            local served_tree
            served_tree="$(curl -fsS --max-time 3 http://127.0.0.1:8080/BUILD_INFO 2>/dev/null | sed -n 's/^frontend_tree=//p' | head -1)"
            if [ -z "$served_tree" ]; then
                echo "ERROR: http://127.0.0.1:8080/BUILD_INFO is not served — the web container is not serving frontend/dist." >&2
                exit 1
            fi
            if [ "$served_tree" != "$expected_tree" ]; then
                echo "ERROR: nginx serves a web bundle built from frontend tree ${served_tree:0:12}, not ${expected_tree:0:12}." >&2
                exit 1
            fi
            echo "==> [platform] Web app on :8080 serves the bundle built from this checkout (frontend tree ${served_tree:0:7})"
            return 0
        fi
        sleep 4
    done
    echo "ERROR: API did not become healthy within 2 minutes. Recent logs:" >&2
    compose -p autobuy-platform --profile "$PLATFORM_PROFILE" logs --tail 40 api >&2 || true
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
