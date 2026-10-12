"""The autodev gate (design 9.2) on GitHub REST responses: every branch, and forged state ignored (#338)."""
import copy
from datetime import datetime
import json
from pathlib import Path

import pytest

from src.schedules import gate as gate_module
from src.schedules.gate import CLAIM_LABEL, STALE_CLAIM_COMMENT, gate, issue_of, trusted_state

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "autodev_gate.json").read_text())
NOW = datetime.fromisoformat(FIXTURE["now"].replace("Z", "+00:00"))


def snapshot(scenario):
    objects = FIXTURE["objects"]

    def resolve(value):
        if isinstance(value, list):
            return [copy.deepcopy(objects[key]) for key in value]
        return copy.deepcopy(objects[value])

    def per_number(name):
        found = {}
        for number, keys in scenario.get(name, {}).items():
            found[number] = resolve(keys)
        return found
    return {"repository": FIXTURE["repository"], "fetched_at": FIXTURE["now"],
            "claimed_issues": resolve(scenario.get("claimed_issues", [])),
            "issue_events": per_number("issue_events"), "pulls": resolve(scenario.get("pulls", [])),
            "linked_issues": per_number("linked_issues"),
            "pull_details": per_number("pull_details"), "issue_comments": per_number("issue_comments"),
            "reviews": per_number("reviews"), "review_comments": per_number("review_comments")}


@pytest.mark.parametrize("name", sorted(FIXTURE["scenarios"]))
def test_gate_scenarios(name):
    scenario = FIXTURE["scenarios"][name]
    expect = scenario["expect"]
    decision = gate(snapshot(scenario), scenario.get("trusted_ids", []), max_bot_rounds=FIXTURE["max_bot_rounds"],
                    pr=scenario.get("pr"))
    assert decision.decision == expect["decision"], decision
    if "reason" in expect:
        assert expect["reason"] in decision.reason
    assert decision.pr == expect.get("pr")
    if "resume_branch" in expect:
        assert decision.resume_branch == expect["resume_branch"]
    if decision.decision != "resume":
        assert decision.resume_branch is None
    actions = [{"op": action["op"], "issue": action["issue"], "label": action["label"],
                "with_comment": action["comment"] is not None} for action in decision.actions]
    assert actions == expect.get("actions", [])
    if "action_reason" in expect:
        assert decision.actions[0]["reason"] == expect["action_reason"]
    if "note" in expect:
        assert any(expect["note"] in note for note in decision.notes), decision.notes


def test_every_branch_of_the_design_has_a_scenario():
    decisions = {(FIXTURE["scenarios"][name]["expect"]["decision"]) for name in FIXTURE["scenarios"]}
    assert decisions == {"select", "resume", "wait"}


def test_stale_claim_comment_carries_the_autodev_marker():
    scenario = FIXTURE["scenarios"]["claim_older_than_6_hours_is_removed_with_a_comment"]
    decision = gate(snapshot(scenario), [], max_bot_rounds=3)
    assert decision.actions[0]["comment"] == STALE_CLAIM_COMMENT
    assert STALE_CLAIM_COMMENT.startswith("<!-- autodev -->\n") and not STALE_CLAIM_COMMENT.startswith("/agent")


def test_the_gate_reads_the_snapshot_without_changing_it():
    scenario = FIXTURE["scenarios"]["open_pr_resumes_on_a_new_trusted_review"]
    data = snapshot(scenario)
    before = json.dumps(data, sort_keys=True)
    gate(data, [9001], max_bot_rounds=3)
    assert json.dumps(data, sort_keys=True) == before


def test_a_trusted_state_needs_the_record_id_and_the_exact_marker():
    objects = FIXTURE["objects"]
    trusted = objects["state_9001_waiting"]
    assert trusted_state([trusted], {9001})[0]["review_status"] == "waiting_bots"
    assert trusted_state([trusted], set()) == (None, 1)
    quoted = {**trusted, "body": "> " + trusted["body"]}  # a quote of the state comment is not one
    assert trusted_state([quoted], {9001}) == (None, 0)
    broken = {**trusted, "body": "<!-- autodev -->\n<!-- autodev:state {not json} -->"}
    assert trusted_state([broken], {9001}) == (None, 1)


def test_the_newest_trusted_state_wins():
    objects = FIXTURE["objects"]
    older = {**objects["state_9003_finished"], "updated_at": "2026-10-11T09:00:00Z"}
    newer = objects["state_9001_waiting"]
    assert trusted_state([newer, older], {9001, 9003})[0]["review_status"] == "waiting_bots"


def test_a_claimed_issue_with_an_open_pr_is_left_to_the_pr():
    scenario = FIXTURE["scenarios"]["open_pr_waits_for_the_bots"]
    data = snapshot(scenario)
    data["issue_events"] = {}  # the claim's age does not matter while its PR is open
    decision = gate(data, [9001], max_bot_rounds=3)
    assert decision.decision == "wait" and decision.actions == []


def test_a_merged_pr_beside_an_open_one_keeps_the_claim():
    objects = FIXTURE["objects"]
    data = snapshot(FIXTURE["scenarios"]["open_pr_waits_for_the_bots"])
    merged = copy.deepcopy(objects["pr_411_merged"])
    merged["head"]["ref"] = "claude/autodev-401-first-try"
    data["pulls"].append(merged)
    decision = gate(data, [9001], max_bot_rounds=3)
    assert decision.actions == []


def test_the_bot_round_limit_comes_from_the_configuration():
    data = snapshot(FIXTURE["scenarios"]["open_pr_resumes_when_next_attempt_has_come"])
    assert gate(data, [9002], max_bot_rounds=1).reason.endswith("the bot-round limit (1) is exhausted")


def test_branch_names_give_the_issue():
    assert issue_of({"head": {"ref": "claude/autodev-338-runner"}}) == 338
    assert issue_of({"head": {"ref": "claude/autodev-338"}}) == 338
    assert issue_of({"head": {"ref": "claude/autodev-x-338"}}) is None
    assert issue_of({"head": {"ref": "claude/issue-338-x"}}) is None


def test_the_gate_needs_a_time():
    with pytest.raises(ValueError):
        gate({"repository": "IEZhu/Agents"}, [], max_bot_rounds=3)


def test_constants_follow_the_design():
    assert CLAIM_LABEL == "autodev:in-progress" and gate_module.WIP_LIMIT == 1
    assert gate_module.CLAIM_TTL.total_seconds() == 6 * 3600
    assert NOW.tzinfo is not None


# --- recorded GitHub responses (review F5) --------------------------------------------------------------
# tests/fixtures/autodev_gate_recorded.json holds real responses of IEZhu/Agents, recorded read-only:
# PR #355 (merged) with its reviews and inline comments by both allowlisted bots and the owner, and
# the events of issue #338. They check that the gate reads GitHub's real field names and values.

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "autodev_gate_recorded.json").read_text())
RECORDED_NOW = datetime.fromisoformat(RECORDED["recorded_at"].replace("Z", "+00:00"))
REPO = "/repos/IEZhu/Agents"


def recorded(path):
    return copy.deepcopy(RECORDED["responses"][REPO + path])


def recorded_snapshot():
    return {"repository": RECORDED["repository"], "fetched_at": RECORDED["recorded_at"],
            "claimed_issues": recorded("/issues?labels=autodev%3Ain-progress&state=open&per_page=100"),
            "issue_events": {"338": recorded("/issues/338/events?per_page=100")},
            "pulls": [recorded("/pulls/355")], "linked_issues": {},
            "pull_details": {"355": recorded("/pulls/355")},
            "issue_comments": {"355": recorded("/issues/355/comments?per_page=100")},
            "reviews": {"355": recorded("/pulls/355/reviews?per_page=100")},
            "review_comments": {"355": recorded("/pulls/355/comments?per_page=100")}}


def test_recorded_repository_state_selects_and_ignores_a_pr_that_is_not_self_development():
    decision = gate(recorded_snapshot(), [], max_bot_rounds=3)
    assert decision.decision == "select" and decision.actions == [] and decision.notes == []
    pull = recorded("/pulls/355")
    assert pull["merged_at"] and issue_of(pull) is None  # claude/autodev-phase0-hardening names no issue


def test_recorded_reviews_and_inline_comments_give_the_trusted_bot_findings():
    reviews, comments = recorded("/pulls/355/reviews?per_page=100"), recorded("/pulls/355/comments?per_page=100")
    # Both bots' real logins and account IDs match the allowlist; the owner's review is not a finding.
    assert {(item["user"]["login"], item["user"]["id"]) for item in reviews} == {
        ("copilot-pull-request-reviewer[bot]", 175728472), ("coderabbitai[bot]", 136622811), ("WonderMr", 5370211)}
    # The bot's reply names its thread's first comment in in_reply_to_id; the owner's reply is not a finding.
    assert [comment.get("in_reply_to_id") for comment in comments] == [None, 4249722422, 4249722422]
    assert gate_module.bot_findings(reviews, comments) == {
        "review:5492801882", "review:5492845478", "review:5492875620", "comment:4249722422", "comment:4249748836"}


def as_self_development(snapshot, findings):
    """The recorded PR as if autodev had opened it, with a trusted state comment that has seen ``findings``."""
    pull = snapshot["pull_details"]["355"]
    for item in (pull, snapshot["pulls"][0]):
        item["state"] = "open"
        item["labels"].append({"name": "autodev"})
        item["head"]["ref"] = "claude/autodev-338-runner"
    state = {"head": pull["head"]["sha"], "rounds": 1, "review_status": "waiting_bots", "findings": sorted(findings)}
    snapshot["issue_comments"]["355"].append({"id": 1, "updated_at": RECORDED["recorded_at"],
                                              "body": "<!-- autodev -->\n<!-- autodev:state " + json.dumps(state) + " -->"})
    return snapshot


def test_recorded_mergeable_null_is_not_a_conflict():
    snapshot = recorded_snapshot()
    pull = snapshot["pull_details"]["355"]
    assert pull["mergeable"] is None and pull["mergeable_state"] == "unknown"  # as GitHub answers before it computes
    seen = gate_module.bot_findings(snapshot["reviews"]["355"], snapshot["review_comments"]["355"])
    decision = gate(as_self_development(snapshot, seen), [1], max_bot_rounds=3, now=RECORDED_NOW)
    assert decision.decision == "wait" and decision.reason == "WIP limit 1: PR #355: waiting for the bots"


def test_recorded_bot_reply_not_yet_seen_resumes():
    snapshot = recorded_snapshot()
    seen = gate_module.bot_findings(snapshot["reviews"]["355"], snapshot["review_comments"]["355"])
    decision = gate(as_self_development(snapshot, seen - {"comment:4249748836"}), [1], max_bot_rounds=3,
                    now=RECORDED_NOW)
    assert decision.decision == "resume" and decision.reason.endswith("comment:4249748836")


def test_recorded_label_events_give_the_age_of_a_label(monkeypatch):
    # No issue of IEZhu/Agents has carried autodev:in-progress yet; the real `labeled` events of
    # issue #338 have the same shape, so the claim age is read from them under another label name.
    monkeypatch.setattr(gate_module, "CLAIM_LABEL", "area:autodev")
    age = gate_module._claim_age(338, recorded_snapshot(), RECORDED_NOW)
    labeled = [event for event in recorded("/issues/338/events?per_page=100")
               if event["event"] == "labeled" and event["label"]["name"] == "area:autodev"]
    assert len(labeled) == 1
    assert age == RECORDED_NOW - datetime.fromisoformat(labeled[0]["created_at"].replace("Z", "+00:00"))
