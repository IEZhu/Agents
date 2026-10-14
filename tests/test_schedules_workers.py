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
from src.schedules import (broker, cli, codex_probes, decoys, gitsafe, materials, records, repos, runner, sandbox,
                           socket_service, worker_launch, workers)
from src.schedules import codex_sandbox as cs
from src.schedules import config as autodev_config
from src.schedules.scan import KnownSecret
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
                prepare=None, importer=None, on_known=None, **options) -> WorkerService:
        binding = Binding(RUN_ID, ISSUE, self.repo.base, self.repo.trusted, self.state, log_copy=self.copy_dir,
                          criteria=criteria)
        options.setdefault("command_runner", keychain)
        launcher = worker_launch.Launcher(claude=self.claude, path=PATH, home=self.home, state_dir=self.state,
                                          run_id=RUN_ID, decoys=self.planted, **options)
        return WorkerService(binding, settings or config(), launcher, prepare=prepare or self.prepare,
                             importer=importer, on_known=on_known)

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
    [record] = attempt_records(world, "review-1")  # the output is withheld: only its digest stays
    assert "raw" not in record["output"] and "names a decoy" in record["output"]["withheld"]
    assert len(record["output"]["sha256"]) == 64


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
    # The open ledger entries with their decision state; the declined docstring medium left it.
    given = json.loads((second_materials / workers.OPEN_NAME).read_text())
    assert {item["key"]: item["decision"] for item in given} == {keys[0]: "fixed", keys[2]: None}
    assert "open-findings.json lists every earlier finding" in " ".join(
        item.description for item in world.packages[1].contract.acceptance_criteria)

    third_head = head_of(world)
    third = finish(service, service.review({"head": third_head}))
    assert third["status"] == "accepted" and third["restricted"] == "last"
    shown = {item["location"] for item in third["findings"]}
    assert shown == {"src/app.py:2", "src/app.py:3", "README.md:1"}  # high, security, regression
    assert third["dropped_out_of_scope"] == 1 and third["blocking"] == 3
    # Findings in the last review's scope end the stage blocked, and the push gate stays closed.
    assert third["stage_blocked"] is True and "the stage ends blocked" in third["reason"]
    review = json.loads((world.run_dir / broker.REVIEW_RECORD).read_text())
    assert review["head"] == third_head and review["blocked"] is True and review["restricted"] == "last"
    assert broker.validated_review_head(world.run_dir) is None
    assert efforts(world) == ["high", "medium", "medium"]
    tasks = [package.contract.task for package in world.packages]
    criteria = [" ".join(item.description for item in package.contract.acceptance_criteria)
                for package in world.packages]
    assert "This is the last review" in criteria[2] and "This is the last" not in criteria[0] + criteria[1]
    assert "decisions.json" in criteria[1] and tasks[0].startswith("Review the change")
    with pytest.raises(RequestRefused, match="the stage is blocked: the last review"):
        service.review({"head": head_of(world)})
    with pytest.raises(RequestRefused, match="the stage is blocked"):
        service.premortem({"head": third_head})


def test_a_follow_up_of_the_same_head_reviews_the_same_range_again(world):
    service = world.service()
    head = head_of(world)
    assert finish(service, service.review({"head": head}))["status"] == "accepted"
    again = finish(service, service.review({"head": head}))
    assert again["range"] == f"{world.repo.base}..{head}" and again["number"] == 2


def test_the_premortem_and_the_verification_review(world):
    # review-1: one medium finding; the pre-mortem and the verification: high, medium, security medium
    world.scenario({"outcomes": [{"findings": [FINDINGS[1]]}, {"findings": FINDINGS[:3]}, {"findings": FINDINGS[:3]}]})
    settings = config()
    settings["delegates"]["change-premortem"]["role"] = "security_expert"  # not the default sysadmin (Q2)
    service = world.service(settings)
    head = head_of(world)
    with pytest.raises(RequestRefused, match="follows an accepted review"):
        service.premortem({"head": head})
    review = finish(service, service.review({"head": head}))
    assert review["status"] == "accepted" and review["medium_or_higher"] == 1
    with pytest.raises(RequestRefused, match="needs a clean validated review .* is neither declined nor resolved"):
        service.premortem({"head": head})
    service.decide({"key": review["findings"][0]["key"], "decision": "declined", "reason": "Docstrings are optional."})
    assert broker.validated_review_head(world.run_dir) == head  # a declined medium opens the gate
    with pytest.raises(RequestRefused, match="follows an accepted pre-mortem"):
        service.review({"head": head, "verification": True})
    premortem = finish(service, service.premortem({"head": head}))
    assert premortem["status"] == "accepted" and premortem["kind"] == "premortem"
    assert world.packages[-1].contract.agent == "security_expert"
    assert world.packages[-1].contract.task.startswith("Pre-mortem of the change")
    reviews = json.loads((world.run_dir / "workers" / "premortem-1" / "materials" / "reviews.json").read_text())
    assert [item["handle"] for item in reviews] == ["review-1"]
    with pytest.raises(RequestRefused, match="the pre-mortem already ran"):  # MAX_PREMORTEMS
        service.premortem({"head": head})
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
    assert {item["location"] for item in verify["findings"]} == {"src/app.py:2", "src/app.py:3"}  # high, security
    assert verify["dropped_out_of_scope"] == 1 and verify["blocking"] == 2  # the docstring medium left out
    assert efforts(world) == ["high", "high", "medium"]  # review, pre-mortem (claude/high), verification
    # Its findings end the stage blocked: the record says so and the push gate stays closed.
    assert verify["stage_blocked"] is True and "the verification review" in verify["reason"]
    record = json.loads((world.run_dir / broker.REVIEW_RECORD).read_text())
    assert (record["head"], record["kind"], record["restricted"], record["blocked"]) == (fixed, "verify",
                                                                                         "verification", True)
    assert broker.validated_review_head(world.run_dir) is None
    with pytest.raises(RequestRefused, match="the stage is blocked"):
        service.review({"head": head_of(world), "verification": True})


def test_records_in_the_brokers_format_and_the_run_record(world):
    head = head_of(world)
    world.scenario({"outcome": {"findings": FINDINGS[:1]}})
    service = world.service()
    finish(service, service.review({"head": head}))
    assert broker.validated_review_head(world.run_dir) is None  # its high finding stays open
    review = json.loads((world.run_dir / broker.REVIEW_RECORD).read_text())
    assert review["range"] == f"{world.repo.base}..{head}" and review["record"] == "workers/review-1/attempt-1.json"
    assert (review["head"], review["handle"], review["kind"], review["number"], review["restricted"]) == \
        (head, "review-1", "review", 1, None)
    assert review["clean"] is False and review["blocked"] is False
    assert review["findings"] == [{"key": workers.finding_key("src/app.py:2", "run() returns a constant."),
                                   "level": "high", "security": False}]
    ledger = json.loads((world.run_dir / workers.LEDGER_NAME).read_text())
    assert (ledger["head"], ledger["handle"]) == (head, "review-1")
    [entry] = ledger["findings"]
    assert (entry["key"], entry["level"], entry["status"], entry["handle"]) == (review["findings"][0]["key"], "high",
                                                                                "open", "review-1")
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
    assert set(state["findings"][0]) == {"key", "severity", "location", "explanation", "recommendation", "security",
                                         "decision"}
    assert state["findings"][0]["security"] is False
    assert "evidence" not in state and "checks" not in state


def test_the_session_sees_the_runners_security_mark(world):
    """The rules on declining, known risks and PR text depend on the runner's mark, so the view carries it."""
    head = head_of(world)
    findings = [{"severity": "medium", "location": "src/app.py:2", "explanation": "The token is logged in clear text, "
                 "so a credential leaks into the run log.", "recommendation": "Mask it."},
                {"severity": "medium", "location": "src/schedules/sandbox.py:3", "explanation": "The author line is "
                 "outdated.", "recommendation": "Update it."}]
    world.scenario({"outcome": {"findings": findings}})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    marks = {item["location"]: item["security"] for item in state["findings"]}
    assert marks == {"src/app.py:2": True, "src/schedules/sandbox.py:3": False}
    assert {item["security"] for item in state["open_findings"]} == {True, False}
    copied = json.loads((world.copy_dir / workers.COPY_NAME).read_text())
    assert {item["location"]: item["security"] for item in copied["tasks"][0]["findings"]} == marks


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
    assert too_long["ok"] is False and "between 0 and 540" in too_long["error"]  # 0 polls
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


def codex_world(world: World, scenario: dict, **service_options):
    codex = fake_codex(world.base / "codex", {"seen": str(world.base / "codex-seen.jsonl"), **scenario})
    settings = config()
    settings["delegates"]["code-review-pass"] = {"client": "codex", "codex": {"model": "gpt-6.1-sol", "effort": "high"},
                                                 "effort_followup": "medium"}
    service = world.service(settings, codex=codex, codex_login=MODE, platform="darwin",
                            codex_execute=Emulator(codex), codex_resolver=lambda host: TEST_NET, **service_options)
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
    schema_file = Path(argv[argv.index("--output-schema") + 1])
    assert argv[argv.index("-o") + 1] == f"{attempt}/codex-out/{cs.OUTPUT_NAME}"
    # The tree, the materials and the schema are copies in a temporary root of their own, outside
    # the state directory (CodexPaths refuses a read root inside it), removed after the attempt.
    read_root = schema_file.parent
    assert sandbox.TMP_NAME.fullmatch(read_root.name) and schema_file.name == "codex-schema.json"
    assert argv[argv.index("-C") + 1] == str(read_root / "tree")
    assert not str(read_root).startswith(os.path.realpath(world.state) + "/") and not read_root.exists()
    assert ["-s", "read-only"] == argv[argv.index("-s"):argv.index("-s") + 2] and argv[-1] == "-"
    assert "-c" in argv and "model_reasoning_effort=high" in argv and argv[argv.index("-m") + 1] == "gpt-6.1-sol"
    assert run["env"]["CODEX_API_KEY"] == KEY and set(run["env"]) >= set(cs.BASE_VARIABLES)
    assert not any(name in run["env"] for name in ("CLAUDE_CONFIG_DIR", "OPENAI_API_KEY"))
    [record] = attempt_records(world, "review-1")
    assert record["client"]["client_applied"] == "codex" and record["output"]["form"] == "text"
    assert record["output"]["sha256"] == workers.output_digest(record["output"]["raw"])
    assert KEY not in json.dumps(record) and world.seen() == []  # no Claude worker ran
    review = json.loads((world.run_dir / broker.REVIEW_RECORD).read_text())
    assert review["head"] == head and review["findings"][0]["level"] == "high"  # validated, not clean


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


# --- review round 1 of #345 ------------------------------------------------------------------------

HANDOVER_BRANCH = BRANCH.removeprefix("refs/heads/")


def handover(world: World, exchange: Path, sha: str, branch: str = HANDOVER_BRANCH) -> Path:
    """The session's side: ``git bundle create <exchange>/handover.bundle <branch> ^<base>``."""
    git(world.repo.work, "branch", "-f", branch, sha)
    path = exchange / workers.HANDOVER_NAME
    if path.exists():
        path.unlink()
    git(world.repo.work, "bundle", "create", "--quiet", str(path), branch, f"^{world.repo.base}")
    return path


def handover_service(world: World, tmp_path: Path, **options):
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    trusted = repos.TrustedRepository(world.repo.trusted, str(world.repo.trusted))
    importer = workers.Handover(trusted, exchange, HANDOVER_BRANCH)
    return exchange, world.service(importer=importer, **options)


def test_a_review_imports_the_handed_over_bundle_first(world, tmp_path):
    exchange, service = handover_service(world, tmp_path)
    sha = world.repo.commit({"src/app.py": "def run():\n    return 7\n"}, "handed over")  # never promoted by hand
    with pytest.raises(RequestRefused, match="not the head of an imported"):
        service.review({"head": sha})  # no bundle yet: nothing is imported
    handover(world, exchange, sha)
    state = finish(service, service.review({"head": sha}))
    assert state["status"] == "accepted", state
    assert materials.ReviewRepository(world.repo.trusted).branch_tips(ISSUE) == {HANDOVER_BRANCH: sha}
    imports = json.loads((world.run_dir / workers.STATE_NAME).read_text())["imports"]
    assert [(item["head"], item["added"]) for item in imports] == [(sha, 1)]
    # The next round: a new bundle moves the branch before the follow-up review and the pre-mortem.
    second = world.repo.commit({"src/app.py": "def run():\n    return 8\n"}, "second round")
    handover(world, exchange, second)
    assert finish(service, service.review({"head": second}))["status"] == "accepted"
    assert finish(service, service.premortem({"head": second}))["status"] == "accepted"  # same bundle, no change


def test_a_refused_bundle_starts_no_review(world, tmp_path):
    exchange, service = handover_service(world, tmp_path)
    sha = world.repo.commit({"src/app.py": "x = 1\n"}, "other branch")
    handover(world, exchange, sha, branch="claude/autodev-345-other")
    with pytest.raises(RequestRefused, match="the handed-over bundle was refused: the bundle names .*autodev-345-other"):
        service.review({"head": sha})
    (exchange / workers.HANDOVER_NAME).write_bytes(b"not a bundle\n")
    with pytest.raises(RequestRefused, match="the handed-over bundle was refused: not a readable git bundle"):
        service.premortem({"head": sha})
    assert service.tasks == {} and world.seen() == []
    assert materials.ReviewRepository(world.repo.trusted).branch_tips(ISSUE) == {}


def test_a_head_that_is_not_the_bundles_is_refused(world, tmp_path):
    exchange, service = handover_service(world, tmp_path)
    first = world.repo.commit({"src/app.py": "x = 1\n"}, "first")
    later = world.repo.commit({"src/app.py": "x = 2\n"}, "later, not handed over")
    handover(world, exchange, first)
    with pytest.raises(RequestRefused, match=f"the handed-over bundle's head is {first}, not {later}"):
        service.review({"head": later})
    assert service.tasks == {}
    handover(world, exchange, later)
    with pytest.raises(RequestRefused, match=f"head is {later}, not {first}"):  # an older head of the branch
        service.review({"head": first})
    with pytest.raises(ValueError, match="not a branch of issue #345"):
        world.service(importer=workers.Handover(repos.TrustedRepository(world.repo.trusted, str(world.repo.trusted)),
                                                exchange, "claude/autodev-346-x"))


def _abort(*args, **kwargs):
    return codex_probes.Decision(codex_probes.CODEX, None, codex_probes.DENY_PROBE_SUCCEEDED, "a deny probe read x",
                                 known=(KnownSecret(codex_probes.API_KEY_LABEL, KEY),), abort=True)


def _fall_back(*args, **kwargs):
    return codex_probes.Decision(codex_probes.CODEX, codex_probes.CLAUDE, "probe_failed", "a probe failed",
                                 known=(KnownSecret(codex_probes.API_KEY_LABEL, KEY),))


def test_a_codex_abort_fails_the_step_and_runs_no_client(world):
    started = []
    codex, service = codex_world(world, {})
    service.launcher.codex_decide = _abort
    service.launcher.codex_start = lambda *args, **kwargs: started.append(args)
    state = finish(service, service.review({"head": head_of(world)}), seconds=120)
    assert state["status"] == "failed" and "codex aborted the step (deny_probe_succeeded)" in state["reason"]
    assert state["attempts"] == 1 and world.seen() == [] and started == []  # neither client ran
    assert any(secret.value == KEY for secret in service.known)  # decision.known reached the service
    clients = records.read_record(RUN_ID, world.state)["clients"]
    assert clients[0]["aborted"] is True and clients[0]["abort_reason"] == "deny_probe_succeeded"
    assert KEY not in (world.run_dir / workers.STATE_NAME).read_text()
    with pytest.raises(RequestRefused, match="the stage is blocked"):
        service.review({"head": head_of(world)})


def test_a_codex_fallback_runs_the_claude_worker(world):
    codex, service = codex_world(world, {})
    service.launcher.codex_decide = _fall_back
    state = finish(service, service.review({"head": head_of(world)}), seconds=120)
    assert state["status"] == "accepted" and state["client_applied"] == "claude"
    [record] = attempt_records(world, "review-1")
    assert record["client"]["fallback_reason"] == "probe_failed" and len(world.seen()) == 1
    assert any(secret.value == KEY for secret in service.known)


def test_a_failed_run_with_a_decoy_denial_trips(world):
    head = head_of(world)
    decoy = next(item for item in world.planted if item.path is not None)
    world.scenario({"calls": [{"tool": "Read", "input": {"file_path": str(decoy.path)}}],
                    "outcome": {"subtype": "error_during_execution", "is_error": True, "exit": 1, "no_output": True}})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "blocked" and state["attempts"] == 1, state
    assert "tripwire" in state["reason"] and "names decoy" in state["reason"]
    assert (world.state / "schedules" / decoys.TRIPWIRE_NAME).exists()


def test_a_failed_run_whose_text_carries_a_decoy_marker_trips(world):
    head = head_of(world)
    decoy = next(item for item in world.planted if item.path is not None)
    world.scenario({"calls": [{"tool": "Read", "input": {"file_path": str(decoy.path)}}],
                    "allow_paths": [decoy.path.name],
                    "outcome": {"echo_reads": True, "exit": 1, "no_output": True}})
    service = world.service()
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "blocked" and state["attempts"] == 1 and "carries decoy" in state["reason"], state
    assert (world.state / "schedules" / decoys.TRIPWIRE_NAME).exists()
    assert decoy.marker not in json.dumps(attempt_records(world, "review-1"))


@pytest.mark.parametrize("finding, decision, opens", [
    (None, None, True),
    ({"severity": "critical", "location": "src/app.py:2", "explanation": "run() crashes."}, "declined", False),
    ({"severity": "medium", "location": "src/app.py:1", "explanation": "No docstring on run()."}, "declined", True),
    ({"severity": "medium", "location": "src/app.py:1", "explanation": "No docstring on run()."}, None, False),
    ({"severity": "high", "location": "src/app.py:2", "explanation": "run() returns a constant."}, "declined", False),
    ({"severity": "medium", "location": "src/app.py:3", "explanation": "The token is logged."}, "declined", False),
])
def test_the_push_gate_follows_the_review_and_the_decisions(world, finding, decision, opens):
    world.scenario({"outcome": {"findings": [finding] if finding else []}})
    service = world.service()
    head = head_of(world)
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "accepted"
    if decision:
        service.decide({"key": state["findings"][0]["key"], "decision": decision, "reason": "Intended behaviour."})
    assert (broker.validated_review_head(world.run_dir) == head) is opens
    record = json.loads((world.run_dir / broker.REVIEW_RECORD).read_text())
    assert record["clean"] is (finding is None) and record["blocked"] is False


def test_a_blocked_last_review_keeps_the_gate_closed(world):
    high = {"severity": "high", "location": "src/app.py:2", "explanation": "run() returns a constant."}
    world.scenario({"outcomes": [{}, {"findings": [high]}]})
    settings = config(limits={**autodev_config.DEFAULTS["limits"], "max_reviews": 2})
    service = world.service(settings)
    first = head_of(world)
    assert finish(service, service.review({"head": first}))["status"] == "accepted"
    assert broker.validated_review_head(world.run_dir) == first  # clean
    second = head_of(world)
    last = finish(service, service.review({"head": second}))
    assert last["restricted"] == "last" and last["stage_blocked"] is True
    service.decide({"key": last["findings"][0]["key"], "decision": "declined", "reason": "No."})
    assert broker.validated_review_head(world.run_dir) is None
    assert json.loads((world.run_dir / broker.REVIEW_RECORD).read_text())["blocked"] is True
    with pytest.raises(RequestRefused, match="the stage is blocked"):
        service.premortem({"head": second})


def test_a_follow_up_review_sees_files_earlier_rounds_changed(world):
    service = world.service()
    first = head_of(world, {"src/new_module.py": "VALUE = 1\n"}, "round 1 adds a module")
    assert finish(service, service.review({"head": first}))["status"] == "accepted"
    second = head_of(world, {"src/app.py": "from src.new_module import VALUE\n"}, "round 2 uses it")
    state = finish(service, service.review({"head": second}))
    assert state["status"] == "accepted" and state["range"] == f"{first}..{second}"
    second_materials = world.run_dir / "workers" / "review-2" / "materials"
    assert (second_materials / "head" / "src" / "new_module.py").read_text() == "VALUE = 1\n"  # from round 1
    assert (second_materials / "head" / "src" / "app.py").read_text() == "from src.new_module import VALUE\n"
    diff, commits = (second_materials / "diff.patch").read_text(), (second_materials / "commits.txt").read_text()
    assert "+from src.new_module import VALUE" in diff and "VALUE = 1" not in diff  # the follow-up range only
    assert "round 2 uses it" in commits and "round 1 adds a module" not in commits
    assert json.loads((second_materials / "range.json").read_text())["pinned"] == world.repo.base


def _probe_context(world: World) -> runner.Context:
    return runner.Context(installation=world.base / "installation", directory=world.state, claude=world.claude,
                          home=world.home, command_runner=keychain, agents_dir=world.base / "LaunchAgents")


PROBE_ANSWER = {"stdin_calls": True, "outcome": {"probe_answer": True}}


def test_the_worker_probe_refuses_without_confirmation(world, capsys):
    world.scenario(PROBE_ANSWER)
    assert cli.main(["worker-probe"], ctx=_probe_context(world)) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["error"] == "confirmation_required" and runner.PROBE_CONFIRMATION in output["message"]
    assert world.seen() == []


def test_the_worker_probe_reads_inside_and_is_denied_outside(world, capsys):
    world.scenario(PROBE_ANSWER)
    code = cli.main(["worker-probe", runner.PROBE_CONFIRMATION], ctx=_probe_context(world))
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["status"] == "completed", output
    check = output["worker_check"]
    assert check["ok"] and check["structured_output"] and (check["denials"], check["denied_outside"]) == (1, 1)
    assert check["read_tree"] is True and check["read_materials"] is True
    [call] = world.seen()
    argv = call["argv"]
    assert argv[:3] == ["-p", "--restricted", "--safe-mode"] and "--json-schema" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == runner.WORKER_PROBE_SCHEMA
    assert call["stdin"].count('{"tool": "Read"') == 3 and output["trigger"] == {"kind": "worker-probe"}
    # The layout of a real review: the tree and the materials under <run_dir>/workers/ of the state.
    work = records.run_dir(output["run_id"], world.state) / workers.WORK_DIR
    assert call["cwd"] == str(work / f"tree-{runner.WORKER_PROBE_STEP}")
    assert argv[argv.index("--add-dir") + 1] == str(work / runner.WORKER_PROBE_STEP / "materials")
    assert not (work / f"tree-{runner.WORKER_PROBE_STEP}").exists()  # removed after the check
    assert not (work / runner.WORKER_PROBE_STEP / "materials").exists()
    assert not (work / runner.WORKER_PROBE_STEP / "outside.txt").exists()


def test_a_worker_probe_whose_setup_fails_is_infra_and_leaves_nothing(world, capsys, monkeypatch):
    world.scenario(PROBE_ANSWER)
    made = []

    def refuse_materials(path):
        if Path(path).name == "materials":
            raise ValueError("not a private directory")
        made.append(Path(path))
        return original(path)

    original = runner.private_dir
    monkeypatch.setattr(runner, "private_dir", refuse_materials)
    assert cli.main(["worker-probe", runner.PROBE_CONFIRMATION], ctx=_probe_context(world)) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "failed: infra" and "worker check: ValueError" in output["reason"], output
    assert [path.name for path in made][-1] == f"tree-{runner.WORKER_PROBE_STEP}"
    assert not made[-1].exists()  # the tree made before the failure is removed
    assert world.seen() == []

@pytest.mark.parametrize("scenario, problem", [
    ({"allow_paths": ["outside.txt"]}, "no Read outside the tree was denied"),
    ({"allow_paths": ["outside.txt"], "outcome": {"echo_reads": True, "probe_answer": True}},
     "the file outside the tree reached the output"),
    ({"calls": [{"tool": "Read", "input": {"file_path": "/etc/hosts"}}]}, "1 other tool call(s) were denied"),
    ({"outcome": {"no_output": True, "subtype": "error_max_structured_output_retries", "is_error": True}},
     "ended without a result"),
    ({"deny_paths": ["tree-worker-probe"]}, "the Read inside the tree did not return its file"),
    ({"deny_paths": ["worker-probe/materials"]}, "the Read inside the materials did not return its file"),
    ({"error_paths": ["tree-worker-probe"]}, "the Read inside the tree did not return its file"),
    ({"outcome": {"structured": {"verdict": "ok", "tree": "a guess", "materials": "a guess"}}},
     "the Read inside the tree did not return its file"),
    ({"outcome": {"structured": {"verdict": "ok"}}}, "the structured_output is not the probe's result"),
])
def test_the_worker_probe_fails_when_the_worker_misbehaves(world, capsys, scenario, problem):
    world.scenario({**PROBE_ANSWER, **scenario})
    assert cli.main(["worker-probe", runner.PROBE_CONFIRMATION], ctx=_probe_context(world)) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "refused" and problem in output["reason"], output


def test_an_unknown_premortem_role_fails_the_task(world):
    settings = config()
    settings["delegates"]["change-premortem"]["role"] = "no_such_agent"
    service = world.service(settings)
    head = head_of(world)
    assert finish(service, service.review({"head": head}))["status"] == "accepted"
    state = finish(service, service.premortem({"head": head}))
    assert state["status"] == "failed" and "could not be prepared" in state["reason"], state


def test_the_premortem_needs_a_validated_review_of_its_head(world):
    service = world.service()
    first = head_of(world)
    assert finish(service, service.review({"head": first}))["status"] == "accepted"
    second = head_of(world)
    with pytest.raises(RequestRefused, match=f"needs a clean validated review of {second}"):
        service.premortem({"head": second})


def test_a_single_review_is_unrestricted_and_the_cap_holds(world):
    settings = config(limits={**autodev_config.DEFAULTS["limits"], "max_reviews": 1})
    world.scenario({"outcome": {"findings": FINDINGS[1:2]}})
    service = world.service(settings)
    only = finish(service, service.review({"head": head_of(world)}))
    assert only["restricted"] is None and "stage_blocked" not in only
    with pytest.raises(RequestRefused, match=r"max_reviews \(1\) is reached"):
        service.review({"head": head_of(world)})


def test_the_decision_history_is_bounded_and_the_log_clipped(world):
    long = "y" * 3000
    world.scenario({"outcome": {"findings": [{"severity": "medium", "location": "a.py:1", "explanation": long}]}})
    service = world.service()
    state = finish(service, service.review({"head": head_of(world)}))
    key = state["findings"][0]["key"]
    for number in range(12):
        service.decide({"key": key, "decision": ("fixed", "declined")[number % 2], "reason": f"reason {number}"})
    [entry] = json.loads((world.run_dir / workers.DECISIONS_NAME).read_text())["decisions"]
    assert len(entry["history"]) == workers.MAX_HISTORY and entry["reason"] == "reason 11"
    assert len(entry["explanation"]) == workers.LIMITS["explanation"]
    copied = json.loads((world.copy_dir / workers.COPY_NAME).read_text())
    assert all(len(item["explanation"]) <= workers.LIMITS["explanation"] for item in copied["decisions"])
    assert long not in (world.copy_dir / workers.COPY_NAME).read_text()


def test_evidence_naming_another_head_is_refused_beside_the_range(world):
    service = world.service()
    inner = world.repo.commit({"src/app.py": "x = 1\n"}, "inner commit")
    head = head_of(world)
    base = world.repo.base
    other = "f" * 40
    world.scenario({"outcome": {"override": {"evidence": [
        {"id": "ev-other", "reference": f"{base}..{other}", "description": "another range"},
        {"id": "ev-range", "reference": f"{base}..{head}", "description": "the reviewed range"}]}}})
    state = finish(service, service.review({"head": head}))
    assert state["status"] == "blocked" and f"the evidence names {other}" in state["reason"], state
    # A commit of the reviewed change may be named.
    world.scenario({"outcome": {"override": {"evidence": [
        {"id": "ev-range", "reference": f"{base}..{head}", "description": f"commit {inner} reviewed too"}]}}})
    again = world.service()
    assert finish(again, again.review({"head": head}))["status"] == "accepted"


def test_the_tree_leaves_out_project_configuration(world, tmp_path):
    sha = world.repo.promote(world.repo.commit({".claude/settings.json": '{"hooks": {}}\n', ".mcp.json": "{}\n",
                                                "sub/.Claude/agents/x.md": "x\n", ".codex/config.toml": "x\n",
                                                "src/kept.py": "kept = 1\n"}, "project config"))
    exported = materials.export_tree(materials.ReviewRepository(world.repo.trusted), sha, tmp_path / "t")
    assert sorted(exported["left_out"]) == [".claude/settings.json", ".codex/config.toml", ".mcp.json",
                                            "sub/.Claude/agents/x.md"]
    assert (tmp_path / "t" / "src" / "kept.py").exists() and not (tmp_path / "t" / ".claude").exists()
    assert not (tmp_path / "t" / ".mcp.json").exists() and not (tmp_path / "t" / "sub" / ".Claude").exists()


def test_a_tree_with_project_settings_never_starts_the_worker(world):
    service = world.service()
    tree = service._tree()
    (tree / ".claude").mkdir()
    (tree / ".claude" / "settings.json").write_text("{}\n")
    state = finish(service, service.review({"head": head_of(world)}))
    assert state["status"] == "failed" and "project settings" in state["reason"] and world.seen() == []


def test_a_new_service_clears_records_it_did_not_write(world):
    first = world.service()
    head = head_of(world)
    world.scenario({"outcome": {"findings": FINDINGS[1:2]}})
    state = finish(first, first.review({"head": head}))
    first.decide({"key": state["findings"][0]["key"], "decision": "declined", "reason": "Optional."})
    assert broker.validated_review_head(world.run_dir) == head
    (world.run_dir / broker.PREMORTEM_RECORD).write_text(json.dumps({"validated": True, "findings": []}))
    world.service()  # a second service of the same run: a restart
    assert broker.validated_review_head(world.run_dir) is None
    assert not (world.run_dir / broker.REVIEW_RECORD).exists() and not (world.run_dir / broker.PREMORTEM_RECORD).exists()
    assert not (world.run_dir / workers.LEDGER_NAME).exists()
    assert not (world.run_dir / workers.DECISIONS_NAME).exists()
    assert len(list(world.run_dir.glob("worker-decisions-before-*.json"))) == 1  # set aside, not lost


def test_read_bounded_refuses_a_fifo_and_a_hard_link(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    began = time.monotonic()
    assert worker_launch.read_bounded(fifo, 10) is None and time.monotonic() - began < 5  # no block
    plain = tmp_path / "plain"
    plain.write_bytes(b"data")
    assert worker_launch.read_bounded(plain, 10) == b"data"
    os.link(plain, tmp_path / "second")
    assert worker_launch.read_bounded(plain, 10) is None  # two links: maybe a file the worker may not read
    large = tmp_path / "large"
    large.write_bytes(b"too large")
    with pytest.raises(worker_launch.OutputTooLarge):
        worker_launch.read_bounded(large, 2)


# --- the findings ledger and the gate's security signal (review round 2) ----------------------------

CRITICAL = {"severity": "critical", "location": "src/app.py:2", "explanation": "run() deletes data."}


def ledger_entry(world: World, key: str) -> dict:
    entries = json.loads((world.run_dir / workers.LEDGER_NAME).read_text())["findings"]
    return next(item for item in entries if item["key"] == key)


def given_to(world: World, handle: str) -> list[dict]:
    return json.loads((world.run_dir / "workers" / handle / "materials" / workers.OPEN_NAME).read_text())


def test_an_undecided_finding_keeps_the_gate_closed_after_a_clean_follow_up(world):
    # Round 2's reproduction: a critical in review 1, no decision, an unrelated commit, a clean review 2.
    world.scenario({"outcomes": [{"findings": [CRITICAL]}, {}]})
    service = world.service()
    one = finish(service, service.review({"head": head_of(world)}))
    key = one["findings"][0]["key"]
    assert broker.validated_review_head(world.run_dir) is None
    second = head_of(world, {"docs/x.md": "x\n"}, "unrelated")
    two = finish(service, service.review({"head": second}))
    assert two["status"] == "accepted" and two["findings"] == []
    # Review 2 was given the finding, undecided; its silence does not resolve an undecided finding.
    assert [(item["key"], item["severity"], item["decision"]) for item in given_to(world, "review-2")] == \
        [(key, "critical", None)]
    head, why = broker.review_gate(world.run_dir)
    assert head is None and f"finding {key} (critical) of review-1 stays open" in why
    assert ledger_entry(world, key)["status"] == "open"
    assert two["open_findings"] == [{"key": key, "severity": "critical", "security": False, "reported_by": "review-1",
                                     "decision": None}]
    with pytest.raises(RequestRefused, match="needs a clean validated review"):
        service.premortem({"head": second})
    copied = json.loads((world.copy_dir / workers.COPY_NAME).read_text())
    assert [item["key"] for item in copied["open_findings"]] == [key]


def test_a_fixed_finding_leaves_the_ledger_only_when_the_next_review_does_not_repeat_it(world):
    world.scenario({"outcomes": [{"findings": [FINDINGS[0], CRITICAL]}, {"findings": [CRITICAL]}, {}]})
    service = world.service()
    one = finish(service, service.review({"head": head_of(world)}))
    high, critical = (item["key"] for item in one["findings"])
    for key in (high, critical):
        service.decide({"key": key, "decision": "fixed"})
    assert broker.validated_review_head(world.run_dir) is None  # a fix needs a later review
    two = finish(service, service.review({"head": head_of(world)}))
    assert {item["key"]: item["decision"] for item in given_to(world, "review-2")} == {high: "fixed", critical: "fixed"}
    # The high one is not reported again: resolved. The critical one is: open under review 2, undecided there.
    assert (ledger_entry(world, high)["status"], ledger_entry(world, high)["resolved_by"]) == ("resolved", "review-2")
    entry = ledger_entry(world, critical)
    assert (entry["status"], entry["handle"], entry["reported_by"]) == ("open", "review-2", ["review-1", "review-2"])
    assert two["open_findings"] == [{"key": critical, "severity": "critical", "security": False, "reported_by": "review-2",
                                     "decision": None}]
    assert broker.validated_review_head(world.run_dir) is None
    service.decide({"key": critical, "decision": "fixed"})
    assert service.decisions[critical]["handle"] == "review-2"
    third = head_of(world)
    three = finish(service, service.review({"head": third}))
    assert three["restricted"] == "last" and "stage_blocked" not in three
    assert ledger_entry(world, critical)["status"] == "resolved"
    assert broker.review_gate(world.run_dir) == (third, "clean")


def test_a_fixed_finding_stays_open_when_the_next_review_sees_the_same_head(world):
    # The verification review's reproduction: "fixed", no commit, a silent review of the same head.
    world.scenario({"outcomes": [{"findings": [CRITICAL]}, {}, {}]})
    service = world.service()
    head = head_of(world)
    one = finish(service, service.review({"head": head}))
    key = one["findings"][0]["key"]
    service.decide({"key": key, "decision": "fixed"})
    two = finish(service, service.review({"head": head}))
    assert two["range"] == one["range"] and two["findings"] == []
    entry = ledger_entry(world, key)
    assert (entry["status"], entry["handle"], entry["unmoved"]) == ("open", "review-1", "review-2")
    assert broker.validated_review_head(world.run_dir) is None
    assert two["open_findings"] == [{"key": key, "severity": "critical", "security": False, "reported_by": "review-1",
                                     "decision": "fixed", "note": "marked fixed, but review-2 reviewed the head that "
                                                                  "review-1 reported it on: commit the fix first"}]
    ledger = json.loads((world.run_dir / workers.LEDGER_NAME).read_text())
    assert ledger["reviews"][-1]["unmoved"] == [key] and ledger["reviews"][-1]["resolved"] == []
    # A commit and a review of it that does not report the finding again resolve it.
    third = head_of(world)
    finish(service, service.review({"head": third}))
    assert given_to(world, "review-3")[0]["note"].startswith("marked fixed")
    assert ledger_entry(world, key)["status"] == "resolved"
    assert broker.review_gate(world.run_dir) == (third, "clean")


def test_a_key_repeated_in_one_result_keeps_its_highest_severity(world):
    low = {**CRITICAL, "severity": "low"}
    world.scenario({"outcomes": [{"findings": [low, CRITICAL]}]})
    service = world.service()
    one = finish(service, service.review({"head": head_of(world)}))
    assert [item["severity"] for item in one["findings"]] == ["critical"]
    assert ledger_entry(world, one["findings"][0]["key"])["level"] == "critical"
    assert broker.validated_review_head(world.run_dir) is None


def test_a_repeated_key_counts_in_the_scope_of_a_restricted_review_at_its_highest_severity(world):
    # The last review drops a plain low finding; the same key repeated as critical is in its scope.
    low = {**CRITICAL, "severity": "low"}
    world.scenario({"outcomes": [{}, {"findings": [low, CRITICAL]}]})
    service = world.service(config(limits={**autodev_config.DEFAULTS["limits"], "max_reviews": 2}))
    finish(service, service.review({"head": head_of(world)}))
    two = finish(service, service.review({"head": head_of(world)}))
    assert two["restricted"] == "last" and [item["severity"] for item in two["findings"]] == ["critical"]
    assert service.tasks["review-2"].dropped == 0
    assert broker.validated_review_head(world.run_dir) is None


def test_a_blocked_later_review_closes_the_gate_over_an_earlier_clean_one(world):
    world.scenario({"outcomes": [{}, {"status": "input_required"}]})
    service = world.service()
    first = head_of(world)
    finish(service, service.review({"head": first}))
    assert broker.validated_review_head(world.run_dir) == first
    state = finish(service, service.review({"head": first}))
    assert state["status"] == "blocked"
    head, why = broker.review_gate(world.run_dir)
    assert head is None and why == "the stage ended blocked: review-2 ended blocked"
    record = json.loads((world.run_dir / broker.REVIEW_RECORD).read_text())
    assert (record["validated"], record["blocked"], record["handle"]) == (False, True, "review-2")


def test_a_declined_medium_leaves_the_ledger_and_a_declined_high_waits_for_the_next_review(world):
    world.scenario({"outcomes": [{"findings": FINDINGS[:2]}, {}]})
    service = world.service()
    one = finish(service, service.review({"head": head_of(world)}))
    high, medium = (item["key"] for item in one["findings"])
    service.decide({"key": medium, "decision": "declined", "reason": "Docstrings are optional here."})
    service.decide({"key": high, "decision": "declined", "reason": "The constant is intended."})
    head, why = broker.review_gate(world.run_dir)
    assert head is None and f"finding {high} (high) of review-1 stays open" in why  # a declined high stays
    second = head_of(world)
    finish(service, service.review({"head": second}))
    # The declined medium left the ledger: review 2 gets only the declined high, with its reason.
    assert [(item["key"], item["decision"], item["reason"]) for item in given_to(world, "review-2")] == \
        [(high, "declined", "The constant is intended.")]
    assert ledger_entry(world, high)["status"] == "resolved"  # review 2 accepted the reason
    assert ledger_entry(world, medium)["status"] == "open"  # declined against review 1
    assert broker.review_gate(world.run_dir) == (second, "every remaining medium finding was declined")


def test_a_last_review_that_repeats_an_open_medium_keeps_it_open_without_blocking(world):
    settings = config(limits={**autodev_config.DEFAULTS["limits"], "max_reviews": 2})
    world.scenario({"outcome": {"findings": FINDINGS[1:2]}})
    service = world.service(settings)
    one = finish(service, service.review({"head": head_of(world)}))
    key = one["findings"][0]["key"]
    service.decide({"key": key, "decision": "fixed"})
    second = head_of(world)
    last = finish(service, service.review({"head": second}))
    # Out of the last review's scope, so it neither shows nor blocks; but it was given and is still there.
    assert last["restricted"] == "last" and last["dropped_out_of_scope"] == 1 and "stage_blocked" not in last
    assert ledger_entry(world, key)["handle"] == "review-2" and broker.validated_review_head(world.run_dir) is None
    service.decide({"key": key, "decision": "declined", "reason": "Docstrings are optional here."})
    assert service.decisions[key]["handle"] == "review-2"
    assert broker.validated_review_head(world.run_dir) == second


@pytest.mark.parametrize("location, explanation, expected", [
    ("src/app.py:1", "The author line is outdated.", False),
    ("src/app.py:1", "The tokenizer drops the last word.", False),
    ("src/app.py:1", "Memory leak in the cache loop.", False),
    ("src/schedules/sandbox.py:10", "The default timeout is wrong.", False),
    ("src/app.py:1", "Dependency injection makes the test brittle.", False),
    ("src/app.py:1", "The deploy credential is written to the log.", True),
    ("src/app.py:1", "Secret exfiltration through the proxy port.", True),
    ("src/app.py:1", "Privilege escalation through the helper.", True),
    ("src/app.py:1", "SQL injection in the query builder.", True),
    ("src/app.py:1", "The token is logged in plain text.", True),
    ("src/app.py:1", "The token leaks into the run log.", True),
    ("src/app.py:1", "The token is written to the log.", True),
    ("src/app.py:1", "The token is stored in plain text.", True),
    ("src/app.py:1", "The token count is off by one.", False),
])
def test_the_gates_security_signal(location, explanation, expected):
    item = {"location": location, "explanation": explanation, "recommendation": "Fix it."}
    assert workers.gate_security_signal(item) is expected


def test_the_pre_mortems_wide_security_rule_is_separate():
    item = {"location": "src/schedules/sandbox.py:10", "explanation": "The tokenizer drops a word.",
            "recommendation": "Fix it."}
    assert workers.security_signal(item) is True and workers.gate_security_signal(item) is False


def test_a_medium_in_a_sandbox_module_can_be_declined(world):
    leak = {"severity": "medium", "location": "src/schedules/sandbox.py:10", "explanation": "Memory leak in the loop."}
    world.scenario({"outcome": {"findings": [leak]}})
    service = world.service()
    head = head_of(world)
    state = finish(service, service.review({"head": head}))
    service.decide({"key": state["findings"][0]["key"], "decision": "declined", "reason": "Bounded by the run."})
    assert json.loads((world.run_dir / broker.REVIEW_RECORD).read_text())["findings"][0]["security"] is False
    assert broker.validated_review_head(world.run_dir) == head


def test_a_codex_key_known_after_a_decision_reaches_the_brokers_scan(world):
    late = "correct-horse-battery-staple-violet-lamp"  # no pattern or entropy rule finds it
    run = broker.Run(RUN_ID, "IEZhu/Agents", ISSUE, world.repo.base, "dry-run", frozenset({"autodev:ready"}))
    gh = broker.Broker(run, credentials=None, git=broker.TrustedGit(world.repo.trusted), run_dir=world.run_dir,
                       earlier_records=lambda: [])
    assert gh.scanner.scan(f"the key is {late}") == ()

    def decide(*args, **kwargs):
        return codex_probes.Decision(codex_probes.CODEX, codex_probes.CLAUDE, "probe_failed", "a probe failed",
                                     known=(KnownSecret(codex_probes.API_KEY_LABEL, late),))

    codex, service = codex_world(world, {}, on_known=gh.add_known)
    service.launcher.codex_decide = decide
    state = finish(service, service.review({"head": head_of(world)}), seconds=120)
    assert state["status"] == "accepted" and state["client_applied"] == "claude"
    with pytest.raises(broker.BrokerRefused, match=codex_probes.API_KEY_LABEL):
        gh._scan("comment", f"{broker.MARKER}\nthe key is {late}")


# --- caps, withheld output and waiting (review round 2) ------------------------------------------

def test_a_second_verification_review_is_refused(world):
    service = world.service()
    head = head_of(world)
    assert finish(service, service.review({"head": head}))["status"] == "accepted"
    assert finish(service, service.premortem({"head": head}))["status"] == "accepted"
    verify = finish(service, service.review({"head": head_of(world), "verification": True}))
    assert verify["status"] == "accepted" and "stage_blocked" not in verify
    with pytest.raises(RequestRefused, match="the verification review already ran"):  # MAX_VERIFICATIONS
        service.review({"head": head_of(world), "verification": True})


def test_an_oversized_worker_output_is_a_new_attempt(world):
    cap = 4 * 1024 * 1024  # worker_launch.MAX_STDOUT_BYTES, written out so a changed cap fails here
    world.scenario({"outcome": {"pad": cap + 1024}})
    service = world.service()
    state = finish(service, service.review({"head": head_of(world)}), seconds=120)
    assert state["status"] == "blocked" and state["attempts"] == workers.MAX_ATTEMPTS, state
    assert f"the claude worker's output exceeds {cap} bytes" in state["reason"]


def test_wait_with_zero_seconds_polls(world):
    world.scenario({"sleep": 3})
    service = world.service()
    started = service.review({"head": head_of(world)})
    began = time.monotonic()
    state = service.wait({"handle": started["handle"], "seconds": 0})
    assert state["status"] == "running" and time.monotonic() - began < 1
    assert finish(service, started)["status"] == "accepted"


def test_record_updates_from_several_threads_keep_every_entry(service_state_dir):
    import threading
    records.write_record(RUN_ID, {"status": "running"}, service_state_dir)

    def add(number):
        for index in range(20):
            records.update_record(RUN_ID, lambda record: record.setdefault("entries", []).append(f"{number}-{index}"),
                                  service_state_dir)

    threads = [threading.Thread(target=add, args=(number,)) for number in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    record = records.read_record(RUN_ID, service_state_dir)
    assert record["status"] == "running" and len(record["entries"]) == 80 and len(set(record["entries"])) == 80
