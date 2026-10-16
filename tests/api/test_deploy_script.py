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


def test_deploy_script_proves_the_guardian_worker_moved_and_is_alive():
    """v0.9.1: the worker container is recreated on the API's image and its heartbeat is fresh
    before the deploy is called done, whether GUARDIAN_ENABLED is on or off."""
    text = SCRIPT.read_text()
    assert "docker inspect --format '{{.Id}}' autobuy_guardian" in text
    assert "the worker is not on the deployed build" in text
    assert "wait_for_guardian_heartbeat" in text and '"guardian": *{' in text
    compose = (ROOT / "docker-compose.yml").read_text()
    guardian_block = compose[compose.index("\n  guardian:"):compose.index("\n  # Web frontend")]
    assert "container_name: autobuy_guardian" in guardian_block and "image: autobuy-api:latest" in guardian_block
    assert "ports:" not in guardian_block, "the worker publishes nothing"
    assert '"python", "-m", "guardian.worker"' in guardian_block
    # Both checks key on the checkout's compose file, so the script stays correct when it runs
    # against a tree without the worker (2026-10-14: a checkout left on the Guardian branch
    # deployed main and the unguarded check aborted the run after the API had moved).
    assert "grep -qx guardian" in text and "guardian_declared=1" in text
    assert "the worker checks are skipped" in text
    body = text[text.index("deploy_platform() {"):text.index("\nwait_for_guardian_heartbeat() {")]
    guards = [m.start() for m in re.finditer(r'if \[ "\$guardian_declared" = 1 \]; then', body)]
    assert len(guards) == 2, "the recreate check and the heartbeat wait are each behind the guard"
    assert guards[0] < body.index("guardian_after_id=") < guards[1] < body.index("wait_for_guardian_heartbeat || exit 1")


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


WEB_LIB = ROOT / "scripts" / "lib" / "platform_web_checks.sh"


def _check(expected: str, dist) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'. "{WEB_LIB}"; check_web_bundle "{expected}" "{dist}"'], capture_output=True, text=True)


def test_deploy_refuses_a_web_bundle_from_another_frontend_tree(tmp_path):
    """The SPA is served from frontend/dist, which the API build never touches.
    The deploy must refuse to complete when that bundle was not built from the
    checkout's frontend/ tree, instead of reporting success with an old UI."""
    dist = tmp_path / "dist"
    dist.mkdir()
    r = _check("a" * 40, dist)
    assert r.returncode == 1 and "index.html missing" in r.stderr
    (dist / "index.html").write_text("<html></html>")
    r = _check("a" * 40, dist)
    assert r.returncode == 1 and "BUILD_INFO is missing" in r.stderr, "an unstamped (pre-v0.6.1) bundle cannot be trusted"
    (dist / "BUILD_INFO").write_text("commit=x\nfrontend_tree=" + "b" * 40 + "\n")
    r = _check("a" * 40, dist)
    assert r.returncode == 1 and "was built from frontend tree bbbbbbbbbbbb" in r.stderr
    (dist / "BUILD_INFO").write_text("commit=x\nfrontend_tree=" + "a" * 40 + "\n")
    assert _check("a" * 40, dist).returncode == 0


def test_deploy_script_checks_the_bundle_before_and_after_moving_the_api():
    text = SCRIPT.read_text()
    assert "--web" in text and 'WEB_BUNDLE="${2:?--web needs a directory}"' in text
    assert "check_web_bundle" in text and "Nothing was deployed" in text
    assert "http://127.0.0.1:8080/BUILD_INFO" in text, "the served bundle is verified through nginx after /health"
    assert text.index("check_web_bundle") < text.index("up -d --build"), "the bundle is checked before the API moves"
    pkg = (ROOT / "frontend" / "package.json").read_text()
    assert "stamp-build.mjs" in pkg, "every frontend build writes dist/BUILD_INFO"


def test_web_bundle_is_installed_in_place_never_by_renaming_the_mounted_directory():
    """nginx bind-mounts frontend/dist by inode; `mv frontend/dist …` leaves the
    container serving the old files (first --web deploy, 2026-09-17)."""
    text = SCRIPT.read_text()
    assert "mv frontend/dist" not in text
    assert 'rsync -a --delete "$WEB_BUNDLE"/ frontend/dist/' in text
    assert "--force-recreate web" in text, "the failure message says how to rebind the mount"


def test_server_address_is_defined_once():
    """The production box's address lives in scripts/lib/server_host.sh only;
    the released auto-assigned address (18.179.57.78) must never come back."""
    lib = (ROOT / "scripts" / "lib" / "server_host.sh").read_text()
    assert 'SERVER_HOST="${SERVER_HOST:-16.76.92.209}"' in lib
    for name in ("server_ssh.sh", "remote_deploy.sh"):
        text = (ROOT / "scripts" / name).read_text()
        assert "scripts/lib/server_host.sh" in text or 'lib/server_host.sh"' in text, name
        assert "16.76.92.209" not in text.replace("scripts/lib/server_host.sh", ""), f"{name} hardcodes the address"
        assert "18.179.57.78" not in text and "ec2-18-179-57-78" not in text, name
    for path in ROOT.rglob("*.sh"):
        if "venv" in path.parts or "node_modules" in path.parts:
            continue
        assert "18.179.57.78" not in path.read_text() and "ec2-18-179-57-78" not in path.read_text(), path
