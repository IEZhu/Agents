"""Agents-Core version string and the footer version segment."""
import subprocess

import pytest

from src import version
from src.engine import persona
from src.schemas.protocol import PersonaDescriptor


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()  # the version helper only trusts the installation's own checkout
    monkeypatch.setattr(version, "ROOT", tmp_path)
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


def fake_git(monkeypatch, log, status=""):
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
    fake_git(monkeypatch, "2026-10-01T08:01:00+00:00\n", " M src/x.py\n")
    assert version.agents_core_version() == "26.10.01.0801-dirty"


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
    assert not any(k.startswith("GIT_") for k in seen)


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
