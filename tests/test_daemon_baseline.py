import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/daemon_baseline.py"


def run_baseline(monkeypatch, tmp_path, output):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--installation", str(tmp_path), "--output", str(output)])
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "")
    runpy.run_path(str(SCRIPT), run_name="__main__")


def test_baseline_replaces_symlink_without_modifying_target(tmp_path, monkeypatch):
    target = tmp_path / "user-file"
    target.write_text("keep this content")
    target.chmod(0o640)
    output = tmp_path / "baseline.json"
    output.symlink_to(target)

    run_baseline(monkeypatch, tmp_path, output)

    assert target.read_text() == "keep this content"
    assert target.stat().st_mode & 0o777 == 0o640
    assert not output.is_symlink()
    assert output.stat().st_mode & 0o777 == 0o600


def test_failed_baseline_replace_preserves_output_and_cleans_temporary(tmp_path, monkeypatch):
    output = tmp_path / "baseline.json"
    output.write_text("previous snapshot")
    def fail_replace(*args):
        raise PermissionError("replace denied")
    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="replace denied"):
        run_baseline(monkeypatch, tmp_path, output)

    assert output.read_text() == "previous snapshot"
    assert set(tmp_path.iterdir()) == {output}
