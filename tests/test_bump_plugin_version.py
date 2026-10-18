"""One calendar release number, using throwaway local Git repositories only."""
from datetime import datetime, timezone
import json
import os
import subprocess
import tomllib

import pytest

from scripts import bump_plugin_version as bump

BASE = "26.10.16.2045"
NEXT = "26.10.18.0805"
NOW = datetime(2026, 10, 18, 8, 5, 49, tzinfo=timezone.utc)


@pytest.fixture
def repository(tmp_path):
    def git(*args):
        environment = {**os.environ, "GIT_AUTHOR_DATE": "2026-10-16T20:45:00Z",
                       "GIT_COMMITTER_DATE": "2026-10-16T20:45:00Z"}
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                              capture_output=True, text=True, env=environment).stdout.strip()
    git("init", "-b", "main")
    git("config", "user.name", "Calendar version test")
    git("config", "user.email", "test@example.invalid")
    path = tmp_path / bump.MANIFEST
    path.parent.mkdir(parents=True)
    path.write_text('{\n  "name": "agents-core",\n  "version": "' + BASE + '",\n  "description": "Keep formatting"\n}\n',
                    encoding="utf-8")
    (tmp_path / "VERSION").write_text(BASE + "\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agents-private"\ndynamic = ["version"]\n'
        '[tool.setuptools.dynamic]\nversion = {file = ["VERSION"]}\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "agents-private"\n'
        'source = {editable = "."}\n\n[[package]]\nname = "dependency"\n'
        'version = "1.2.3"\nsource = {registry = "https://example.invalid"}\n', encoding="utf-8")
    git("add", "--force", str(bump.MANIFEST), "VERSION")
    git("commit", "-m", "Published release")
    return tmp_path, path, git("rev-parse", "HEAD"), git


def test_prepare_syncs_both_versions_and_rerun_is_idempotent(repository):
    root, path, sha, _ = repository
    original = path.read_text(encoding="utf-8")
    first = bump.prepare(root, sha, now=NOW)
    assert first == {"version": NEXT, "released_version": BASE, "base_sha": sha,
                     "changed": True, "checked": False}
    assert (root / "VERSION").read_text() == NEXT + "\n"
    assert path.read_text() == original.replace(BASE, NEXT)
    assert "version" not in tomllib.loads((root / "uv.lock").read_text())["package"][0]
    assert tomllib.loads((root / "uv.lock").read_text())["package"][1]["version"] == "1.2.3"
    modified = [p.stat().st_mtime_ns for p in (path, root / "VERSION")]
    assert bump.prepare(root, sha, now=NOW.replace(day=19))["changed"] is False
    assert bump.prepare(root, sha, check=True)["checked"] is True
    assert [p.stat().st_mtime_ns for p in (path, root / "VERSION")] == modified


@pytest.mark.parametrize("candidate", [BASE, "26.10.15.2045", None])
def test_check_refuses_reused_older_or_missing_version_without_writes(repository, candidate):
    root, path, sha, _ = repository
    if candidate is None:
        (root / "VERSION").unlink()
    else:
        (root / "VERSION").write_text(candidate + "\n", encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        bump.prepare(root, sha, check=True)
    assert path.read_bytes() == original
    assert not (root / "VERSION").exists() if candidate is None else (root / "VERSION").read_text() == candidate + "\n"


def test_check_refuses_plugin_mismatch_and_preparation_repairs_it(repository):
    root, path, sha, _ = repository
    (root / "VERSION").write_text(NEXT + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must match"):
        bump.prepare(root, sha, check=True)
    assert bump.prepare(root, sha, now=NOW)["version"] == NEXT
    assert json.loads(path.read_text())["version"] == NEXT
    assert bump.prepare(root, sha, check=True)["checked"]


def test_each_published_release_requires_another_number_even_in_same_minute(repository):
    root, _, sha, git = repository
    bump.prepare(root, sha, now=NOW)
    git("commit", "-am", "Release")
    with pytest.raises(ValueError, match="must be greater"):
        bump.prepare(root, "HEAD", check=True)
    assert bump.prepare(root, "HEAD", now=NOW)["version"] == "26.10.18.0806"


def test_utc_conversion_and_next_day_rollover(repository):
    root, path, _, git = repository
    (root / "VERSION").write_text("26.12.31.2359\n", encoding="utf-8")
    path.write_text(path.read_text().replace(BASE, "26.12.31.2359"), encoding="utf-8")
    git("commit", "-am", "Year end release")
    assert bump.prepare(root, "HEAD", now=NOW)["version"] == "27.01.01.0000"


def test_preparation_refuses_a_branch_behind_main(repository):
    root, path, sha, _ = repository
    (root / "VERSION").write_text("26.10.15.2045\n", encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="behind main"):
        bump.prepare(root, sha, now=NOW)
    assert path.read_bytes() == original


@pytest.mark.parametrize("invalid", ["0.1.3", "26.1.18.0805", "26.02.30.0805", "26.10.18.2460",
                                     "v26.10.18.0805", "26.10.18.0805-dev", "26.10.18.0805\nextra"])
def test_invalid_current_version_fails_before_writes(repository, invalid):
    root, path, sha, _ = repository
    (root / "VERSION").write_text(invalid + "\n", encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        bump.prepare(root, sha, now=NOW)
    assert path.read_bytes() == original
    assert (root / "VERSION").read_text() == invalid + "\n"


def test_migration_from_semver_manifest_uses_legacy_main_timestamp(repository):
    root, path, _, git = repository
    (root / "VERSION").unlink()
    path.write_text(path.read_text().replace(BASE, "0.1.3"), encoding="utf-8")
    git("add", "-u")
    git("commit", "-m", "Legacy release")
    with pytest.raises(ValueError, match="must be greater"):
        bump.prepare(root, "HEAD", check=True)
    result = bump.prepare(root, "HEAD", now=NOW)
    assert (result["released_version"], result["version"]) == (BASE, NEXT)
    assert json.loads(path.read_text())["version"] == (root / "VERSION").read_text().strip() == NEXT


@pytest.mark.parametrize("base", ["missing-main", "--output=/tmp/not-a-ref"])
def test_bad_baseline_never_changes_either_file(repository, base):
    root, path, _, _ = repository
    original = [p.read_bytes() for p in (path, root / "VERSION")]
    with pytest.raises(subprocess.CalledProcessError):
        bump.prepare(root, base, now=NOW)
    assert [p.read_bytes() for p in (path, root / "VERSION")] == original


def test_cli_bump_then_check(repository, monkeypatch, capsys):
    root, path, sha, _ = repository
    monkeypatch.setattr(bump, "ROOT", root)
    with pytest.raises(SystemExit) as error:
        bump.main(["--check", "--base", sha])
    assert error.value.code == 1
    assert bump.main(["--base", sha]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["version"] == json.loads(path.read_text())["version"] == (root / "VERSION").read_text().strip()
    assert bump.main(["--check", "--base", sha]) == 0


def test_pinned_root_package_is_rejected_before_any_version_write(repository):
    root, _, sha, _ = repository
    bump.prepare(root, sha, now=NOW)
    path = root / "uv.lock"
    path.write_text(path.read_text().replace('name = "agents-private"', 'name = "agents-private"\nversion = "0.1.0"'), encoding="utf-8")
    original = [p.read_bytes() for p in (root / bump.MANIFEST, root / "VERSION", path)]
    with pytest.raises(ValueError, match="must use dynamic"):
        bump.prepare(root, sha, check=True)
    with pytest.raises(ValueError, match="must use dynamic"):
        bump.prepare(root, sha, now=NOW)
    assert [p.read_bytes() for p in (root / bump.MANIFEST, root / "VERSION", path)] == original


def test_invalid_package_metadata_fails_before_any_version_write(repository):
    root, path, sha, _ = repository
    (root / "pyproject.toml").write_text('[project]\nname = "agents-private"\nversion = "0.1.0"\n', encoding="utf-8")
    original = [p.read_bytes() for p in (path, root / "VERSION", root / "uv.lock")]
    with pytest.raises(ValueError, match="must read the canonical"):
        bump.prepare(root, sha, now=NOW)
    assert [p.read_bytes() for p in (path, root / "VERSION", root / "uv.lock")] == original


def test_preparation_converts_non_utc_time_to_utc(repository):
    from datetime import timedelta
    root, _, sha, _ = repository
    offset = datetime(2026, 10, 18, 10, 5, tzinfo=timezone(timedelta(hours=2)))
    assert bump.prepare(root, sha, now=offset)["version"] == NEXT
