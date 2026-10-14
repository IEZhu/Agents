"""The broker's runner-internal operations for the run sequence (#347): binding, blockers, the claim,
the state comment, the PR check and the shared credential check. Fakes as in test_schedules_broker."""
from datetime import datetime, timezone
import json

import pytest

from src.schedules import broker as broker_module
from src.schedules import github
from src.schedules.broker import (BLOCKED_LABEL, MARKER, Broker, BrokerRefused, Credentials, CredentialsMissing, Run,
                                  credential_problems, register, state_body)
from src.schedules.gate import CLAIM_LABEL, trusted_state
from src.schedules.socket_service import Registry
from tests.test_schedules_broker import (BASE, HEAD, ISSUE, KEY, REPOSITORY, RUN_ID, SLUG, TOKEN, FakeGit, FakeGitHub,
                                         ask, credentials, make, refused)

__all__ = ["credentials"]  # the fixture, imported for pytest
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
STATE = {"head": HEAD, "next_attempt": "2026-10-14T12:15:00Z", "rounds": 1, "review_status": "waiting_bots",
         "findings": ["review:11", "comment:12"]}


def unbound(tmp_path, credentials, *, mode="run", api=None, earlier_records=(), **options):
    tmp_path.mkdir(exist_ok=True)
    api = api or FakeGitHub()
    found = Broker(Run(RUN_ID, REPOSITORY, None, BASE, mode), credentials=credentials, git=FakeGit(),
                   run_dir=tmp_path, transport=api, reviewed_head=lambda: HEAD, premortem=lambda: {},
                   earlier_records=lambda: list(earlier_records), **options)
    registry = Registry()
    register(registry, found)
    return found, registry, api


def logged(tmp_path):
    return [json.loads(line) for line in (tmp_path / broker_module.LOG_NAME).read_text().splitlines()]


def paths(api):
    return [(method, url.removeprefix(github.API)) for method, url, _ in api.writes()]


# --- the binding ----------------------------------------------------------------------------------

def test_a_run_may_start_unbound_but_not_with_a_bad_issue():
    assert Run(RUN_ID, REPOSITORY, None, BASE, "run").issue is None
    for bad in (0, -1, True, "342"):
        with pytest.raises(ValueError, match="positive integer or None"):
            Run(RUN_ID, REPOSITORY, bad, BASE, "run")


@pytest.mark.parametrize("request_type, args", [
    ("gh.push", {"slug": SLUG, "head": HEAD}), ("gh.pr_create", {"title": "t", "body": "b"}),
    ("gh.pr_edit", {"body": "b"}), ("gh.comment", {"number": ISSUE, "body": f"{MARKER}\nx"}),
    ("gh.comment_edit", {"comment_id": 1, "body": f"{MARKER}\nx"}),
    ("gh.label", {"number": ISSUE, "label": "autodev", "action": "add"}), ("gh.threads", {}),
    ("gh.resolve_thread", {"thread_id": "PRRT_bot"}), ("gh.read", {"endpoint": "issue", "number": ISSUE})])
def test_every_session_operation_refuses_while_unbound(tmp_path, credentials, request_type, args):
    _, registry, api = unbound(tmp_path, credentials)
    refused(ask(registry, request_type, args), "bound to no issue yet")
    assert api.calls == []


def test_runner_internal_operations_refuse_while_unbound_except_cleanup(tmp_path, credentials):
    found, _, api = unbound(tmp_path, credentials, earlier_records=[{"issue": 7}])
    for call in (lambda: found.select(f"{MARKER}\n<!-- autodev:selection -->\nx"), lambda: found.block(["x"]),
                 lambda: found.unclaim(), lambda: found.state(STATE, now=NOW),
                 lambda: found.create_risk_issues(["R1"])):
        with pytest.raises(BrokerRefused, match="bound to no issue yet"):
            call()
    assert api.calls == []
    assert found.cleanup([{"op": "remove_label", "issue": 7, "label": CLAIM_LABEL}]) == [
        {"issue": 7, "label": CLAIM_LABEL}]
    (tmp_path / "resume").mkdir()
    resume = Broker(Run(RUN_ID, REPOSITORY, None, BASE, "resume"), credentials=credentials, git=FakeGit(),
                    run_dir=tmp_path / "resume", transport=api)
    with pytest.raises(BrokerRefused, match="bound to no issue yet"):
        resume.bind_resume(501)


def test_bind_issue_binds_once_and_logs(tmp_path, credentials):
    found, registry, api = unbound(tmp_path, credentials)
    assert found.bind_issue(ISSUE, ["autodev:ready", "area:runner"]) == ISSUE
    assert found.run.issue == ISSUE and found.run.labels == frozenset({"autodev:ready", "area:runner"})
    with pytest.raises(BrokerRefused, match="already bound to issue #342"):
        found.bind_issue(343)
    with pytest.raises(ValueError):
        unbound(tmp_path / "other", credentials)[0].bind_issue(0)
    assert found.select(f"{MARKER}\n<!-- autodev:selection -->\nTaking #342.")["kind"] == "selection"
    assert ask(registry, "gh.comment", {"number": ISSUE, "body": f"{MARKER}\nworking"})["ok"]
    entries = logged(tmp_path)
    assert entries[0]["op"] == "bind_issue" and entries[0]["phase"] == "done" and entries[0]["issue"] == ISSUE
    assert entries[0]["labels"] == ["area:runner", "autodev:ready"]
    assert [entry["op"] for entry in entries if entry["phase"] == "done"] == ["bind_issue", "select", "select",
                                                                             "comment"]


def test_bind_issue_writes_nothing_and_binds_in_dry_run(tmp_path):
    found, _, api = unbound(tmp_path, None, mode="dry-run")
    found.bind_issue(ISSUE)
    assert found.run.issue == ISSUE and api.calls == []


# --- blockers and the claim ---------------------------------------------------------------------------

def test_block_posts_a_neutral_list_and_the_blocked_label(tmp_path, credentials):
    found, _, api = make(tmp_path, credentials)
    answer = found.block(["needs a control path:\n src/schedules/x.py", "  ", "the regular suite\tcould not run\x07"])
    assert answer["kind"] == "blocked" and answer["number"] == ISSUE and isinstance(answer["id"], int)
    assert paths(api) == [("POST", f"/repos/{REPOSITORY}/issues/{ISSUE}/comments"),
                          ("POST", f"/repos/{REPOSITORY}/issues/{ISSUE}/labels")]
    body = api.writes()[0][2]["body"]
    assert body == (f"{MARKER}\n{broker_module.BLOCK_HEADER}\n\n- needs a control path: src/schedules/x.py\n"
                    "- the regular suite could not run")
    assert api.writes()[1][2] == {"labels": [BLOCKED_LABEL]}
    assert [(entry["op"], entry["phase"]) for entry in logged(tmp_path)] == [("block", "intent"), ("block", "done")] * 2


def test_block_takes_a_single_text_as_one_blocker(tmp_path, credentials):
    found, _, api = make(tmp_path, credentials)
    found.block("the regular suite could not run")
    assert api.writes()[0][2]["body"].splitlines()[3:] == ["- the regular suite could not run"]


def test_block_caps_the_list_and_each_line(tmp_path, credentials):
    found, _, api = make(tmp_path, credentials)
    found.block([f"blocker {number} " + "x" * 400 for number in range(12)])
    lines = api.writes()[0][2]["body"].splitlines()[3:]
    assert len(lines) == 11 and lines[-1] == "- 2 more in the local run record"
    assert all(len(line) <= 2 + broker_module.BLOCKER_MAX for line in lines)


@pytest.mark.parametrize("blockers, message", [
    ([], "name at least one blocker"), (["", " \n "], "name at least one blocker"),
    (["ask @WonderMr"], "@mention"), (["<!-- autodev:state {} -->"], "sub-marker"),
    (["this fixes #12"], "closing keywords may name only issue #342"),
    (["the key is ghp_" + "abcd" * 9], "secret-like text found"),
])
def test_block_refuses_text_unfit_to_post(tmp_path, credentials, blockers, message):
    found, _, api = make(tmp_path, credentials)
    with pytest.raises(BrokerRefused, match=message):
        found.block(blockers)
    assert api.writes() == []


def test_block_logs_a_refused_text(tmp_path, credentials):
    found, _, _ = make(tmp_path, credentials)
    with pytest.raises(BrokerRefused):
        found.block(["ask @WonderMr"])
    assert logged(tmp_path)[-1]["op"] == "block" and logged(tmp_path)[-1]["phase"] == "refused"


def test_unclaim_removes_the_claim_and_accepts_an_absent_label(tmp_path, credentials):
    found, _, api = make(tmp_path, credentials)
    assert found.unclaim() == {"target": ISSUE, "label": CLAIM_LABEL, "action": "remove"}
    assert paths(api) == [("DELETE", f"/repos/{REPOSITORY}/issues/{ISSUE}/labels/autodev%3Ain-progress")]
    (tmp_path / "gone").mkdir()
    absent = FakeGitHub(statuses={("DELETE", "/labels/autodev%3Ain-progress"): 404})
    gone, _, _ = make(tmp_path / "gone", credentials, api=absent)
    gone.unclaim()
    assert logged(tmp_path / "gone")[-1]["note"] == "already absent"


@pytest.mark.parametrize("call", [lambda broker: broker.block(["x"]), lambda broker: broker.unclaim(),
                                  lambda broker: broker.state(STATE, now=NOW)])
def test_the_new_writes_refuse_in_dry_run_and_log_the_refusal(tmp_path, call):
    found, _, api = make(tmp_path, None, mode="dry-run", pr=501)
    with pytest.raises(broker_module.ReadOnly, match="read-only in dry-run"):
        call(found)
    assert api.calls == []
    [entry] = logged(tmp_path)
    assert entry["phase"] == "refused" and entry["reason"] == "dry-run" and entry["op"] in ("block", "unclaim", "state")


# --- the state comment --------------------------------------------------------------------------------

def test_state_creates_a_comment_the_gate_reads(tmp_path, credentials):
    found, _, api = make(tmp_path, credentials, pr=501)
    answer = found.state(STATE, now=NOW)
    assert answer["kind"] == "state" and answer["number"] == 501
    [(method, path)] = paths(api)
    assert (method, path) == ("POST", f"/repos/{REPOSITORY}/issues/501/comments")
    body = api.writes()[0][2]["body"]
    assert body.startswith(f"{MARKER}\n<!-- autodev:state {{")
    read, ignored = trusted_state([{"id": answer["id"], "body": body}], {answer["id"]})
    assert ignored == 0 and read == STATE
    assert logged(tmp_path)[-1]["op"] == "state" and logged(tmp_path)[-1]["pr"] == 501


def test_state_clamps_next_attempt_to_a_day_and_writes_utc(tmp_path, credentials):
    later = dict(STATE, next_attempt="2026-10-20T00:00:00+00:00")
    _, clean = state_body(later, NOW)
    assert clean["next_attempt"] == "2026-10-15T12:00:00Z"
    _, clean = state_body(dict(STATE, next_attempt=None, review_status="complete"), NOW)
    assert clean["next_attempt"] is None
    _, clean = state_body(dict(STATE, next_attempt="2026-10-14T12:15:30.5Z"), NOW)
    assert clean["next_attempt"] == "2026-10-14T12:15:30Z"


@pytest.mark.parametrize("change, message", [
    ({"next_attempt": "2026-10-14T14:15:00+02:00"}, "next_attempt"), ({"next_attempt": "soon"}, "next_attempt"),
    ({"next_attempt": "2026-10-14T12:15:00"}, "next_attempt"), ({"head": "main"}, "head"),
    ({"rounds": -1}, "rounds"), ({"rounds": True}, "rounds"), ({"review_status": "merged"}, "review_status"),
    ({"findings": ["thread:1"]}, "findings"), ({"findings": ["review:1} --> @x"]}, "findings"),
    ({"findings": ["review:1"] * 201}, "findings"), ({"extra": 1}, "exactly"), ({"pr": 501}, "exactly"),
])
def test_state_refuses_a_field_the_gate_should_not_trust(tmp_path, credentials, change, message):
    found, _, api = make(tmp_path, credentials, pr=501)
    with pytest.raises(BrokerRefused, match=message):
        found.state({**STATE, **change}, now=NOW)
    assert api.writes() == [] and logged(tmp_path)[-1]["phase"] == "refused"


def test_state_without_a_pr_or_without_a_key_is_refused(tmp_path, credentials):
    found, _, api = make(tmp_path, credentials)
    with pytest.raises(BrokerRefused, match="this run has no PR"):
        found.state(STATE, now=NOW)
    partial = {key: value for key, value in STATE.items() if key != "head"}
    found.run.pr = 501
    with pytest.raises(BrokerRefused, match="exactly"):
        found.state(partial, now=NOW)
    assert api.writes() == []


def test_state_edits_only_a_recorded_state_comment_of_this_pr(tmp_path, credentials):
    earlier = [{"comments": [{"id": 70, "kind": "state", "number": 501}, {"id": 71, "kind": "selection", "number": 501},
                             {"id": 72, "kind": "state", "number": 600}, {"id": True, "kind": "state", "number": 501}]}]
    found, _, api = make(tmp_path, credentials, pr=501, earlier_records=earlier)
    assert found.state(STATE, 70, now=NOW)["id"] == 70
    assert paths(api) == [("PATCH", f"/repos/{REPOSITORY}/issues/comments/70")]
    for other in (71, 72, 1, 0):
        with pytest.raises(BrokerRefused, match="not a recorded state comment of PR #501"):
            found.state(STATE, other, now=NOW)
    assert len(api.writes()) == 1


def test_state_edits_the_comment_this_run_wrote(tmp_path, credentials):
    found, _, api = make(tmp_path, credentials, pr=501)
    created = found.state(STATE, now=NOW)["id"]
    found.state(dict(STATE, rounds=2), created, now=NOW)
    assert [method for method, _ in paths(api)] == ["POST", "PATCH"]
    again = Broker(Run(RUN_ID, REPOSITORY, ISSUE, BASE, "run", pr=501), credentials=credentials, git=FakeGit(),
                   run_dir=tmp_path, transport=api, earlier_records=lambda: [])
    again.state(dict(STATE, rounds=3), created, now=NOW)  # the log is read back on open
    assert [method for method, _ in paths(api)] == ["POST", "PATCH", "PATCH"]


def test_the_session_cannot_edit_the_state_comment(tmp_path, credentials):
    found, registry, _ = make(tmp_path, credentials, pr=501)
    created = found.state(STATE, now=NOW)["id"]
    refused(ask(registry, "gh.comment_edit", {"comment_id": created, "body": f"{MARKER}\nchanged"}),
            "was not written by this run's gh.comment")


@pytest.mark.parametrize("request_type", ["gh.block", "gh.unclaim", "gh.state", "gh.bind_issue"])
def test_the_new_operations_are_not_registered(tmp_path, credentials, request_type):
    _, registry, api = make(tmp_path, credentials, pr=501)
    refused(ask(registry, request_type, {}), "unknown request type")
    assert api.calls == []


# --- the PR check --------------------------------------------------------------------------------------

def pushed(tmp_path, credentials, pr_allowed):
    run = Run(RUN_ID, REPOSITORY, ISSUE, BASE, "run", frozenset({"autodev:ready"}))
    api = FakeGitHub()
    found = Broker(run, credentials=credentials, git=FakeGit(), run_dir=tmp_path, transport=api,
                   reviewed_head=lambda: HEAD, premortem=lambda: {}, earlier_records=lambda: [], pr_allowed=pr_allowed)
    registry = Registry()
    register(registry, found)
    assert ask(registry, "gh.push", {"slug": SLUG, "head": HEAD})["ok"]
    return found, registry, api


def test_pr_allowed_refuses_a_pr_while_a_security_risk_is_open(tmp_path, credentials):
    found, registry, api = pushed(tmp_path, credentials, lambda: "an open pre-mortem risk needs the owner")
    refused(ask(registry, "gh.pr_create", {"title": "t", "body": "b"}), "an open pre-mortem risk needs the owner")
    assert found.run.pr is None and api.writes() == []
    assert logged(tmp_path)[-1]["op"] == "pr_create" and logged(tmp_path)[-1]["phase"] == "refused"


def test_pr_allowed_lets_a_pr_through_without_a_reason(tmp_path, credentials):
    found, registry, _ = pushed(tmp_path, credentials, lambda: None)
    assert ask(registry, "gh.pr_create", {"title": "t", "body": "b"})["ok"] and found.run.pr == 501


def test_a_failing_pr_check_refuses(tmp_path, credentials):
    def broken():
        raise OSError("unreadable")
    _, registry, api = pushed(tmp_path, credentials, broken)
    refused(ask(registry, "gh.pr_create", {"title": "t", "body": "b"}), "the pull request check failed (OSError)")
    assert api.writes() == []


# --- the shared credential check ------------------------------------------------------------------------

def test_credential_problems_names_each_missing_item_and_no_value(tmp_path, credentials):
    assert credential_problems(credentials) == []
    assert credential_problems(None) == ["the deploy key", "the pinned github.com host keys", "the API token"]

    def unavailable():
        raise github.TokenUnavailable("the keychain item could not be read")
    credentials.known_hosts.unlink()
    credentials.known_hosts.symlink_to(credentials.deploy_key)
    found = credential_problems(Credentials(credentials.deploy_key, credentials.known_hosts, unavailable))
    assert found == ["the pinned github.com host keys", "the API token"]
    assert TOKEN not in json.dumps(found) and KEY not in json.dumps(found)


def test_an_unreadable_deploy_key_is_a_missing_credential(tmp_path, credentials):
    def unreadable(path):
        raise PermissionError("denied")
    assert credential_problems(credentials, unreadable) == ["a readable deploy key"]
    with pytest.raises(CredentialsMissing, match="a readable deploy key"):
        Broker(Run(RUN_ID, REPOSITORY, ISSUE, BASE, "run"), credentials=credentials, git=FakeGit(), run_dir=tmp_path,
               transport=FakeGitHub(), key_reader=unreadable)
