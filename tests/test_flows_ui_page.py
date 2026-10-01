"""The flow editor page's automatic sign-in, run in Node against a stub DOM and fetch."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def run(scenario):
    completed = subprocess.run([NODE, str(ROOT / "tests" / "flows_ui_page_harness.mjs"),
                                str(ROOT / "src" / "daemon" / "flows_ui.html"), scenario],
                               capture_output=True, text=True, timeout=60, check=True)
    return json.loads(completed.stdout)


pytestmark = pytest.mark.skipif(NODE is None, reason="needs node")


def test_first_visit_signs_in_without_showing_the_sign_in_page():
    result = run("signs_in")
    assert result["posts"] == [{}]
    assert not result["signin_shown"] and not result["main_hidden"]


def test_concurrent_401s_share_one_attempt_and_are_retried():
    result = run("concurrent")
    assert result["concurrent_posts"] == 1
    assert result["answers"] == ["ok", "ok", "ok"]
    assert not result["signin_shown"]


def test_refused_sign_in_shows_the_command_once():
    result = run("refused")
    assert result["posts"] == [{}]
    assert result["signin_shown"] and result["main_hidden"]


def test_a_cookie_that_never_arrives_does_not_loop():
    result = run("cookie_lost")
    assert result["posts"] == [{}]
    assert result["signin_shown"]


def test_a_used_code_falls_back_to_automatic_sign_in():
    result = run("used_code")
    assert result["posts"] == [{"code": "used-code"}, {}]
    assert not result["signin_shown"] and not result["main_hidden"]
