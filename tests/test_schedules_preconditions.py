"""The real-run preconditions (src/schedules/preconditions.py, #347) on synthetic answers.

The answers come from tests/fixtures/autodev_preconditions_synthetic.json (synthetic, in the shape
GitHub's REST reference gives); a fake transport serves them. No network, no keychain.
"""
import copy
import json
from pathlib import Path

import pytest

from src.schedules import github, preconditions
from src.schedules.broker import Credentials
from src.schedules.github import GitHubClient

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "autodev_preconditions_synthetic.json").read_text())
REPOSITORY = FIXTURE["repository"]
TOKEN = "fake-precondition-token"
KEY = "fake deploy key material for the precondition tests"
RULES = f"/repos/{REPOSITORY}/rules/branches/main?per_page=100"
WORKFLOW = f"/repos/{REPOSITORY}/actions/permissions/workflow"
SECRETS = f"/repos/{REPOSITORY}/actions/secrets?per_page=100&page=1"


class Transport:
    """Serves the fixture's answers by path and query; ``statuses`` turns a path into an error."""

    def __init__(self, responses=None, statuses=None, link=None):
        self.responses = copy.deepcopy(FIXTURE["responses"] if responses is None else responses)
        self.statuses = statuses or {}
        self.link = link or {}
        self.requests = []

    def __call__(self, url, headers):
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        key = url.removeprefix(github.API)
        self.requests.append(key)
        if key in self.statuses:
            return self.statuses[key], {}, b'{"message": "Resource not accessible by personal access token"}'
        if key not in self.responses:
            return 404, {}, b'{"message": "Not Found"}'
        return 200, ({"Link": self.link[key]} if key in self.link else {}), json.dumps(self.responses[key]).encode()


@pytest.fixture
def credentials(tmp_path):
    key = tmp_path / "keys" / "deploy_key"
    key.parent.mkdir()
    key.write_text(KEY + "\n")
    (key.parent / "github_known_hosts").write_text("github.com ssh-ed25519 placeholder\n")
    return Credentials(key, key.parent / "github_known_hosts", lambda: TOKEN)


def check(credentials, transport=None):
    transport = transport or Transport()
    client = GitHubClient(REPOSITORY, lambda: TOKEN, transport=transport)
    return preconditions.check(client, credentials, repository=REPOSITORY), transport


def without_rule(rule):
    responses = copy.deepcopy(FIXTURE["responses"])
    responses[RULES] = [item for item in responses[RULES] if item["type"] != rule]
    return Transport(responses)


def with_answer(path, value):
    responses = copy.deepcopy(FIXTURE["responses"])
    responses[path] = value
    return Transport(responses)


def review_count(count):
    responses = copy.deepcopy(FIXTURE["responses"])
    for item in responses[RULES]:
        if item["type"] == "pull_request":
            item["parameters"]["required_approving_review_count"] = count
    return Transport(responses)


def test_the_synthetic_state_meets_every_precondition(credentials):
    missing, transport = check(credentials)
    assert missing == [] and transport.requests == [RULES, WORKFLOW, SECRETS]
    assert FIXTURE["synthetic"] is True


def unavailable():
    raise github.TokenUnavailable("the keychain item agents-autodev-github could not be read")


def change_credentials(credentials, what):
    if what == "key":
        credentials.deploy_key.unlink()
    elif what == "hosts symlink":
        credentials.known_hosts.unlink()
        credentials.known_hosts.symlink_to(credentials.deploy_key)
    elif what == "token":
        return Credentials(credentials.deploy_key, credentials.known_hosts, unavailable)
    return credentials


CASES = [
    ("no deletion rule", lambda: without_rule("deletion"), None, preconditions.rule_item("deletion")),
    ("no non_fast_forward rule", lambda: without_rule("non_fast_forward"), None,
     preconditions.rule_item("non_fast_forward")),
    ("no pull_request rule", lambda: without_rule("pull_request"), None, preconditions.REVIEW_ITEM),
    ("no required approval", lambda: review_count(0), None, preconditions.REVIEW_ITEM),
    ("workflows may approve", lambda: with_answer(WORKFLOW, {"default_workflow_permissions": "read",
                                                             "can_approve_pull_request_reviews": True}),
     None, preconditions.WORKFLOW_ITEM),
    ("workflow permission 403", lambda: Transport(statuses={WORKFLOW: 403}), None,
     "cannot verify the workflow approval permission (HTTP 403)"),
    ("rules 404", lambda: Transport(statuses={RULES: 404}), None, "cannot verify main protection (HTTP 404)"),
    ("secrets 403", lambda: Transport(statuses={SECRETS: 403}), None,
     "cannot verify the repository secrets (HTTP 403)"),
    ("routine token as a repository secret",
     lambda: with_answer(SECRETS, {"total_count": 2, "secrets": [
         {"name": "OTHER", "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"},
         {"name": "CLAUDE_ROUTINE_TOKEN", "created_at": "2026-10-01T00:00:00Z",
          "updated_at": "2026-10-01T00:00:00Z"}]}), None, preconditions.SECRET_ITEM),
    ("deploy key missing", Transport, "key", "the deploy key"),
    ("host keys a symlink", Transport, "hosts symlink", "the pinned github.com host keys"),
    ("token unavailable", Transport, "token", "the API token"),
]


@pytest.mark.parametrize("name, transport, credential_change, item", CASES, ids=[case[0] for case in CASES])
def test_each_missing_item_stops_the_run_named(credentials, name, transport, credential_change, item):
    missing, used = check(change_credentials(credentials, credential_change), transport())
    assert missing == [item]
    outcome = preconditions.refusal(missing)
    assert outcome["status"] == "refused" and outcome["missing"] == [item] and item in outcome["reason"]
    text = json.dumps(outcome)
    assert TOKEN not in text and KEY not in text
    if credential_change == "token":
        assert used.requests == []  # without the token nothing is read


def test_all_items_together(credentials):
    responses = copy.deepcopy(FIXTURE["responses"])
    responses[RULES] = [{"type": "required_linear_history"}]
    responses[WORKFLOW]["can_approve_pull_request_reviews"] = True
    responses[SECRETS] = {"total_count": 1, "secrets": [{"name": "CLAUDE_ROUTINE_TOKEN"}]}
    credentials.known_hosts.unlink()
    missing, _ = check(credentials, Transport(responses))
    assert missing == ["the pinned github.com host keys", preconditions.rule_item("deletion"),
                       preconditions.rule_item("non_fast_forward"), preconditions.REVIEW_ITEM,
                       preconditions.WORKFLOW_ITEM, preconditions.SECRET_ITEM]


def test_no_credentials_at_all(credentials):
    missing, transport = check(None)
    assert missing == ["the deploy key", "the pinned github.com host keys", "the API token"]
    assert transport.requests == []


@pytest.mark.parametrize("path, value, item", [
    (RULES, {"message": "not a list"}, "cannot verify main protection (unexpected answer)"),
    (RULES, ["deletion"], "cannot verify main protection (unexpected answer)"),
    (WORKFLOW, {"default_workflow_permissions": "read"}, "cannot verify the workflow approval permission "
                                                         "(unexpected answer)"),
    (SECRETS, [{"name": "x"}], "cannot verify the repository secrets (unexpected answer)"),
])
def test_an_unexpected_answer_cannot_verify(credentials, path, value, item):
    missing, _ = check(credentials, with_answer(path, value))
    assert missing == [item]


def test_the_secrets_are_read_page_by_page(credentials):
    responses = copy.deepcopy(FIXTURE["responses"])
    responses[SECRETS] = {"total_count": 101, "secrets": [{"name": f"S{number}"} for number in range(100)]}
    responses[SECRETS.removesuffix("&page=1") + "&page=2"] = {"total_count": 101,
                                                              "secrets": [{"name": "CLAUDE_ROUTINE_TOKEN"}]}
    missing, transport = check(credentials, Transport(responses))
    assert missing == [preconditions.SECRET_ITEM] and transport.requests[-1].endswith("page=2")


def test_a_network_failure_cannot_verify(credentials):
    def offline(url, headers):
        raise github.NetworkError("GitHub is unreachable: URLError")
    missing, _ = check(credentials, offline)
    assert missing == [f"cannot verify {what} (GitHub unreachable)" for what in preconditions.WHAT.values()]


def test_only_reads_under_the_repository(credentials):
    _, transport = check(credentials)
    assert all(path.startswith(f"/repos/{REPOSITORY}/") for path in transport.requests)
