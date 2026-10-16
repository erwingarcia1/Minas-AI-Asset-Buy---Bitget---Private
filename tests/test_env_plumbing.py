"""Guard against settings drift: every environment variable the code reads must
be listed in .env.example (documented) AND passed through docker-compose.yml's
api environment list (docker compose does not forward .env to the container
by itself, so a variable missing from that list is silently ignored in
production — which is how BITGET_WITHDRAW_FEE_MODE went unread). Breaks CI
on drift in either direction."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Set by the compose file itself or by the runtime, never by an operator:
COMPOSE_PROVIDED = {"DATABASE_URL", "REDIS_URL"}
# Test-only or developer-only switches that must NOT be in the container:
DEV_ONLY = {"AUTOBUY_SKIP_DOTENV", "TEST_DATABASE_URL", "TEST_REDIS_URL"}
# Operator knobs for the compose file / deploy script rather than the app:
COMPOSE_ONLY = {"POSTGRES_PASSWORD", "SKIP_MIGRATIONS"}


def env_vars_read_by_code() -> set:
    names = set()
    for f in (ROOT / "api").rglob("*.py"):
        text = f.read_text()
        names.update(re.findall(r'os\.(?:getenv|environ\.get)\(\s*"([A-Z0-9_]+)"', text))
        names.update(re.findall(r'os\.environ\[\s*"([A-Z0-9_]+)"\s*\]', text))
    return names - DEV_ONLY


def env_example_keys() -> set:
    return set(re.findall(r"^([A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))


def compose_service_env_keys(service: str) -> set:
    compose = (ROOT / "docker-compose.yml").read_text()
    start = compose.index(f"\n  {service}:")
    # the service block runs until the next top-level service (two-space indent)
    block = compose[start:]
    nxt = re.search(r"\n  [a-z_-]+:\n", block[6:])
    block = block[: nxt.start() + 6] if nxt else block
    return set(re.findall(r"^\s{6}([A-Z0-9_]+):", block, re.M))


def compose_api_env_keys() -> set:
    return compose_service_env_keys("api")


def test_guardian_worker_carries_every_api_setting():
    """The worker imports api/config.py, so a variable missing from its block silently means the
    code default — the same failure this file exists to prevent for the api container."""
    api_keys = compose_api_env_keys()
    guardian_keys = compose_service_env_keys("guardian")
    assert api_keys - guardian_keys == set(), f"in the api block but not the guardian block: {sorted(api_keys - guardian_keys)}"
    assert guardian_keys - api_keys == {"SKIP_MIGRATIONS"}, "the worker adds only SKIP_MIGRATIONS (the api container migrates)"


def test_every_setting_the_code_reads_is_documented_in_env_example():
    missing = env_vars_read_by_code() - env_example_keys() - COMPOSE_PROVIDED
    assert not missing, f"read by the code but absent from .env.example: {sorted(missing)}"


def test_every_setting_the_code_reads_is_passed_through_compose():
    missing = env_vars_read_by_code() - compose_api_env_keys() - COMPOSE_PROVIDED
    assert not missing, (f"read by the code but not in docker-compose.yml's api environment list, so the container "
                         f"would silently use the code default: {sorted(missing)}")


def test_compose_and_env_example_carry_nothing_the_code_ignores():
    read = env_vars_read_by_code() | COMPOSE_PROVIDED | COMPOSE_ONLY
    stray_compose = compose_api_env_keys() - read
    stray_example = env_example_keys() - read
    assert not stray_compose, f"in compose but read by nothing: {sorted(stray_compose)}"
    assert not stray_example, f"in .env.example but read by nothing: {sorted(stray_example)}"


def test_compose_passes_settings_in_interpolated_style():
    """Every value is `${VAR:-default}` (or a fixed compose-internal URL), never a hard-coded literal,
    with one deliberate exception: the worker's SKIP_MIGRATIONS is the literal "true"."""
    compose = (ROOT / "docker-compose.yml").read_text()
    block = compose[compose.index("\n  api:"):]
    for name, value in re.findall(r"^\s{6}([A-Z0-9_]+):\s*(.+)$", block, re.M):
        if name in COMPOSE_PROVIDED:
            continue
        if name == "SKIP_MIGRATIONS":
            # The worker never migrates (the api container does, at boot). A literal, so no
            # .env value can make the worker a second migrator next to the API.
            assert value.split("#")[0].strip() == '"true"', f'SKIP_MIGRATIONS must be the literal "true", got {value!r}'
            continue
        assert re.fullmatch(r"\$\{" + name + r":-[^}]*\}", value.strip()), f"{name}: expected ${{{name}:-default}}, got {value!r}"
