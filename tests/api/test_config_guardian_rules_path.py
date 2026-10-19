"""An empty GUARDIAN_RULES_PATH means the rules file baked into the image.

docker-compose passes the variable through as '' when the server .env does not set it,
and pydantic-settings keeps that '' over the Python default; RulesConfig('') would open
'.' as the rules file and crash the worker the moment GUARDIAN_ENABLED is 1."""
import os
import pathlib

from api.config import Settings


def test_an_empty_rules_path_resolves_to_the_shipped_file(monkeypatch):
    monkeypatch.setenv("GUARDIAN_RULES_PATH", "")
    monkeypatch.setenv("AUTOBUY_SKIP_DOTENV", "1")
    s = Settings()
    assert s.guardian_rules_path.endswith(os.path.join("guardian", "rules.yaml"))
    assert pathlib.Path(s.guardian_rules_path).is_file()


def test_a_configured_rules_path_is_kept(monkeypatch, tmp_path):
    custom = tmp_path / "rules.yaml"
    custom.write_text("{}")
    monkeypatch.setenv("GUARDIAN_RULES_PATH", str(custom))
    monkeypatch.setenv("AUTOBUY_SKIP_DOTENV", "1")
    assert Settings().guardian_rules_path == str(custom)
