"""Production hosts must declare API_ENV exactly "production": boot guard + deploy checks."""
import os
import pathlib
import subprocess
import textwrap

import pytest

from api.config import Settings, enforce_production_secrets

ROOT = pathlib.Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "lib" / "platform_env_checks.sh"
GOOD = dict(_env_file=None, api_env="production", secret_key="s" * 40, encryption_key="e" * 40,
            resend_api_key="re_live_ok", resend_from_email="Minas AutoBuy <noreply@example.com>",
            app_base_url="https://app.example.com", log_reset_links="", require_production="1")


# --------------------------------------------------------------------------- boot guard

@pytest.mark.parametrize("value", ["Production", "PRODUCTION", "prod", "live", "production ", " production"])
def test_boot_guard_rejects_lookalikes_of_production(value):
    with pytest.raises(RuntimeError, match="API_ENV must be exactly 'production'"):
        enforce_production_secrets(Settings(**{**GOOD, "api_env": value}))


@pytest.mark.parametrize("value", ["development", "staging", "test", ""])
def test_require_production_refuses_any_other_env(value):
    with pytest.raises(RuntimeError, match="REQUIRE_PRODUCTION is set but API_ENV is"):
        enforce_production_secrets(Settings(**{**GOOD, "api_env": value}))


def test_exact_production_with_good_config_starts():
    enforce_production_secrets(Settings(**GOOD))  # no raise


def test_dev_box_without_require_production_is_unguarded():
    enforce_production_secrets(Settings(**{**GOOD, "api_env": "development", "require_production": "", "resend_api_key": ""}))


# --------------------------------------------------------------------------- scripts/deploy.sh checks

GOOD_ENV = textwrap.dedent("""
    API_ENV=production
    REQUIRE_PRODUCTION=1
    SECRET_KEY={s}
    ENCRYPTION_KEY={e}
    RESEND_API_KEY=re_live_ok
    RESEND_FROM_EMAIL="Minas AutoBuy <noreply@example.com>"
    APP_BASE_URL=https://app.example.com
    LOG_RESET_LINKS=0
""").format(s="s" * 40, e="e" * 40)


def run_check(env_text: str) -> subprocess.CompletedProcess:
    f = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / f"platform-env-{os.getpid()}.env"
    f.write_text(env_text)
    try:
        return subprocess.run(["bash", "-c", f'. "{LIB}" && check_platform_env "$1"', "_", str(f)],
                              capture_output=True, text=True, timeout=30)
    finally:
        f.unlink(missing_ok=True)


def test_deploy_check_accepts_a_good_production_env():
    r = run_check(GOOD_ENV)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("replace, expect", [
    (("API_ENV=production", "API_ENV=development"), "API_ENV must be exactly 'production'"),
    (("API_ENV=production", "API_ENV=Production"), "API_ENV must be exactly 'production'"),
    (("API_ENV=production", "# no API_ENV"), "API_ENV must be exactly 'production'"),
    (("REQUIRE_PRODUCTION=1", "REQUIRE_PRODUCTION="), "REQUIRE_PRODUCTION=1 must be set"),
    (("RESEND_API_KEY=re_live_ok", "RESEND_API_KEY=re_CHANGE_ME"), "RESEND_API_KEY is empty or a placeholder"),
    (("noreply@example.com", "onboarding@resend.dev"), "cannot deliver to customers"),
    (("https://app.example.com", "http://app.example.com"), "APP_BASE_URL must start with https://"),
    (("https://app.example.com", "https://localhost:5173"), "localhost"),
    (("LOG_RESET_LINKS=0", "LOG_RESET_LINKS=1"), "LOG_RESET_LINKS=1"),
    (("SECRET_KEY=" + "s" * 40, "SECRET_KEY="), "SECRET_KEY is not set"),
])
def test_deploy_check_refuses_each_bad_value(replace, expect):
    r = run_check(GOOD_ENV.replace(*replace))
    assert r.returncode == 1 and expect in r.stderr, r.stderr


def test_deploy_sh_uses_the_library():
    text = (ROOT / "scripts" / "deploy.sh").read_text()
    assert "platform_env_checks.sh" in text and "check_platform_env .env || exit 1" in text
