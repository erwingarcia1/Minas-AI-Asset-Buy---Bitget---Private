FROM python:3.11-slim

# Run as non-root; the bot needs no privileges.
RUN useradd --create-home --shell /usr/sbin/nologin autobuy
WORKDIR /app

# Legacy bot + web UI need only requests/flask — NOT the platform API's
# requirements.txt (which no longer carries flask and would bloat this image).
COPY requirements-legacy.txt .
RUN pip install --no-cache-dir -r requirements-legacy.txt

# The on-chain subsystem's heavy dependencies (web3, solders, solana) are NOT
# installed here — the spot bot and web UI never import them, and leaving them
# out keeps this image small. `memebuy_cli.py --check-token` still works, since
# the safety gate needs only `requests`. To sign transactions from inside the
# container, uncomment the two lines below and rebuild:
# COPY requirements-onchain.txt .
# RUN pip install --no-cache-dir -r requirements-onchain.txt

COPY exchange_errors.py bitget_client.py bybit_client.py okx_client.py binance_client.py autobuy.py ./
COPY webapp/ ./webapp/

# On-chain meme-buy subsystem. Copied but never auto-started: nothing in
# autobuy.py imports it, and it only runs via memebuy_cli.py.
COPY payout/ ./payout/
COPY chains/ ./chains/
COPY safety/ ./safety/
COPY memebuy/ ./memebuy/
COPY memebuy_cli.py ./

USER autobuy

# Unbuffered so logs stream straight to `docker logs` / cloud log collectors.
ENV PYTHONUNBUFFERED=1

# docker-compose.legacy.yml mounts ./data and points AUTOBUY_CONFIG at
# /app/data/config.json. Secrets can stay in env vars via ${VAR} references.
ENTRYPOINT ["python", "autobuy.py"]
