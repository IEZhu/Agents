"""Reject unsupported interpreters before activating or changing an environment."""
from collections import namedtuple
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
VersionInfo = namedtuple("VersionInfo", "major minor micro releaselevel serial")


@pytest.mark.parametrize("version", [(3, 10), (3, 11), (3, 12)])
def test_windows_version_helper(version, monkeypatch, capsys):
    """Accept supported versions and emit no version for an unsupported Python."""
    with monkeypatch.context() as patch:
        patch.setattr(sys, "version_info", VersionInfo(*version, 0, "final", 0))
        if version < (3, 11):
            with pytest.raises(SystemExit) as error:
                runpy.run_path(str(ROOT / "scripts/_helpers/check_version.py"))
            assert error.value.code == 1
        else:
            runpy.run_path(str(ROOT / "scripts/_helpers/check_version.py"))
    output = capsys.readouterr()
    assert output.out == ("" if version < (3, 11) else f"{version[0]}.{version[1]}\n")


@pytest.fixture
def run_installer(tmp_path):
    """Run the real Unix installer with isolated interpreter and activation stubs."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required for the Unix installer")
    script = tmp_path / "scripts/init_repo.sh"
    script.parent.mkdir()
    shutil.copyfile(ROOT / "scripts/init_repo.sh", script)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    venv_bin = tmp_path / ".venv/bin"
    venv_bin.mkdir(parents=True)
    events = tmp_path / "events"

    for path, version_key in ((fake_bin / "python3", "SYSTEM_VERSION"),
                              (venv_bin / "python", "VENV_VERSION")):
        path.write_text(
            '#!/bin/sh\n'
            f'if [ "$1" = "-c" ]; then printf "%s\\n" "${version_key}"; exit 0; fi\n'
            'echo unexpected-python >> "$TEST_EVENTS"\nexit 99\n'
        )
        path.chmod(0o755)
    for command in ("pip", "pip3"):
        path = fake_bin / command
        path.write_text('#!/bin/sh\necho pip >> "$TEST_EVENTS"\nexit 99\n')
        path.chmod(0o755)
    (venv_bin / "activate").write_text(
        'echo activated >> "$TEST_EVENTS"\nexit 77\n'
    )

    def run(system_version, venv_version):
        """Return installer output and events without continuing past activation."""
        result = subprocess.run(
            [bash, str(script), "--skip-env", "--skip-index", "--skip-mcp"],
            cwd=tmp_path, input="N\n", text=True, capture_output=True, timeout=10,
            env={"PATH": f"{fake_bin}{os.pathsep}{os.defpath}",
                 "SYSTEM_VERSION": system_version, "VENV_VERSION": venv_version,
                 "TEST_EVENTS": str(events)},
        )
        return result, events.read_text().splitlines() if events.exists() else []

    return run


def test_unix_rejects_old_system_python_before_pip(run_installer):
    """Reject an old system interpreter before inspecting or invoking pip."""
    result, events = run_installer("3.10", "3.11")
    assert result.returncode == 1
    assert "3.11" in result.stdout
    assert "Checking pip" not in result.stdout
    assert events == []


@pytest.mark.parametrize("venv_version", ["3.10", "unknown", "", "invalid"])
def test_unix_rejects_reuse_of_unsupported_venv(run_installer, venv_version):
    """Declining recreation must not activate an old or unverifiable venv."""
    result, events = run_installer("3.11", venv_version)
    assert result.returncode == 1
    assert "3.11" in result.stdout
    if venv_version != "3.10":
        assert "Could not verify existing virtual environment Python version" in result.stdout
    assert events == []


def test_unix_supported_venv_reaches_activation(run_installer):
    """A supported venv reaches activation, which stops this focused test early."""
    result, events = run_installer("3.11", "3.11")
    assert result.returncode == 77, result.stdout + result.stderr
    assert events == ["activated"]
