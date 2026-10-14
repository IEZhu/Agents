"""The PR thread helper in its default mode and in the autodev mode of flows/pr-review.md (#346).

Offline: `gh` is replaced by a fake that answers from recorded shapes and records every call.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pr_threads", ROOT / "scripts" / "dev" / "pr_threads.py")
pr_threads = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pr_threads)

REPO, ME, HEAD, OLD = "IEZhu/Agents", "WonderMr", "a" * 40, "b" * 40
BOT = "coderabbitai"


def comment(database_id, author, body):
    return {"databaseId": database_id, "author": {"login": author}, "body": body}


def thread(thread_id, line, first, last, resolved=False):
    return {"id": thread_id, "isResolved": resolved, "path": "src/x.py", "line": line,
            "first": {"nodes": [first]}, "last": {"nodes": [last]}}


THREADS = [
    # The bot's finding, answered by an unmarked comment of the account: the owner, or a default-mode reply.
    thread("T_owner", 1, comment(11, BOT, "Finding one"), comment(12, ME, "Fixed in abc1234.")),
    # The bot's finding, answered by the agent with its marker line.
    thread("T_agent", 2, comment(21, BOT, "Finding two"), comment(22, ME, "<!-- autodev -->\nFixed in abc1234.")),
    # The bot spoke last.
    thread("T_bot", 3, comment(31, BOT, "Finding three"), comment(31, BOT, "Finding three")),
    thread("T_done", 4, comment(41, BOT, "Old finding"), comment(42, ME, "Fixed."), resolved=True),
]
REVIEWS = [[
    {"user": {"login": ME}, "commit_id": HEAD, "submitted_at": "2026-10-14T08:00:00Z", "body": "Owner review body"},
    {"user": {"login": "copilot-pull-request-reviewer[bot]"}, "commit_id": HEAD,
     "submitted_at": "2026-10-14T08:05:00Z", "body": "Copilot overview"},
    {"user": {"login": BOT}, "commit_id": OLD, "submitted_at": "2026-10-13T08:00:00Z", "body": "Earlier head"},
    {"user": {"login": BOT}, "commit_id": HEAD, "submitted_at": "2026-10-14T08:10:00Z", "body": "  "},
]]


@pytest.fixture
def fake_gh(monkeypatch):
    calls = []

    def gh(*args):
        calls.append(args)
        if args[:2] == ("repo", "view"):
            return REPO + "\n"
        if args[:2] == ("api", "user"):
            return ME + "\n"
        if args[:2] == ("api", "graphql") and args[3].startswith("query=mutation"):
            return json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}})
        if args[:2] == ("api", "graphql"):
            return json.dumps({"data": {"repository": {"pullRequest": {
                "headRefOid": HEAD,
                "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": THREADS}}}}})
        if args[:3] == ("api", "--paginate", "--slurp") and args[3] == f"repos/{REPO}/pulls/7/reviews":
            return json.dumps(REVIEWS)
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(pr_threads, "gh", gh)
    return calls


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pr_threads.py", *argv])
    pr_threads.main()


def mutations(calls):
    return [args[3] for args in calls if args[:2] == ("api", "graphql") and args[3].startswith("query=mutation")]


def test_default_mode_counts_the_accounts_last_comment_and_hides_its_reviews(monkeypatch, capsys, fake_gh):
    run(monkeypatch, "7")
    out = capsys.readouterr().out
    assert "#7 head aaaaaaa: 3 unresolved of 4 threads" in out
    assert "  answered src/x.py:1 coderabbitai c11" in out
    assert "  answered src/x.py:2 coderabbitai c21" in out
    assert "  UNANSWERED src/x.py:3 coderabbitai c31" in out
    assert "Copilot overview" in out
    assert "Owner review body" not in out  # the logged-in account's own review
    assert "Earlier head" not in out and "Old finding" not in out
    assert mutations(fake_gh) == []


def test_default_mode_resolve_mine_resolves_threads_the_account_spoke_last_in(monkeypatch, capsys, fake_gh):
    run(monkeypatch, "7", "--resolve-mine")
    out = capsys.readouterr().out
    resolved = mutations(fake_gh)
    assert len(resolved) == 2
    assert 'threadId:"T_owner"' in resolved[0] and 'threadId:"T_agent"' in resolved[1]
    assert "  resolved src/x.py:1" in out and "  resolved src/x.py:2" in out
    assert "  UNANSWERED src/x.py:3 coderabbitai c31" in out
    assert "REVIEW" not in out  # review bodies are listed only without --resolve-mine


def test_autodev_mode_refuses_resolve_mine_before_any_gh_call(monkeypatch, capsys, fake_gh):
    with pytest.raises(SystemExit) as raised:
        run(monkeypatch, "7", "--mode", "autodev", "--resolve-mine")
    assert raised.value.code == 2
    assert "--resolve-mine is not used in autodev mode" in capsys.readouterr().err
    assert fake_gh == []


def test_autodev_mode_shows_the_owners_reviews_and_counts_only_marked_answers(monkeypatch, capsys, fake_gh):
    run(monkeypatch, "7", "--mode", "autodev")
    out = capsys.readouterr().out
    assert "  REVIEW WonderMr 2026-10-14T08:00:00Z: Owner review body" in out
    assert "Copilot overview" in out
    assert "Earlier head" not in out
    # The account's unmarked comment may be the owner's: the agent has not answered that thread.
    assert "  UNANSWERED src/x.py:1 coderabbitai c11" in out
    assert "  answered src/x.py:2 coderabbitai c21" in out
    assert "  UNANSWERED src/x.py:3 coderabbitai c31" in out
    assert mutations(fake_gh) == []


def test_show_refuses_resolve_mine_in_autodev_mode(fake_gh):
    with pytest.raises(ValueError):
        pr_threads.show(REPO, 7, ME, True, pr_threads.AUTODEV)
    assert fake_gh == []


@pytest.mark.parametrize("body, expected", [
    ("<!-- autodev -->", True),
    ("<!-- autodev -->\nFixed.", True),
    ("<!-- autodev -->\r\nFixed.", True),
    ("Fixed. <!-- autodev -->", False),
    (" <!-- autodev -->\nFixed.", False),
    ("<!-- autodev:state {} -->", False),
    (None, False),
])
def test_marker_is_the_first_line(body, expected):
    assert pr_threads.marked(body) is expected
