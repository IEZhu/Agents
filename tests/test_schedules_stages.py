"""Stage schemas, output validation, prompts and inputs files (src/schedules/stages.py, #347).

Pure functions and files in a temporary directory; no session, no network.
"""
import copy
import json
from pathlib import Path
import re
import stat

import pytest

from src.schedules import broker, config, repos, sandbox, stages

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "IEZhu/Agents"
SHA = "a" * 40
FLOW_BLOCKS = {"select": ("flows/autodev-select.md", "\n## Result\n"),
               "implement": ("flows/autodev-implement.md", "\n## Result\n"),
               "submit": ("flows/pr-review.md", "\n## Autodev mode\n")}

SELECT = {"decision": "task", "issue": 347,
          "slice": {"this_pr": "Part A: the building blocks.", "remaining": ["Part B: the sequence."]},
          "criteria_refs": [f"https://github.com/{REPOSITORY}/issues/347",
                            f"https://github.com/{REPOSITORY}/issues/347#issuecomment-123"],
          "scores": [{"issue": 347, "value": 8, "urgency": 5, "unblocking": 8, "size": 13, "readiness": 0.5,
                      "next": True, "score": 0.81, "rationale": "Unblocks phase 1a."},
                     {"issue": 348, "value": 3, "urgency": 2, "unblocking": 1, "size": 5, "readiness": 1,
                      "next": False, "score": 1.2, "rationale": "Settings page | small."}],
          "rationale": "The run sequence comes first."}
NONE = {"decision": "none", "issue": None, "slice": None, "criteria_refs": [], "scores": [],
        "rationale": "No candidate is ready."}
IMPLEMENT = {"branch": "claude/autodev-347-run", "head_sha": SHA,
             "changes": [{"path": "src/schedules/stages.py", "description": "Stage schemas."}],
             "checks": [{"name": "regular suite", "command": "pytest tests/ -q", "status": "passed", "detail": "ok"}],
             "reviews": [{"handle": "review-1", "head": SHA, "status": "accepted", "findings": 1, "fixed": 1,
                          "declined": 0, "mitigated": 0, "known_risk": 0}],
             "blocked": False, "blockers": [],
             "pr_sections": {"self_review": "Two reviews.", "premortem_known_risks": "", "declined_findings": ""}}
SUBMIT = {"pr_url": f"https://github.com/{REPOSITORY}/pull/501", "head_sha": SHA, "review_status": "waiting_bots",
          "bots": [{"bot": "coderabbit", "status": "pending", "head": SHA, "evidence": "queued",
                    "next_attempt": "2026-10-14T12:15:00Z"}],
          "followups": [], "next_attempt": "2026-10-14T12:15:00Z", "rounds": 1,
          "findings": ["review:1", "comment:2"], "reason": "Waiting for CodeRabbit."}
EXAMPLES = {"select": SELECT, "implement": IMPLEMENT, "submit": SUBMIT}


def flow_block(kind):
    path, heading = FLOW_BLOCKS[kind]
    text = (ROOT / path).read_text(encoding="utf-8")
    return json.loads(re.search(r"```json\n(.*?)\n```", text[text.index(heading):], re.S)[1])


def changed(example, path, value):
    """A deep copy of ``example`` with the value at ``path`` (keys and indices) replaced."""
    found = copy.deepcopy(example)
    target = found
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return found


def validate(kind, value, **options):
    options.setdefault("repository", REPOSITORY)
    return stages.validate_output(kind, value, **options)


# --- the schemas ----------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", stages.KINDS)
def test_each_schema_constant_equals_its_flows_result_block(kind):
    assert stages.SCHEMAS[kind] == flow_block(kind)
    assert stages.FLOWS[kind] == FLOW_BLOCKS[kind][0]


def test_the_kinds_are_the_sandboxs_and_the_configurations():
    assert stages.KINDS == sandbox.KINDS == config.STAGES


def _object_schemas(schema, where="$"):
    """Every object node of a schema with its location."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" or "properties" in schema:
            yield where, schema
        for key in ("properties",):
            for name, sub in (schema.get(key) or {}).items():
                yield from _object_schemas(sub, f"{where}.{name}")
        if isinstance(schema.get("items"), dict):
            yield from _object_schemas(schema["items"], f"{where}[]")


@pytest.mark.parametrize("kind", stages.KINDS)
def test_each_model_has_the_schemas_properties_all_required(kind):
    models = {"$": stages.MODELS[kind]}
    for where, node in _object_schemas(stages.SCHEMAS[kind]):
        model = models.get(where)
        if model is None:  # a nested model: find it from its parent's annotation
            parent, _, name = where.rstrip("[]").rpartition(".")
            annotation = models[parent].model_fields[name].annotation
            model = next(arg for arg in _flatten(annotation) if isinstance(arg, type) and
                         issubclass(arg, stages.BaseModel))
            models[where] = model
        assert set(model.model_fields) == set(node["properties"]) == set(node["required"]), where
        assert node["additionalProperties"] is False and model.model_config["extra"] == "forbid", where


def _flatten(annotation):
    args = getattr(annotation, "__args__", None)
    if not args:
        return [annotation]
    return [annotation] + [item for arg in args for item in _flatten(arg)]


@pytest.mark.parametrize("kind", stages.KINDS)
def test_schema_text_is_the_compact_sorted_schema(kind):
    text = stages.schema_text(kind)
    assert json.loads(text) == stages.SCHEMAS[kind] and len(text) <= sandbox.SCHEMA_MAX_CHARS
    assert text == json.dumps(stages.SCHEMAS[kind], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="not a stage kind"):
        stages.schema_text("probe")


# --- the models -----------------------------------------------------------------------------------

@pytest.mark.parametrize("kind, example", [("select", SELECT), ("select", NONE), ("implement", IMPLEMENT),
                                           ("submit", SUBMIT)])
def test_each_model_accepts_a_valid_example(kind, example):
    output, problems = validate(kind, example, issue=347, candidates=[347, 348])
    assert problems == [] and output == example


@pytest.mark.parametrize("kind, path, value", [
    ("select", ("extra",), 1), ("select", ("slice", "extra"), 1), ("select", ("scores", 0, "extra"), 1),
    ("select", ("rationale",), "x" * 1001), ("select", ("slice", "this_pr"), "x" * 2001),
    ("select", ("scores", 0, "rationale"), "x" * 201),
    ("select", ("criteria_refs", 0), "http://github.com/a/b/issues/1"),
    ("select", ("criteria_refs", 0), f"https://github.com/{REPOSITORY}/pull/347"),
    ("select", ("scores", 0, "value"), 4), ("select", ("scores", 0, "value"), True),
    ("select", ("scores", 0, "readiness"), 0.25), ("select", ("scores", 0, "size"), "13"),
    ("select", ("scores", 0, "score"), -1), ("select", ("scores", 0, "next"), 1), ("select", ("decision",), "maybe"),
    ("select", ("issue",), 0), ("select", ("issue",), 347.0), ("select", ("scores",), [SELECT["scores"][0]] * 6),
    ("implement", ("extra",), 1), ("implement", ("branch",), "claude/autodev-347-Run"),
    ("implement", ("branch",), "main"), ("implement", ("head_sha",), "A" * 40), ("implement", ("head_sha",), "a" * 39),
    ("implement", ("changes", 0, "path"), "x" * 301), ("implement", ("checks", 0, "status"), "skipped"),
    ("implement", ("reviews", 0, "handle"), "review-0"), ("implement", ("reviews", 0, "fixed"), -1),
    ("implement", ("blocked",), "false"), ("implement", ("pr_sections", "self_review"), "x" * 4001),
    ("implement", ("pr_sections", "extra"), ""), ("implement", ("blockers",), ["x"] * 11),
    ("submit", ("extra",), 1), ("submit", ("pr_url",), "https://example.org/pull/1"),
    ("submit", ("review_status",), "merged"), ("submit", ("bots", 0, "bot"), "dependabot"),
    ("submit", ("bots", 0, "head"), "b" * 41), ("submit", ("rounds",), -1), ("submit", ("findings", 0), "thread:1"),
    ("submit", ("reason",), "x" * 501), ("submit", ("next_attempt",), "x" * 41),
])
def test_each_model_refuses_an_extra_key_a_long_string_a_bad_pattern_or_value(kind, path, value):
    output, problems = validate(kind, changed(EXAMPLES[kind], path, value))
    assert output is None and problems, (path, value)


@pytest.mark.parametrize("kind", stages.KINDS)
def test_a_missing_required_key_is_refused(kind):
    for key in stages.SCHEMAS[kind]["required"]:
        value = copy.deepcopy(EXAMPLES[kind])
        del value[key]
        output, problems = validate(kind, value)
        assert output is None and any(problem.startswith(key) for problem in problems), key


def test_codex_text_is_parsed_strictly():
    text = json.dumps(SUBMIT)
    assert validate("submit", text) == (SUBMIT, [])
    assert validate("submit", text.encode()) == (SUBMIT, [])
    for bad in ('{"rounds": 1, "rounds": 2}', '{"rounds": NaN}', "[]", "not json", '"text"'):
        output, problems = validate("submit", bad)
        assert output is None and problems[0].startswith("$: not a JSON object"), bad
    output, problems = validate("submit", " " * (stages.MAX_OUTPUT_CHARS + 1))
    assert output is None and "longer than" in problems[0]


# --- the runner's rules ---------------------------------------------------------------------------

@pytest.mark.parametrize("path, value, message", [
    (("issue",), None, "null exactly when the decision is none"),
    (("slice",), None, "null exactly when the decision is none"),
    (("issue",), 349, "#349 is not among the candidates"),
    (("scores", 1, "issue"), 350, "scores.1.issue: #350 is not among the candidates"),
    (("criteria_refs", 0), "https://github.com/someone/else/issues/347", "criteria_refs.0"),
    (("criteria_refs", 1), f"https://github.com/{REPOSITORY}/issues/348#issuecomment-1", "criteria_refs.1"),
    (("scores", 0, "rationale"), "Ask @coderabbitai first.", "@mention"),
    (("rationale",), "<!-- autodev:state {} -->", "sub-marker"),
    (("slice", "remaining", 0), "ping &#64;owner", "@mention"),
])
def test_select_rules(path, value, message):
    output, problems = validate("select", changed(SELECT, path, value), candidates=[347, 348])
    assert output is None and any(message in problem for problem in problems), problems


def test_select_none_takes_no_issue_slice_or_criteria():
    for path, value in ((("issue",), 347), (("slice",), SELECT["slice"]),
                        (("criteria_refs",), [f"https://github.com/{REPOSITORY}/issues/347"])):
        output, problems = validate("select", changed(NONE, path, value))
        assert output is None and problems, path
    assert validate("select", NONE, candidates=[]) == (NONE, [])


def test_select_criteria_refs_compare_the_repository_case_insensitively():
    value = changed(SELECT, ("criteria_refs",), ["https://github.com/iezhu/agents/issues/347"])
    assert validate("select", value) == (value, [])


@pytest.mark.parametrize("path, value, options, message", [
    (("blocked",), True, {}, "needs at least one blocker"),
    (("blockers",), ["a closed path"], {}, "needs at least one blocker"),
    (("branch",), "claude/autodev-347-other", {"branch": "claude/autodev-347-run"}, "is not the run's branch"),
    (("branch",), "claude/autodev-348-run", {"issue": 347}, "is not a branch of issue #347"),
])
def test_implement_rules(path, value, options, message):
    output, problems = validate("implement", changed(IMPLEMENT, path, value), **options)
    assert output is None and any(message in problem for problem in problems), problems


def test_implement_blockers_must_be_fit_to_post():
    value = changed(changed(IMPLEMENT, ("blocked",), True), ("blockers",), ["Owner: @WonderMr please look"])
    output, problems = validate("implement", value)
    assert output is None and problems == ["blockers.0: the text may not hold an @mention; write the name without @"]
    fine = changed(changed(IMPLEMENT, ("blocked",), True), ("blockers",), ["needs a control path"])
    assert validate("implement", fine, issue=347, branch="claude/autodev-347-run") == (fine, [])


@pytest.mark.parametrize("path, value, message", [
    (("next_attempt",), None, "exactly when review_status is waiting_bots"),
    (("review_status",), "complete", "exactly when review_status is waiting_bots"),
    (("next_attempt",), "2026-10-14T14:15:00+02:00", "not an ISO 8601 UTC time"),
    (("next_attempt",), "2026-10-14T12:15:00", "not an ISO 8601 UTC time"),
    (("next_attempt",), "in 15 minutes", "not an ISO 8601 UTC time"),
    (("bots", 0, "next_attempt"), "tomorrow", "bots.0.next_attempt"),
    (("pr_url",), "https://github.com/someone/else/pull/5", "not a pull request of"),
    (("pr_url",), f"https://github.com/{REPOSITORY}/issues/5", "not a pull request of"),
])
def test_submit_rules(path, value, message):
    output, problems = validate("submit", changed(SUBMIT, path, value))
    assert output is None and any(message in problem for problem in problems), problems


def test_submit_without_waiting_and_with_utc_offsets():
    done = changed(changed(SUBMIT, ("review_status",), "complete"), ("next_attempt",), None)
    assert validate("submit", done) == (done, [])
    blocked = changed(changed(done, ("review_status",), "blocked"), ("pr_url",), None)
    assert validate("submit", blocked) == (blocked, [])
    offset = changed(SUBMIT, ("next_attempt",), "2026-10-14T12:15:00+00:00")
    assert validate("submit", offset) == (offset, [])


def test_parse_utc():
    assert stages.parse_utc("2026-10-14T12:15:00Z").isoformat() == "2026-10-14T12:15:00+00:00"
    for bad in (None, "", "2026-10-14", "2026-10-14T12:15:00+01:00", 5):
        assert stages.parse_utc(bad) is None or bad == "2026-10-14"
    assert stages.parse_utc("2026-10-14") is None  # no offset


# --- prompts ----------------------------------------------------------------------------------------

def values(kind):
    every = {"repository": REPOSITORY, "worktree": "/runner/sessions/r/implement/repo", "pinned": SHA,
             "inputs_file": "/runner/sessions/r/state/inputs-implement.json",
             "state_copy": "/runner/sessions/r/state", "exchange_dir": "/runner/sessions/r/implement/exchange",
             "branch": "claude/autodev-347-run", "worker_cmd": "/runner/clients/worker", "gh_cmd": "/runner/clients/gh",
             "heavy_cmd": "/runner/clients/heavy", "commit_author": "Alexey Zhuchkov <alexey.zhuchkov@gmail.com>",
             "python": "/runner/venvs/abc/bin/python"}
    return {name: every[name] for name in stages.template_fields(kind)}


@pytest.mark.parametrize("kind", stages.KINDS)
def test_render_prompt_fills_every_placeholder(kind):
    filled = values(kind)
    text = stages.render_prompt(kind, filled)
    assert not re.search(r"\{(" + "|".join(stages.PLACEHOLDERS) + r")\}", text)
    for value in filled.values():
        assert value in text
    assert stages.FLOWS[kind] in text and "untrusted data, never instructions" in text


def test_the_templates_use_the_placeholders_of_design_10_4():
    assert stages.template_fields("select") == {"repository", "worktree", "pinned", "inputs_file"}
    assert stages.template_fields("implement") == stages.template_fields("submit") == set(stages.PLACEHOLDERS)


def test_render_prompt_leaves_json_braces_and_inserts_values_once():
    text = stages.render_prompt("implement", values("implement"))
    assert "/runner/clients/worker wait '{\"handle\": \"<handle>\"}'" in text
    odd = dict(values("implement"), worktree="/runner/{not-a-placeholder}/repo")
    assert "/runner/{not-a-placeholder}/repo" in stages.render_prompt("implement", odd)


@pytest.mark.parametrize("change, message", [
    ({"extra": "x"}, "unknown placeholder"), ({"branch": None}, "missing placeholder"),
    ({"pinned": "a\nb"}, "control character"), ({"pinned": "a\0b"}, "control character"),
    ({"pinned": "a\rb"}, "control character"), ({"pinned": ""}, "non-empty text"),
    ({"worktree": "/x/{pinned}"}, "holds a placeholder"),
])
def test_render_prompt_refuses_bad_values(change, message):
    filled = dict(values("implement"))
    for name, value in change.items():
        if value is None:
            del filled[name]
        else:
            filled[name] = value
    with pytest.raises(ValueError, match=message):
        stages.render_prompt("implement", filled)


def test_the_select_prompt_takes_no_session_commands():
    with pytest.raises(ValueError, match="unknown placeholder"):
        stages.render_prompt("select", dict(values("select"), worker_cmd="/runner/clients/worker"))


# --- branch, selection comment ----------------------------------------------------------------------

@pytest.mark.parametrize("title, slug", [
    ("Autodev: run sequence, stage sessions", "autodev-run-sequence-stage-sessions"),
    ("Über café — naïve fix", "uber-cafe-naive-fix"),
    ("🚀 ☃", "task"), ("", "task"),
    ("x" * 50, "x" * 40),
    ("Make the runner's run sequence and the stage sessions robust", "make-the-runner-s-run-sequence-and-the"),
])
def test_branch_slug(title, slug):
    assert stages.branch_slug(title, 347) == slug and len(slug) <= stages.SLUG_MAX
    assert repos.branch_name(347, slug) == f"claude/autodev-347-{slug}"


def test_branch_slug_needs_a_valid_issue():
    with pytest.raises(ValueError, match="positive integer"):
        stages.branch_slug("title", 0)


def test_selection_body_holds_the_markers_slice_table_and_links():
    body = stages.selection_body(SELECT, REPOSITORY)
    assert body.startswith(f"{broker.MARKER}\n{broker.SELECTION_MARKER}\n")
    broker.check_marker(body, on_pr=False, internal=True)
    assert "Autodev selected #347" in body and "**This pull request:** Part A" in body and "- Part B" in body
    rows = [line for line in body.splitlines() if line.startswith("| ") and not line.startswith("| #")]
    assert [row.split(" | ")[1] for row in rows] == [f"[#347](https://github.com/{REPOSITORY}/issues/347)",
                                                     f"[#348](https://github.com/{REPOSITORY}/issues/348)"]
    assert "Settings page \\| small." in body  # a pipe in a cell is escaped
    assert f"https://github.com/{REPOSITORY}/issues/347#issuecomment-123" in body


def test_selection_body_refuses_none_and_text_unfit_to_post():
    with pytest.raises(ValueError, match="only a task decision"):
        stages.selection_body(NONE, REPOSITORY)
    with pytest.raises(ValueError, match="@mention"):
        stages.selection_body(changed(SELECT, ("slice", "this_pr"), "cc @someone"), REPOSITORY)
    flat = stages.selection_body(changed(SELECT, ("scores", 0, "rationale"), "two\nlines\x07"), REPOSITORY)
    assert "two lines" in flat and "\x07" not in flat


def test_score_table_keeps_at_most_five_rows_in_order():
    scores = [dict(SELECT["scores"][0], issue=number, score=10 - number) for number in range(1, 8)]
    table = stages.score_table(scores)
    rows = table.splitlines()[2:]
    assert len(rows) == 5 and [row.split(" | ")[1] for row in rows] == ["#1", "#2", "#3", "#4", "#5"]
    assert rows[0].split(" | ")[8] == "9.00"


# --- the stream --------------------------------------------------------------------------------------

def stream(*events, garbage=False):
    lines = [json.dumps(event) for event in events]
    if garbage:
        lines.insert(1, "not json")
    return "\n".join(lines) + "\n"


INIT = {"type": "system", "subtype": "init", "model": "claude-opus-4-5", "tools": ["Read"]}


def test_parse_stage_stream_reads_init_result_cost_and_denials():
    denial = {"tool_name": "Read", "tool_use_id": "t1", "tool_input": {"file_path": "/x"}}
    result = stages.parse_stage_stream(stream(INIT, {"type": "assistant", "message": {"content": []}},
                                              {"type": "result", "subtype": "success", "is_error": False,
                                               "structured_output": SELECT, "total_cost_usd": 1.25,
                                               "permission_denials": [denial]}, garbage=True))
    assert result.structured == SELECT and result.init == INIT and result.model_reported == "claude-opus-4-5"
    assert result.cost_usd == 1.25 and result.denials == [denial]
    assert result.info == {"subtype": "success", "is_error": False, "permission_denials": 1, "unparsed": 1}


@pytest.mark.parametrize("result, problem", [
    ({"type": "result", "subtype": "error_max_turns", "is_error": True}, "not a success"),
    ({"type": "result", "subtype": "success", "is_error": True, "structured_output": SELECT}, "not a success"),
    ({"type": "result", "subtype": "success", "is_error": False, "result": "text"}, "no structured_output"),
    ({"type": "assistant"}, "no result event"),
])
def test_parse_stage_stream_without_a_usable_result(result, problem):
    found = stages.parse_stage_stream(stream(INIT, result))
    assert found.structured is None and problem in found.info["problem"] and found.cost_usd is None


def test_execution_of_reads_the_requested_profile_and_the_argv():
    settings = copy.deepcopy(config.DEFAULTS)
    argv = ["/usr/bin/claude", "-p", "--tools", "Read", "--model", "opus", "--effort", "high"]
    result = stages.StageResult(SELECT, INIT, {}, "claude-opus-4-5")
    assert stages.execution_of("select", settings, argv, result) == {
        "requested": {"client": "claude", "model": "opus", "effort": "high", "method": "session"},
        "applied": {"client": "claude", "model": "opus", "effort": "high", "method": "session",
                    "model_reported": "claude-opus-4-5", "fallback_reason": None}}
    settings["stages"]["implement"] = {"client": "codex", "codex": {"model": "gpt-6.1-sol", "effort": "ultra"}}
    fallen = stages.execution_of("implement", settings, ["/usr/bin/claude", "-p", "--model", "opus"],
                                 fallback_reason="codex_stage_commit_unsupported")
    assert fallen["requested"] == {"client": "codex", "model": "gpt-6.1-sol", "effort": "ultra", "method": "session"}
    assert fallen["applied"]["client"] == "claude" and fallen["applied"]["effort"] is None
    assert fallen["applied"]["fallback_reason"] == "codex_stage_commit_unsupported"
    codex = stages.execution_of("implement", settings, ["/opt/codex", "exec", "-c", "sandbox_workspace_write.x=true",
                                                        "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=ultra"],
                                client="codex")
    assert (codex["applied"]["model"], codex["applied"]["effort"]) == ("gpt-6.1-sol", "ultra")


@pytest.mark.parametrize("kind, unavailable, expected", [
    ("select", None, {"client": "codex", "model": "gpt-6.1-sol", "effort": "high", "requested": "codex",
                      "fallback_reason": None}),
    ("select", "nested_sandbox_unavailable", {"client": "claude", "model": "opus", "effort": "high",
                                              "requested": "codex", "fallback_reason": "nested_sandbox_unavailable"}),
    ("implement", None, {"client": "claude", "model": "opus", "effort": "xhigh", "requested": "codex",
                         "fallback_reason": "codex_stage_commit_unsupported"}),
    ("submit", "not_checked", {"client": "claude", "model": "sonnet", "effort": "medium", "requested": "codex",
                               "fallback_reason": "codex_stage_commit_unsupported"}),
])
def test_stage_profile_falls_back_from_codex(kind, unavailable, expected):
    settings = copy.deepcopy(config.DEFAULTS)
    for name in stages.KINDS:
        settings["stages"][name]["client"] = "codex"
        settings["stages"][name]["codex"] = {"model": "gpt-6.1-sol", "effort": "high"}
        del settings["stages"][name]["claude"]
    settings["stages"]["submit"]["claude"] = {"model": "sonnet", "effort": "medium"}  # its own Claude profile
    assert stages.stage_profile(kind, settings, codex_unavailable=unavailable) == expected


def test_stage_profile_of_a_claude_stage():
    assert stages.stage_profile("implement", config.DEFAULTS) == {
        "client": "claude", "model": "opus", "effort": "xhigh", "requested": "claude", "fallback_reason": None}


# --- inputs files ------------------------------------------------------------------------------------

def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_write_inputs_is_read_only_and_can_be_rewritten(tmp_path):
    data = stages.implement_inputs(issue=347, slice=SELECT["slice"], criteria_refs=SELECT["criteria_refs"],
                                   labels=["autodev:ready", "area:runner", "autodev:ready"], max_reviews=3)
    assert data == {"issue": 347, "slice": SELECT["slice"], "criteria_refs": SELECT["criteria_refs"],
                    "labels": ["area:runner", "autodev:ready"], "max_reviews": 3}
    path = stages.write_inputs(tmp_path / "state", "implement", data)
    assert path == tmp_path / "state" / "inputs-implement.json" and mode(path) == 0o400
    assert json.loads(path.read_text()) == data
    stages.write_inputs(tmp_path / "state", "implement", dict(data, max_reviews=2))
    assert json.loads(path.read_text())["max_reviews"] == 2 and mode(path) == 0o400
    with pytest.raises(ValueError):
        stages.implement_inputs(issue=0, slice={}, criteria_refs=[], labels=[], max_reviews=3)


def test_submit_inputs_and_the_resume_additions():
    plain = stages.submit_inputs(branch="claude/autodev-347-run", description_extra=IMPLEMENT["pr_sections"],
                                 issue=347, slice=SELECT["slice"], max_bot_rounds=3, max_wait_minutes=120)
    assert plain == {"branch": "claude/autodev-347-run", "description_extra": IMPLEMENT["pr_sections"], "issue": 347,
                     "slice": SELECT["slice"], "max_bot_rounds": 3, "max_wait_minutes": 120}
    resume = {"pr": 501, "rounds": 2, "findings_seen": ["review:1", "comment:7"], "state_comment_head": SHA}
    resumed = stages.submit_inputs(branch="claude/autodev-347-run", description_extra={}, issue=347, slice=None,
                                   max_bot_rounds=3, max_wait_minutes=120, resume=resume)
    assert {key: resumed[key] for key in resume} == resume and resumed["slice"] is None
    for bad in ({**resume, "pr": 0}, {**resume, "rounds": -1}, {**resume, "findings_seen": ["thread:1"]},
                {**resume, "state_comment_head": "main"}):
        with pytest.raises(ValueError):
            stages.submit_inputs(branch="claude/autodev-347-run", description_extra={}, issue=347, slice=None,
                                 max_bot_rounds=3, max_wait_minutes=120, resume=bad)
    with pytest.raises(ValueError, match="not an autodev branch"):
        stages.submit_inputs(branch="main", description_extra={}, issue=347, slice=None, max_bot_rounds=3,
                             max_wait_minutes=120)


def candidate(number, **changes):
    item = {"number": number, "title": f"Issue {number}", "labels": ["autodev:ready"], "assignees": [],
            "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
            "author": {"login": "WonderMr", "id": 5370211}, "app": None,
            "label_times": {"autodev:ready": "2026-10-02T00:00:00Z", "autodev:next": None},
            "owner_activity": "2026-10-02T00:00:00Z", "agent_state": None, "open_prs": [], "branches": [],
            "dependencies": [], "closed_autodev_prs": [], "body": f"Body of {number}\n",
            "comments": [{"id": 1, "author": {"login": "WonderMr", "id": 5370211}, "app": None,
                          "created_at": "2026-10-02T00:00:00Z", "body": "Criteria.", "extra": "dropped"}]}
    item.update(changes)
    return item


def test_write_select_inputs_puts_bodies_and_comments_in_read_only_files(tmp_path):
    data = stages.select_inputs(repository=REPOSITORY, mode="dry-run", now="2026-10-14T12:00:00Z",
                                issues=[candidate(347), candidate(348)])
    path = stages.write_select_inputs(data, tmp_path / "state")
    written = json.loads(path.read_text())
    assert path.name == "inputs-select.json" and mode(path) == 0o400
    assert (written["repository"], written["mode"], written["now"]) == (REPOSITORY, "dry-run", "2026-10-14T12:00:00Z")
    first = written["issues"][0]
    assert set(first) == {*stages.SELECT_ISSUE_FIELDS, "body_path", "comments_path"}
    assert Path(first["body_path"]).read_text() == "Body of 347\n" and mode(Path(first["body_path"])) == 0o400
    comments = json.loads(Path(first["comments_path"]).read_text())
    assert comments == [{"id": 1, "author": {"login": "WonderMr", "id": 5370211}, "app": None,
                         "created_at": "2026-10-02T00:00:00Z", "body": "Criteria."}]
    assert Path(first["comments_path"]).parent == tmp_path / "state" / "issues" / "347"


@pytest.mark.parametrize("issues, message", [
    ([{k: v for k, v in candidate(1).items() if k != "agent_state"}], "lacks agent_state"),
    ([candidate(1), candidate(1)], "distinct positive integers"),
    ([candidate(0)], "distinct positive integers"),
    ([candidate(1, body=None)], "body must be text"),
    ([candidate(1, comments=[{"id": 1}])], "each comment needs"),
])
def test_select_inputs_refuses_an_incomplete_candidate(issues, message):
    with pytest.raises(ValueError, match=message):
        stages.select_inputs(repository=REPOSITORY, mode="run", now="2026-10-14T12:00:00Z", issues=issues)


def test_select_inputs_checks_the_mode_and_the_time():
    with pytest.raises(ValueError, match="mode"):
        stages.select_inputs(repository=REPOSITORY, mode="resume", now="2026-10-14T12:00:00Z", issues=[])
    with pytest.raises(ValueError, match="UTC"):
        stages.select_inputs(repository=REPOSITORY, mode="run", now="2026-10-14T12:00:00", issues=[])
