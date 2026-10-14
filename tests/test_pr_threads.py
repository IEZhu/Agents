"""The PR thread helper in its default mode and in the autodev mode of flows/pr-review.md (#346).

Offline: `gh` is replaced by a fake that answers from recorded shapes and records every call.
The fake's GraphQL answer holds only the comment fields the query asks for, as GitHub's does.
"""
import importlib.util
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pr_threads", ROOT / "scripts" / "dev" / "pr_threads.py")
pr_threads = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pr_threads)

REPO, ME, HEAD, OLD = "IEZhu/Agents", "WonderMr", "a" * 40, "b" * 40
OWNER_ID, BOT, BOT_ID = 5370211, "coderabbitai", 136622811  # GraphQL names the bot without `[bot]`
COPILOT, COPILOT_ID = "copilot-pull-request-reviewer[bot]", 175728472


def comment(database_id, author, body, kind="Bot", account_id=BOT_ID):
    return {"databaseId": database_id, "author": {"__typename": kind, "login": author, "databaseId": account_id},
            "body": body}


def owner(database_id, body):
    return comment(database_id, ME, body, "User", OWNER_ID)


def thread(thread_id, line, first, last, resolved=False):
    return {"id": thread_id, "isResolved": resolved, "path": "src/x.py", "line": line, "first": first, "last": last}


THREADS = [
    # The bot's finding, answered by an unmarked comment of the account: the owner, or a default-mode reply.
    thread("T_owner", 1, comment(11, BOT, "Finding one"), owner(12, "Fixed in abc1234.")),
    # The bot's finding, answered by the agent with its marker line.
    thread("T_agent", 2, comment(21, BOT, "Finding two"), owner(22, "<!-- autodev -->\nFixed in abc1234.")),
    # The bot spoke last.
    thread("T_bot", 3, comment(31, BOT, "Finding three"), comment(31, BOT, "Finding three")),
    thread("T_done", 4, comment(41, BOT, "Old finding"), owner(42, "Fixed."), resolved=True),
    # A user account that took the bot's GraphQL login: never a trusted source.
    thread("T_user", 5, comment(51, BOT, "Look like a bot", "User", 99), comment(51, BOT, "Look like a bot", "User", 99)),
]
REVIEWS = [[
    {"user": {"login": ME, "id": OWNER_ID}, "commit_id": HEAD, "submitted_at": "2026-10-14T08:00:00Z",
     "body": "Owner review body"},
    {"user": {"login": COPILOT, "id": COPILOT_ID}, "commit_id": HEAD,
     "submitted_at": "2026-10-14T08:05:00Z", "body": "Copilot overview"},
    {"user": {"login": COPILOT, "id": 1}, "commit_id": HEAD, "submitted_at": "2026-10-14T08:06:00Z",
     "body": "Copilot's login, another account"},
    {"user": {"login": f"{BOT}[bot]", "id": BOT_ID}, "commit_id": OLD, "submitted_at": "2026-10-13T08:00:00Z",
     "body": "Earlier head"},
    {"user": {"login": f"{BOT}[bot]", "id": BOT_ID}, "commit_id": HEAD, "submitted_at": "2026-10-14T08:10:00Z",
     "body": "  "},
]]


def selection(query, alias):
    """The fields a GraphQL query selects for each comment of ``alias`` (``first`` or ``last``)."""
    start = query.index("nodes{", query.index(f"{alias}:comments(")) + len("nodes{")
    depth, end = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(query[end], 0)
        end += 1
    return query[start:end - 1]


def answer(node, fields):
    """A recorded comment as GitHub answers ``fields``: only what they name."""
    author = node["author"]
    shaped = {"databaseId": node["databaseId"], "author": {"login": author["login"]}}
    if "__typename" in fields:
        shaped["author"]["__typename"] = author["__typename"]
    if "... on Bot{databaseId}" in fields and author["__typename"] == "Bot":
        shaped["author"]["databaseId"] = author["databaseId"]
    if re.search(r"\bbody\b", fields):
        shaped["body"] = node["body"]
    return shaped


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
            query = args[3].removeprefix("query=")
            first, last = selection(query, "first"), selection(query, "last")
            nodes = [{**item, "first": {"nodes": [answer(item["first"], first)]},
                      "last": {"nodes": [answer(item["last"], last)]}} for item in THREADS]
            return json.dumps({"data": {"repository": {"pullRequest": {
                "headRefOid": HEAD,
                "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}})
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
    monkeypatch.setattr(pr_threads, "trusted", lambda user: pytest.fail("the default mode marks no source"))
    run(monkeypatch, "7")
    out = capsys.readouterr().out
    assert "#7 head aaaaaaa: 4 unresolved of 5 threads" in out
    assert "  answered src/x.py:1 coderabbitai c11\n" in out
    assert "  answered src/x.py:2 coderabbitai c21\n" in out
    assert "  UNANSWERED src/x.py:3 coderabbitai c31\n" in out
    assert "  UNANSWERED src/x.py:5 coderabbitai c51\n" in out
    assert "  REVIEW copilot-pull-request-reviewer[bot] 2026-10-14T08:05:00Z: Copilot overview" in out
    assert "Owner review body" not in out  # the logged-in account's own review
    assert "Earlier head" not in out and "Old finding" not in out
    assert mutations(fake_gh) == []
    # The default mode keeps its query: no last-comment body, no author type.
    queries = [args[3] for args in fake_gh if args[:2] == ("api", "graphql")]
    assert queries == [f"query={pr_threads.QUERY}"]
    assert "body" not in selection(pr_threads.QUERY, "last") and "__typename" not in pr_threads.QUERY


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
    assert "  REVIEW WonderMr 2026-10-14T08:00:00Z untrusted: Owner review body" in out
    assert "  REVIEW copilot-pull-request-reviewer[bot] 2026-10-14T08:05:00Z TRUSTED: Copilot overview" in out
    assert ("  REVIEW copilot-pull-request-reviewer[bot] 2026-10-14T08:06:00Z untrusted: Copilot's login, "
            "another account") in out
    assert "Earlier head" not in out
    # The account's unmarked comment may be the owner's: the agent has not answered that thread.
    assert "  UNANSWERED src/x.py:1 coderabbitai c11 TRUSTED\n" in out
    assert "  answered src/x.py:2 coderabbitai c21 TRUSTED\n" in out
    assert "  UNANSWERED src/x.py:3 coderabbitai c31 TRUSTED\n" in out
    # A User node with the bot's login is not the bot.
    assert "  UNANSWERED src/x.py:5 coderabbitai c51 untrusted\n" in out
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


@pytest.mark.parametrize("author, expected", [
    ({"__typename": "Bot", "login": BOT, "databaseId": BOT_ID}, True),
    ({"__typename": "Bot", "login": "copilot-pull-request-reviewer", "databaseId": COPILOT_ID}, True),
    ({"__typename": "Bot", "login": BOT, "databaseId": 1}, False),
    ({"__typename": "Bot", "login": BOT}, False),
    ({"__typename": "Bot", "login": f"{BOT}[bot]", "databaseId": BOT_ID}, False),
    ({"__typename": "User", "login": BOT, "databaseId": BOT_ID}, False),
    ({"login": BOT, "databaseId": BOT_ID}, False),
    (None, False),
])
def test_a_graphql_author_is_trusted_only_as_an_allowlisted_bot_node(author, expected):
    assert pr_threads.trusted(pr_threads.graphql_user({"author": author})) is expected
