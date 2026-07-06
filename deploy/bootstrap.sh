#!/usr/bin/env bash
# One-shot server setup for the Bitget auto-buy bot + admin web UI on a
# fresh Ubuntu box (AWS EC2, Alibaba ECS, any VPS). Run from the repo root:
#
#     bash deploy/bootstrap.sh
#
# Installs Docker and Tailscale, prepares config/.env templates, builds the
# image, and prints the next steps. Safe to re-run; never overwrites an
# existing config or .env and never starts live trading by itself.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Installing Docker + compose (sudo required)..."
if ! command -v docker >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker.io docker-compose-v2
    sudo systemctl enable --now docker
else
    echo "    docker already installed, skipping"
fi

if ! docker info >/dev/null 2>&1; then
    sudo usermod -aG docker "$USER"
    echo "    NOTE: added $USER to the docker group — log out and back in,"
    echo "    then re-run this script."
    exit 0
fi

echo "==> Installing Tailscale (private access to the web UI)..."
if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
else
    echo "    tailscale already installed, skipping"
fi

echo "==> Checking clock sync (Bitget rejects skewed timestamps)..."
if command -v timedatectl >/dev/null 2>&1; then
    timedatectl show -p NTPSynchronized --value | grep -q yes \
        && echo "    clock is NTP-synchronized" \
        || echo "    WARNING: clock not NTP-synchronized — run: sudo timedatectl set-ntp true"
fi

echo "==> Preparing config files..."
mkdir -p data
if [ ! -f data/config.json ]; then
    cp config.example.json data/config.json
    echo "    created data/config.json — or manage it from the web UI instead"
else
    echo "    data/config.json already exists, leaving it alone"
fi

if [ ! -f .env ]; then
    cat > .env <<'EOF'
# Web UI password (recommended even on the tailnet, since VAs share the URL)
#WEB_PASSWORD=

# Bitget sub-account API credentials, matched by sub-account name:
# a sub-account called "sub1" in the UI expects SUB1_API_KEY / _SECRET /
# _API_PASSPHRASE ("my-sub 2" would expect MY_SUB_2_API_KEY, etc.)
#SUB1_API_KEY=
#SUB1_API_SECRET=
#SUB1_API_PASSPHRASE=
EOF
    chmod 600 .env
    echo "    created .env template (chmod 600) — API keys and UI password go here"
else
    echo "    .env already exists, leaving it alone"
fi

echo "==> Building the Docker image..."
docker compose build --quiet

cat <<'EOF'

==========================================================================
 Setup complete. Next steps:

 1. Selftest (no API keys needed — checks Bitget connectivity + clock):
        docker compose run --rm autobuy --selftest

 2. Join your Tailscale network and expose the web UI to it:
        sudo tailscale up            # prints a login URL — open it
        sudo tailscale serve --bg 8080

 3. Start everything:
        docker compose up -d

    Web UI (tailnet only):  https://<machine-name>.<tailnet>.ts.net
    Your VAs get access by being invited to your tailnet at
    https://login.tailscale.com/admin/users — nothing is ever public.

 4. VAs add clients in the UI and press "Apply to bot". Entries without
    API keys show "awaiting keys" and are skipped by the bot until you
    add their keys to .env (then: docker compose restart autobuy).

 5. When ready to trade: add keys to .env, dry-run first:
        docker compose run --rm autobuy --once --dry-run
==========================================================================
EOF
