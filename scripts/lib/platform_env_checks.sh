#!/usr/bin/env bash
# Pre-deploy checks for the platform API's .env on a production host.
# Sourced by scripts/deploy.sh; unit-tested by tests/api/test_production_env.py.
#
#   check_platform_env <env-file>   -> prints "ERROR: ..." lines, returns 1 on the first problem
#
# Set PLATFORM_ENV_SUDO=sudo when the file is root-owned.

platform_envval() {  # platform_envval FILE KEY -> value with surrounding quotes stripped (last occurrence wins)
    ${PLATFORM_ENV_SUDO:-} grep -E "^$2=" "$1" | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

platform_lower() { printf '%s' "$1" | tr 'A-Z' 'a-z'; }

check_platform_env() {
    local f="$1" v lv
    if ! ${PLATFORM_ENV_SUDO:-} test -f "$f"; then
        echo "ERROR: $f not found — the platform's secrets live there. Create it from .env.production.example." >&2; return 1
    fi
    # --- the host must declare itself production, exactly -------------------
    v="$(platform_envval "$f" API_ENV)"
    if [ "$v" != "production" ]; then
        echo "ERROR: API_ENV must be exactly 'production' on the server (is '${v:-<unset>}') — the boot guard, HSTS and Alembic-only schema management all key on that exact value." >&2; return 1
    fi
    v="$(platform_envval "$f" REQUIRE_PRODUCTION)"
    case "$(platform_lower "$v")" in
        1|true|yes) ;;
        *) echo "ERROR: REQUIRE_PRODUCTION=1 must be set in $f on the server so the API refuses to start under any other API_ENV (is '${v:-<unset>}')." >&2; return 1;;
    esac
    # --- secrets ---------------------------------------------------------------
    for var in SECRET_KEY ENCRYPTION_KEY; do
        v="$(platform_envval "$f" "$var")"
        if [ "${#v}" -lt 2 ]; then
            echo "ERROR: $var is not set in $f — the platform API refuses to boot without it." >&2
            echo "       Generate one with:  openssl rand -hex 32   then add '${var}=<value>' and re-run." >&2; return 1
        fi
    done
    # --- email + links (the API's boot guard refuses these too) ----------------
    v="$(platform_envval "$f" RESEND_API_KEY)"
    case "$(platform_lower "$v")" in
        ""|your_*|*change_me*|*placeholder*|xxx)
            echo "ERROR: RESEND_API_KEY is empty or a placeholder in $f — set the Resend API key (https://resend.com/api-keys)." >&2; return 1;;
    esac
    v="$(platform_envval "$f" RESEND_FROM_EMAIL)"
    if [ -z "$v" ]; then
        echo "ERROR: RESEND_FROM_EMAIL is empty in $f — set a sender on a domain verified in Resend, e.g. 'Minas AutoBuy <noreply@minastrade.com>'." >&2; return 1
    fi
    case "$(platform_lower "$v")" in
        *resend.dev*|*autobuy.platform*)
            echo "ERROR: RESEND_FROM_EMAIL ($v) uses a domain that cannot deliver to customers — use a domain verified in Resend." >&2; return 1;;
    esac
    v="$(platform_envval "$f" APP_BASE_URL)"; lv="$(platform_lower "$v")"
    if [ -z "$v" ]; then
        echo "ERROR: APP_BASE_URL is empty in $f — set the public https origin of the web app (every emailed link is built from it)." >&2; return 1
    fi
    case "$lv" in https://*) ;; *) echo "ERROR: APP_BASE_URL must start with https:// (is '$v')." >&2; return 1;; esac
    case "$lv" in *localhost*|*127.0.0.1*) echo "ERROR: APP_BASE_URL points at localhost/127.0.0.1 ('$v') — emailed links would not reach the app." >&2; return 1;; esac
    case "$(platform_lower "$(platform_envval "$f" LOG_RESET_LINKS)")" in
        1|true|yes) echo "ERROR: LOG_RESET_LINKS=1 writes password reset links to the log — it must be unset or 0 in production." >&2; return 1;;
    esac
    return 0
}
