import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

from scripts.daemon_baseline import snapshot


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


def test_baseline_counts_script_and_module_servers_without_recording_arguments(tmp_path, monkeypatch):
    processes = f'''10 1 100 00:01 /usr/bin/python {tmp_path}/src/server.py
11 1 200 00:02 /usr/bin/python -u -m src.server --private=redacted-test-value
12 1 300 00:03 /usr/bin/python -m src.server_helper
13 1 400 00:04 /usr/bin/node {tmp_path}/bridge/stdio.mjs private-config.json
14 1 500 00:05 /usr/bin/python -m src.daemon serve
'''
    executables = "10 /usr/bin/python\n11 /usr/bin/python\n12 /usr/bin/python\n13 /usr/bin/node\n14 /usr/bin/python"
    monkeypatch.setattr(subprocess, "check_output", lambda args, **kwargs:
                        executables if args[-1] == "pid=,comm=" else processes if args[0] == "/bin/ps" else "swap")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "Physical footprint: 1.0M"))

    result = snapshot(tmp_path)

    assert result["model_processes"] == 3
    assert {row["pid"]: row["kind"] for row in result["processes"]} == {10: "stdio", 11: "stdio", 13: "bridge", 14: "daemon"}
    assert result["total_physical_footprint_bytes"] == 4 * 1024**2
    assert "redacted-test-value" not in str(result)


def test_baseline_excludes_shell_wrappers_from_counts_and_footprints(tmp_path, monkeypatch):
    commands = {
        10: ("sh", "sh -c 'python -m src.server'"),
        11: ("/bin/sh", "/bin/sh -c '/usr/bin/python3.11 -m src.server'"),
        12: ("/bin/zsh", f"/bin/zsh -c '/usr/bin/python {tmp_path}/src/server.py'"),
        13: ("python", "python -m src.server"),
        14: ("/usr/bin/Python3.11", "/usr/bin/Python3.11 -u -m src.server"),
        15: ("/path with spaces/bin/python3", f"/path with spaces/bin/python3 {tmp_path}/src/server.py"),
        16: ("/bin/sh", "sh -c '/usr/bin/python -m src.daemon serve'"),
        17: ("/usr/bin/python3", "/usr/bin/python3 -m src.daemon serve"),
        18: ("/bin/sh", f"sh -c '/usr/bin/node {tmp_path}/bridge/stdio.mjs'"),
        19: ("/usr/bin/node", f"/usr/bin/node {tmp_path}/bridge/stdio.mjs config.json"),
    }
    processes = "\n".join(f"{pid} 1 100 00:01 {command}" for pid, (_, command) in commands.items())
    executables = "\n".join(f"{pid} {executable}" for pid, (executable, _) in commands.items())
    measured = []

    def output(args, **kwargs):
        if args[0] != "/bin/ps":
            return "swap"
        return executables if args[-1] == "pid=,comm=" else processes

    def measure(args, **kwargs):
        measured.append(int(args[-1]))
        return subprocess.CompletedProcess(args, 0, "Physical footprint: 1.0M")

    monkeypatch.setattr(subprocess, "check_output", output)
    monkeypatch.setattr(subprocess, "run", measure)

    result = snapshot(tmp_path)

    assert result["model_processes"] == 4
    assert {row["pid"]: row["kind"] for row in result["processes"]} == {
        13: "stdio", 14: "stdio", 15: "stdio", 17: "daemon", 19: "bridge",
    }
    assert measured == [13, 14, 15, 17, 19]
    assert result["total_physical_footprint_bytes"] == 5 * 1024**2
