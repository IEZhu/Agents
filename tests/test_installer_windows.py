"""Exercise the existing-venv gate through native cmd.exe and real Python venvs.

Run on Windows with Python 3.11+ and AGENTS_TEST_PYTHON310 set to a Python 3.10
executable. The activation sentinel ends cmd.exe before dependency installation,
model downloads, or client configuration; the installer itself is unmodified.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires native cmd.exe")


@pytest.fixture
def run_windows_installer(tmp_path):
    """Run a copied installer in a disposable checkout whose path contains spaces."""
    root = tmp_path / "checkout with spaces"
    helpers = root / "scripts" / "_helpers"
    helpers.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/init_repo.bat", root / "scripts/init_repo.bat")
    shutil.copyfile(ROOT / "scripts/_helpers/check_version.py", helpers / "check_version.py")
    events = root / "events.txt"
    profile = tmp_path / "profile"
    profile.mkdir()
    env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
           "USERPROFILE": str(profile), "APPDATA": str(profile / "AppData"),
           "LOCALAPPDATA": str(profile / "LocalAppData"), "TEST_EVENTS": str(events),
           "AGENTS_PERSONA_PROTOCOL": "2", "PIP_NO_INDEX": "1",
           "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)

    def run(venv_kind, reply):
        """Prepare a real supported, old, or broken venv and answer its reuse prompt."""
        python = sys.executable
        if venv_kind == "unsupported":
            python = os.environ.get("AGENTS_TEST_PYTHON310")
            if not python:
                if os.environ.get("CI"):
                    pytest.fail("Windows CI must provide AGENTS_TEST_PYTHON310")
                pytest.skip("set AGENTS_TEST_PYTHON310 to a Python 3.10 executable")
        venv = root / ".venv"
        subprocess.run([python, "-m", "venv", "--without-pip", str(venv)],
                       check=True, capture_output=True, text=True, env=env, timeout=60)
        venv_python = venv / "Scripts/python.exe"
        config = venv / "pyvenv.cfg"
        if venv_kind == "unverifiable":
            # The native venv launcher cannot find its base interpreter.
            config.write_text(f"home = {tmp_path / 'missing base python'}\n", encoding="utf-8")
        probe = subprocess.run(
            [str(venv_python), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, env=env, timeout=15,
        )
        if venv_kind == "unverifiable":
            assert probe.returncode != 0 and not probe.stdout.strip(), probe
        else:
            assert probe.returncode == 0, probe.stderr
            version = tuple(map(int, probe.stdout.strip().split(".")))
            assert version == (3, 10) if venv_kind == "unsupported" else version >= (3, 11)

        activation = venv / "Scripts/activate.bat"
        activation.write_bytes(
            b'@echo off\r\n'
            b'if not "%SKIP_ENV%"=="true" exit 78\r\n'
            b'if not "%SKIP_INDEX%"=="true" exit 78\r\n'
            b'if not "%SKIP_MCP%"=="true" exit 78\r\n'
            b'echo activated>>"%TEST_EVENTS%"\r\nexit 77\r\n'
        )
        before_config, before_activation = config.read_bytes(), activation.read_bytes()
        result = subprocess.run(
            [os.environ["COMSPEC"], "/d", "/c",
             r"scripts\init_repo.bat --skip-env --skip-index --skip-mcp"],
            cwd=root, input=reply, text=True, encoding="utf-8", errors="replace",
            capture_output=True, env=env, timeout=45,
        )
        # Declining recreation must preserve the existing environment.
        assert config.read_bytes() == before_config, result.stdout + result.stderr
        assert activation.read_bytes() == before_activation, result.stdout + result.stderr
        assert "Found suitable Python" in result.stdout, result.stdout + result.stderr
        assert "Reinstall? [y/N]:" in result.stdout, result.stdout + result.stderr
        if venv_kind == "supported":
            expected_version = probe.stdout.strip()
            assert f"Virtual environment exists ({expected_version})" in result.stdout, result.stdout + result.stderr
        assert "Installing Dependencies" not in result.stdout
        return result, events.read_text().splitlines() if events.exists() else []

    return run


@pytest.mark.parametrize("reply", ["N\n", "\n"], ids=["explicit-no", "default-no"])
@pytest.mark.parametrize("venv_kind", ["unsupported", "unverifiable"])
def test_windows_declining_recreation_rejects_invalid_venv(run_windows_installer, venv_kind, reply):
    """A declined rebuild rejects Python 3.10 or an unreadable version before activation."""
    result, events = run_windows_installer(venv_kind, reply)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Existing virtual environment requires Python 3.11 or newer" in result.stdout
    assert "choose to recreate it" in result.stdout
    if venv_kind == "unverifiable":
        assert "Could not verify existing virtual environment Python version" in result.stdout
        assert "Virtual environment exists (unknown)" in result.stdout
    else:
        assert "Virtual environment exists (3.10)" in result.stdout
    assert "Activating virtual environment" not in result.stdout
    assert events == []


@pytest.mark.parametrize("reply", ["N\n", "\n"], ids=["explicit-no", "default-no"])
def test_windows_declining_recreation_reaches_activation_for_supported_venv(run_windows_installer, reply):
    """A supported venv reaches activation after either explicit or default refusal."""
    result, events = run_windows_installer("supported", reply)
    assert result.returncode == 77, result.stdout + result.stderr
    assert "Using existing virtual environment" in result.stdout
    assert events == ["activated"]
