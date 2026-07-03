FROM python:3.11-slim

# Run as non-root; the bot needs no privileges.
RUN useradd --create-home --shell /usr/sbin/nologin autobuy
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bitget_client.py autobuy.py ./

USER autobuy

# Unbuffered so logs stream straight to `docker logs` / cloud log collectors.
ENV PYTHONUNBUFFERED=1

# Mount your config at /app/config.json (see docker-compose.yml), or point
# AUTOBUY_CONFIG elsewhere. Secrets can stay in env vars via ${VAR} references.
ENTRYPOINT ["python", "autobuy.py"]
