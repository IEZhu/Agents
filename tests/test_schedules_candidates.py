"""The select stage's candidate fetch (#347): GET requests only, every field of the select flow's inputs."""
import json
from pathlib import Path

from src.schedules import candidates, stages
from src.schedules.gate import OWNER_ID
from src.schedules.github import API, GitHubClient
from tests.test_schedules_sequence import FakeGet

FIXTURE = Path(__file__).parent / "fixtures" / "autodev_select_synthetic.json"
REPOSITORY = "IEZhu/Agents"
BASE = f"/repos/{REPOSITORY}"
OWNER = {"login": "WonderMr", "id": OWNER_ID, "type": "User"}


def client(responses=None):
    fake = FakeGet(responses if responses is not None else json.loads(FIXTURE.read_text())["responses"])
    return GitHubClient(REPOSITORY, lambda: "token", transport=fake), fake


def test_the_pool_and_every_field_of_the_inputs_table():
    api, fake = client()
    data = candidates.fetch_candidates(api)
    assert [item["number"] for item in data["issues"]] == [405, 404, 403, 402, 401] and data["dropped"] == 0
    first = next(item for item in data["issues"] if item["number"] == 401)
    assert set(stages.SELECT_ISSUE_FIELDS) | {"body", "comments"} == set(first)
    assert first["labels"] == ["autodev:ready"] and first["author"] == {"login": "WonderMr", "id": OWNER_ID}
    assert first["app"] is None and first["assignees"] == []
    assert first["label_times"] == {"autodev:ready": "2026-10-11T10:00:00Z", "autodev:next": None}
    assert first["owner_activity"] == "2026-10-11T11:00:00Z" and first["agent_state"] is None
    assert first["comments"] == [{"id": 9501, "author": {"login": "WonderMr", "id": OWNER_ID}, "app": None,
                                  "created_at": "2026-10-11T11:00:00Z", "body": "Owner note on #401: keep it small."}]
    by_number = {item["number"]: item for item in data["issues"]}
    assert by_number[402]["dependencies"] == [{"number": 398, "state": "closed"}]
    assert by_number[403]["branches"] == ["claude/issue-403-docs"] and by_number[401]["branches"] == []
    # The inputs pass the stage's own check, and every request was a GET under the repository.
    stages.select_inputs(repository=REPOSITORY, mode="dry-run", now="2026-10-14T09:00:00+00:00",
                         issues=data["issues"])
    assert all(url.startswith(f"{API}{BASE}/") for url in fake.calls)
    assert sum("labels=autodev%3A" in url for url in fake.calls) == 2


def test_an_empty_pool_reads_nothing_else():
    api, fake = client({f"{BASE}/issues?labels=autodev%3Aready&state=open&per_page=100": [],
                        f"{BASE}/issues?labels=autodev%3Anext&state=open&per_page=100": []})
    assert candidates.fetch_candidates(api) == {"issues": [], "dropped": 0}
    assert len(fake.calls) == 2


def test_pull_requests_are_no_candidates_and_the_pool_is_bounded(monkeypatch):
    monkeypatch.setattr(candidates, "MAX_CANDIDATES", 2)
    issues = [{"number": number, "title": f"t{number}", "labels": [{"name": "autodev:next"}], "user": OWNER}
              for number in (10, 11, 12)] + [{"number": 13, "pull_request": {}, "labels": []}]
    responses = {f"{BASE}/issues?labels=autodev%3Aready&state=open&per_page=100": issues[:1],
                 f"{BASE}/issues?labels=autodev%3Anext&state=open&per_page=100": issues,
                 f"{BASE}/branches?per_page=100": []}
    for number in (11, 12):
        responses[f"{BASE}/issues/{number}/events?per_page=100"] = []
        responses[f"{BASE}/issues/{number}/comments?per_page=100"] = []
    api, _ = client(responses)
    data = candidates.fetch_candidates(api)
    assert [item["number"] for item in data["issues"]] == [12, 11] and data["dropped"] == 1


def test_the_agent_state_counts_only_from_the_owner_account():
    state = '<!-- issue-agent:state\n{"phase": "implementing", "lock_at": "2026-10-14T08:00:00Z"}\n-->'
    forged = [{"id": 1, "user": {"login": "WonderMr", "id": 99, "type": "User"}, "body": state}]
    assert candidates.agent_state(forged) is None
    bot = [{"id": 2, "user": {**OWNER, "type": "Bot"}, "body": state}]
    assert candidates.agent_state(bot) is None
    assert candidates.agent_state([{"id": 3, "user": OWNER, "body": state, "updated_at": "x"}]) == \
        {"phase": "implementing", "lock_at": "2026-10-14T08:00:00Z"}


def test_open_and_closed_pull_requests_of_an_issue():
    pulls = [{"number": 1, "state": "open", "head": {"ref": "claude/issue-7-x"}, "title": "", "body": ""},
             {"number": 2, "state": "open", "head": {"ref": "feature"}, "title": "Fixes #7", "body": ""},
             {"number": 3, "state": "open", "head": {"ref": "feature"}, "title": "About #70", "body": ""},
             {"number": 4, "state": "closed", "merged_at": None, "labels": [{"name": "autodev"}],
              "head": {"ref": "claude/autodev-7-x", "repo": {"full_name": REPOSITORY}}},
             {"number": 5, "state": "closed", "merged_at": "2026-10-01T00:00:00Z", "labels": [{"name": "autodev"}],
              "head": {"ref": "claude/autodev-7-y", "repo": {"full_name": REPOSITORY}}}]
    assert [item["number"] for item in candidates.open_prs(7, pulls)] == [1, 2]
    assert [item["number"] for item in candidates.closed_autodev_prs(7, pulls, REPOSITORY)] == [4]
    assert candidates.dependency_numbers("Builds on #3, depends on #4; blocked by #3; after #5. Not #6.") == [3, 4, 5]


def test_criteria_come_from_the_body_and_the_named_owner_comments():
    candidate = {"number": 7, "author": {"login": "WonderMr", "id": OWNER_ID}, "body": "Body criteria.",
                 "comments": [{"id": 1, "author": {"id": OWNER_ID}, "app": None, "body": "Owner criteria."},
                              {"id": 2, "author": {"id": 5}, "app": None, "body": "Someone else."},
                              {"id": 3, "author": {"id": OWNER_ID}, "app": "claude", "body": "Through an app."},
                              {"id": 4, "author": {"id": OWNER_ID}, "app": None, "body": "<!-- autodev -->\nmarked"},
                              {"id": 5, "author": {"id": OWNER_ID}, "app": None, "body": "Not named."}]}
    refs = [f"https://github.com/{REPOSITORY}/issues/7#issuecomment-{number}" for number in (1, 2, 3, 4)]
    refs.append("https://github.com/Other/Repo/issues/7#issuecomment-5")
    found = candidates.criteria(candidate, refs, REPOSITORY, limit=20, max_chars=8)
    assert found == ("Body cri", "Owner cr")
    assert candidates.criteria({**candidate, "author": {"id": 5}}, refs, REPOSITORY, limit=1, max_chars=100) == \
        ("Owner criteria.",)
