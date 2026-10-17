"""Client model catalogs: read-only, newest-version selection, failures read as empty."""
import json
import re
import sqlite3

import pytest

from src.engine import model_catalogs


@pytest.fixture(autouse=True)
def fresh_cache():
    model_catalogs.clear_cache()
    yield
    model_catalogs.clear_cache()


def write_codex(path, models):
    path.write_text(json.dumps({"fetched_at": "2026-10-17T00:00:00Z", "models": models}), encoding="utf-8")
    return path


def write_cursor(path, value):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        db.execute("INSERT INTO ItemTable VALUES (?, ?)", (model_catalogs.CURSOR_STATE_KEY, value))
    return path


@pytest.mark.parametrize("slugs,pattern,expected", [
    (["gpt-6-sol", "gpt-6.1-sol", "gpt-5.6-sol"], r"gpt-(\d+(?:\.\d+)*)-sol", "gpt-6.1-sol"),
    (["gpt-5.6-luna", "gpt-6-luna", "gpt-6.1-sol"], r"gpt-(\d+(?:\.\d+)*)-luna", "gpt-6-luna"),
    (["composer-2.5", "composer-10", "composer-2.5-fast"], r"composer-(\d+(?:\.\d+)*)", "composer-10"),
    (["gpt-6-astra"], r"gpt-(\d+(?:\.\d+)*)-luna", None),
    ([], r"composer-(\d+(?:\.\d+)*)", None),
])
def test_newest_compares_numeric_versions(slugs, pattern, expected):
    assert model_catalogs.newest(slugs, re.compile(pattern)) == expected


def test_codex_lists_only_visible_models(tmp_path):
    path = write_codex(tmp_path / "models_cache.json", [
        {"slug": "gpt-6-luna", "visibility": "list"},
        {"slug": "gpt-7-luna", "visibility": "hide"},
        {"slug": 7, "visibility": "list"},
        "not a model",
    ])
    assert model_catalogs.codex_models(path) == ["gpt-6-luna"]


def test_codex_defaults_to_the_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    write_codex(tmp_path / "models_cache.json", [{"slug": "gpt-6-luna", "visibility": "list"}])
    assert model_catalogs.codex_models() == ["gpt-6-luna"]


@pytest.mark.parametrize("content", [None, "{not json", json.dumps(["list"]), json.dumps({"models": "x"})])
def test_codex_failures_read_as_empty(tmp_path, content):
    path = tmp_path / "models_cache.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert model_catalogs.codex_models(path) == []


def test_cursor_reads_model_names_read_only(tmp_path):
    value = json.dumps({"availableDefaultModels2": [
        {"serverModelName": "default"}, {"serverModelName": "composer-2.5"}, {"name": "no server name"}]})
    path = write_cursor(tmp_path / "state with space.vscdb", value.encode("utf-8"))
    before = path.read_bytes()
    assert model_catalogs.cursor_models(path) == ["default", "composer-2.5"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", ["{broken", json.dumps({"other": 1}), json.dumps([1])])
def test_cursor_failures_read_as_empty(tmp_path, value):
    assert model_catalogs.cursor_models(write_cursor(tmp_path / "state.vscdb", value)) == []
    assert model_catalogs.cursor_models(tmp_path / "missing.vscdb") == []
    (tmp_path / "garbage.vscdb").write_bytes(b"not a database")
    assert model_catalogs.cursor_models(tmp_path / "garbage.vscdb") == []


def test_reads_are_cached_per_path(tmp_path, monkeypatch):
    path = write_codex(tmp_path / "models_cache.json", [{"slug": "gpt-6-luna", "visibility": "list"}])
    assert model_catalogs.codex_models(path) == ["gpt-6-luna"]
    write_codex(path, [{"slug": "gpt-7-luna", "visibility": "list"}])
    assert model_catalogs.codex_models(path) == ["gpt-6-luna"]
    monkeypatch.setattr(model_catalogs, "CACHE_SECONDS", 0)
    assert model_catalogs.codex_models(path) == ["gpt-7-luna"]


@pytest.mark.parametrize("platform,variable,expected", [
    ("darwin", None, "Library/Application Support/Cursor/User/globalStorage/state.vscdb"),
    ("linux", "XDG_CONFIG_HOME", "xdg/Cursor/User/globalStorage/state.vscdb"),
    ("win32", "APPDATA", "xdg/Cursor/User/globalStorage/state.vscdb"),
])
def test_cursor_state_path_per_platform(tmp_path, monkeypatch, platform, variable, expected):
    monkeypatch.setattr(model_catalogs.sys, "platform", platform)
    monkeypatch.setattr(model_catalogs.Path, "home", lambda: tmp_path)
    for name in ("XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.delenv(name, raising=False)
    if variable:
        monkeypatch.setenv(variable, str(tmp_path / "xdg"))
    assert model_catalogs.cursor_state_path() == tmp_path / expected
