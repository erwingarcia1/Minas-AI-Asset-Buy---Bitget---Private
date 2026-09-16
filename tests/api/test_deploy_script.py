"""scripts/deploy.sh must verify a deploy rather than assume it: the compose
profile must carry the api service, the container must be recreated, and the
running commit must match the checkout. /health exposes that commit."""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "scripts" / "deploy.sh"


def test_deploy_script_parses():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_platform_profile_is_prod_and_never_a_lookalike():
    text = SCRIPT.read_text()
    assert 'PLATFORM_PROFILE="prod"' in text
    assert "--profile production" not in text and "--profile prod " not in text.replace('"$PLATFORM_PROFILE"', "")
    compose = (ROOT / "docker-compose.yml").read_text()
    assert 'profiles: ["prod"]' in compose


def test_deploy_script_refuses_a_profile_without_the_api_service():
    text = SCRIPT.read_text()
    assert "config --services" in text and "grep -qx api" in text
    assert "does not include the api service" in text


def test_deploy_script_asserts_the_container_was_recreated_and_the_commit_matches():
    text = SCRIPT.read_text()
    assert "docker inspect --format '{{.Id}}' autobuy_api" in text
    assert "was not recreated" in text and "Nothing was deployed" in text
    assert 'GIT_COMMIT="$expected_commit"' in text, "the build is stamped with the checkout's commit"
    assert '"commit": *"' in text and "is not running the deployed code" in text


def test_image_is_stamped_with_the_commit_and_health_reports_it(monkeypatch, tmp_path):
    dockerfile = (ROOT / "Dockerfile.api").read_text()
    assert "ARG GIT_COMMIT" in dockerfile and "/app/GIT_COMMIT" in dockerfile
    compose = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r"GIT_COMMIT:\s*\$\{GIT_COMMIT:-unknown\}", compose)
    from api import main as main_mod
    assert main_mod.build_commit() == "unknown", "no stamp outside a built image"
    stamp = ROOT / "GIT_COMMIT"
    assert not stamp.exists(), "a checkout must not carry a stamp file"


def test_api_binds_loopback_only():
    compose = (ROOT / "docker-compose.yml").read_text()
    api_block = compose[compose.index("\n  api:"):compose.index("\n  # Optional metrics scraper")]
    assert '"127.0.0.1:8000:8000"' in api_block and '"8000:8000"' not in api_block.replace("127.0.0.1:8000:8000", "")


async def test_health_exposes_the_build_commit(client):
    async with client as c:
        r = await c.get("/health")
    assert r.status_code == 200 and r.json()["commit"] == "unknown"
