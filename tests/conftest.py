"""Shared fixtures: every test gets throwaway HEXIEL_DATA and HEXIEL_HOME dirs so
sessions, memory, incidents, jobs and config never touch the user's real files."""

import pytest


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path_factory, monkeypatch):
    data = tmp_path_factory.mktemp("hexiel-data")
    home = tmp_path_factory.mktemp("hexiel-home")
    monkeypatch.setenv("HEXIEL_DATA", str(data))
    monkeypatch.setenv("HEXIEL_HOME", str(home))
    monkeypatch.setenv("HEXIEL_NO_REPORTS", "1")        # tests must never send error reports anywhere
    for key in ("OLLAMA_API_KEY", "ANTHROPIC_ADMIN_KEY", "OPENAI_ADMIN_KEY"):
        monkeypatch.delenv(key, raising=False)          # nor ask a real provider what was spent (spend.py)
    import hexiel.config as config
    monkeypatch.setattr(config, "CONFIG_FILE", home / "config.toml")   # resolved at import time
