#!/usr/bin/env bash
# Deploy/update this repo on the server (run ON the server, from the repo root).
#
#
# Normally you run this from your laptop via scripts/remote_deploy.sh, which
# streams this script over SSH — so first-time deploys work even before the
# server's checkout contains it.
#
# Safe by design:
#   * git updates are fast-forward-only unless --force, and refuse to run if
#     live containers are touched, and the old image is kept tagged
#     bitget-autobuy:rollback (automatic rollback if the new one crash-loops);
#     and never written;
#   * the platform stack runs under a separate compose project
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash scripts/deploy.sh [platform] [--check] [--force] [--branch NAME] [--web DIR]

  platform  (default, and the only target) deploy the FastAPI platform stack
            (docker-compose.yml, prod profile: Postgres + Redis + API + web +
            caddy). Refuses to start without SECRET_KEY / ENCRYPTION_KEY in .env.

  --check   read-only: fetch and report what a deploy would change, then exit
  --force   discard local commits/changes on the server and hard-reset to
            origin (never touches .env, data/, secrets/ — those are gitignored)
  --branch  deploy a specific branch (default: the branch checked out here)
  --web DIR ship a prebuilt web bundle (a `frontend/dist` built on a laptop
            with `npm run build` and copied to DIR on this server) into
            frontend/dist before starting the API. Without --web the deploy
            refuses to complete unless frontend/dist was built from this
            checkout's frontend/ tree (frontend/dist/BUILD_INFO), so the served
            SPA can never silently be an older release.
EOF
}

if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    cd "$(dirname "${BASH_SOURCE[0]}")/.."
fi
if [ ! -f docker-compose.yml ] || [ ! -d .git ]; then
    echo "ERROR: run from the repo root (docker-compose.yml + .git not found in $PWD)" >&2
    echo "       e.g.  cd ~/autobuy && bash scripts/deploy.sh" >&2
    exit 1
fi

# --- parse args -------------------------------------------------------------
TARGET=platform
CHECK=0
FORCE=0
BRANCH=""
WEB_BUNDLE=""
while [ $# -gt 0 ]; do
    case "$1" in
        platform) TARGET="$1" ;;
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

# --- sanity checks ----------------------------------------------------------
if ! $SUDO test -f .env; then
    echo "ERROR: .env not found in $PWD — the platform's secrets live there." >&2
    echo "       Create it first (see docs/DEPLOYMENT.md)." >&2
    exit 1
fi

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

    # The Guardian worker exists from v0.9.1. Its checks key on THIS checkout's compose file,
    # not on this script: deploy.sh runs from the file on disk at invocation, so a newer script
    # can run against an older tree (2026-10-14: a checkout left on the Guardian branch deployed
    # main, and the unguarded check below aborted the run after the API had already moved).
    local guardian_declared=0
    if compose -p autobuy-platform --profile "$PLATFORM_PROFILE" config --services 2>/dev/null | grep -qx guardian; then
        guardian_declared=1
    else
        echo "==> [platform] docker-compose.yml declares no guardian service in this checkout — the worker checks are skipped"
    fi

    local before_id before_created expected_commit expected_tree guardian_before_id
    before_id="$($SUDO docker inspect --format '{{.Id}}' autobuy_api 2>/dev/null || echo none)"
    before_created="$($SUDO docker inspect --format '{{.Created}}' autobuy_api 2>/dev/null || echo none)"
    guardian_before_id="$($SUDO docker inspect --format '{{.Id}}' autobuy_guardian 2>/dev/null || echo none)"
    expected_commit="$(git rev-parse HEAD)"

    # 0. the web bundle: shipped from --web, or already on disk and built from this checkout.
    #    Checked BEFORE the API moves, so a stale SPA never rides along with a new API.
    # shellcheck source=scripts/lib/platform_web_checks.sh
    . "$(dirname "${BASH_SOURCE[0]:-$0}")/lib/platform_web_checks.sh" 2>/dev/null || . scripts/lib/platform_web_checks.sh
    expected_tree="$(frontend_tree_of .)"
    if [ -n "$WEB_BUNDLE" ]; then
        echo "==> [platform] Checking the web bundle at $WEB_BUNDLE against this checkout's frontend/ tree ${expected_tree:0:12} ..."
        check_web_bundle "$expected_tree" "$WEB_BUNDLE" || exit 1
        # Install IN PLACE. nginx bind-mounts frontend/dist by inode: renaming the directory
        # away leaves the container serving the old files from their new name (seen on the
        # first --web deploy, 2026-09-17), so the directory itself must never be replaced.
        mkdir -p frontend/dist
        rm -rf frontend/dist.prev && mkdir -p frontend/dist.prev && cp -a frontend/dist/. frontend/dist.prev/
        if command -v rsync >/dev/null 2>&1; then
            rsync -a --delete "$WEB_BUNDLE"/ frontend/dist/
        else
            find frontend/dist -mindepth 1 -delete && cp -a "$WEB_BUNDLE"/. frontend/dist/
        fi
        echo "==> [platform] Web bundle installed into frontend/dist in place (previous copy at frontend/dist.prev)"
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

    # 1b. the Guardian worker (v0.9.1) must have been recreated on the SAME image as the API:
    #     it imports api/config.py, so an old worker next to a new API is a half-deployed box.
    if [ "$guardian_declared" = 1 ]; then
        local guardian_after_id api_image guardian_image
        guardian_after_id="$($SUDO docker inspect --format '{{.Id}}' autobuy_guardian 2>/dev/null || echo none)"
        if [ "$guardian_after_id" = "none" ]; then
            echo "ERROR: no autobuy_guardian container after 'up -d' — the Guardian worker did not start." >&2
            exit 1
        fi
        if [ "$guardian_after_id" = "$guardian_before_id" ]; then
            echo "ERROR: the autobuy_guardian container was not recreated (same id ${guardian_after_id:0:12})." >&2
            exit 1
        fi
        api_image="$($SUDO docker inspect --format '{{.Image}}' autobuy_api 2>/dev/null || echo api)"
        guardian_image="$($SUDO docker inspect --format '{{.Image}}' autobuy_guardian 2>/dev/null || echo guardian)"
        if [ "$api_image" != "$guardian_image" ]; then
            echo "ERROR: autobuy_guardian runs image ${guardian_image:0:19} but autobuy_api runs ${api_image:0:19} — the worker is not on the deployed build." >&2
            exit 1
        fi
        echo "==> [platform] guardian container recreated: ${guardian_before_id:0:12} -> ${guardian_after_id:0:12} (same image as the API)"
    fi

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
                echo "       If frontend/dist was ever renamed or recreated, the bind mount still points at the old inode:" >&2
                echo "       $SUDO docker compose -p autobuy-platform --profile $PLATFORM_PROFILE up -d --force-recreate web" >&2
                exit 1
            fi
            if [ "$served_tree" != "$expected_tree" ]; then
                echo "ERROR: nginx serves a web bundle built from frontend tree ${served_tree:0:12}, not ${expected_tree:0:12}." >&2
                exit 1
            fi
            echo "==> [platform] Web app on :8080 serves the bundle built from this checkout (frontend tree ${served_tree:0:7})"
            # 4. the Guardian worker (v0.9.1) must be alive: /health.guardian.running is true once its
            #    heartbeat is younger than 180 s, whether GUARDIAN_ENABLED is on or off.
            if [ "$guardian_declared" = 1 ]; then
                wait_for_guardian_heartbeat || exit 1
            fi
            return 0
        fi
        sleep 4
    done
    echo "ERROR: API did not become healthy within 2 minutes. Recent logs:" >&2
    compose -p autobuy-platform --profile "$PLATFORM_PROFILE" logs --tail 40 api >&2 || true
    exit 1
}

wait_for_guardian_heartbeat() {
    local _try health running
    echo "==> [platform] Waiting for the Guardian worker's heartbeat (/health.guardian.running) ..."
    for _try in $(seq 1 24); do
        health="$(curl -fsS --max-time 3 http://127.0.0.1:8000/health 2>/dev/null || true)"
        running="$(printf '%s' "$health" | sed -n 's/.*"guardian": *{[^}]*"running": *\([a-z]*\).*/\1/p')"
        if [ "$running" = "true" ]; then
            echo "==> [platform] Guardian worker is alive (heartbeat fresh)"
            return 0
        fi
        sleep 5
    done
    echo "ERROR: the Guardian worker did not report a fresh heartbeat within 2 minutes (/health.guardian.running stayed '${running:-<none>}'). Recent logs:" >&2
    compose -p autobuy-platform --profile "$PLATFORM_PROFILE" logs --tail 40 guardian >&2 || true
    return 1
}

deploy_platform

echo "==> Pruning dangling images older than 48h (rollback image is tagged, so it survives) ..."
$SUDO docker image prune -f --filter "until=48h" >/dev/null || true

echo "==> Deploy complete. Containers:"
$SUDO docker ps --format '      {{.Names}}\t{{.Status}}'
