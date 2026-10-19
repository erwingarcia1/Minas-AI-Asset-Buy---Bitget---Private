#!/usr/bin/env bash
# The SPA is static files served by nginx from frontend/dist (a bind mount).
# `deploy.sh platform` rebuilds only the API image, so without this check a
# deploy could report success while the served web app is an older release.
# Every build stamps frontend/dist/BUILD_INFO (frontend/scripts/stamp-build.mjs)
# with the frontend tree hash it was built from; the deploy refuses to complete
# unless the bundle on disk was built from the checkout's frontend/ tree.

# check_web_bundle EXPECTED_TREE DIST_DIR -> 0 when DIST_DIR was built from EXPECTED_TREE
check_web_bundle() {
    local expected="$1" dist="$2" stamp built
    stamp="$dist/BUILD_INFO"
    if [ ! -f "$dist/index.html" ]; then
        echo "ERROR: no web bundle at $dist (index.html missing)." >&2
        return 1
    fi
    if [ ! -f "$stamp" ]; then
        echo "ERROR: $stamp is missing — the bundle predates build stamping, so its release cannot be verified." >&2
        return 1
    fi
    built="$(sed -n 's/^frontend_tree=//p' "$stamp" | head -1)"
    if [ -z "$built" ]; then
        echo "ERROR: $stamp carries no frontend_tree line." >&2
        return 1
    fi
    if [ "$built" != "$expected" ]; then
        echo "ERROR: the web bundle at $dist was built from frontend tree ${built:0:12}, but this checkout's frontend/ is ${expected:0:12}." >&2
        return 1
    fi
    return 0
}

# frontend_tree_of CHECKOUT_DIR -> the git tree hash of frontend/ at HEAD
frontend_tree_of() {
    git -C "$1" rev-parse "HEAD:frontend"
}
