"""Exercise the template and installed bridge without GitHub or Claude access."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import urllib.error
import urllib.parse

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts/templates/issue-agent-bridge.yml"
INSTALLED = ROOT / ".github/workflows/issue-agent-bridge.yml"
OWNER = "repository-owner"
REPO = "example/project"
GH_TOKEN = "github-secret-test-token"
ROUTINE_TOKEN = "routine-secret-test-token"
STATUS_ID = 9001
SOURCE_ID = 1234
RAW_PRIVATE = "private-account-and-response-body"


@pytest.fixture(params=[TEMPLATE, INSTALLED], ids=["template", "installed"])
def workflow_path(request):
    return request.param


@pytest.fixture
def workflow(workflow_path):
    # BaseLoader preserves GitHub's `on` key instead of interpreting it as True.
    return yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)


@pytest.fixture
def bridge_namespace(workflow, workflow_path):
    step = next(step for step in workflow["jobs"]["fire"]["steps"] if step.get("id") == "dispatch")
    namespace = {"__name__": "bridge_under_test"}
    exec(compile(step["run"], str(workflow_path), "exec"), namespace)
    return namespace


def comment(body, *, login=OWNER, user_type="User", comment_id=SOURCE_ID):
    return {"id": comment_id, "body": body, "user": {"login": login, "type": user_type}}


def acknowledgement(*, login=OWNER, bridge_id=STATUS_ID, user_type="User"):
    return comment(
        f"<!-- issue-agent -->\n<!-- issue-agent:started bridge_comment_id={bridge_id} -->\nWorking on it.",
        login=login,
        user_type=user_type,
    )


class BridgeRun:
    """A small API fake with real event/output files and an advancing clock."""

    def __init__(self, namespace, tmp_path, monkeypatch):
        self.namespace = namespace
        self.event_path = tmp_path / "event.json"
        self.output_path = tmp_path / "output.txt"
        self.output_path.touch()
        self.event = {"action": "created", "issue": {"number": 17}, "comment": comment("/agent work")}
        self.existing_comments = []
        self.startup_comments = [acknowledgement()]
        self.calls = []
        self.statuses = []
        self.fire_response = (200, {"claude_code_session_id": "session_Abc123"}, {})
        self.reaction_status = 201
        self.poll_request_seconds = 0
        self.elapsed = 0
        self.sleeps = []
        for name, value in {
            "GITHUB_EVENT_PATH": str(self.event_path),
            "GITHUB_OUTPUT": str(self.output_path),
            "GITHUB_EVENT_NAME": "issue_comment",
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_API_URL": "https://api.github.test",
            "GITHUB_SERVER_URL": "https://github.test",
            "GITHUB_RUN_ID": "456",
            "AGENT_OWNER": OWNER,
            "GH_TOKEN": GH_TOKEN,
            "ROUTINE_TOKEN": ROUTINE_TOKEN,
            "ROUTINE_ID": "trig_Abc123",
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setitem(namespace, "request", self.request)
        monkeypatch.setitem(namespace, "time", SimpleNamespace(monotonic=lambda: self.elapsed, sleep=self.sleep))

    def sleep(self, seconds):
        assert 0 < seconds <= 15
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def request(self, method, url, token, body=None, anthropic=False):
        self.calls.append({"method": method, "url": url, "token": token, "body": body, "anthropic": anthropic})
        if anthropic:
            assert method == "POST"
            assert url == "https://api.anthropic.com/v1/claude_code/routines/trig_Abc123/fire"
            assert token == ROUTINE_TOKEN
            if isinstance(self.fire_response, Exception):
                raise self.fire_response
            return self.fire_response
        assert token == GH_TOKEN
        parsed = urllib.parse.urlsplit(url)
        assert parsed.scheme == "https" and parsed.netloc == "api.github.test"
        path = parsed.path.removeprefix(f"/repos/{REPO}")
        if method == "GET" and path == "/issues/17/comments":
            query = urllib.parse.parse_qs(parsed.query)
            assert query["per_page"] == ["100"]
            page = int(query["page"][0])
            comments = self.startup_comments if "since" in query else self.existing_comments
            if "since" in query:
                self.elapsed += self.poll_request_seconds
            return 200, comments[(page - 1) * 100:page * 100], {}
        if method == "POST" and path == "/issues/17/comments":
            self.statuses.append(body["body"])
            self.existing_comments.append(comment(body["body"], login="github-actions[bot]", user_type="Bot", comment_id=STATUS_ID))
            return 201, {"id": STATUS_ID}, {}
        if method == "PATCH" and path == f"/issues/comments/{STATUS_ID}":
            self.statuses.append(body["body"])
            self.existing_comments[-1]["body"] = body["body"]
            return 200, {"id": STATUS_ID}, {}
        if method == "POST" and path.endswith("/reactions"):
            assert body == {"content": "eyes"}
            if isinstance(self.reaction_status, Exception):
                raise self.reaction_status
            return self.reaction_status, {"id": 501}, {}
        pytest.fail(f"Unexpected request: {method} {url}")

    @property
    def fires(self):
        return [call for call in self.calls if call["anthropic"]]

    @property
    def reactions(self):
        return [call for call in self.calls if call["url"].endswith("/reactions")]

    def run(self):
        self.event_path.write_text(json.dumps(self.event))
        self.namespace["main"]()

    def review(self, monkeypatch, login="coderabbitai[bot]"):
        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_review")
        self.event = {
            "action": "submitted",
            "review": comment("/agent work", login=login, user_type="Bot" if login.endswith("[bot]") else "User"),
            "pull_request": {"number": 17, "head": {"ref": "claude/issue-17", "repo": {"full_name": REPO}}},
        }


@pytest.fixture
def bridge(bridge_namespace, tmp_path, monkeypatch):
    return BridgeRun(bridge_namespace, tmp_path, monkeypatch)


def test_dispatch_permissions_cover_issue_and_pull_request_comments(workflow):
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["fire"]["permissions"] == {
        "issues": "write",
        "pull-requests": "write",
    }


def test_completion_permissions_cover_issue_and_pull_request_reactions(workflow):
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["complete"]["permissions"] == {
        "issues": "write",
        "pull-requests": "write",
    }


def test_owner_review_command_on_pull_request_starts_once(bridge):
    bridge.event["issue"]["pull_request"] = {"url": f"https://api.github.test/repos/{REPO}/pulls/17"}
    bridge.event["comment"]["body"] = "/agent review"
    bridge.run()
    assert len(bridge.fires) == 1
    assert "event=issue_comment number=17" in bridge.fires[0]["body"]["text"]
    assert bridge.reactions[0]["url"].endswith(f"/issues/comments/{SOURCE_ID}/reactions")
    assert "Claude confirmed startup" in bridge.statuses[-1]


def test_workflow_subscribes_only_to_new_owner_comments(workflow):
    assert workflow["on"] == {"issue_comment": {"types": ["created"]}}
    condition = " ".join(workflow["jobs"]["fire"]["if"].split())
    for guard in (
        "github.event_name == 'issue_comment'",
        "vars.AGENT_OWNER != ''",
        "github.event.comment.user.type == 'User'",
        "github.event.comment.user.login == vars.AGENT_OWNER",
    ):
        assert guard in condition
    assert "pull_request_review" not in condition
    assert "||" not in condition


@pytest.mark.parametrize("body", ["/agent", "/agent work", "/agent plan", "/agent\nstatus", "/agent\tstop"])
def test_owner_exact_command_is_dispatched(bridge, body):
    bridge.event["comment"]["body"] = body
    bridge.run()
    assert len(bridge.fires) == 1
    assert "Claude confirmed startup" in bridge.statuses[-1]


@pytest.mark.parametrize("body", ["/agentx", " /agent", "/Agent", "please /agent", "/agent <!-- issue-agent -->"])
def test_noncommands_and_agent_markers_are_ignored(bridge, body):
    bridge.event["comment"]["body"] = body
    bridge.run()
    assert bridge.calls == []
    assert bridge.output_path.read_text() == ""


@pytest.mark.parametrize("change", ["different_owner", "owner_case", "edited", "deleted"])
def test_untrusted_or_wrong_comment_events_are_ignored(bridge, change):
    if change == "different_owner":
        bridge.event["comment"]["user"]["login"] = "outsider"
    elif change == "owner_case":
        bridge.event["comment"]["user"]["login"] = OWNER.upper()
    else:
        bridge.event["action"] = change
    bridge.run()
    assert bridge.calls == []
    assert bridge.output_path.read_text() == ""


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "NONE"])
def test_other_people_cannot_authorize_commands_through_role_or_display_name(bridge, association):
    bridge.event["comment"]["user"].update(login="another-person", name=OWNER)
    bridge.event["comment"]["author_association"] = association
    bridge.run()
    assert bridge.calls == []
    assert bridge.output_path.read_text() == ""


@pytest.mark.parametrize("login", ["coderabbitai[bot]", "copilot-pull-request-reviewer[bot]", "github-actions[bot]", "automation-account"])
def test_bot_comments_are_rejected_even_if_configured_as_owner(bridge, monkeypatch, login):
    monkeypatch.setenv("AGENT_OWNER", login)
    bridge.event["comment"]["user"].update(login=login, type="Bot")
    bridge.run()
    assert bridge.calls == []
    assert bridge.output_path.read_text() == ""


@pytest.mark.parametrize("user_type", ["Mannequin", None])
def test_comment_requires_a_human_user_type(bridge, user_type):
    if user_type is None:
        del bridge.event["comment"]["user"]["type"]
    else:
        bridge.event["comment"]["user"]["type"] = user_type
    bridge.run()
    assert bridge.calls == []
    assert bridge.output_path.read_text() == ""


@pytest.mark.parametrize("owner_setting", [None, ""])
def test_unconfigured_owner_fails_closed(bridge, monkeypatch, owner_setting):
    if owner_setting is None:
        monkeypatch.delenv("AGENT_OWNER")
    else:
        monkeypatch.setenv("AGENT_OWNER", owner_setting)
    bridge.event["comment"]["user"]["login"] = ""
    bridge.run()
    assert bridge.calls == []
    assert bridge.output_path.read_text() == ""


@pytest.mark.parametrize("login", [OWNER, "another-person", "coderabbitai[bot]", "copilot-pull-request-reviewer[bot]", "other[bot]"])
def test_reviews_never_dispatch_even_on_agent_branch_in_same_repository(bridge, monkeypatch, login):
    bridge.review(monkeypatch, login)
    bridge.run()
    assert bridge.calls == []
    assert bridge.output_path.read_text() == ""


@pytest.mark.parametrize("event_name", ["pull_request_review", "pull_request_review_comment", "pull_request", "push"])
def test_unsubscribed_event_is_ignored_without_comment_fields(bridge, monkeypatch, event_name):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    bridge.event = {}
    bridge.run()
    assert bridge.calls == []
    assert bridge.output_path.read_text() == ""


def test_literal_command_is_never_executed_or_forwarded(bridge, tmp_path):
    sentinel = tmp_path / "command-executed"
    command = f"/agent $(touch {sentinel}) `touch {sentinel}`\nOWNER-COMMAND-PRIVATE"
    bridge.event["comment"]["body"] = command
    bridge.run()
    assert not sentinel.exists()
    assert bridge.fires[0]["body"] == {"text": f"repo={REPO} event=issue_comment number=17 comment_id={SOURCE_ID} bridge_comment_id={STATUS_ID}"}
    assert OWNER not in bridge.fires[0]["body"]["text"]
    assert command not in json.dumps(bridge.calls)
    assert "OWNER-COMMAND-PRIVATE" not in "\n".join(bridge.statuses)
    assert bridge.reactions[0]["url"].endswith(f"/issues/comments/{SOURCE_ID}/reactions")
    assert bridge.calls.index(bridge.reactions[0]) < bridge.calls.index(bridge.fires[0])


@pytest.mark.parametrize("bad_ack", [
    acknowledgement(login="outsider"),
    acknowledgement(login=OWNER.upper()),
    acknowledgement(user_type="Bot"),
    acknowledgement(bridge_id=STATUS_ID + 1),
    comment(f"preface\n<!-- issue-agent -->\n<!-- issue-agent:started bridge_comment_id={STATUS_ID} -->"),
    comment(f"<!-- issue-agent -->\n\n<!-- issue-agent:started bridge_comment_id={STATUS_ID} -->"),
    comment(f"<!-- issue-agent -->\n<!-- issue-agent:started bridge_comment_id={STATUS_ID} --> extra"),
])
def test_spoofed_or_malformed_ack_does_not_confirm_startup(bridge, bad_ack):
    bridge.startup_comments = [bad_ack]
    bridge.run()
    assert bridge.elapsed == 300
    assert "did not confirm startup within 5 minutes" in bridge.statuses[-1]
    assert "Claude confirmed startup" not in "\n".join(bridge.statuses)
    assert len(bridge.fires) == 1


def test_exact_owner_ack_confirms_without_waiting(bridge):
    bridge.startup_comments = [acknowledgement(login="outsider"), acknowledgement()]
    bridge.run()
    assert bridge.sleeps == []
    assert "Claude confirmed startup" in bridge.statuses[-1]
    assert "https://claude.ai/code/session_Abc123" in bridge.statuses[-1]
    assert all(f"<!-- issue-agent:bridge event=issue_comment id={SOURCE_ID} -->" == body.splitlines()[0] for body in bridge.statuses)


def test_five_minutes_without_ack_reports_uncertainty_and_session_link(bridge):
    bridge.startup_comments = []
    bridge.run()
    assert sum(bridge.sleeps) == 300
    final = bridge.statuses[-1]
    assert "did not confirm startup within 5 minutes" in final
    assert "https://claude.ai/code/session_Abc123" in final
    assert "Open the session" in final
    assert "may be blocking" in final
    assert "No automatic retry" in final
    assert final.splitlines()[1] == "<!-- issue-agent:reaction id=501 -->"
    assert "keep_reaction=true" not in bridge.output_path.read_text()
    assert len(bridge.fires) == 1


def test_startup_watch_stops_paging_at_deadline(bridge):
    bridge.startup_comments = [comment("Other discussion", comment_id=i) for i in range(3000)]
    bridge.poll_request_seconds = 15
    bridge.run()
    polls = [call for call in bridge.calls if "since=" in call["url"]]
    assert len(polls) == 20
    assert bridge.elapsed == 300
    assert bridge.sleeps == []
    assert "did not confirm startup within 5 minutes" in bridge.statuses[-1]


def test_startup_ack_is_found_after_first_page(bridge):
    bridge.startup_comments = [comment("Other discussion", comment_id=i) for i in range(100)]
    bridge.startup_comments.append(acknowledgement())
    bridge.run()
    polls = [call for call in bridge.calls if "since=" in call["url"]]
    assert len(polls) == 2
    assert "page=2" in polls[-1]["url"]
    assert "Claude confirmed startup" in bridge.statuses[-1]


@pytest.mark.parametrize("retry_after, expected", [("120", "Retry after 120 seconds"), ("0015", "Retry after 15 seconds"), (RAW_PRIVATE + "\n::warning::", "Try again after the limit resets")])
def test_rate_limit_is_sanitized_and_never_retried(bridge, capsys, retry_after, expected):
    bridge.fire_response = (429, {"error": RAW_PRIVATE, "token": ROUTINE_TOKEN}, {"Retry-After": retry_after})
    bridge.run()
    assert expected in bridge.statuses[-1]
    assert "no automatic retry" in bridge.statuses[-1]
    assert bridge.statuses[-1].splitlines()[1] == "<!-- issue-agent:reaction id=501 -->"
    assert "keep_reaction=true" not in bridge.output_path.read_text()
    assert len(bridge.fires) == 1
    assert bridge.sleeps == []
    public = "\n".join(bridge.statuses) + str(capsys.readouterr())
    assert RAW_PRIVATE not in public
    assert ROUTINE_TOKEN not in public
    assert GH_TOKEN not in public


@pytest.mark.parametrize("code, expected", [(403, "Launch rejected"), (503, "Launch not confirmed")])
def test_api_error_reports_only_status_and_does_not_retry(bridge, capsys, code, expected):
    bridge.fire_response = (code, {"private": RAW_PRIVATE, "token": ROUTINE_TOKEN}, {})
    bridge.run()
    assert f"{expected}: Claude API returned HTTP {code}" in bridge.statuses[-1]
    assert bridge.statuses[-1].splitlines()[1] == "<!-- issue-agent:reaction id=501 -->"
    assert "keep_reaction=true" not in bridge.output_path.read_text()
    assert len(bridge.fires) == 1
    public = "\n".join(bridge.statuses) + str(capsys.readouterr())
    for secret in (RAW_PRIVATE, GH_TOKEN, ROUTINE_TOKEN):
        assert secret not in public


def test_fire_timeout_stays_unknown_and_is_not_retried(bridge, capsys):
    bridge.fire_response = TimeoutError(f"{RAW_PRIVATE} {ROUTINE_TOKEN} {GH_TOKEN}")
    with pytest.raises(RuntimeError, match="^Bridge failed; see its status comment\\.$") as error:
        bridge.run()
    assert error.value.__suppress_context__
    assert "Launch or startup confirmation is uncertain" in bridge.statuses[-1]
    assert bridge.statuses[-1].splitlines()[1] == "<!-- issue-agent:reaction id=501 -->"
    assert "keep_reaction=true" not in bridge.output_path.read_text()
    assert "no automatic retry" in bridge.statuses[-1]
    assert len(bridge.fires) == 1
    public = "\n".join(bridge.statuses) + str(error.value) + str(capsys.readouterr())
    for secret in (RAW_PRIVATE, GH_TOKEN, ROUTINE_TOKEN):
        assert secret not in public


@pytest.mark.parametrize("response", [None, [], "not-json", {}, {"claude_code_session_id": 42}, {"claude_code_session_id": "session_bad/path"}, {"claude_code_session_id": RAW_PRIVATE}, {"session_id": "session_Alternative123"}])
def test_success_without_valid_session_still_waits_for_owner_ack(bridge, response):
    bridge.fire_response = (200, response, {})
    bridge.run()
    assert "Claude confirmed startup" in bridge.statuses[-1]
    assert "https://claude.ai/code/session_" not in "\n".join(bridge.statuses)
    assert RAW_PRIVATE not in "\n".join(bridge.statuses)
    assert len(bridge.fires) == 1
    assert bridge.sleeps == []


def test_missing_session_id_without_ack_waits_five_minutes_without_refiring(bridge):
    bridge.fire_response = (200, {"id": "session_Alternative123", "private": RAW_PRIVATE}, {})
    bridge.startup_comments = []
    bridge.run()
    assert sum(bridge.sleeps) == 300
    assert "did not confirm startup within 5 minutes" in bridge.statuses[-1]
    assert "https://claude.ai/code/session_" not in "\n".join(bridge.statuses)
    assert RAW_PRIVATE not in "\n".join(bridge.statuses)
    assert "No automatic retry" in bridge.statuses[-1]
    assert "keep_reaction=true" not in bridge.output_path.read_text()
    assert len(bridge.fires) == 1


def test_receipt_on_second_page_prevents_duplicate_session(bridge):
    marker = f"<!-- issue-agent:bridge event=issue_comment id={SOURCE_ID} -->"
    bridge.existing_comments = [comment("Old discussion", comment_id=i) for i in range(100)]
    bridge.existing_comments.append(comment(marker + "\nEarlier uncertain launch", login="github-actions[bot]", user_type="Bot"))
    bridge.run()
    assert len(bridge.calls) == 2
    assert "page=2" in bridge.calls[-1]["url"]
    assert bridge.fires == []
    assert bridge.reactions == []
    assert bridge.statuses == []


@pytest.mark.parametrize("login, user_type, prefix, event_id", [
    ("outsider", "Bot", "", SOURCE_ID),
    ("github-actions[bot]", "User", "", SOURCE_ID),
    ("github-actions[bot]", "Bot", "quoted\n", SOURCE_ID),
    ("github-actions[bot]", "Bot", "", SOURCE_ID + 1),
])
def test_only_authentic_exact_receipts_suppress_dispatch(bridge, login, user_type, prefix, event_id):
    body = prefix + f"<!-- issue-agent:bridge event=issue_comment id={event_id} -->"
    bridge.existing_comments = [comment(body, login=login, user_type=user_type)]
    bridge.run()
    assert len(bridge.fires) == 1


def test_actions_rerun_uses_receipt_instead_of_firing_again(bridge):
    bridge.run()
    bridge.run()
    assert len(bridge.fires) == 1
    assert len(bridge.reactions) == 1


@pytest.mark.parametrize("code, expected", [(201, f"reaction_path=/issues/comments/{SOURCE_ID}/reactions/501\nkeep_reaction=true\n"), (200, "")])
def test_cleanup_output_records_only_reaction_created_by_this_run(bridge, code, expected):
    bridge.reaction_status = code
    bridge.run()
    assert bridge.output_path.read_text() == expected


def test_reaction_metadata_is_saved_before_fire_and_preserved_through_ack(bridge):
    bridge.run()
    marker = "<!-- issue-agent:reaction id=501 -->"
    fire_index = bridge.calls.index(bridge.fires[0])
    before_fire = [call for call in bridge.calls[:fire_index] if call["method"] == "PATCH"]
    assert before_fire
    assert before_fire[-1]["body"]["body"].splitlines()[1] == marker
    first_record = next(i for i, body in enumerate(bridge.statuses) if marker in body)
    assert all(body.splitlines()[1] == marker for body in bridge.statuses[first_record:])
    assert "keep_reaction=true\n" in bridge.output_path.read_text()


@pytest.mark.parametrize("failure", [403, 500, TimeoutError(f"{RAW_PRIVATE} {GH_TOKEN}")])
def test_reaction_failure_warns_but_still_launches_without_cleanup_output(bridge, capsys, failure):
    bridge.reaction_status = failure
    bridge.run()
    assert len(bridge.fires) == 1
    assert "Claude confirmed startup" in bridge.statuses[-1]
    assert bridge.output_path.read_text() == ""
    output = str(capsys.readouterr())
    assert "Could not add the bridge reaction; continuing dispatch" in output
    assert RAW_PRIVATE not in output
    assert GH_TOKEN not in output


@pytest.mark.parametrize("setting, value", [("ROUTINE_TOKEN", ""), ("ROUTINE_ID", ""), ("ROUTINE_ID", "trig_invalid/path")])
def test_invalid_configuration_is_reported_without_fire(bridge, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    bridge.run()
    assert bridge.fires == []
    assert "Launch blocked: configure CLAUDE_ROUTINE_TOKEN and CLAUDE_ROUTINE_ID" in bridge.statuses[-1]
    assert bridge.output_path.read_text() == f"reaction_path=/issues/comments/{SOURCE_ID}/reactions/501\n"


def test_http_error_body_is_not_returned_or_printed(bridge_namespace, monkeypatch, capsys):
    def fail_open(request, timeout):
        assert timeout == 15
        raise urllib.error.HTTPError(request.full_url, 403, RAW_PRIVATE, {"Retry-After": "12"}, io.BytesIO(RAW_PRIVATE.encode()))

    monkeypatch.setattr(bridge_namespace["urllib"].request, "build_opener", lambda *args: SimpleNamespace(open=fail_open))
    code, data, headers = bridge_namespace["request"]("POST", "https://api.anthropic.com/test", ROUTINE_TOKEN, {"text": "pointer"}, anthropic=True)
    assert (code, data) == (403, None)
    assert headers["Retry-After"] == "12"
    assert RAW_PRIVATE not in str(capsys.readouterr())


@pytest.mark.parametrize("status, expected_returncode", [("204", 0), ("404", 0), ("403", 1)])
def test_cleanup_deletes_only_recorded_reaction_and_accepts_already_removed(workflow, tmp_path, status, expected_returncode):
    cleanup = next(step for step in workflow["jobs"]["fire"]["steps"] if step.get("name") == "Clear the bridge reaction")
    assert "always()" in cleanup["if"]
    assert "steps.dispatch.outputs.reaction_path != ''" in cleanup["if"]
    assert "steps.dispatch.outputs.keep_reaction != 'true'" in cleanup["if"]
    assert cleanup["env"]["REACTION_PATH"] == "${{ steps.dispatch.outputs.reaction_path }}"
    curl = tmp_path / "curl"
    curl.write_text(f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\nPath(os.environ['CURL_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\nprint(os.environ['CURL_STATUS'], end='')\n")
    curl.chmod(0o755)
    trace = tmp_path / "curl.json"
    reaction_path = f"/issues/comments/{SOURCE_ID}/reactions/501"
    result = subprocess.run(
        ["bash", "-e", "-c", cleanup["run"]],
        env={"PATH": str(tmp_path) + os.pathsep + os.defpath, "CURL_CAPTURE": str(trace), "CURL_STATUS": status, "GH_TOKEN": GH_TOKEN, "GITHUB_API_URL": "https://api.github.test", "GITHUB_REPOSITORY": REPO, "REACTION_PATH": reaction_path},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == expected_returncode
    args = json.loads(trace.read_text())
    assert args[args.index("--request") + 1] == "DELETE"
    assert args[-1] == f"https://api.github.test/repos/{REPO}{reaction_path}"
    assert sum(arg.startswith("https://") for arg in args) == 1
    assert GH_TOKEN not in result.stdout + result.stderr
    if expected_returncode == 0:
        assert result.stdout == result.stderr == ""
    else:
        assert "HTTP 403" in result.stdout


@pytest.fixture
def completion_namespace(workflow, workflow_path):
    step = next(step for step in workflow["jobs"]["complete"]["steps"] if step.get("id") == "cleanup")
    namespace = {"__name__": "completion_under_test"}
    exec(compile(step["run"], str(workflow_path), "exec"), namespace)
    assert "request" in namespace, "Completion API access must be replaced before running the test"
    return namespace


def bot_reaction(reaction_id=501, *, login="github-actions[bot]", user_type="Bot", content="eyes"):
    return {"id": reaction_id, "content": content, "user": {"login": login, "type": user_type}}


class CompletionRun:
    """Only the recorded reaction may be deleted; all other writes fail the test."""

    def __init__(self, namespace, tmp_path, monkeypatch):
        self.namespace = namespace
        self.event_path = tmp_path / "completion.json"
        self.calls = []
        self.delete_status = 204
        self.receipt_status = self.source_status = self.list_status = 200
        issue_url = f"https://api.github.test/repos/{REPO}/issues/17"
        self.event = {
            "action": "created", "issue": {"number": 17},
            "comment": comment(f"<!-- issue-agent -->\n<!-- issue-agent:finished bridge_comment_id={STATUS_ID} -->\nDone.", comment_id=9999),
        }
        self.receipt = comment(
            f"<!-- issue-agent:bridge event=issue_comment id={SOURCE_ID} -->\n"
            "<!-- issue-agent:reaction id=501 -->\nClaude confirmed startup.",
            login="github-actions[bot]", user_type="Bot", comment_id=STATUS_ID,
        )
        self.source = comment("/agent work")
        self.source["issue_url"] = self.receipt["issue_url"] = issue_url
        self.reactions = [bot_reaction()]
        for name, value in {
            "GITHUB_EVENT_PATH": str(self.event_path), "GITHUB_EVENT_NAME": "issue_comment",
            "GITHUB_REPOSITORY": REPO, "GITHUB_API_URL": "https://api.github.test",
            "AGENT_OWNER": OWNER, "GH_TOKEN": GH_TOKEN,
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("ROUTINE_TOKEN", raising=False)
        monkeypatch.delenv("ROUTINE_ID", raising=False)
        monkeypatch.setitem(namespace, "request", self.request)

    def request(self, method, url, token, body=None):
        self.calls.append({"method": method, "url": url, "body": body})
        assert token == GH_TOKEN
        assert body is None
        parsed = urllib.parse.urlsplit(url)
        assert parsed.scheme == "https" and parsed.netloc == "api.github.test"
        assert parsed.path.startswith(f"/repos/{REPO}/")
        path = parsed.path.removeprefix(f"/repos/{REPO}")
        if method == "GET" and path == f"/issues/comments/{STATUS_ID}":
            return self.receipt_status, self.receipt, {}
        if method == "GET" and path == f"/issues/comments/{SOURCE_ID}":
            return self.source_status, self.source, {}
        if method == "GET" and path == f"/issues/comments/{SOURCE_ID}/reactions":
            query = urllib.parse.parse_qs(parsed.query)
            assert query["per_page"] == ["100"]
            page = int(query["page"][0])
            return self.list_status, self.reactions[(page - 1) * 100:page * 100], {}
        if method == "DELETE" and path == f"/issues/comments/{SOURCE_ID}/reactions/501":
            if self.delete_status in {204, 404}:
                self.reactions = [item for item in self.reactions if item["id"] != 501]
            return self.delete_status, None, {}
        pytest.fail(f"Completion attempted an unexpected request: {method} {url}")

    @property
    def deletes(self):
        return [call for call in self.calls if call["method"] == "DELETE"]

    def run(self):
        self.event_path.write_text(json.dumps(self.event))
        self.namespace["run"]()


@pytest.fixture
def completion(completion_namespace, tmp_path, monkeypatch):
    return CompletionRun(completion_namespace, tmp_path, monkeypatch)


def test_completion_job_is_owner_only_and_has_no_routine_secret(workflow):
    job = workflow["jobs"]["complete"]
    condition = " ".join(job["if"].split())
    for guard in (
        "github.event_name == 'issue_comment'", "github.event.action == 'created'",
        "vars.AGENT_OWNER != ''", "github.event.comment.user.type == 'User'",
        "github.event.comment.user.login == vars.AGENT_OWNER",
    ):
        assert guard in condition
    assert "ROUTINE_TOKEN" not in json.dumps(job)
    assert "api.anthropic.com" not in json.dumps(job)
    assert "needs" not in job


def test_completion_deletes_only_recorded_bot_eyes_and_reruns_safely(completion):
    others = [bot_reaction(502), bot_reaction(503, login=OWNER, user_type="User"), bot_reaction(504, content="+1")]
    completion.reactions = others + [bot_reaction()]
    completion.run()
    completion.run()
    assert len(completion.deletes) == 1
    assert completion.deletes[0]["url"].endswith(f"/issues/comments/{SOURCE_ID}/reactions/501")
    assert completion.reactions == others
    assert all(call["method"] in {"GET", "DELETE"} for call in completion.calls)


def test_pr_completion_deletes_reaction_with_workflow_write_permission(completion, workflow, monkeypatch):
    """Emulate PR DELETE authorization offline; a live run must verify the token."""
    completion.event["issue"]["pull_request"] = {"url": f"https://api.github.test/repos/{REPO}/pulls/17"}
    others = [bot_reaction(502), bot_reaction(503, login=OWNER, user_type="User")]
    completion.reactions = others + [bot_reaction()]

    def permission_checked_request(method, url, token, body=None):
        if method == "DELETE" and workflow["jobs"]["complete"]["permissions"].get("pull-requests") != "write":
            completion.delete_status = 403
        return completion.request(method, url, token, body)

    monkeypatch.setitem(completion.namespace, "request", permission_checked_request)
    completion.run()
    assert len(completion.deletes) == 1
    assert completion.reactions == others
    assert all(call["method"] in {"GET", "DELETE"} for call in completion.calls)


@pytest.mark.parametrize("status_field, stage, method, code", [
    ("receipt_status", "read bridge receipt", "GET", 403),
    ("source_status", "read source command", "GET", 403),
    ("list_status", "list command reactions", "GET", 403),
    ("delete_status", "delete command reaction", "DELETE", 403),
    ("delete_status", "delete command reaction", "DELETE", 503),
])
def test_completion_http_failure_reports_only_safe_stage_method_and_status(
    completion, monkeypatch, capsys, status_field, stage, method, code,
):
    setattr(completion, status_field, code)

    def private_error_response(method, url, token, body=None):
        status, data, headers = completion.request(method, url, token, body)
        if status >= 400:
            return status, {"detail": RAW_PRIVATE, "token": GH_TOKEN}, {"X-Private": ROUTINE_TOKEN}
        return status, data, headers

    monkeypatch.setitem(completion.namespace, "request", private_error_response)
    with pytest.raises(RuntimeError) as error:
        completion.run()
    assert str(error.value) == f"GitHub {stage} ({method}) returned HTTP {code}."
    assert error.value.__suppress_context__
    assert error.value.__cause__ is None
    assert completion.reactions == [bot_reaction()]
    assert all(call["method"] in {"GET", "DELETE"} for call in completion.calls)
    public = str(error.value) + str(capsys.readouterr())
    for secret in (RAW_PRIVATE, GH_TOKEN, ROUTINE_TOKEN):
        assert secret not in public


def test_completion_transport_failure_stays_generic_and_preserves_reaction(completion, monkeypatch, capsys):
    def timed_out_delete(method, url, token, body=None):
        if method == "DELETE":
            raise TimeoutError(f"{RAW_PRIVATE} {GH_TOKEN} {ROUTINE_TOKEN}")
        return completion.request(method, url, token, body)

    monkeypatch.setitem(completion.namespace, "request", timed_out_delete)
    with pytest.raises(RuntimeError) as error:
        completion.run()
    assert str(error.value) == "Could not clear the completed command reaction."
    assert error.value.__suppress_context__
    assert error.value.__cause__ is None
    assert completion.reactions == [bot_reaction()]
    public = str(error.value) + str(capsys.readouterr())
    for secret in (RAW_PRIVATE, GH_TOKEN, ROUTINE_TOKEN):
        assert secret not in public


def test_completion_finds_recorded_reaction_on_second_page(completion):
    others = [bot_reaction(1000 + i) for i in range(100)]
    completion.reactions = others + [bot_reaction()]
    completion.run()
    pages = [call["url"] for call in completion.calls if "/reactions?" in call["url"]]
    assert len(pages) == 2
    assert "page=2" in pages[-1]
    assert len(completion.deletes) == 1
    assert completion.reactions == others


def test_completion_accepts_reaction_already_deleted_between_list_and_delete(completion):
    completion.delete_status = 404
    completion.run()
    assert len(completion.deletes) == 1


@pytest.mark.parametrize("change", ["other_owner", "owner_case", "bot", "edited", "missing_owner", "empty_owner", "review_event"])
def test_completion_untrusted_events_make_no_api_calls(completion, monkeypatch, change):
    if change == "other_owner":
        completion.event["comment"]["user"].update(login="other-person", name=OWNER)
        completion.event["comment"]["author_association"] = "OWNER"
    elif change == "owner_case":
        completion.event["comment"]["user"]["login"] = OWNER.upper()
    elif change == "bot":
        completion.event["comment"]["user"]["type"] = "Bot"
    elif change == "edited":
        completion.event["action"] = "edited"
    elif change == "missing_owner":
        monkeypatch.delenv("AGENT_OWNER")
    elif change == "empty_owner":
        monkeypatch.setenv("AGENT_OWNER", "")
    else:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_review")
        completion.event = {}
    completion.run()
    assert completion.calls == []


@pytest.mark.parametrize("body", [
    f"preface\n<!-- issue-agent -->\n<!-- issue-agent:finished bridge_comment_id={STATUS_ID} -->",
    f"<!-- issue-agent -->\n\n<!-- issue-agent:finished bridge_comment_id={STATUS_ID} -->",
    f"<!-- issue-agent -->\n<!-- issue-agent:finished bridge_comment_id={STATUS_ID} --> trailing",
    f"<!-- issue-agent -->\n<!-- issue-agent:started bridge_comment_id={STATUS_ID} -->",
    "<!-- issue-agent -->\n<!-- issue-agent:finished bridge_comment_id=0 -->",
    "<!-- issue-agent -->\n<!-- issue-agent:finished bridge_comment_id=9001/../../bad -->",
])
def test_completion_requires_exact_first_two_lines(completion, body):
    completion.event["comment"]["body"] = body
    completion.run()
    assert completion.calls == []


@pytest.mark.parametrize("target", ["receipt", "source"])
@pytest.mark.parametrize("issue_url", [
    f"https://api.github.test/repos/{REPO}/issues/18",
    "https://api.github.test/repos/other/project/issues/17",
    f"https://attacker.test/repos/{REPO}/issues/17",
])
def test_completion_rejects_comments_outside_current_issue(completion, target, issue_url):
    getattr(completion, target)["issue_url"] = issue_url
    completion.run()
    assert completion.deletes == []
    assert all("/reactions" not in call["url"] for call in completion.calls)


@pytest.mark.parametrize("target, login, user_type", [
    ("receipt", OWNER, "User"),
    ("receipt", "github-actions[bot]", "User"),
    ("receipt", "another[bot]", "Bot"),
    ("source", "other-person", "User"),
    ("source", OWNER, "Bot"),
])
def test_completion_requires_authentic_receipt_and_owner_source(completion, target, login, user_type):
    getattr(completion, target)["user"] = {"login": login, "type": user_type}
    completion.run()
    assert completion.deletes == []
    assert all("/reactions" not in call["url"] for call in completion.calls)


@pytest.mark.parametrize("metadata", [
    "", "<!-- issue-agent:reaction id=0 -->", "<!-- issue-agent:reaction id=-501 -->",
    "<!-- issue-agent:reaction id=501/../../bad -->", "<!-- issue-agent:reaction id=501 --> trailing",
    "status text\n<!-- issue-agent:reaction id=501 -->",
])
def test_completion_refuses_missing_or_malformed_reaction_metadata(completion, metadata):
    first_line = completion.receipt["body"].splitlines()[0]
    completion.receipt["body"] = first_line + "\n" + metadata
    completion.run()
    assert len(completion.calls) == 1
    assert completion.deletes == []


@pytest.mark.parametrize("first_line", [
    f"<!-- issue-agent:bridge event=pull_request_review id={SOURCE_ID} -->",
    f"quoted <!-- issue-agent:bridge event=issue_comment id={SOURCE_ID} -->",
    "<!-- issue-agent:bridge event=issue_comment id=1234/../../bad -->",
])
def test_completion_refuses_noncommand_or_forged_receipt_marker(completion, first_line):
    completion.receipt["body"] = first_line + "\n<!-- issue-agent:reaction id=501 -->"
    completion.run()
    assert len(completion.calls) == 1
    assert completion.deletes == []


@pytest.mark.parametrize("body", ["/agentx", " /agent", "/agent <!-- issue-agent -->", "ordinary discussion"])
def test_completion_requires_original_owner_command(completion, body):
    completion.source["body"] = body
    completion.run()
    assert completion.deletes == []
    assert all("/reactions" not in call["url"] for call in completion.calls)


@pytest.mark.parametrize("reaction", [
    bot_reaction(502), bot_reaction(login=OWNER, user_type="User"),
    bot_reaction(login="other[bot]"), bot_reaction(user_type="User"), bot_reaction(content="+1"),
])
def test_completion_preserves_reactions_with_wrong_id_author_type_or_content(completion, reaction):
    completion.reactions = [reaction]
    completion.run()
    assert completion.deletes == []
    assert completion.reactions == [reaction]


@pytest.mark.parametrize("target", ["receipt", "source"])
def test_completion_tolerates_deleted_receipt_or_command(completion, target):
    setattr(completion, target + "_status", 404)
    completion.run()
    assert completion.deletes == []
