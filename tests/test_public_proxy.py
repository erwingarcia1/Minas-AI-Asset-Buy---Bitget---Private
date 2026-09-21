"""The public entry point: caddy (TLS) -> web (nginx) -> api.

The API's per-IP rate limits key on request.client, which gunicorn takes from
X-Forwarded-For because it trusts every peer. That is only sound while nginx
overwrites the header with an address it verified and nothing but caddy is
published on a public interface. These checks pin that arrangement.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "docker-compose.yml").read_text()
NGINX = (ROOT / "deployment" / "nginx.web.conf").read_text()


def test_nginx_overwrites_forwarded_for_instead_of_appending():
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in NGINX
    assert "$proxy_add_x_forwarded_for" not in NGINX, "appending lets a client-supplied value reach the API"
    assert "real_ip_header X-Forwarded-For;" in NGINX and "real_ip_recursive on;" in NGINX


def test_nginx_takes_the_real_ip_from_private_peers_only():
    for cidr in re.findall(r"set_real_ip_from\s+([^;]+);", NGINX):
        assert cidr.startswith(("172.16.", "192.168.", "10.")), f"{cidr} is not a private range"


def test_gunicorn_trusts_the_proxy_header():
    assert '"--forwarded-allow-ips", "*"' in (ROOT / "Dockerfile.api").read_text()


def test_only_caddy_is_published_on_a_public_interface():
    public = [p for p in re.findall(r'^\s+- "([^"]+:\d+(?:/udp)?)"$', COMPOSE, re.M) if not p.startswith("127.0.0.1:")]
    bind = "${PUBLIC_BIND_IP:-0.0.0.0}"   # an operator may narrow it (tailscaled holds :443 on the tailnet address)
    assert sorted(public) == [f"{bind}:443:443", f"{bind}:80:80"], public
    caddy = COMPOSE[COMPOSE.index("\n  caddy:"):COMPOSE.index("\n  # Optional metrics scraper")]
    assert all(p in caddy for p in public) and "caddy_data:/data" in caddy


def test_caddy_proxies_to_web_and_nothing_else():
    caddyfile = (ROOT / "deployment" / "caddy" / "Caddyfile").read_text()
    assert re.findall(r"^\s*reverse_proxy\s+(.+)$", caddyfile, re.M) == ["web:80"]
    assert "trusted_proxies" not in caddyfile, "caddy must keep discarding client-sent X-Forwarded-*"
