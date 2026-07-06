#!/usr/bin/env bash
# One-shot server setup for the Bitget auto-buy bot on a fresh Ubuntu box
# (AWS EC2, Alibaba ECS, any VPS). Run from the repo root after cloning:
#
#     bash deploy/bootstrap.sh
#
# Installs Docker, prepares config.json/.env templates, builds the image,
# and prints the exact next steps. Safe to re-run; it never overwrites an
# existing config.json or .env and never starts live trading by itself.
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

echo "==> Checking clock sync (Bitget rejects skewed timestamps)..."
if command -v timedatectl >/dev/null 2>&1; then
    timedatectl show -p NTPSynchronized --value | grep -q yes \
        && echo "    clock is NTP-synchronized" \
        || echo "    WARNING: clock not NTP-synchronized — run: sudo timedatectl set-ntp true"
fi

echo "==> Preparing config files..."
if [ ! -f config.json ]; then
    cp config.example.json config.json
    echo "    created config.json from template — EDIT IT before running"
else
    echo "    config.json already exists, leaving it alone"
fi

if [ ! -f .env ]; then
    cat > .env <<'EOF'
# Bitget sub-account API credentials, referenced from config.json as ${VAR}.
# Example — one set per sub-account:
#SUB1_API_KEY=
#SUB1_API_SECRET=
#SUB1_API_PASSPHRASE=
EOF
    chmod 600 .env
    echo "    created .env template (chmod 600) — put your API keys here"
else
    echo "    .env already exists, leaving it alone"
fi

echo "==> Building the Docker image..."
docker compose build --quiet

cat <<'EOF'

==========================================================================
 Setup complete. Next steps:

 1. Edit your settings:      nano config.json     (coins, thresholds;
                                                    use ${VAR} for secrets)
                             nano .env             (the actual API keys)

 2. Dry-run (no orders):     docker compose run --rm autobuy --once --dry-run

 3. Go live:                 docker compose up -d
    Watch logs:              docker compose logs -f
    Stop:                    docker compose down
==========================================================================
EOF
