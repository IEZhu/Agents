"""init_repo.sh's virtual environment step with a .venv link (#181).

Auto-update makes ``.venv`` a link to an environment under ``.venvs``. The real step
of ``scripts/init_repo.sh`` runs here with stub helpers and a fake interpreter:
whatever happens to the link, the environment it points at survives.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not BASH, reason="requires bash on Unix")
ENVIRONMENT = "py313-0123456789ab"
FAKE_PYTHON = """#!/bin/sh
# -c prints the version; -m venv PATH makes a minimal environment there.
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    mkdir -p "$3/bin" && cp "$0" "$3/bin/python" && : > "$3/pyvenv.cfg" && echo "fresh" > "$3/created"
    exit 0
fi
echo 3.13
"""


def venv_step():
    source = (ROOT / "scripts/init_repo.sh").read_text(encoding="utf-8")
    start = source.index("SKIP_INSTALL=false\n")
    return source[start:source.index("# Activate venv", start)]


def run_step(root, *, assume_yes, answer=""):
    fake = root / "fake-python"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(0o755)
    script = "\n".join([
        "set -e",
        "print_header() { :; }; print_step() { :; }; print_success() { :; }; print_warn() { :; }",
        "print_error() { echo \"$*\" >&2; }",
        "get_python_version() { echo 3.13; }",
        "version_gte() { return 0; }",
        'PYTHON_MIN_VERSION="3.11"',
        f'VENV_PATH="{root}/.venv"',
        f'SELECTED_PYTHON="{fake}"',
        f"ASSUME_YES={'true' if assume_yes else 'false'}",
        venv_step(),
        'echo "SKIP_INSTALL=$SKIP_INSTALL"',
    ])
    return subprocess.run([BASH, "-c", script], input=answer, capture_output=True, text=True, timeout=60)


@pytest.fixture
def root(tmp_path):
    """An installation whose .venv links to the active environment under .venvs."""
    environment = tmp_path / ".venvs" / ENVIRONMENT
    (environment / "bin").mkdir(parents=True)
    python = environment / "bin/python"
    python.write_text(FAKE_PYTHON)
    python.chmod(0o755)
    (environment / "pyvenv.cfg").write_text("version = 3.13.1\n")
    os.symlink(os.path.join(".venvs", ENVIRONMENT), tmp_path / ".venv", target_is_directory=True)
    return tmp_path


def test_a_working_link_is_reused_and_its_environment_refreshed(root):
    result = run_step(root, assume_yes=True)
    assert result.returncode == 0, result.stderr
    assert "SKIP_INSTALL=false" in result.stdout  # --yes refreshes the dependencies, in the active environment
    assert (root / ".venv").is_symlink() and os.readlink(root / ".venv") == os.path.join(".venvs", ENVIRONMENT)


def test_a_dangling_link_is_replaced_by_a_new_environment(root):
    os.unlink(root / ".venv")
    os.symlink(os.path.join(".venvs", "py313-gone00000000"), root / ".venv", target_is_directory=True)
    result = run_step(root, assume_yes=True)
    assert result.returncode == 0, result.stderr
    assert not (root / ".venv").is_symlink() and (root / ".venv/created").read_text() == "fresh\n"
    assert (root / ".venvs" / ENVIRONMENT / "pyvenv.cfg").exists()


def test_recreating_removes_only_the_link(root):
    result = run_step(root, assume_yes=False, answer="y\n")
    assert result.returncode == 0, result.stderr
    assert not (root / ".venv").is_symlink() and (root / ".venv/created").read_text() == "fresh\n"
    assert (root / ".venvs" / ENVIRONMENT / "pyvenv.cfg").read_text() == "version = 3.13.1\n"
