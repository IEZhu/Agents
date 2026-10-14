"""The worker launcher (#345): packages, materials, both CLIs, validation, policy and records.

The fake ``claude`` (tests/fixtures/fake_claude.py, worker mode) and the fake ``codex``
(tests/fixtures/fake_codex.py) build their results from the worker_context they receive
(tests/fixtures/fake_worker.py); a real trusted repository is made with git in a temporary
directory. No model runs, no network is used and no real keychain is read.
"""
import copy
import json
import os
from pathlib import Path
import re
import stat
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

from src.engine.subagent import prepare_subagent
from src.schedules import (broker, codex_probes, decoys, gitsafe, materials, records, sandbox, socket_service,
                           worker_launch, workers)
from src.schedules import codex_sandbox as cs
from src.schedules import config as autodev_config
from src.schedules.socket_service import Connection, Registry, RequestRefused, serve
from src.schedules.workers import Binding, WorkerService
from src.schemas.subagent import RESULT_MODELS
from tests.test_schedules_codex import KEY, MODE, TEST_NET, Emulator, fake_codex, keychain

FAKE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"
PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
RUN_ID = "20261014T130000Z-abc345"
ISSUE = 345
BRANCH = f"refs/heads/claude/autodev-{ISSUE}-workers"
SESSION = {"session_root": lambda: 1, "lineage": lambda pid: {pid, 1}}
_SHA = re.compile(r"[0-9a-f]{40}")


# --- a trusted repository --------------------------------------------------------------------------

def git(cwd: Path, *args: str) -> str:
    env = {"PATH": os.environ.get("PATH", PATH), "HOME": str(cwd), "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.org",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.org", "LC_ALL": "C"}
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return done.stdout.strip()


class Repo:
    """A work repository and the bare trusted repository it pushes promoted heads to."""

    def __init__(self, base: Path):
        self.work, self.trusted = base / "work", base / "trusted.git"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        git(base, "init", "-q", "--bare", str(self.trusted))
        self.base = self.commit({"README.md": "base\n", "src/app.py": "def run():\n    return 1\n"}, "base")
        git(self.work, "push", "-q", str(self.trusted), f"{self.base}:refs/remotes/origin/main")
        git(self.work, "checkout", "-q", "-b", "feature")

    def commit(self, files: dict, message: str = "change") -> str:
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", message)
        return git(self.work, "rev-parse", "HEAD")

    def promote(self, sha: str, ref: str = BRANCH) -> str:
        git(self.work, "push", "-q", "-f", str(self.trusted), f"{sha}:{ref}")
        return sha


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def fake_claude(directory: Path, scenario: dict) -> str:
    scenario_file = directory / "scenario.json"
    scenario_file.write_text(json.dumps(scenario))
    wrapper = directory / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_CLAUDE} {scenario_file} \"$@\"\n")
    wrapper.chmod(0o755)
    return str(wrapper)


def config(**changes) -> dict:
    found = copy.deepcopy(autodev_config.DEFAULTS)
    found["delegates"]["code-review-pass"] = {"client": "claude", "claude": {"model": "opus", "effort": "high"},
                                              "effort_followup": "medium"}
    for key, value in changes.items():
        found[key] = value
    return autodev_config.validate(found)


class World:
    def __init__(self, base: Path, state: Path, repo: Repo):
        self.base, self.state, self.repo = base, state, repo
        self.home = base / "home"
        for name in (".ssh", "Library/Keychains", ".config/agents-autodev"):
            (self.home / name).mkdir(parents=True)
        self.planted = decoys.ensure(state, self.home)
        self.bin = base / "bin"
        self.bin.mkdir()
        self.seen_file = base / "seen.jsonl"
        self.counter = base / "counter"
        self.copy_dir = base / "state-copy"
        self.packages = []
        self.scenario({})

    def scenario(self, scenario: dict) -> None:
        self.claude = fake_claude(self.bin, {"seen": str(self.seen_file), "counter": str(self.counter), **scenario})

    def seen(self) -> list[dict]:
        if not self.seen_file.exists():
            return []
        return [json.loads(line) for line in self.seen_file.read_text().splitlines()]

    def prepare(self, args):
        package = prepare_subagent(args)
        self.packages.append(package)
        return package

    def service(self, settings=None, *, criteria=("Workers are validated against the retained contract.",),
                prepare=None, **options) -> WorkerService:
        binding = Binding(RUN_ID, ISSUE, self.repo.base, self.repo.trusted, self.state, log_copy=self.copy_dir,
                          criteria=criteria)
        options.setdefault("command_runner", keychain)
        launcher = worker_launch.Launcher(claude=self.claude, path=PATH, home=self.home, state_dir=self.state,
                                          run_id=RUN_ID, decoys=self.planted, **options)
        return WorkerService(binding, settings or config(), launcher, prepare=prepare or self.prepare)

    @property
    def run_dir(self) -> Path:
        return records.run_dir(RUN_ID, self.state)


@pytest.fixture
def world(tmp_path_factory, service_state_dir, repo):
    return World(Path(os.path.realpath(tmp_path_factory.mktemp("wrk"))), service_state_dir, repo)


def head_of(world: World, files=None, message="change") -> str:
    sha = world.repo.commit(files or {"src/app.py": f"def run():\n    return {time.time_ns()}\n"}, message)
    return world.repo.promote(sha)


def finish(service: WorkerService, started: dict, seconds: float = 60) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = service.wait({"handle": started["handle"], "seconds": 5})
        if state["status"] != workers.RUNNING:
            return state
    raise AssertionError("the worker did not finish")


def attempt_records(world: World, handle: str) -> list[dict]:
    directory = world.run_dir / workers.WORK_DIR / handle
    return [json.loads(path.read_text()) for path in sorted(directory.glob("attempt-*.json"))]


# --- the Claude worker -----------------------------------------------------------------------------

def test_claude_worker_argv_environment_and_stdin(world):
    head = head_of(world, {"src/app.py": "def run():\n    return 2\n", "docs/new.md": "new\n"})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "accepted", state
    [call] = world.seen()
    package = world.packages[-1]
    assert call["stdin"] == package.worker_context and len(call["stdin"]) == package.context_chars
    tree = world.run_dir / "workers" / f"tree-{world.repo.base}"
    materials_dir = world.run_dir / "workers" / "review-1" / "materials"
    settings_file = world.run_dir / "workers" / "review-1" / "attempt-1" / "claude" / "settings.json"
    schema = worker_launch.schema_text(package.result_schema)
    assert call["argv"] == ["-p", "--restricted", "--safe-mode", "--tools", "Read,Grep,Glob", "--strict-mcp-config",
                            "--permission-mode", "dontAsk", "--permission-prompts", "none", "--disallowedTools",
                            "mcp__*", "--no-session-persistence", "--settings", str(settings_file),
                            "--add-dir", str(materials_dir), "--output-format", "json", "--json-schema", schema,
                            "--model", "opus", "--effort", "high"]
    assert json.loads(schema) == RESULT_MODELS["review"].model_json_schema()  # unchanged for Claude
    assert os.path.realpath(call["cwd"]) == os.path.realpath(tree)
    env = call["env"]
    noise = {"PWD", "SHLVL", "_", "OLDPWD", "__CF_USER_TEXT_ENCODING", "PYTHONNOUSERSITE"}  # set by sh and python
    assert set(env) - noise == set(sandbox.BASE_VARIABLES)
    assert env["CLAUDE_CONFIG_DIR"] == str(sandbox.config_dir(world.state)) and env["HOME"] == str(world.home)
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false" and env["TMPDIR"] == env["CLAUDE_CODE_TMPDIR"]
    assert sandbox.TMP_NAME.fullmatch(Path(env["TMPDIR"]).name) and not Path(env["TMPDIR"]).exists()  # removed
    assert (tree / "src" / "app.py").read_text() == "def run():\n    return 1\n"  # the tree at pinned
    assert (materials_dir / "head" / "docs" / "new.md").read_text() == "new\n"
    assert "+    return 2" in (materials_dir / "diff.patch").read_text()
    assert json.loads((materials_dir / "range.json").read_text())["range"] == f"{world.repo.base}..{head}"
    assert (materials_dir / "criteria.md").read_text().startswith("Workers are validated")
    assert not (world.run_dir / "workers" / "review-1" / "attempt-1" / "stdout").exists()
    # The worker's settings: the closed sandbox block, the read block, nothing writable but its temp root.
    content = json.loads(settings_file.read_text())
    assert content["permissions"] == {"blockReadsOutsideWorkingDirectories": True, "allow": [],
                                      "deny": [f"Edit(/{tree}/**)", f"Edit(/{materials_dir}/**)"]}
    assert content["sandbox"]["filesystem"]["allowWrite"] == [env["TMPDIR"]]
    assert content["disableClaudeAiConnectors"] is True and content["sandbox"]["network"]["allowedDomains"] == []
    assert stat.S_IMODE(settings_file.stat().st_mode) == 0o600
    assert package.contract.agent == "code_reviewer" and package.contract.result_type == "review"


def test_the_worker_settings_and_argv_are_checked(world, tmp_path):
    temp_root = sandbox.make_temp_root()
    try:
        (tmp_path / "tree").mkdir()
        (tmp_path / "materials").mkdir()
        paths = sandbox.WorkerPaths(home=world.home, tree=tmp_path / "tree", materials=tmp_path / "materials",
                                    config_dir=sandbox.config_dir(world.state), gh_config=tmp_path / "gh",
                                    settings_file=tmp_path / "conf" / "settings.json", tmp_root=temp_root,
                                    state_dir=world.state)
        (tmp_path / "gh").mkdir()
        spec = sandbox.build_worker(world.claude, paths, schema='{"type":"object"}', path=PATH, effort="high")
        assert sandbox.check_worker_spec(spec) == []
        loosened = copy.deepcopy(dict(spec.settings))
        loosened["permissions"]["allow"] = ["Read(//**)"]
        assert "permissions.allow must be empty for a worker" in sandbox.check_worker_settings(loosened, paths)
        loosened = copy.deepcopy(dict(spec.settings))
        loosened["sandbox"]["network"]["allowLocalBinding"] = True
        assert any("allowLocalBinding" in item for item in sandbox.check_worker_settings(loosened, paths))
        assert sandbox.check_worker_spec(sandbox.WorkerSpec((*spec.argv, "--add-dir", str(world.home)), spec.env, paths,
                                                            spec.settings, spec.schema, executable=spec.executable))
        wide = sandbox.WorkerPaths(**{**paths.__dict__, "materials": world.home})
        assert any("equals or contains home" in item for item in wide.problems())
        inside = sandbox.WorkerPaths(**{**paths.__dict__, "config_dir": tmp_path / "tree" / "conf"})
        assert any("config_dir lies inside or around the tree" in item for item in inside.problems())
        with pytest.raises(sandbox.SandboxError, match="JSON object"):
            sandbox.worker_argv(world.claude, paths, "[1]")
    finally:
        sandbox.remove_temp_root(temp_root)


def test_a_wrong_worker_context_length_never_starts_the_worker(world):
    head = head_of(world)

    def wrong(args):
        package = prepare_subagent(args)
        return package.model_copy(update={"context_chars": package.context_chars + 1})

    service = world.service(prepare=wrong)
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "failed" and "context_chars" in state["reason"] and "not started" in state["reason"]
    assert world.seen() == []
    [record] = attempt_records(world, "review-1")
    assert record["verdict"] == "failed" and record["contract"]["attempt_id"] == "1"
    assert broker.validated_review_head(world.run_dir) is None
    with pytest.raises(RequestRefused, match="the stage is blocked"):
        service.review({"head": head})


def test_valid_false_gives_a_new_attempt_with_a_new_package(world):
    head = head_of(world)
    world.scenario({"outcomes": [{"override": {"attempt_id": "99"}}, {}]})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "accepted" and state["attempts"] == 2
    first, second = attempt_records(world, "review-1")
    assert first["verdict"] == "retry" and "valid: false (identity_mismatch)" in first["reason"]
    assert first["validation"]["valid"] is False and second["validation"]["valid"] is True
    assert (first["contract"]["attempt_id"], second["contract"]["attempt_id"]) == ("1", "2")
    assert first["contract"]["contract_revision"] != second["contract"]["contract_revision"]
    assert broker.validated_review_head(world.run_dir) == head


def test_partial_and_failed_retry_twice_then_block(world):
    head = head_of(world)
    world.scenario({"outcomes": [{"status": "partial"}, {"status": "failed"}, {"status": "partial"}, {}]})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "blocked" and state["attempts"] == 3, state
    assert "no accepted result after 3 attempts" in state["reason"] and "status partial" in state["reason"]
    assert [record["verdict"] for record in attempt_records(world, "review-1")] == ["retry", "retry", "retry"]
    assert len(world.seen()) == 3 and broker.validated_review_head(world.run_dir) is None
    assert "summary" not in state and "findings" not in state


def test_input_required_blocks_at_once(world):
    head = head_of(world)
    world.scenario({"outcomes": [{"status": "input_required"}, {}]})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "blocked" and state["attempts"] == 1
    assert state["reason"].startswith("input_required") and "Which branch" not in json.dumps(state)


def test_evidence_naming_another_head_is_never_accepted(world):
    head = head_of(world)
    other = "0" * 40
    world.scenario({"outcome": {"range": f"{world.repo.base}..{other}"}})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "blocked" and "does not name the reviewed range" in state["reason"]
    records_ = attempt_records(world, "review-1")
    assert all(record["validation"]["valid"] for record in records_)  # valid, yet not accepted
    assert broker.validated_review_head(world.run_dir) is None


def test_a_cli_failure_is_a_new_attempt(world):
    head = head_of(world)
    world.scenario({"outcomes": [{"subtype": "error_max_structured_output_retries", "is_error": True, "exit": 1,
                                  "no_output": True}, {}]})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "accepted" and state["attempts"] == 2
    first = attempt_records(world, "review-1")[0]
    assert first["cli"]["subtype"] == "error_max_structured_output_retries" and first["cli"]["exit_code"] == 1
    assert "output" not in first


# --- refs and materials ----------------------------------------------------------------------------

@pytest.mark.parametrize("head", ["--output=/tmp/x", "HEAD", "main", "-n1", "A" * 40, "a" * 39])
def test_a_session_supplied_ref_never_reaches_git(world, monkeypatch, head):
    calls = []
    original = gitsafe.command
    monkeypatch.setattr(gitsafe, "command", lambda *args, **kwargs: calls.append(args) or original(*args, **kwargs))
    service = world.service()
    with pytest.raises(RequestRefused, match="40-character"):
        service.review({"head": head})
    assert calls == []
    registry = Registry()
    workers.register(registry, service)
    line = json.dumps({"type": "worker.review", "args": {"head": head}}).encode()
    answer = registry.handle(line, Connection(4242), **SESSION)
    assert answer["ok"] is False and "argument head" in answer["error"] and calls == []


def test_every_git_call_puts_revisions_after_end_of_options(world, monkeypatch):
    head = head_of(world)
    calls = []
    original = gitsafe.command
    monkeypatch.setattr(gitsafe, "command", lambda repository, args, config=(): calls.append(list(args)) or
                        original(repository, args, config))
    service = world.service()
    with pytest.raises(RequestRefused, match="not the head of an imported"):
        service.review({"head": world.repo.commit({"x.txt": "unpromoted\n"})})
    assert finish(service, service.review({"head": head}))["status"] == "accepted"
    named = [args for args in calls if any(_SHA.search(part) for part in args)]
    assert named and all("--end-of-options" in args for args in named), named
    for args in named:
        marker = args.index("--end-of-options")
        assert not any(_SHA.search(part) for part in args[:marker]), args


def test_the_head_must_be_a_promoted_branch_tip_of_this_issue(world):
    head = world.repo.commit({"src/app.py": "x = 1\n"})
    world.repo.promote(head, "refs/heads/claude/autodev-999-other")
    service = world.service()
    with pytest.raises(RequestRefused, match="not the head of an imported"):
        service.review({"head": head})
    with pytest.raises(RequestRefused, match="nothing to review"):
        service.review({"head": world.repo.base})


def test_materials_refuse_an_outside_symlink_and_a_gitlink(world):
    os.symlink("../../../etc/passwd", world.repo.work / "escape")
    head = head_of(world, {"src/app.py": "y = 2\n"})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "blocked" and "points outside the tree" in state["reason"]
    assert world.seen() == []
    with pytest.raises(RequestRefused, match="the stage is blocked"):
        service.review({"head": head})


def test_materials_refuse_a_gitlink_and_keep_inward_links_as_text(world, tmp_path):
    repo = world.repo
    os.symlink("src/app.py", repo.work / "inward")
    inward = repo.promote(repo.commit({}, "inward link"))
    service = world.service()
    assert finish(service, service.review({"head": inward}))["status"] == "accepted"
    links = json.loads((world.run_dir / "workers" / "review-1" / "materials" / "links.json").read_text())
    assert links == {"inward": "src/app.py"}
    assert not os.path.lexists(world.run_dir / "workers" / "review-1" / "materials" / "head" / "inward")
    git(repo.work, "update-index", "--add", "--cacheinfo", f"160000,{repo.base},vendor/sub")
    git(repo.work, "commit", "-q", "-m", "gitlink")
    gitlinked = repo.promote(git(repo.work, "rev-parse", "HEAD"))
    with pytest.raises(materials.MaterialsRefused, match="gitlink vendor/sub"):
        materials.assemble(materials.ReviewRepository(repo.trusted), inward, gitlinked, tmp_path / "m")
    with pytest.raises(materials.MaterialsRefused, match="gitlink vendor/sub"):
        materials.export_tree(materials.ReviewRepository(repo.trusted), gitlinked, tmp_path / "t")
    assert not (tmp_path / "t").exists()


def test_a_read_of_a_decoy_outside_the_materials_is_denied(world):
    head = head_of(world)
    decoy = next(item for item in world.planted if item.path is not None)
    world.scenario({"calls": [{"tool": "Read", "input": {"file_path": str(decoy.path)}}]})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "blocked" and "tripwire" in state["reason"] and "names decoy" in state["reason"]
    tripwire = json.loads((world.state / "schedules" / decoys.TRIPWIRE_NAME).read_text())
    assert tripwire["last"]["run_id"] == RUN_ID and decoy.marker not in json.dumps(tripwire)
    # The file tools see only the tree and the materials: the decoy lies in neither.
    record = attempt_records(world, "review-1")[0]
    assert record["cli"]["permission_denials"] == 1 and decoy.marker not in json.dumps(record)


def test_a_decoy_marker_in_the_output_blocks_and_trips(world):
    head = head_of(world)
    decoy = next(item for item in world.planted if item.path is not None)
    world.scenario({"calls": [{"tool": "Read", "input": {"file_path": str(decoy.path)}}],
                    "allow_paths": [decoy.path.name], "outcome": {"echo_reads": True}})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "blocked" and "carries decoy" in state["reason"]
    assert decoy.marker not in (world.run_dir / workers.STATE_NAME).read_text()


# --- the review policy -----------------------------------------------------------------------------

FINDINGS = [
    {"severity": "high", "location": "src/app.py:2", "explanation": "run() returns a constant."},
    {"severity": "medium", "location": "src/app.py:1", "explanation": "No docstring on run()."},
    {"severity": "medium", "location": "src/app.py:3", "explanation": "The token is logged in plain text."},
    {"severity": "low", "location": "README.md:1", "explanation": "The fix regressed the README example."},
]


def efforts(world: World) -> list[str]:
    return [call["argv"][call["argv"].index("--effort") + 1] for call in world.seen()]


def test_follow_up_effort_and_the_restricted_last_review(world):
    world.scenario({"outcome": {"findings": FINDINGS}})
    service = world.service()
    first_head = head_of(world)
    first = finish(service, service.review({"head": first_head}))
    assert first["status"] == "accepted" and first["restricted"] is None and len(first["findings"]) == 4
    assert first["medium_or_higher"] == 3 and first["dropped_out_of_scope"] == 0
    keys = [item["key"] for item in first["findings"]]
    assert keys[0] == workers.finding_key("src/app.py:2", "run() returns a constant.")
    service.decide({"key": keys[1], "decision": "declined", "reason": "Docstrings are not required here."})
    service.decide({"key": keys[0], "decision": "fixed"})

    second_head = head_of(world)
    second = finish(service, service.review({"head": second_head}))
    assert second["status"] == "accepted" and second["range"] == f"{first_head}..{second_head}"
    second_materials = world.run_dir / "workers" / "review-2" / "materials"
    logged = json.loads((second_materials / "decisions.json").read_text())
    assert {entry["key"]: entry["decision"] for entry in logged} == {keys[1]: "declined", keys[0]: "fixed"}

    third = finish(service, service.review({"head": head_of(world)}))
    assert third["status"] == "accepted" and third["restricted"] == "last"
    shown = {item["location"] for item in third["findings"]}
    assert shown == {"src/app.py:2", "src/app.py:3", "README.md:1"}  # high, security, regression
    assert third["dropped_out_of_scope"] == 1 and third["blocking"] == 3
    assert efforts(world) == ["high", "medium", "medium"]
    tasks = [package.contract.task for package in world.packages]
    criteria = [" ".join(item.description for item in package.contract.acceptance_criteria)
                for package in world.packages]
    assert "This is the last review" in criteria[2] and "This is the last" not in criteria[0] + criteria[1]
    assert "decisions.json" in criteria[1] and tasks[0].startswith("Review the change")
    with pytest.raises(RequestRefused, match=r"max_reviews \(3\) is reached"):
        service.review({"head": head_of(world)})


def test_a_follow_up_of_the_same_head_reviews_the_same_range_again(world):
    service = world.service()
    head = head_of(world)
    assert finish(service, service.review({"head": head}))["status"] == "accepted"
    again = finish(service, service.review({"head": head}))
    assert again["range"] == f"{world.repo.base}..{head}" and again["number"] == 2


def test_the_premortem_and_the_verification_review(world):
    world.scenario({"outcome": {"findings": FINDINGS[:3]}})
    settings = config()
    settings["delegates"]["change-premortem"]["role"] = "sysadmin"
    service = world.service(settings)
    head = head_of(world)
    with pytest.raises(RequestRefused, match="follows an accepted review"):
        service.premortem({"head": head})
    assert finish(service, service.review({"head": head}))["status"] == "accepted"
    with pytest.raises(RequestRefused, match="follows an accepted pre-mortem"):
        service.review({"head": head, "verification": True})
    premortem = finish(service, service.premortem({"head": head}))
    assert premortem["status"] == "accepted" and premortem["kind"] == "premortem"
    assert world.packages[-1].contract.agent == "sysadmin"
    assert world.packages[-1].contract.task.startswith("Pre-mortem of the change")
    reviews = json.loads((world.run_dir / "workers" / "premortem-1" / "materials" / "reviews.json").read_text())
    assert [item["handle"] for item in reviews] == ["review-1"]
    risks = broker.read_premortem(world.run_dir)
    key = workers.finding_key("src/app.py:3", "The token is logged in plain text.")
    assert risks[key].level == "medium" and risks[key].security is True
    other = risks[workers.finding_key("src/app.py:1", "No docstring on run().")]
    assert other.security is False and other.title == "No docstring on run()."
    with pytest.raises(RequestRefused, match="takes one of: fixed, mitigated, known_risk"):
        service.decide({"key": key, "decision": "declined", "reason": "no"})
    with pytest.raises(RequestRefused, match="needs a reason"):
        service.decide({"key": key, "decision": "known_risk"})
    service.decide({"key": key, "decision": "fixed"})
    with pytest.raises(RequestRefused, match="only the verification review"):
        service.review({"head": head})
    with pytest.raises(RequestRefused, match="nothing to verify"):
        service.review({"head": head, "verification": True})
    fixed = head_of(world)
    verify = finish(service, service.review({"head": fixed, "verification": True}))
    assert verify["status"] == "accepted" and verify["kind"] == "verify" and verify["restricted"] == "verification"
    assert verify["range"] == f"{head}..{fixed}"
    assert {item["severity"] for item in verify["findings"]} == {"high", "medium"}  # medium: the security one
    assert efforts(world) == ["high", "high", "medium"]  # review, pre-mortem (claude/high), verification
    assert broker.validated_review_head(world.run_dir) == fixed


def test_records_in_the_brokers_format_and_the_run_record(world):
    head = head_of(world)
    world.scenario({"outcome": {"findings": FINDINGS[:1]}})
    service = world.service()
    finish(service, service.review({"head": head}))
    assert broker.validated_review_head(world.run_dir) == head
    review = json.loads((world.run_dir / broker.REVIEW_RECORD).read_text())
    assert review["range"] == f"{world.repo.base}..{head}" and review["record"] == "workers/review-1/attempt-1.json"
    [record] = attempt_records(world, "review-1")
    package = world.packages[-1]
    assert record["package"]["bundle_revision"] == package.contract.bundle_revision
    assert record["package"]["contract_revision"] == package.contract_revision
    assert record["contract"] == package.contract.model_dump(mode="json")
    assert record["output"]["sha256"] == workers.output_digest(record["output"]["raw"]) == review["output_sha256"]
    assert record["validation"] == {"valid": True, "errors": [], "verification": "structural_only"}
    assert (record["range"], record["head"], record["verdict"]) == (f"{world.repo.base}..{head}", head, "accepted")
    assert record["client"] == {"client_requested": "claude", "client_applied": "claude", "fallback_reason": None,
                                "detail": ""}
    run_record = records.read_record(RUN_ID, world.state)
    [entry] = run_record["workers"]
    assert entry["handle"] == "review-1" and entry["status"] == "accepted" and entry["head"] == head
    log_copy = world.copy_dir / workers.COPY_NAME
    assert stat.S_IMODE(log_copy.stat().st_mode) == 0o400
    assert json.loads(log_copy.read_text())["tasks"][0]["findings"][0]["location"] == "src/app.py:2"


def test_the_session_view_is_parsed_and_length_limited(world):
    head = head_of(world)
    long = "x" * 3000 + "\x07"  # within the schema's 4000 characters, beyond the session's limits
    world.scenario({"outcome": {"summary": long, "findings": [{"severity": "low", "location": "a.py:1",
                                                                "explanation": long}]}})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert len(state["summary"]) == workers.LIMITS["summary"] and "\x07" not in state["summary"]
    assert len(state["findings"][0]["explanation"]) == workers.LIMITS["explanation"]
    assert set(state["findings"][0]) == {"key", "severity", "location", "explanation", "recommendation", "decision"}
    assert "evidence" not in state and "checks" not in state


# --- the socket ------------------------------------------------------------------------------------

def test_registered_types_are_session_bound_and_wait_in_portions(world):
    world.scenario({"sleep": 3})
    service = world.service()
    registry = Registry()
    workers.register(registry, service)
    assert registry.types() == ["worker.decide", "worker.premortem", "worker.review", "worker.wait"]
    head = head_of(world)
    line = json.dumps({"type": "worker.review", "args": {"head": head}}).encode()
    outsider = registry.handle(line, Connection(4242), session_root=lambda: 1, lineage=lambda pid: {pid, 7})
    assert outsider["ok"] is False and "not a process of the current stage session" in outsider["error"]
    started = registry.handle(line, Connection(4242), **SESSION)
    assert started["ok"] and started["result"]["status"] == "running" and started["result"]["handle"] == "review-1"
    waited = registry.handle(json.dumps({"type": "worker.wait", "args": {"handle": "review-1", "seconds": 1}}).encode(),
                             Connection(4242), **SESSION)
    assert waited["ok"] and waited["result"]["status"] == "running"
    busy = registry.handle(line, Connection(4242), **SESSION)
    assert busy["ok"] is False and "review-1 is still running" in busy["error"]
    too_long = registry.handle(json.dumps({"type": "worker.wait", "args": {"handle": "review-1",
                                                                          "seconds": workers.WAIT_MAX + 1}}).encode(),
                               Connection(4242), **SESSION)
    assert too_long["ok"] is False and "between 1 and 540" in too_long["error"]
    done = registry.handle(json.dumps({"type": "worker.wait", "args": {"handle": "review-1"}}).encode(),
                           Connection(4242), **SESSION)
    assert done["ok"] and done["result"]["status"] == "accepted"
    unknown = registry.handle(json.dumps({"type": "worker.decide", "args": {"key": "0" * 16,
                                                                           "decision": "fixed"}}).encode(),
                              Connection(4242), **SESSION)
    assert unknown["ok"] is False and "no accepted result" in unknown["error"]
    assert workers.WAIT_DEADLINE <= socket_service.MAX_DEADLINE


def test_the_thin_client_starts_a_review_and_waits_for_it(world):
    """End to end over the real socket: the ``worker`` client of a session (here a child of this
    process, named as the session root) starts a review and waits for its parsed result."""
    world.scenario({"outcome": {"findings": FINDINGS[:1]}})
    service = world.service()
    registry = Registry()
    workers.register(registry, service)
    head = head_of(world)
    short = Path(tempfile.mkdtemp(prefix="wk-"))
    try:
        run_dir = short / "run"
        run_dir.mkdir(mode=0o700)
        with serve(run_dir, registry, session_root=os.getpid) as endpoint:
            client = str(endpoint.clients["worker"])
            done = subprocess.run([client, "review", json.dumps({"head": head})], capture_output=True, text=True,
                                  timeout=60)
            assert done.returncode == 0, done.stderr
            assert json.loads(done.stdout)["handle"] == "review-1"
            done = subprocess.run([client, "wait", '{"handle": "review-1"}'], capture_output=True, text=True,
                                  timeout=120)
            result = json.loads(done.stdout)
            assert done.returncode == 0 and result["status"] == "accepted", done.stderr
            key = result["findings"][0]["key"]
            done = subprocess.run([client, "decide", json.dumps({"key": key, "decision": "declined",
                                                                 "reason": "Intended."})],
                                  capture_output=True, text=True, timeout=60)
            assert done.returncode == 0 and json.loads(done.stdout)["recorded"] is True
            done = subprocess.run([client, "review", json.dumps({"head": head, "argv": ["--upload-pack=x"]})],
                                  capture_output=True, text=True, timeout=60)
            assert done.returncode == 1 and "unknown argument: 'argv'" in done.stderr
    finally:
        shutil.rmtree(short, ignore_errors=True)
    logged = json.loads((world.run_dir / workers.DECISIONS_NAME).read_text())["decisions"]
    assert [(item["key"], item["decision"], item["reason"]) for item in logged] == [(key, "declined", "Intended.")]
    copied = json.loads((world.copy_dir / workers.COPY_NAME).read_text())
    assert copied["decisions"][0]["key"] == key and copied["tasks"][0]["findings"][0]["decision"] == "declined"


def test_a_missing_login_fails_without_starting_the_worker(world):
    world.scenario({"auth": {"loggedIn": False}})
    service = world.service()
    state = finish(service, service.review({"head": head_of(world)}))
    assert state["status"] == "failed" and "claude auth login" in state["reason"] and world.seen() == []


def test_close_stops_a_running_worker(world):
    world.scenario({"sleep": 60})
    service = world.service()
    started = service.review({"head": head_of(world)})
    deadline = time.monotonic() + 10
    while not world.seen() and time.monotonic() < deadline:
        time.sleep(0.05)
    began = time.monotonic()
    service.close()
    state = service.wait({"handle": started["handle"], "seconds": 5})
    assert time.monotonic() - began < 10 and state["status"] in ("blocked", "failed")


# --- the Codex worker ------------------------------------------------------------------------------

def test_the_codex_schema_only_relaxes():
    original = RESULT_MODELS["review"].model_json_schema()
    derived = worker_launch.codex_schema(original)
    text = json.dumps(derived)
    assert '"minLength"' not in text and '"maxLength"' not in text and '"title"' not in text and '"const"' not in text
    assert derived["properties"]["protocol"] == {"enum": ["agents-core/subagent/1"], "type": "string"}
    assert worker_launch.relaxation_problems(original, derived) == []
    with_title = {"type": "object", "properties": {"title": {"type": "string", "title": "Title", "maxLength": 9}},
                  "required": ["title"], "additionalProperties": False}
    assert worker_launch.codex_schema(with_title)["properties"] == {"title": {"type": "string"}}
    narrowed = copy.deepcopy(derived)
    narrowed["$defs"]["Finding"]["properties"]["location"]["minLength"] = 5
    narrowed["$defs"]["ReviewPayload"]["properties"]["summary"]["pattern"] = "^ok$"
    narrowed["properties"]["status"]["enum"] = ["completed"]
    narrowed["$defs"]["Evidence"]["required"] = ["id"]
    del narrowed["$defs"]["Artifact"]["properties"]["purpose"]
    problems = worker_launch.relaxation_problems(original, narrowed)
    assert any("location.minLength: changes the original value" in item for item in problems)
    assert any("summary.pattern: is not in the original" in item for item in problems)
    assert any("status.enum: changes the original value" in item for item in problems)
    assert any("Evidence.required: changes the original value" in item for item in problems)
    assert any("Artifact.properties: names other entries" in item for item in problems)


def codex_world(world: World, scenario: dict):
    codex = fake_codex(world.base / "codex", {"seen": str(world.base / "codex-seen.jsonl"), **scenario})
    settings = config()
    settings["delegates"]["code-review-pass"] = {"client": "codex", "codex": {"model": "gpt-6.1-sol", "effort": "high"},
                                                 "effort_followup": "medium"}
    service = world.service(settings, codex=codex, codex_login=MODE, platform="darwin",
                            codex_execute=Emulator(codex), codex_resolver=lambda host: TEST_NET)
    return codex, service


def test_codex_falls_back_to_the_claude_worker_and_records_why(world):
    codex, service = codex_world(world, {})
    head = head_of(world)
    state = finish(service, service.review({"head": head}), seconds=120)
    assert state["status"] == "accepted" and state["client_applied"] == "claude"
    [record] = attempt_records(world, "review-1")
    assert record["client"]["client_requested"] == "codex" and record["client"]["client_applied"] == "claude"
    assert record["client"]["fallback_reason"] == "nested_sandbox_unavailable"
    assert efforts(world) == ["high"] and record["model"] == "opus"
    clients = records.read_record(RUN_ID, world.state)["clients"]
    assert clients[0]["step"] == "code-review-pass" and clients[0]["fallback_reason"] == "nested_sandbox_unavailable"
    assert KEY not in json.dumps(records.read_record(RUN_ID, world.state))


def test_codex_worker_argv_environment_and_raw_text(world, monkeypatch):
    codex, service = codex_world(world, {"nested": "ok", "worker": {"findings": FINDINGS[:1]}})
    real_popen = subprocess.Popen

    def strip_profile(argv, **kwargs):  # the fake codex cannot run under the real outer profile
        argv = list(argv)
        if argv[0] == cs.SANDBOX_EXEC:
            index = 3
            while argv[index] == "-D":
                index += 2
            argv = argv[index:]
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(codex_probes.subprocess, "Popen", strip_profile)
    head = head_of(world)
    state = finish(service, service.review({"head": head}), seconds=120)
    assert state["status"] == "accepted" and state["client_applied"] == "codex", state
    calls = [json.loads(line) for line in (world.base / "codex-seen.jsonl").read_text().splitlines()]
    [run] = [call for call in calls if call["argv"][:1] == ["exec"] and "--output-schema" in call["argv"]]
    argv = run["argv"]
    attempt = os.path.realpath(world.run_dir / "workers" / "review-1" / "attempt-1")
    schema_file = argv[argv.index("--output-schema") + 1]
    assert schema_file == f"{attempt}/codex-schema.json" and argv[argv.index("-o") + 1] == \
        f"{attempt}/codex-out/{cs.OUTPUT_NAME}"
    assert argv[argv.index("-C") + 1] == os.path.realpath(world.run_dir / "workers" / f"tree-{world.repo.base}")
    assert ["-s", "read-only"] == argv[argv.index("-s"):argv.index("-s") + 2] and argv[-1] == "-"
    assert "-c" in argv and "model_reasoning_effort=high" in argv and argv[argv.index("-m") + 1] == "gpt-6.1-sol"
    assert run["env"]["CODEX_API_KEY"] == KEY and set(run["env"]) >= set(cs.BASE_VARIABLES)
    assert not any(name in run["env"] for name in ("CLAUDE_CONFIG_DIR", "OPENAI_API_KEY"))
    [record] = attempt_records(world, "review-1")
    assert record["client"]["client_applied"] == "codex" and record["output"]["form"] == "text"
    assert record["output"]["sha256"] == workers.output_digest(record["output"]["raw"])
    assert KEY not in json.dumps(record) and world.seen() == []  # no Claude worker ran
    assert broker.validated_review_head(world.run_dir) == head


def test_a_strict_server_rejects_the_unrelaxed_schema(world, monkeypatch):
    """The fake codex refuses minLength and maxLength as OpenAI's strict mode does: passing the
    original schema would fail every turn, which is why the launcher derives the Codex schema."""
    codex, service = codex_world(world, {"nested": "ok"})
    monkeypatch.setattr(worker_launch, "codex_schema", lambda schema: copy.deepcopy(dict(schema)))
    real_popen = subprocess.Popen

    def strip_profile(argv, **kwargs):
        argv = list(argv)
        if argv[0] == cs.SANDBOX_EXEC:
            index = 3
            while argv[index] == "-D":
                index += 2
            argv = argv[index:]
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(codex_probes.subprocess, "Popen", strip_profile)
    state = finish(service, service.review({"head": head_of(world)}), seconds=180)
    assert state["status"] == "blocked"
    assert "codex ended without a result (exit code 1, no output file)" in state["reason"]


# --- settings --------------------------------------------------------------------------------------

def test_the_premortem_role_is_a_setting():
    assert autodev_config.DEFAULTS["delegates"]["change-premortem"]["role"] == "sysadmin"
    bad = copy.deepcopy(autodev_config.DEFAULTS)
    bad["delegates"]["change-premortem"]["role"] = "Not An Agent"
    with pytest.raises(autodev_config.ConfigError, match="role must be an agent name"):
        autodev_config.validate(bad)
    bad = copy.deepcopy(autodev_config.DEFAULTS)
    bad["delegates"]["code-review-pass"]["role"] = "sysadmin"
    with pytest.raises(autodev_config.ConfigError, match="unknown keys: role"):
        autodev_config.validate(bad)


def test_the_personal_library_is_empty_during_preparation(world, monkeypatch):
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", "/nonexistent/personal")
    seen = []

    def spy(args):
        seen.append(os.environ.get("AGENTS_USER_FLOWS_DIR"))
        return prepare_subagent(args)

    service = world.service(prepare=spy)
    assert finish(service, service.review({"head": head_of(world)}))["status"] == "accepted"
    assert seen == [str(service.library)] and os.environ["AGENTS_USER_FLOWS_DIR"] == "/nonexistent/personal"
    assert list(service.library.iterdir()) == []
