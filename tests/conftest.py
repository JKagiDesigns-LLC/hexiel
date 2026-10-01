"""Shared fixtures: every test gets a throwaway HEXIEL_DATA dir so sessions,
memory and incidents never leak into the user's real ~/.local/share/hexiel."""

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HEXIEL_DATA", str(tmp_path_factory.mktemp("hexiel-data")))
