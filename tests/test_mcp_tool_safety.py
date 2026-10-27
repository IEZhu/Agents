"""Run the actual ESM and no-import code-mode adapters in the regular pytest suite."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_mcp_metadata_guard():
    completed = subprocess.run(
        [NODE, "--test", str(ROOT / "tests/mcp_metadata_guard.test.mjs")],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
