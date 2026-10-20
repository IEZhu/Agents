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
    version.agents_core_commit.cache_clear()
    yield
    version.agents_core_version.cache_clear()
    version.agents_core_commit.cache_clear()
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


def test_commit_is_frozen_from_the_running_source_tree(monkeypatch):
    calls = []
    commits = iter(["a" * 40, "b" * 40])

    def git(*args):
        calls.append(args)
        return next(commits) + "\n"
    monkeypatch.setattr(version, "_git", git)
    assert version.agents_core_commit() == "a" * 40
    assert version.agents_core_commit() == "a" * 40
    assert calls == [("rev-parse", "--verify", "HEAD^{commit}")]


def test_source_archive_does_not_claim_the_content_installations_commit(monkeypatch, tmp_path):
    from src import runtime_paths
    canonical = tmp_path / "canonical"
    (canonical / ".git").mkdir(parents=True)
    monkeypatch.setattr(runtime_paths, "_installation_root", canonical)
    source = tmp_path / "source-archive"
    source.mkdir()
    monkeypatch.setattr(version, "ROOT", source)
    monkeypatch.setattr(version, "_git", lambda *args: pytest.fail("must not use canonical Git metadata"))
    assert version.agents_core_commit() is None
    assert version.agents_core_version() == "unknown"


@pytest.mark.parametrize("output", [None, "unknown", "a" * 39, "a" * 40 + "\nextra"])
def test_unreadable_or_malformed_commit_identity_stays_unknown(monkeypatch, output):
    monkeypatch.setattr(version, "_git", lambda *args: output)
    assert version.agents_core_commit() is None


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


MAIN, DEV = "e" * 40, "f" * 40  # a release merge commit's parents: main's previous HEAD, the dev HEAD


def fake_merge(monkeypatch, branch, parents="2026-10-14T18:07:38+00:00\n2026-10-15T18:14:29+00:00\n", code=0):
    """HEAD is the merge commit of #415: main's previous HEAD and the dev HEAD it merged."""
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if "log" in args:
            return subprocess.CompletedProcess(args, 0, stdout=f"2026-10-15T19:05:20+00:00 {MAIN} {DEV}\n", stderr="")
        if "show" in args:
            return subprocess.CompletedProcess(args, code, stdout=parents, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout=porcelain(branch), stderr="")
    monkeypatch.setattr(version.subprocess, "run", run)
    return calls


def test_a_release_on_main_keeps_the_version_of_the_dev_head_it_merged(monkeypatch):
    calls = fake_merge(monkeypatch, "main")
    assert version.agents_core_version() == "26.10.15.1814"  # dev showed 26.10.15.1814-dev
    assert [c[-4:] for c in calls if "show" in c] == [["-s", "--format=%cI", MAIN, DEV]]


def test_a_merge_older_than_main_keeps_its_own_time(monkeypatch):
    fake_merge(monkeypatch, "main", parents="2026-10-15T18:14:29+00:00\n2026-10-14T18:07:38+00:00\n")
    assert version.agents_core_version() == "26.10.15.1905"


@pytest.mark.parametrize("branch", ["dev", "(detached)", "feature/main"])
def test_merges_elsewhere_keep_their_own_time(monkeypatch, branch):
    calls = fake_merge(monkeypatch, branch)
    assert version.agents_core_version().startswith("26.10.15.1905")
    assert not [c for c in calls if "show" in c]


@pytest.mark.parametrize("parents, code", [("", 128), ("2026-10-14T18:07:38+00:00\n", 0)])
def test_missing_parents_keep_the_merge_time(monkeypatch, parents, code):
    # A shallow clone has the parents' SHAs but not their commits.
    fake_merge(monkeypatch, "main", parents, code)
    assert version.agents_core_version() == "26.10.15.1905"


def test_unknown_when_the_merged_times_are_garbage(monkeypatch):
    fake_merge(monkeypatch, "main", "2026-10-14T18:07:38\n2026-10-15T18:14:29\n")
    assert version.agents_core_version() == "unknown"


def test_release_time_compares_moments_not_text():
    # 20:14+02:00 is later than 19:00+00:00 although it sorts first as text.
    assert version.release_time("m", "2026-10-15T17:00:00+00:00", "2026-10-15T19:14:29+02:00") == \
        "2026-10-15T19:14:29+02:00"
    assert version.release_time("m", "2026-10-15T18:00:00+00:00", "2026-10-15T19:14:29+02:00") == "m"


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


@pytest.mark.skipif(shutil.which("git") is None or _git_version() < (2, 17),
                    reason="needs git 2.17 or newer (status --no-ahead-behind)")
def test_release_merge_in_a_real_repository(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    def git(*args, at):
        env = {**os.environ, "GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at}
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *args],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL, env=env)
    repo.mkdir()
    git("init", "-q", at="2026-10-14T18:07:38+00:00")
    git("checkout", "-q", "-b", "main", at="2026-10-14T18:07:38+00:00")
    git("commit", "-q", "--allow-empty", "-m", "previous release", at="2026-10-14T18:07:38+00:00")
    git("checkout", "-q", "-b", "dev", at="2026-10-15T18:14:29+00:00")
    git("commit", "-q", "--allow-empty", "-m", "last dev merge", at="2026-10-15T20:14:29+02:00")
    git("checkout", "-q", "main", at="2026-10-15T19:05:20+00:00")
    git("merge", "-q", "--no-ff", "-m", "Merge pull request #415 from IEZhu/dev", "dev",
        at="2026-10-15T19:05:20+00:00")
    monkeypatch.setattr(version, "ROOT", repo)

    def current():
        version.agents_core_version.cache_clear()
        return version.agents_core_version()

    assert current() == "26.10.15.1814"
    git("checkout", "-q", "dev", at="2026-10-15T19:05:20+00:00")
    assert current() == "26.10.15.1814-dev"


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
