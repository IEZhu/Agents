"""The flow editor page, run in Node against a stub DOM and fetch: automatic sign-in and search."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
PAGE = ROOT / "src" / "daemon" / "flows_ui.html"


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


@pytest.fixture(scope="module")
def search():
    return run("search")


def test_search_matches_all_terms_case_insensitively_and_orders_name_matches_first(search):
    assert search["alpha"]["list"][1:] == ["Personal", "Alpha plan", "Beta [in text]"]
    assert search["alpha"]["count"] == "2 of 3"
    assert search["and_terms"]["list"][1:] == ["Personal", "Alpha plan"]
    assert search["and_terms"]["count"] == "1 of 3"
    assert search["rules_logging"]["list"] == ["Rules (2/3 on)", "Logging style", "Security [in text]", "Third [in text]"]
    assert search["rules_logging"]["count"] == "3 of 3"
    assert search["rules_and"]["list"][1:] == ["Security [in text]", "Third [in text]"]


def test_empty_query_leaves_the_list_unchanged(search):
    assert search["cleared"] == search["initial"]
    assert search["initial"]["count"] == ""
    assert search["escape"]["query"] == "" and search["escape"]["count"] == ""
    assert search["escape"]["list"][1:] == ["Security", "Logging style", "Third"]


def test_flows_are_found_by_their_text_within_the_shown_category(search):
    assert search["needle_user"]["list"][1:] == ["github.com/o/r", "Repo flow [in text]"]
    assert search["needle_user"]["count"] == "1 of 3"
    assert search["needle_system"]["list"][1:] == ["Built-in", "Review [in text]"]
    assert search["needle_system"]["count"] == "1 of 1"
    assert search["none"]["list"][1:] == ["No matches"] and search["none"]["count"] == "0 of 3"


def test_queries_are_kept_per_tab(search):
    assert search["rules_query_empty"] == ""
    assert search["flows_query_kept"] == "nothing-matches"
    assert search["rules_query_kept"] == "zzz"


def test_the_open_item_stays_open_while_filtered_out_and_scroll_is_kept(search):
    assert search["opened"] == {"title": "rule-a", "hidden": False}
    assert search["filtered_out"]["list"][1:] == ["No matches"]
    assert search["filtered_out"]["title"] == "rule-a" and not search["filtered_out"]["hidden"]
    assert search["scroll_kept"] == 120


def test_slash_focuses_the_search_box_except_while_typing(search):
    assert search["slash"] == {"prevented": True, "focused": 1, "typing_prevented": False}


def page_parts():
    html = PAGE.read_text(encoding="utf-8")
    script = re.search(r'<script nonce="\{\{NONCE\}\}">([\s\S]*?)</script>', html).group(1)
    style = re.search(r'<style nonce="\{\{NONCE\}\}">([\s\S]*?)</style>', html).group(1)
    return html, script, style


def test_only_the_list_and_the_detail_pane_scroll():
    html, _, style = page_parts()
    assert re.search(r"body \{[^}]*height: 100dvh[^}]*overflow: hidden", style)
    assert re.search(r"main \{[^}]*flex: 1[^}]*min-height: 0[^}]*overflow: hidden", style)
    assert re.search(r"#items \{[^}]*overflow-y: auto", style)
    assert re.search(r"main > section \{[^}]*overflow-y: auto", style)
    assert re.search(r"grid-template-rows: minmax\(0, 40%\) minmax\(0, 1fr\)", style)
    assert "vh" not in re.sub(r"100(d?)vh", "", style)  # no fixed viewport-height textareas
    assert html.index('id="search"') < html.index('id="items"')


def test_the_page_keeps_its_content_security_policy():
    html, script, _ = page_parts()
    assert not re.search(r"\son[a-z]+\s*=", html)
    assert not re.search(r"(src|href)=[\"']https?:", html)
    assert "<style nonce" in html and html.count("{{NONCE}}") == 2
    completed = subprocess.run([NODE, "--check", "-"], input=script, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
