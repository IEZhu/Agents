"""Agents-Core version string and the footer version segment."""
import os
import re
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from src import version
from src.engine import persona
from src.schemas.protocol import PersonaDescriptor


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()  # the version helper only trusts the installation's own checkout
    monkeypatch.setattr(version, "ROOT", tmp_path)
    # The version and line-ending lookups are git calls of their own (#279); these tests count the footer's.
    monkeypatch.setattr(version.executables, "_installed_git_version", lambda: (2, 45))
    monkeypatch.setattr(version.executables, "_line_endings", lambda repository: [])
    version.agents_core_version.cache_clear()
    yield
    version.agents_core_version.cache_clear()
    persona.configure_ui_port(None)


def descriptor(**changes):
    values = dict(agent="lawyer", activation_id="a", bundle_revision="b" * 64, scope="s",
                  skills_loaded=["skill-x"], implants_loaded=["Imp"], rules_loaded=["language-match"])
    return PersonaDescriptor(**{**values, **changes})


def test_format_converts_to_utc_and_marks_dirty():
    assert version.format_version("2026-10-01T10:01:30+02:00") == "26.10.01.0801"
    assert version.format_version("2026-10-01T23:59:00-05:00", dirty=True) == "26.10.02.0459-dirty"


@pytest.mark.parametrize("branch, dirty, expected", [
    ("dev", False, "26.10.01.0801-dev"),
    ("dev", True, "26.10.01.0801-dev-dirty"),
    ("main", False, "26.10.01.0801"),
    ("(detached)", False, "26.10.01.0801"),
    ("development", False, "26.10.01.0801"),
    ("feature/dev", False, "26.10.01.0801"),
    (None, True, "26.10.01.0801-dirty"),
])
def test_format_marks_only_the_dev_branch(branch, dirty, expected):
    assert version.format_version("2026-10-01T08:01:00+00:00", dirty, branch) == expected


MODIFIED = "1 .M N... 100644 100644 100644 " + "a" * 40 + " " + "b" * 40 + " src/x.py\n"


def porcelain(branch="main", changes=""):
    """``git status --porcelain=v2 --branch`` output."""
    return f"# branch.oid {'c' * 40}\n# branch.head {branch}\n{changes}"


def fake_git(monkeypatch, log, status=None):
    status = porcelain() if status is None else status
    def run(args, **kwargs):
        out = log if "log" in args else status
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
    monkeypatch.setattr(version.subprocess, "run", run)


def test_version_from_head_commit_time(monkeypatch):
    fake_git(monkeypatch, "2026-10-01T08:01:00+00:00\n")
    assert version.agents_core_version() == "26.10.01.0801"


def test_unknown_without_own_git_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(version, "ROOT", tmp_path / "missing")
    fake_git(monkeypatch, "2026-10-01T08:01:00+00:00")
    assert version.agents_core_version() == "unknown"


def test_unknown_when_status_fails(monkeypatch):
    def run(args, **kwargs):
        code = 0 if "log" in args else 1
        return subprocess.CompletedProcess(args, code, stdout="2026-10-01T08:01:00+00:00", stderr="")
    monkeypatch.setattr(version.subprocess, "run", run)
    assert version.agents_core_version() == "unknown"


def test_dirty_tree_is_marked(monkeypatch):
    fake_git(monkeypatch, "2026-10-01T08:01:00+00:00\n", porcelain(changes=MODIFIED))
    assert version.agents_core_version() == "26.10.01.0801-dirty"


@pytest.mark.parametrize("status, expected", [
    (porcelain("dev"), "26.10.01.0801-dev"),
    (porcelain("dev", MODIFIED), "26.10.01.0801-dev-dirty"),
    (porcelain("dev") + "# branch.upstream origin/dev\n# branch.ab +0 -0\n", "26.10.01.0801-dev"),
    (porcelain("(detached)"), "26.10.01.0801"),
    ("", "26.10.01.0801"),  # no branch header at all: no suffix, not unknown
])
def test_dev_branch_from_the_status_header(monkeypatch, status, expected):
    fake_git(monkeypatch, "2026-10-01T08:01:00+00:00\n", status)
    assert version.agents_core_version() == expected


@pytest.mark.parametrize("plain, expected", [("", "26.10.01.0801"), (" M src/x.py\n", "26.10.01.0801-dirty")])
def test_a_git_without_the_v2_flags_keeps_the_plain_version(monkeypatch, plain, expected):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if "log" in args:
            return subprocess.CompletedProcess(args, 0, stdout="2026-10-01T08:01:00+00:00", stderr="")
        if "--porcelain=v2" in args:  # git < 2.17: unknown option --no-ahead-behind
            return subprocess.CompletedProcess(args, 129, stdout="", stderr="error: unknown option")
        return subprocess.CompletedProcess(args, 0, stdout=plain, stderr="")
    monkeypatch.setattr(version.subprocess, "run", run)
    assert version.agents_core_version() == expected
    assert [c for c in calls if "status" in c][-1][-2:] == ["--porcelain", "--untracked-files=no"]


def test_one_status_call_reads_branch_and_changes(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        out = "2026-10-01T08:01:00+00:00" if "log" in args else porcelain("dev")
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
    monkeypatch.setattr(version.subprocess, "run", run)
    assert version.agents_core_version() == "26.10.01.0801-dev"
    status = [c for c in calls if "status" in c]
    assert len(status) == 1
    assert {"--porcelain=v2", "--branch", "--no-ahead-behind", "--untracked-files=no"} <= set(status[0])


def _git_version() -> tuple[int, int]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    found = re.search(r"(\d+)\.(\d+)", out)
    return (int(found[1]), int(found[2])) if found else (0, 0)


# A git older than 2.17 gets the plain version by design (no --no-ahead-behind, see
# src/version.py), so the suffixes below need a newer one; the fixture itself uses only
# commands an older git has (no init -b, switch or restore).
@pytest.mark.skipif(shutil.which("git") is None or _git_version() < (2, 17),
                    reason="needs git 2.17 or newer (status --no-ahead-behind)")
def test_dev_suffix_in_a_real_repository(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *args],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)
    repo.mkdir()
    git("init", "-q")
    git("checkout", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "a")
    monkeypatch.setattr(version, "ROOT", repo)

    def current():
        version.agents_core_version.cache_clear()
        return version.agents_core_version()

    assert current().count("-") == 0
    git("checkout", "-q", "-b", "dev")
    assert current().endswith("-dev")
    (repo / "a.txt").write_text("b\n")
    assert current().endswith("-dev-dirty")
    (repo / "untracked.txt").write_text("u\n")
    git("checkout", "--", "a.txt")
    assert current().endswith("-dev")  # untracked files do not make it dirty
    git("checkout", "-q", "--detach")
    assert "-" not in current()


@pytest.mark.parametrize("failure", [OSError("no git"), subprocess.TimeoutExpired("git", 5)])
def test_unknown_when_git_fails(monkeypatch, failure):
    def run(*args, **kwargs):
        raise failure
    monkeypatch.setattr(version.subprocess, "run", run)
    assert version.agents_core_version() == "unknown"


def test_unknown_on_nonzero_exit_or_garbage(monkeypatch):
    monkeypatch.setattr(version.subprocess, "run",
                        lambda args, **kw: subprocess.CompletedProcess(args, 128, stdout="", stderr="x"))
    assert version.agents_core_version() == "unknown"
    version.agents_core_version.cache_clear()
    fake_git(monkeypatch, "not a date")
    assert version.agents_core_version() == "unknown"


def test_version_is_computed_once(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="2026-10-01T08:01:00+00:00", stderr="")
    monkeypatch.setattr(version.subprocess, "run", run)
    version.agents_core_version(); version.agents_core_version()
    assert len(calls) == 2  # log and status, once
    assert all("--no-optional-locks" in c for c in calls)


def test_git_environment_is_scrubbed(monkeypatch):
    seen = {}
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    def run(args, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, stdout="2026-10-01T08:01:00+00:00", stderr="")
    monkeypatch.setattr(version.subprocess, "run", run)
    version.agents_core_version()
    # Inherited GIT_* variables are gone; only the isolation from the user's configuration is set (#279).
    assert {k for k in seen if k.startswith("GIT_")} == {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                                                         "GIT_CONFIG_VALUE_0"}
    assert seen["GIT_CONFIG_GLOBAL"] == os.devnull and seen["GIT_CONFIG_KEY_0"] == "safe.directory"


def test_git_never_gets_the_server_stdin(monkeypatch):
    stdin = []
    def run(args, **kwargs):
        stdin.append(kwargs.get("stdin"))
        return subprocess.CompletedProcess(args, 0, stdout="2026-10-01T08:01:00+00:00", stderr="")
    monkeypatch.setattr(version.subprocess, "run", run)
    version.agents_core_version()
    assert stdin == [subprocess.DEVNULL] * 2


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_version_while_the_server_reads_its_stdin(monkeypatch, pending_stdin_read):
    """Under the Claude desktop app the footer said "Agents-Core unknown": git timed out on the MCP stdin."""
    monkeypatch.setattr(version, "ROOT", Path(__file__).resolve().parents[1])  # this checkout
    started = time.monotonic()
    assert version.agents_core_version() != version.UNKNOWN
    assert time.monotonic() - started < pending_stdin_read


def test_no_link_under_daemon_without_port(monkeypatch):
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    persona.configure_ui_port(None)
    assert persona.ui_link() is None


def test_load_runtime_configures_the_ui_port():
    import inspect
    from src.daemon import app
    assert "configure_ui_port(port)" in inspect.getsource(app.load_runtime)


def test_footer_has_version_and_no_url_under_stdio(monkeypatch):
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    persona.configure_ui_port(8765)
    fake_git(monkeypatch, "2026-10-01T08:01:00+00:00")
    footer = persona.persona_footer(descriptor())
    assert footer.startswith("**Agent**: lawyer · **Skills**: skill-x · **Implants**: Imp · **Rules**: language-match")
    assert footer.endswith(" · Agents-Core 26.10.01.0801")
    assert "http" not in footer
    assert "<" not in footer and ">" not in footer


def test_footer_link_under_daemon_uses_configured_port(monkeypatch):
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    persona.configure_ui_port(9123)
    fake_git(monkeypatch, "2026-10-01T08:01:00+00:00")
    footer = persona.persona_footer(descriptor())
    assert footer.endswith(" · [Agents-Core 26.10.01.0801](http://127.0.0.1:9123/ui)")
    assert "?" not in footer and "#" not in footer
    assert "<" not in footer and ">" not in footer


def test_version_and_link_stay_out_of_the_descriptor(monkeypatch):
    assert "version" not in PersonaDescriptor.model_fields
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    persona.configure_ui_port(9123)
    assert "9123" not in descriptor().model_dump_json()
