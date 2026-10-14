"""The run sequence (#347) end to end, with fakes only.

The fake ``claude`` (tests/fixtures/fake_claude.py) answers the probe sessions, the stage sessions
(stage mode: a script per stage, run for real in the one-off repository, and a structured output)
and the worker reviews; GitHub is a table of synthetic GET answers in the recorded shape
(tests/fixtures/autodev_select_synthetic.json and autodev_preconditions_synthetic.json) and a fake
transport for the broker's writes; the trusted repository is a real bare repository in a
temporary directory, the push runner and the notification runner are fakes. No model runs, no
network is used, no keychain is read and nothing is planted in the real home directory.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.schedules import broker, decoys, records, repos, runner, sandbox, sequence, stages
from src.schedules import config as autodev_config
from src.schedules.github import API, GitHubClient, TokenUnavailable
from tests.test_schedules_probes import FakeCommands

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"
ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "IEZhu/Agents"
BASE = f"/repos/{REPOSITORY}"
TOKEN = "fake-broker-token-for-the-sequence-tests"
KEY = "fake deploy key material for the sequence tests"
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
ISSUE = 401
TITLE = "Add a greeting helper"
SLUG = "add-a-greeting-helper"
BRANCH = f"claude/autodev-{ISSUE}-{SLUG}"
PR = 501
PR_URL = f"https://github.com/{REPOSITORY}/pull/{PR}"
PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def git(cwd: Path, *args: str) -> str:
    env = {"PATH": os.environ.get("PATH", PATH), "HOME": str(cwd), "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.org",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.org", "LC_ALL": "C"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


def score(issue: int, value: float, rationale: str) -> dict:
    return {"issue": issue, "value": 5, "urgency": 3, "unblocking": 2, "size": 2, "readiness": 1, "next": False,
            "score": value, "rationale": rationale}


SELECT_OUTPUT = {
    "decision": "task", "issue": ISSUE,
    "slice": {"this_pr": "Add the greeting helper with its unit test.", "remaining": []},
    "criteria_refs": [f"https://github.com/{REPOSITORY}/issues/{ISSUE}",
                      f"https://github.com/{REPOSITORY}/issues/{ISSUE}#issuecomment-{9100 + ISSUE}"],
    "scores": [score(401, 5.0, "Ready and small."), score(403, 4.5, "Documentation only."),
               score(404, 4.0, "A small cleanup."), score(405, 3.5, "Useful, less urgent."),
               score(402, 3.0, "Waits for a cache decision.")],
    "rationale": "The greeting helper is ready, small and has criteria."}

IMPLEMENT_OUTPUT = {
    "branch": "{branch}", "head_sha": "{head}",
    "changes": [{"path": "src/greeting.py", "description": "The greeting helper."}],
    "checks": [{"name": "unit tests", "command": "python -m pytest tests/test_greeting.py", "status": "passed",
                "detail": "1 passed"}],
    "reviews": [{"handle": "review-1", "head": "{head}", "status": "accepted", "findings": 0, "fixed": 0,
                 "declined": 0, "mitigated": 0, "known_risk": 0},
                {"handle": "premortem-1", "head": "{head}", "status": "accepted", "findings": 0, "fixed": 0,
                 "declined": 0, "mitigated": 0, "known_risk": 0}],
    "blocked": False, "blockers": [],
    "pr_sections": {"self_review": "Read the diff twice.", "premortem_known_risks": "None.",
                    "declined_findings": "None."}}

COMMIT = ["git switch -q -c {branch}",
          "echo 'def greet(name): return \"hello \" + name' > src/greeting.py",
          "git add -A && git commit -q -m 'Add a greeting helper'",
          "git bundle create -q {exchange_dir}/handover.bundle {branch} ^{pinned}"]
REVIEW = ["""{worker_cmd} review '{"head": "{head}"}'""", """{worker_cmd} wait '{"handle": "review-1"}'"""]
PREMORTEM = ["""{worker_cmd} premortem '{"head": "{head}"}'""", """{worker_cmd} wait '{"handle": "premortem-1"}'"""]
SUBMIT = ["""{gh_cmd} push '{"slug": "%s", "head": "{head}"}'""" % SLUG,
          """{gh_cmd} pr_create '{"title": "Add a greeting helper", "body": "Adds greet().\\n\\nCloses #401"}'""",
          """{gh_cmd} label '{"number": %d, "label": "autodev", "action": "add"}'""" % PR]


def submit_output(status: str = "complete", next_attempt: str | None = None) -> dict:
    return {"pr_url": PR_URL, "head_sha": "{head}", "review_status": status, "bots": [], "followups": [],
            "next_attempt": next_attempt, "rounds": 0, "findings": [], "reason": "Opened the pull request."}


def stages_plan(*, implement_script=None, implement_output=None, submit_script=None, submit_out=None) -> dict:
    return {"select": {"output": SELECT_OUTPUT},
            "implement": {"script": COMMIT + REVIEW + PREMORTEM if implement_script is None else implement_script,
                          "output": IMPLEMENT_OUTPUT if implement_output is None else implement_output},
            "submit": {"script": SUBMIT if submit_script is None else submit_script,
                       "output": submit_output() if submit_out is None else submit_out}}


class FakeGet:
    """GitHub's GET answers by path and query, as the read-only client sends them; 404 otherwise."""

    def __init__(self, responses: dict):
        self.responses, self.calls = responses, []

    def __call__(self, url, headers):
        self.calls.append(url)
        key = url.removeprefix(API)
        if key not in self.responses:
            return 404, {}, b'{"message": "Not Found"}'
        answer = self.responses[key]
        if isinstance(answer, dict) and "__status__" in answer:
            return answer["__status__"], {}, b'{"message": "error"}'
        return 200, {}, json.dumps(answer).encode()


class FakeBrokerApi:
    """The broker's REST writes and reads; records every call. ``fail`` lists ``(method, path,
    payload or None, status)``: a matching call gets that HTTP status."""

    def __init__(self):
        self.calls, self.next_id, self.pulls, self.fail = [], 9000, {}, []

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        payload = json.loads(body) if body else None
        path = url.removeprefix(API).split("?")[0]
        self.calls.append((method, path, payload))
        self.next_id += 1
        for fail_method, fail_path, fail_payload, status in self.fail:
            if (fail_method, fail_path) == (method, path) and fail_payload in (None, payload):
                return status, {}, b'{"message": "Validation Failed"}'
        if method == "GET" and path.startswith(f"{BASE}/pulls/"):
            number = int(path.rsplit("/", 1)[1])
            return 200, {}, json.dumps(self.pulls.get(number, {})).encode()
        if method == "POST" and path == f"{BASE}/pulls":
            return 201, {}, json.dumps({"number": PR, "id": self.next_id, "html_url": PR_URL}).encode()
        if method == "POST" and path == f"{BASE}/issues":
            return 201, {}, json.dumps({"number": 600 + len(self.writes()), "id": self.next_id}).encode()
        return 200, {}, json.dumps({"id": self.next_id}).encode()

    def writes(self) -> list:
        return [(method, path) for method, path, _ in self.calls if method != "GET"]


class World:
    def __init__(self, base: Path, state: Path, socket_dir: Path):
        self.base, self.state = base, state
        self.home = base / "home"
        for name in (".ssh", "Library/Keychains", ".config/agents-autodev"):
            (self.home / name).mkdir(parents=True)
        self.temps = {name: base / "temps" / name for name in ("owner_temp", "claude_temp", "claude_shared")}
        for path in self.temps.values():
            path.mkdir(parents=True)
        self.work = base / "work"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        files = {"README.md": "base\n", "src/app.py": "def run():\n    return 1\n",
                 ".github/autodev-protected-paths": (ROOT / ".github/autodev-protected-paths").read_text(),
                 ".github/autodev-control-paths": (ROOT / ".github/autodev-control-paths").read_text()}
        self.pinned = self.commit(files, "base")
        self.runner_dir = base / "runner"
        self.runner_dir.mkdir()
        self.trusted = self.runner_dir / "trusted.git"
        git(base, "init", "-q", "--bare", str(self.trusted))
        git(self.work, "push", "-q", str(self.trusted), f"{self.pinned}:refs/remotes/origin/main")
        keys = base / "keys"
        keys.mkdir()
        (keys / "deploy_key").write_text(KEY + "\n")
        (keys / "github_known_hosts").write_text("github.com ssh-ed25519 placeholder\n")
        self.token = lambda: TOKEN
        self.credentials = lambda config: broker.Credentials(keys / "deploy_key", keys / "github_known_hosts",
                                                             self.token)
        (base / "installation").mkdir()
        (base / "LaunchAgents").mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        self.stage_seen_file, self.probe_seen = base / "stage-seen.jsonl", base / "probe-seen.json"
        self.worker_seen, self.counter = base / "worker-seen.jsonl", base / "counter"
        self.responses = json.loads((FIXTURES / "autodev_select_synthetic.json").read_text())["responses"]
        self.responses.update(json.loads((FIXTURES / "autodev_preconditions_synthetic.json").read_text())["responses"])
        self.get, self.api = FakeGet(self.responses), FakeBrokerApi()
        self.pushes, self.notifications = [], []
        config = copy.deepcopy(autodev_config.DEFAULTS)
        config.update(enabled=True, clone=str(self.trusted))
        config["delegates"]["code-review-pass"] = {"client": "claude", "claude": {"model": "opus", "effort": "high"},
                                                   "effort_followup": "medium"}
        self.save(config)
        self.scenario(stages_plan())
        self.ctx = runner.Context(
            installation=base / "installation", directory=state, python=sys.executable, clock=lambda: NOW,
            agents_dir=base / "LaunchAgents",
            client_factory=lambda config: GitHubClient(REPOSITORY, self.token, transport=self.get),
            drift_check=lambda installation: {"pinned": self.pinned, "runner_commit": self.pinned, "changed": []},
            claude=str(self.bin / "claude"), home=self.home, platform="darwin",
            command_runner=FakeCommands(), managed_sources=lambda: [], socket_root=socket_dir,
            resolver=lambda host: True, notify_runner=self.notify, credentials=self.credentials,
            broker_transport=self.api, push_runner=self.push, venv_builder=self.build_venv,
            session_temps=dict(self.temps), probe_timeout=120)
        self.ctx.scheduler().plist.write_text("<plist/>\n")

    # --- helpers ------------------------------------------------------------------------------

    def commit(self, files: dict, message: str) -> str:
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", message)
        return git(self.work, "rev-parse", "HEAD")

    def save(self, config: dict) -> None:
        autodev_config.save(autodev_config.validate(config), self.state)
        self.config = autodev_config.load(self.state)

    def scenario(self, plan: dict, **extra) -> None:
        scenario = {"seen": str(self.probe_seen), "stage_seen": str(self.stage_seen_file),
                    "counter": str(self.counter), "fallback_tmpdir": str(self.temps["claude_temp"]),
                    "stages": plan, **extra}
        (self.bin / "scenario.json").write_text(json.dumps(scenario))
        wrapper = self.bin / "claude"
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_CLAUDE} {self.bin / 'scenario.json'} \"$@\"\n")
        wrapper.chmod(0o755)

    def push(self, argv, **kwargs):
        self.pushes.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def notify(self, argv, **kwargs):
        self.notifications.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    @staticmethod
    def build_venv(venv: Path, files: dict) -> None:
        (venv / "bin").mkdir(parents=True)
        os.symlink(os.path.realpath(sys.executable), venv / "bin" / "python")

    def run(self, mode: str = "run", pr: int | None = None) -> tuple[int, dict]:
        return runner.run_autodev(self.ctx, mode, pr=pr)

    def stage_seen(self) -> list[dict]:
        if not self.stage_seen_file.exists():
            return []
        return [json.loads(line) for line in self.stage_seen_file.read_text().splitlines()]

    def run_dir(self, record: dict) -> Path:
        return records.run_dir(record["run_id"], self.state)

    def log(self, record: dict) -> list[dict]:
        path = self.run_dir(record) / broker.LOG_NAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def ops(self, record: dict) -> list[str]:
        """The broker log's operations in order, each once per phase run."""
        found = []
        for entry in self.log(record):
            if not found or found[-1] != entry["op"]:
                found.append(entry["op"])
        return found


@pytest.fixture
def socket_dir():
    path = Path(tempfile.mkdtemp(prefix="sock-", dir="/private/tmp" if os.path.isdir("/private/tmp") else None))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def world(tmp_path_factory, service_state_dir, socket_dir):
    if sys.platform != "darwin" or not sandbox.TMP_BASE.is_dir():
        pytest.skip("the session sandbox is macOS-only")
    found = World(Path(os.path.realpath(tmp_path_factory.mktemp("seq"))), service_state_dir, socket_dir)
    yield found
    for directory, _, _ in os.walk(found.runner_dir):  # the environments are read-only
        os.chmod(directory, 0o700)


# --- acceptance 1: the dry-run -------------------------------------------------------------------

def test_full_dry_run_against_recorded_responses_prints_decision_and_top_five(world):
    code, record = world.run("dry-run")
    assert code == 0 and record["status"] == "completed", record.get("reason")
    assert record["gate"]["decision"] == "select" and record["cleanup_executed"] is False
    selection = record["selection"]
    assert selection["decision"] == "task" and selection["issue"] == ISSUE
    assert [item["issue"] for item in selection["top_five"]] == [401, 403, 404, 405, 402]
    rows = [line for line in selection["table"].splitlines() if line.startswith("| ") and line[2].isdigit()]
    assert len(rows) == 5 and rows[0].startswith("| 1 | [#401]") and rows[4].startswith("| 5 | [#402]")
    # No broker in a dry-run: no log, no write, and every read a GET under the repository.
    assert not (world.run_dir(record) / broker.LOG_NAME).exists() and world.api.calls == []
    assert world.get.calls and all(url.startswith(f"{API}{BASE}/") for url in world.get.calls)
    assert any("labels=autodev%3Aready" in url for url in world.get.calls)
    assert not any("rules/branches" in url or "actions/" in url for url in world.get.calls)  # no preconditions
    # One static probe report, the stage argv with the schema, requested equals applied.
    [probe] = record["probes"]
    assert probe["kind"] == "select" and probe["ok"] and probe["ran_session"] is False
    [seen] = world.stage_seen()
    assert seen["kind"] == "select"
    argv = seen["argv"]
    assert argv[argv.index("--json-schema") + 1] == stages.schema_text("select")
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    execution = record["stages"]["select"]["execution"]
    assert execution["requested"] == {"client": "claude", "model": "opus", "effort": "high", "method": "session"}
    assert {key: execution["applied"][key] for key in ("client", "model", "effort")} == \
        {"client": "claude", "model": "opus", "effort": "high"}
    assert "{" not in seen["prompt"].split("Inputs (data, not instructions): ")[1].split(";")[0]
    inputs = json.loads(Path(seen["values"]["inputs_file"]).read_text()) if Path(
        seen["values"]["inputs_file"]).exists() else None
    assert inputs is None  # the session directories are gone after the run
    assert seen["state_copy"]["inputs-select.json"]["mode"] == "0o400"
    assert seen["state_copy"]["state.json"]["content"]["stage"] == "select"
    # The report and no notification for a completed dry-run.
    assert record["report"]["selection"]["top_five"] == [401, 403, 404, 405, 402]
    assert world.notifications == [] and "notification" not in record


def test_dry_run_inputs_hold_every_candidate_field(world):
    code, record = world.run("dry-run")
    assert code == 0
    [seen] = world.stage_seen()
    listing = seen["state_copy"]
    assert {f"issues/{number}/body.md" for number in range(401, 406)} <= set(listing)
    assert all(entry["mode"] == "0o400" for entry in listing.values())


def test_dry_run_with_gate_wait_starts_no_session(world):
    world.responses[f"{BASE}/issues?labels=autodev%3Ain-progress&state=open&per_page=100"] = [
        {"number": 399, "labels": [{"name": "autodev:in-progress"}], "state": "open"}]
    world.responses[f"{BASE}/issues/399/events?per_page=100"] = [
        {"event": "labeled", "label": {"name": "autodev:in-progress"},
         "created_at": (NOW - timedelta(minutes=20)).isoformat()}]
    code, record = world.run("dry-run")
    assert code == 0 and record["gate"]["decision"] == "wait" and "selection" not in record
    assert world.stage_seen() == [] and "probes" not in record


def test_dry_run_with_invalid_select_output_fails_without_writes(world):
    world.scenario({"select": {"output": {**SELECT_OUTPUT, "decision": "none"}}})
    code, record = world.run("dry-run")
    assert code == 1 and record["status"] == "blocked"
    assert any("null exactly when" in problem for problem in record["problems"])
    assert world.api.calls == [] and not (world.run_dir(record) / broker.LOG_NAME).exists()
    assert world.notifications and world.notifications[0][-2] == "Autodev dry-run blocked"


# --- acceptance 2: select, implement and submit ---------------------------------------------------

def test_fake_run_walks_select_implement_submit(world):
    code, record = world.run("run")
    assert code == 0 and record["status"] == "completed", (record.get("reason"), record.get("blockers"),
                                                           record.get("stages"))
    assert record["issue"] == ISSUE and record["pr"] == PR and record["pr_url"] == PR_URL
    assert record["branch"] == BRANCH and record["review_status"] == "complete"
    kinds = {(item["kind"], item["number"]) for item in record["comments"]}
    assert kinds == {("selection", ISSUE), ("state", PR)}
    assert set(record["endpoint"]) == {"socket", "private_directory", "clients"}
    assert [(probe["kind"], probe["ran_session"]) for probe in record["probes"]] == \
        [("select", False), ("implement", True), ("submit", True)]
    assert all(probe["ok"] for probe in record["probes"])
    # The broker log: only the expected operations, in order; the GitHub writes match.
    assert world.ops(record) == ["bind_issue", "select", "push", "pr_create", "label", "state"]
    assert world.api.writes() == [("POST", f"{BASE}/issues/{ISSUE}/comments"), ("POST", f"{BASE}/issues/{ISSUE}/labels"),
                                  ("POST", f"{BASE}/pulls"), ("POST", f"{BASE}/issues/{PR}/labels"),
                                  ("POST", f"{BASE}/issues/{PR}/comments")]
    # The stage binding: the session writes are all the submit stage's; the implement stage wrote
    # nothing, and the runner's own writes name no stage.
    done = [(entry["op"], entry.get("stage")) for entry in world.log(record) if entry["phase"] == "done"]
    assert done == [("bind_issue", None), ("select", None), ("select", None), ("push", "submit"),
                    ("pr_create", "submit"), ("label", "submit"), ("state", None)]
    assert not [entry for entry in world.log(record) if entry.get("stage") == "implement"]
    [push] = world.pushes
    head = git(world.trusted, "rev-parse", f"refs/heads/{BRANCH}")
    assert push[-1] == f"{head}:refs/heads/{BRANCH}" and record["head"] == head
    # One worker service for both stages: review-1 and premortem-1 accepted, and the push gate
    # (validated-review.json) survived into the submit stage.
    run_dir = world.run_dir(record)
    tasks = {task["handle"]: task["status"] for task in json.loads((run_dir / "workers.json").read_text())["tasks"]}
    assert tasks == {"review-1": "accepted", "premortem-1": "accepted"}
    assert broker.validated_review_head(run_dir) == head
    # The state file, its read-only copy (seen by the sessions) and the worker log beside it.
    state = json.loads((run_dir / "state.json").read_text())
    assert state["stage"] == "done" and state["issue"] == ISSUE and state["branch"] == BRANCH and state["pr"] == PR
    assert state["stages_done"] == ["select", "implement", "submit"] and state["pinned"] == world.pinned
    seen = {item["kind"]: item for item in world.stage_seen()}
    assert seen["implement"]["state_copy"]["state.json"]["mode"] == "0o400"
    assert seen["implement"]["state_copy"]["state.json"]["content"]["stage"] == "implement"
    assert seen["submit"]["state_copy"]["state.json"]["content"]["stage"] == "submit"
    assert "worker-log.json" in seen["submit"]["state_copy"]
    assert seen["implement"]["codes"] == [0] * 8 and seen["submit"]["codes"] == [0, 0, 0]
    assert seen["implement"]["values"]["branch"] == BRANCH
    # Every stage's execution is recorded, and the report has the PR.
    for kind in stages.KINDS:
        execution = record["stages"][kind]["execution"]
        assert execution["applied"]["client"] == "claude" and execution["requested"]["client"] == "claude"
        assert record["stages"][kind]["integrity"] == "unchanged" and record["stages"][kind]["status"] == "completed"
    assert record["report"]["pr_url"] == PR_URL and record["report"]["checks"] == [
        {"name": "unit tests", "status": "passed"}]
    assert record["selection"]["issue"] == ISSUE
    # The owner is notified of every real run; the session directories are gone.
    [argv] = world.notifications
    assert argv[-2] == "Autodev run completed" and PR_URL in argv[-1]
    assert not (world.runner_dir / "sessions" / record["run_id"]).exists()


def test_fake_run_waiting_bots_writes_state_comment_and_tick_next_attempt_with_codex_fallback(world):
    """Codex implement and submit stages fall back to Claude with the recorded reason."""
    config = copy.deepcopy(world.config)
    for kind in ("implement", "submit"):
        config["stages"][kind]["client"] = "codex"
        config["stages"][kind]["codex"] = {"model": "gpt-6.1-sol", "effort": "high"}
    world.save(config)
    attempt = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
    text = attempt.strftime("%Y-%m-%dT%H:%M:%SZ")
    world.scenario(stages_plan(submit_out=submit_output("waiting_bots", text)))
    code, record = world.run("run")
    assert code == 0 and record["status"] == "completed", record.get("reason")
    assert record["review_status"] == "waiting_bots" and record["next_attempt"] == text
    [state_comment] = [call for call in world.api.calls if call[1] == f"{BASE}/issues/{PR}/comments"]
    assert f'"next_attempt": "{text}"' in state_comment[2]["body"] and "waiting_bots" in state_comment[2]["body"]
    tick = json.loads((world.state / "schedules" / "tick.json").read_text())
    assert tick["next_attempt"] == {"at": text, "pr": PR}
    for kind in ("implement", "submit"):
        stage = record["stages"][kind]
        assert stage["client"] == "claude" and stage["requested_client"] == "codex"
        assert stage["fallback_reason"] == stages.CODEX_COMMIT_UNSUPPORTED
        assert stage["execution"]["requested"]["client"] == "codex"
        assert stage["execution"]["applied"]["fallback_reason"] == stages.CODEX_COMMIT_UNSUPPORTED
    [argv] = world.notifications
    assert f"run-now --mode resume --pr {PR}" in argv[-1]


# --- acceptance 3: a missing precondition stops a real run ----------------------------------------

RULES = f"{BASE}/rules/branches/main?per_page=100"
WORKFLOW = f"{BASE}/actions/permissions/workflow"
SECRETS = f"{BASE}/actions/secrets?per_page=100&page=1"


def _drop_rule(name):
    def change(world):
        world.responses[RULES] = [rule for rule in world.responses[RULES] if rule["type"] != name]
    return change


def _no_approval(world):
    world.responses[RULES] = [{**rule, "parameters": {**rule["parameters"], "required_approving_review_count": 0}}
                              if rule["type"] == "pull_request" else rule for rule in world.responses[RULES]]


def _workflow_approves(world):
    world.responses[WORKFLOW] = {**world.responses[WORKFLOW], "can_approve_pull_request_reviews": True}


def _workflow_forbidden(world):
    world.responses[WORKFLOW] = {"__status__": 403}


def _routine_secret(world):
    world.responses[SECRETS] = {"total_count": 1, "secrets": [{"name": "CLAUDE_ROUTINE_TOKEN"}]}


def _no_key(world):
    (world.base / "keys" / "deploy_key").unlink()


def _linked_host_keys(world):
    hosts = world.base / "keys" / "github_known_hosts"
    target = world.base / "keys" / "elsewhere"
    hosts.rename(target)
    hosts.symlink_to(target)


def _no_token(world):
    def unavailable():
        raise TokenUnavailable("the keychain item agents-autodev-github could not be read")
    world.token = unavailable


@pytest.mark.parametrize("change, item", [
    (_drop_rule("deletion"), "main protection: the deletion rule"),
    (_drop_rule("non_fast_forward"), "main protection: the non_fast_forward rule"),
    (_no_approval, "main protection: a pull_request rule with at least one required approving review"),
    (_workflow_approves, "workflows may not approve pull requests (can_approve_pull_request_reviews false)"),
    (_workflow_forbidden, "cannot verify the workflow approval permission (HTTP 403)"),
    (_routine_secret, "CLAUDE_ROUTINE_TOKEN only as an environment secret, not a repository secret"),
    (_no_key, "the deploy key"),
    (_linked_host_keys, "the pinned github.com host keys"),
    (_no_token, "the API token"),
])
def test_missing_precondition_stops_a_real_run_with_the_item_named(world, change, item):
    change(world)
    code, record = world.run("run")
    assert code == 1 and record["status"] == "refused" and record["missing"] == [item]
    assert item in record["reason"] and "gate" not in record
    assert world.stage_seen() == [] and not world.probe_seen.exists() and "probes" not in record
    assert not (world.run_dir(record) / broker.LOG_NAME).exists() and world.api.calls == []
    assert TOKEN not in json.dumps(record) and KEY not in json.dumps(record)
    [argv] = world.notifications
    assert argv[-2] == "Autodev run refused"


def test_dry_run_skips_preconditions(world):
    _workflow_forbidden(world)
    code, record = world.run("dry-run")
    assert code == 0 and record["status"] == "completed"
    assert not any("actions/" in url or "rules/" in url for url in world.get.calls)


# --- acceptance 4: a control-path change leaves a patch and a bundle -----------------------------

CONTROL = ["git switch -q -c {branch}",
           "mkdir -p src/schedules && echo 'VALUE = 1' > src/schedules/extra.py",
           "git add -A && git commit -q -m 'Add a schedules module'",
           "git bundle create -q {exchange_dir}/handover.bundle {branch} ^{pinned}"]


def blocked_output(*blockers: str) -> dict:
    return {**IMPLEMENT_OUTPUT, "reviews": [], "blocked": True, "blockers": list(blockers)}


def test_control_path_refusal_exports_patch_and_bundle_and_names_them(world):
    world.scenario(stages_plan(implement_script=CONTROL, implement_output=blocked_output("needs a control path")))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked", record.get("reason")
    run_dir = world.run_dir(record)
    exports = record["exports"]
    patch, bundle = Path(exports["patch"]), Path(exports["bundle"])
    assert patch == run_dir / repos.EXPORT_DIR / f"{record['run_id']}.patch" and patch.is_file()
    assert bundle == run_dir / repos.EXPORT_DIR / f"{record['run_id']}.bundle" and bundle.is_file()
    assert exports["sha256"] == {"patch": repos._sha256(patch), "bundle": repos._sha256(bundle)}
    git(world.trusted, "bundle", "verify", "-q", str(bundle))
    clone = world.base / "check"
    git(world.base, "clone", "-q", str(world.trusted), str(clone))
    git(clone, "checkout", "-q", world.pinned)
    git(clone, "apply", "--check", str(patch))
    assert "src/schedules/extra.py" in patch.read_text()
    # Named in the report and in the notification's arguments.
    assert record["report"]["exports"]["patch"] == str(patch) and record["report"]["exports"]["bundle"] == str(bundle)
    [argv] = world.notifications
    assert argv[-2] == "Autodev run blocked" and str(patch) in argv[-1] and str(bundle) in argv[-1]
    # The branch is quarantined, the issue blocked and unclaimed; nothing was pushed, no PR.
    assert record["quarantined"] == f"{repos.QUARANTINE}/{record['run_id']}/final"
    git(world.trusted, "rev-parse", "--verify", "-q", record["quarantined"])
    assert subprocess.run(["git", "-C", str(world.trusted), "rev-parse", "--verify", "-q", f"refs/heads/{BRANCH}"],
                          capture_output=True).returncode != 0
    assert world.ops(record) == ["bind_issue", "select", "block", "unclaim"]
    assert world.pushes == [] and ("POST", f"{BASE}/pulls") not in world.api.writes()
    assert record["blockers"] == ["needs a control path", sequence.CONTROL_PATH]
    assert [item["kind"] for item in world.stage_seen()] == ["select", "implement"]
    [block] = [call for call in world.api.calls if call[1] == f"{BASE}/issues/{ISSUE}/comments"
               and "needs the owner" in call[2]["body"]]
    assert "- needs a control path" in block[2]["body"]


def test_control_path_found_by_the_runner_without_a_blocked_output(world):
    world.scenario(stages_plan(implement_script=CONTROL + REVIEW + PREMORTEM))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked"
    assert record["blockers"] == [sequence.CONTROL_PATH]
    assert Path(record["exports"]["patch"]).is_file() and Path(record["exports"]["bundle"]).is_file()
    assert world.pushes == [] and [item["kind"] for item in world.stage_seen()] == ["select", "implement"]


# --- safety -----------------------------------------------------------------------------------------

def test_git_dir_tampering_stops_the_run_and_turns_the_schedule_off(world):
    tamper = COMMIT + ["mv .git .git-moved && printf '[core]\\n\\tfsmonitor = /bin/true\\n' >> .git-moved/config "
                       "&& mv .git-moved .git"]
    world.scenario(stages_plan(implement_script=tamper))
    assert autodev_config.load(world.state)["enabled"] is True
    code, record = world.run("run")
    assert code == 1 and record["status"] == "refused"
    assert record["reason"].startswith("sandbox: the one-off repository's .git changed (.git/config changed")
    assert record["stages"]["implement"]["status"] == "refused" and record["tripwire"] is True
    assert autodev_config.load(world.state)["enabled"] is False
    tripwire = json.loads((world.state / "schedules" / decoys.TRIPWIRE_NAME).read_text())
    assert ".git changed" in tripwire["last"]["reason"] and tripwire["last"]["schedule"] == "disabled"
    # Nothing imported after the session, no further stage, no push; the issue is blocked with the
    # generic phrase and unclaimed, as a blocked run's (#347 review).
    assert [item["kind"] for item in world.stage_seen()] == ["select", "implement"]
    assert world.pushes == [] and world.ops(record) == ["bind_issue", "select", "block", "unclaim"]
    assert record["blockers"] == [sequence.GENERIC_BLOCKED]
    assert subprocess.run(["git", "-C", str(world.trusted), "rev-parse", "--verify", "-q", f"refs/heads/{BRANCH}"],
                          capture_output=True).returncode != 0
    [argv] = world.notifications  # the run turned the schedule off: the notification says so first (#350)
    assert argv[-2] == "Autodev schedule off" and argv[-1].startswith("a tripwire fired and turned the schedule off")
    assert record["schedule_off"] == "tripwire" and tripwire["last"]["cause"] == "tripwire"


def test_invalid_stage_output_ends_the_stage_blocked_without_other_writes(world):
    world.scenario(stages_plan(implement_output={"branch": "{branch}", "head_sha": "{head}"}))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked"
    assert record["stages"]["implement"]["status"] == "invalid" and record["stages"]["implement"]["problems"]
    assert record["blockers"] == [sequence.INVALID_OUTPUT.format(kind="implement")]
    assert world.ops(record) == ["bind_issue", "select", "block", "unclaim"]
    assert world.api.writes() == [("POST", f"{BASE}/issues/{ISSUE}/comments"), ("POST", f"{BASE}/issues/{ISSUE}/labels"),
                                  ("POST", f"{BASE}/issues/{ISSUE}/comments"), ("POST", f"{BASE}/issues/{ISSUE}/labels"),
                                  ("DELETE", f"{BASE}/issues/{ISSUE}/labels/autodev%3Ain-progress")]
    labels = [payload for method, path, payload in world.api.calls if path.endswith("/labels") and method == "POST"]
    assert labels == [{"labels": ["autodev:in-progress"]}, {"labels": ["autodev:blocked"]}]
    assert world.pushes == [] and [item["kind"] for item in world.stage_seen()] == ["select", "implement"]


SECURITY_RISK = {"severity": "medium", "location": "src/greeting.py",
                 "explanation": "A token could leak into the greeting when a caller passes one."}


@pytest.mark.parametrize("variant, blocker", [
    ("head", sequence.HEAD_MISMATCH),
    ("no-review", sequence.NO_VALIDATED_REVIEW),
    ("no-premortem", sequence.NO_PREMORTEM),
    ("security", sequence.OPEN_RISK),
])
def test_after_implement_checks_block_the_submit_stage(world, variant, blocker):
    script, output, extra = COMMIT + REVIEW + PREMORTEM, IMPLEMENT_OUTPUT, {}
    if variant == "head":
        output = {**IMPLEMENT_OUTPUT, "head_sha": "e" * 40}
    elif variant == "no-review":
        script = COMMIT
    elif variant == "no-premortem":
        script = COMMIT + REVIEW
    else:
        extra = {"outcomes": [{}, {"findings": [SECURITY_RISK]}]}
    world.scenario(stages_plan(implement_script=script, implement_output=output), **extra)
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked"
    assert blocker in record["blockers"]
    assert world.pushes == [] and [item["kind"] for item in world.stage_seen()] == ["select", "implement"]
    assert record["quarantined"].endswith("/final")
    if variant == "security":
        assert record["blockers"] == [sequence.OPEN_RISK]
        assert record["report"]["security"] == {"kept_local": 1, "open": 1}
        assert "token" not in json.dumps(world.api.calls)  # the risk's details stay local


def test_pr_create_refuses_while_a_security_risk_is_open(world, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / broker.PREMORTEM_RECORD).write_text(json.dumps({"validated": True, "findings": [
        {"id": "a" * 16, "level": "medium", "title": "A token leak", "body": "details", "security": True}]}))
    found = sequence.Sequence(world.ctx, world.config, records.new_run_id(NOW), "run", pr=None, now=NOW)
    found.run_dir, found.credentials = run_dir, world.credentials(world.config)
    opened = found._open_broker("run", world.pinned)
    assert opened._pr_refusal() == sequence.OPEN_RISK
    (run_dir / broker.PREMORTEM_RECORD).write_text(json.dumps({"validated": True, "findings": []}))
    assert opened._pr_refusal() is None


def test_pr_create_needs_the_accepted_premortem_in_a_new_run(world, tmp_path):
    """Security review #347: a new run's pull request needs the accepted pre-mortem; a resume's exists."""
    for mode, expected in (("run", sequence.NO_PREMORTEM), ("resume", None)):
        run_dir = tmp_path / mode
        run_dir.mkdir()
        found = sequence.Sequence(world.ctx, world.config, records.new_run_id(NOW), mode, pr=None, now=NOW)
        found.run_dir, found.credentials = run_dir, world.credentials(world.config)
        opened = found._open_broker(mode, world.pinned)
        assert opened._pr_refusal() == expected
        (run_dir / broker.PREMORTEM_RECORD).write_text(json.dumps({"validated": False, "findings": []}))
        assert opened._pr_refusal() == expected


def test_the_implement_session_cannot_push_or_open_a_pull_request(world):
    """Security review #347: the implement session gets gh.read and gh.threads only. A push and a
    pull request it asks for after a validated review are refused and logged with the stage, and
    the runner's checks after the stage still decide (here: no pre-mortem)."""
    world.scenario(stages_plan(implement_script=COMMIT + REVIEW + [SUBMIT[0], SUBMIT[1]]))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked" and record["blockers"] == [sequence.NO_PREMORTEM]
    [implement] = [item for item in world.stage_seen() if item["kind"] == "implement"]
    assert implement["codes"][-2:] == [1, 1]
    refused = [(entry["op"], entry.get("stage")) for entry in world.log(record) if entry["phase"] == "refused"]
    assert refused == [("push", "implement"), ("pr_create", "implement")]
    assert world.pushes == [] and ("POST", f"{BASE}/pulls") not in world.api.writes() and record.get("pr") is None
    assert world.ops(record) == ["bind_issue", "select", "push", "pr_create", "block", "unclaim"]


def test_a_pull_request_opened_before_the_checks_blocks_and_is_named(world, monkeypatch):
    """Defence in depth: should the broker let the implement session open a pull request, the
    runner's step after the stage names it in the record and the notification and blocks."""
    monkeypatch.setattr(broker, "SESSION_WRITE_STAGES", ("implement", "submit"))
    world.scenario(stages_plan(implement_script=COMMIT + REVIEW + PREMORTEM + [SUBMIT[0], SUBMIT[1]]))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked", record.get("reason")
    assert record["blockers"] == [sequence.EARLY_PR] and record["pr"] == PR and record["pr_url"] == PR_URL
    [argv] = world.notifications
    assert argv[-2] == "Autodev run blocked" and PR_URL in argv[-1]
    assert [item["kind"] for item in world.stage_seen()] == ["select", "implement"]
    assert world.ops(record) == ["bind_issue", "select", "push", "pr_create", "block"]  # the claim stays


def test_stage_stream_naming_a_decoy_trips_the_schedule(world):
    planted = decoys.ensure(world.state, world.home)
    marker = next(decoy.marker for decoy in planted if decoy.path is not None)
    world.scenario({"select": {"output": SELECT_OUTPUT, "say": f"I found {marker} somewhere."}})
    code, record = world.run("dry-run")
    assert code == 1 and record["status"] == "refused" and record["tripwire"] is True
    assert autodev_config.load(world.state)["enabled"] is False
    assert marker not in json.dumps(record) and "selection" not in record
    [argv] = world.notifications
    assert argv[-2:] == ["Autodev schedule off",
                         f"a tripwire fired and turned the schedule off; details in run {record['run_id']}."]
    assert record["schedule_off"] == "tripwire" and record["report"]["schedule_off"] == "tripwire"


def test_a_decoy_marker_after_two_megabytes_of_stream_still_trips(world):
    """Security review #347: the tripwire scans the whole stream, not its first 2,000,000 characters."""
    planted = decoys.ensure(world.state, world.home)
    marker = next(decoy.marker for decoy in planted if decoy.path is not None)
    world.scenario({"select": {"output": SELECT_OUTPUT, "pad": 2_400_000, "say": f"I found {marker} somewhere."}})
    code, record = world.run("dry-run")
    assert code == 1 and record["status"] == "refused" and record["tripwire"] is True
    assert autodev_config.load(world.state)["enabled"] is False
    assert any("marker of decoy" in finding for finding in record["tripwire_findings"])


def test_a_long_stage_stream_keeps_its_result_event(world):
    """Security review #347: the result event is read from the end of a stream of any length."""
    world.scenario({"select": {"output": SELECT_OUTPUT, "pad": 2_400_000}})
    code, record = world.run("dry-run")
    assert code == 0 and record["status"] == "completed", (record.get("reason"), record.get("problems"))
    assert record["selection"]["issue"] == ISSUE
    assert (world.run_dir(record) / "stages" / "select" / "stream.jsonl").stat().st_size > 2_400_000


def test_a_stage_stream_over_the_hard_limit_is_refused_and_turns_the_schedule_off(world, monkeypatch):
    monkeypatch.setattr(sequence, "MAX_STREAM_BYTES", 200_000)
    world.scenario({"select": {"output": SELECT_OUTPUT, "pad": 300_000}})
    code, record = world.run("dry-run")
    assert code == 1 and record["status"] == "refused" and record["tripwire"] is True
    assert "200000 bytes" in record["reason"] and record["stages"]["select"]["status"] == "refused"
    assert autodev_config.load(world.state)["enabled"] is False


def test_failed_static_probe_never_starts_the_stage_session(world):
    world.ctx.managed_sources = lambda: ["/Library/Application Support/ClaudeCode/managed-settings.json"]
    assert autodev_config.load(world.state)["enabled"] is True
    code, record = world.run("dry-run")
    assert code == 1 and record["status"] == "refused" and record["reason"].startswith("sandbox probes failed")
    assert world.stage_seen() == [] and record["probes"][0]["ok"] is False
    # A failed probe turns the schedule off and tells the owner (#350).
    assert record["reason"].endswith("the schedule is off") and record["schedule"] == "disabled"
    assert autodev_config.load(world.state)["enabled"] is False
    tripwire = json.loads((world.state / "schedules" / decoys.TRIPWIRE_NAME).read_text())["last"]
    assert (tripwire["cause"], tripwire["run_id"]) == ("probe", record["run_id"])
    [argv] = world.notifications
    assert argv[0] == "/usr/bin/osascript" and argv[-2:] == [
        "Autodev schedule off", f"a sandbox probe failed and turned the schedule off; details in run {record['run_id']}."]
    assert record["schedule_off"] == "probe"


def test_a_guarded_probe_that_leaked_a_decoy_trips_the_schedule_once(world, monkeypatch):
    """Kills mutant M17: the guard's own trip (a decoy, cause tripwire) is the record; the failed
    report must not trip again as a failed probe, which would count twice and say 'probe'."""
    from src.schedules import probes
    real = probes.guard

    def leaking(spec, **options):  # the probe session read a decoy: the guard trips, as `guard` does
        report = real(spec, **options)
        decoys.trip(options["directory"], "decoy read or leaked during a sandbox probe: decoy token",
                    run_id=options["run_id"])
        report.failures.append("read.decoy.bash: expected denied, got allowed")
        report.ok, report.tripped = False, True
        return report
    monkeypatch.setattr(probes, "guard", leaking)
    code, record = world.run("dry-run")
    assert code == 1 and record["status"] == "refused" and world.stage_seen() == []
    assert record["reason"].endswith("the decoy tripwire turned the schedule off")
    stored = json.loads((world.state / "schedules" / decoys.TRIPWIRE_NAME).read_text())
    assert stored["count"] == 1 and (stored["last"]["cause"], stored["last"]["run_id"]) == ("tripwire", record["run_id"])
    [argv] = world.notifications
    assert argv[-2:] == ["Autodev schedule off",
                         f"a tripwire fired and turned the schedule off; details in run {record['run_id']}."]
    assert record["schedule_off"] == "tripwire"


def test_stage_timeout_kills_the_session_group(world):
    pid_file = world.base / "child.pid"
    world.scenario({"select": {"output": SELECT_OUTPUT, "sleep": 60,
                               "script": [f"/bin/sleep 300 >/dev/null 2>&1 & echo $! > {pid_file}"]}})
    world.ctx.stage_seconds = {"select": 3}
    started = time.monotonic()
    code, record = world.run("dry-run")
    assert time.monotonic() - started < 45
    assert code == 1 and record["status"] == "failed: infra" and record["reason"].startswith("timeout: the select")
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        os.kill(child, signal.SIGKILL)
        raise AssertionError("the session's child survived")


def test_endpoint_is_recorded_before_the_first_session_with_bash(world):
    world.scenario(stages_plan(implement_script=["cat /dev/null"], implement_output=blocked_output("stop here")))
    code, record = world.run("run")
    assert record["status"] == "blocked" and record["endpoint"]["socket"].endswith("control.sock")
    [implement] = [item for item in world.stage_seen() if item["kind"] == "implement"]
    assert implement["values"]["worker_cmd"] == record["endpoint"]["clients"]["worker"]
    assert not Path(record["endpoint"]["socket"]).exists()  # closed with the stage


# --- a branch an earlier run left, a stop from the supervisor --------------------------------------

def test_a_branch_an_earlier_run_left_is_set_aside_before_the_implement_stage(world):
    """Correctness review #347: refs/heads/<branch> left by an earlier run of the issue (a failed or
    refused run, a squash-merged slice, a closed PR) moves to this run's quarantine and is recorded;
    the run builds on pinned and pushes only its own commits."""
    stale = _branch_in_trusted(world)
    code, record = world.run("run")
    assert code == 0 and record["status"] == "completed", (record.get("reason"), record.get("blockers"))
    quarantined = f"{repos.QUARANTINE}/{record['run_id']}/stale"
    assert record["stale_branch"] == {"head": stale, "quarantined": quarantined}
    assert git(world.trusted, "rev-parse", quarantined) == stale
    head = git(world.trusted, "rev-parse", f"refs/heads/{BRANCH}")
    assert head == record["head"] and head != stale
    assert git(world.trusted, "rev-parse", f"{head}~1") == world.pinned
    [push] = world.pushes
    assert push[-1] == f"{head}:refs/heads/{BRANCH}"


class Escaped(BaseException):
    """SIGTERM reached the test's own handler: the sequence installed none."""


@pytest.fixture
def sigterm_guard():
    """A SIGTERM the sequence does not handle raises `Escaped` instead of ending pytest."""
    def escaped(number, frame):
        raise Escaped(number)
    previous = signal.signal(signal.SIGTERM, escaped)
    yield
    signal.signal(signal.SIGTERM, previous)


def _stop_during(world, kind: str, delay: float = 1.0) -> None:
    """SIGTERM to this process ``delay`` seconds after the ``kind`` session started, as the
    supervisor's sweep sends it on a timeout or a stop."""
    start = world.ctx.stage_starter

    def starter(spec, report, prompt, **options):
        process = start(spec, report, prompt, **options)
        if prompt.startswith(f"You are the {kind} stage"):
            threading.Timer(delay, os.kill, (os.getpid(), signal.SIGTERM)).start()
        return process
    world.ctx.stage_starter = starter


def test_a_stop_during_the_implement_stage_records_unclaims_cleans_up_and_notifies(world, sigterm_guard):
    """Correctness review #347: SIGTERM during a stage with the runner socket (RunStopped) still
    writes the record, blocks and unclaims the issue, removes the session directories and notifies."""
    plan = stages_plan(implement_script=["cat /dev/null"])
    plan["implement"]["sleep"] = 60
    world.scenario(plan)
    _stop_during(world, "implement")
    started = time.monotonic()
    code, record = world.run("run")
    assert time.monotonic() - started < 50
    assert code == 1 and record["status"] == "failed: infra" and record["reason"].startswith("stopped: ")
    assert "SIGTERM" in record["reason"] and record["report"]["status"] == "failed: infra"
    [argv] = world.notifications
    assert argv[-2] == "Autodev run failed" and f"issue #{ISSUE}" in argv[-1]
    assert world.ops(record) == ["bind_issue", "select", "block", "unclaim"]
    assert record["blockers"] == [sequence.GENERIC_BLOCKED]
    assert not (world.runner_dir / "sessions" / record["run_id"]).exists()
    assert records.read_record(record["run_id"], world.state)["notification"]["sent"] is True


def test_a_stop_during_the_select_stage_records_and_notifies(world, sigterm_guard):
    """Correctness review #347: SIGTERM outside the runner socket (the select stage) no longer ends
    the process without a record, a cleanup or a notification."""
    world.scenario({"select": {"output": SELECT_OUTPUT, "sleep": 60}})
    _stop_during(world, "select")
    code, record = world.run("dry-run")
    assert code == 1 and record["status"] == "failed: infra" and record["reason"].startswith("stopped: ")
    [argv] = world.notifications
    assert argv[-2] == "Autodev dry-run failed"
    assert not (world.runner_dir / "sessions" / record["run_id"]).exists()
    assert signal.getsignal(signal.SIGTERM).__name__ == "escaped"  # the previous handler is back


# --- resume ----------------------------------------------------------------------------------------

def _open_pr(world, head: str, comments=None) -> None:
    pull = {"number": PR, "state": "open", "labels": [{"name": "autodev"}], "title": TITLE, "body": "Closes #401",
            "head": {"ref": BRANCH, "sha": head, "repo": {"full_name": REPOSITORY}}, "base": {"ref": "main"}}
    world.responses[f"{BASE}/pulls?state=open&per_page=100"] = [pull]
    world.responses[f"{BASE}/pulls/{PR}"] = {**pull, "mergeable": True}
    world.responses[f"{BASE}/issues/{PR}/comments?per_page=100"] = comments or []
    world.responses[f"{BASE}/pulls/{PR}/reviews?per_page=100"] = []
    world.responses[f"{BASE}/pulls/{PR}/comments?per_page=100"] = []
    world.api.pulls[PR] = {"number": PR, "id": 77, "state": "open", "labels": [{"name": "autodev"}],
                           "head": {"ref": BRANCH, "repo": {"full_name": REPOSITORY}}}


def _branch_in_trusted(world) -> str:
    git(world.work, "switch", "-q", "-c", "feature")
    head = world.commit({"src/greeting.py": "def greet(name):\n    return name\n"}, "Add a greeting helper")
    git(world.work, "push", "-q", str(world.trusted), f"{head}:refs/heads/{BRANCH}")
    return head


def test_a_dry_run_keeps_the_sequences_note(world):
    """Correctness review #347: run_autodev adds its dry-run note to the sequence's, not over it."""
    _open_pr(world, _branch_in_trusted(world))
    code, record = world.run("dry-run", pr=PR)
    assert code == 0 and record["gate"]["decision"] == "resume" and world.stage_seen() == []
    assert record["note"] == ("dry-run: the gate decided resume; no session started; "
                              "dry-run: the gate's cleanup actions were not executed")


def test_resume_with_a_foreign_head_blocks(world):
    _branch_in_trusted(world)
    _open_pr(world, "f" * 40)
    code, record = world.run("resume", pr=PR)
    assert code == 1 and record["status"] == "blocked" and record["reason"] == sequence.FOREIGN_HEAD
    assert record["gate"]["decision"] == "resume" and world.stage_seen() == []
    assert world.ops(record) == ["bind_issue", "bind_resume", "block"]  # the claim stays while the PR is open
    assert world.pushes == []


def test_resume_binds_the_pr_and_needs_a_new_review_before_push(world):
    head = _branch_in_trusted(world)
    earlier = records.new_run_id(NOW - timedelta(days=1))
    records.write_record(earlier, {"status": "completed", "issue": ISSUE, "pr": PR,
                                   "comments": [{"id": 7001, "kind": "state", "number": PR}]}, world.state)
    state = {"findings": ["review:11"], "head": head, "next_attempt": None, "review_status": "waiting_bots",
             "rounds": 1}
    _open_pr(world, head, [{"id": 7001, "body": "<!-- autodev -->\n<!-- autodev:state " + json.dumps(state) + " -->",
                            "updated_at": "2026-10-13T00:00:00Z"}])
    push = SUBMIT[0]
    script = [push, "git bundle create -q {exchange_dir}/handover.bundle {branch} ^{pinned}", *REVIEW, push]
    world.scenario({"submit": {"script": script, "output": {**submit_output("complete"), "rounds": 1}}})
    code, record = world.run("resume", pr=PR)
    assert code == 0 and record["status"] == "completed", (record.get("reason"), record.get("stages"))
    [seen] = world.stage_seen()
    assert seen["kind"] == "submit" and seen["codes"][0] == 1 and seen["codes"][-1] == 0  # refused, then pushed
    inputs = json.loads((world.run_dir(record) / "state.json").read_text())
    assert inputs["pr"] == PR and inputs["base"] == world.pinned
    assert seen["state_copy"]["inputs-submit.json"]["mode"] == "0o400"
    [pushed] = world.pushes
    assert pushed[-1] == f"{head}:refs/heads/{BRANCH}"
    log = world.log(record)
    refused = [entry for entry in log if entry["op"] == "push" and entry["phase"] == "refused"]
    assert refused and "no validated review" in refused[0]["reason"]
    # The recorded state comment is edited, not duplicated.
    assert ("PATCH", f"{BASE}/issues/comments/7001") in world.api.writes()
    assert not any(path == f"{BASE}/issues/{PR}/comments" for _, path in world.api.writes())
    assert {"id": 7001, "kind": "state", "number": PR} in record["comments"]


def test_a_resume_that_did_not_push_keeps_the_pr_head_in_the_state_comment(world):
    """#347 review round 2: an in-session review imports a commit and moves the trusted branch head
    without a push; the state comment names the PR's head, which the resume checked, not that one."""
    head = _branch_in_trusted(world)
    earlier = records.new_run_id(NOW - timedelta(days=1))
    records.write_record(earlier, {"status": "completed", "issue": ISSUE, "pr": PR,
                                   "comments": [{"id": 7001, "kind": "state", "number": PR}]}, world.state)
    state = {"findings": [], "head": head, "next_attempt": None, "review_status": "waiting_bots", "rounds": 1}
    _open_pr(world, head, [{"id": 7001, "body": "<!-- autodev -->\n<!-- autodev:state " + json.dumps(state) + " -->",
                            "updated_at": "2026-10-13T00:00:00Z"}])
    script = [*COMMIT, "git bundle create -q {exchange_dir}/handover.bundle {branch} ^{pinned}", *REVIEW]
    world.scenario({"submit": {"script": script, "output": {**submit_output("blocked"), "rounds": 1}}})
    code, record = world.run("resume", pr=PR)
    assert code == 1 and record["status"] == "blocked", (record.get("reason"), record.get("stages"))
    moved = git(world.trusted, "rev-parse", f"refs/heads/{BRANCH}")
    assert moved != head and world.pushes == []  # imported for the review, never pushed
    [patch] = [call for call in world.api.calls if call[:2] == ("PATCH", f"{BASE}/issues/comments/7001")]
    written = json.loads(patch[2]["body"].split("<!-- autodev:state ", 1)[1].removesuffix(" -->"))
    assert written["head"] == head and written["review_status"] == "blocked"

def test_an_invalid_resume_output_keeps_the_rounds_and_findings_of_the_state_comment(world):
    """Correctness review #347: the trusted state's rounds and findings survive an invalid output."""
    head = _branch_in_trusted(world)
    earlier = records.new_run_id(NOW - timedelta(days=1))
    records.write_record(earlier, {"status": "completed", "issue": ISSUE, "pr": PR,
                                   "comments": [{"id": 7001, "kind": "state", "number": PR}]}, world.state)
    state = {"findings": ["review:11", "comment:12"], "head": head, "next_attempt": None,
             "review_status": "waiting_bots", "rounds": 2}
    _open_pr(world, head, [{"id": 7001, "body": "<!-- autodev -->\n<!-- autodev:state " + json.dumps(state) + " -->",
                            "updated_at": "2026-10-13T00:00:00Z"}])
    world.scenario({"submit": {"script": ["cat /dev/null"], "output": {"pr_url": PR_URL}}})
    code, record = world.run("resume", pr=PR)
    assert code == 1 and record["status"] == "blocked" and record["stages"]["submit"]["status"] == "invalid"
    [patch] = [call for call in world.api.calls if call[:2] == ("PATCH", f"{BASE}/issues/comments/7001")]
    written = json.loads(patch[2]["body"].split("<!-- autodev:state ", 1)[1].removesuffix(" -->"))
    assert written == {"findings": ["review:11", "comment:12"], "head": head, "next_attempt": None,
                       "review_status": "blocked", "rounds": 2}


def test_resume_after_main_moved_uses_the_merge_base(world):
    head = _branch_in_trusted(world)
    git(world.work, "switch", "-q", "main")
    moved = world.commit({"docs/new.md": "new\n"}, "main moved")
    git(world.work, "push", "-q", "-f", str(world.trusted), f"{moved}:refs/remotes/origin/main")
    old_pinned, world.pinned = world.pinned, moved
    _open_pr(world, head)
    world.scenario({"submit": {"script": ["cat /dev/null"], "output": submit_output("blocked")}})
    code, record = world.run("resume", pr=PR)
    assert record["base"] == old_pinned and record["status"] == "blocked"
    assert json.loads((world.run_dir(record) / "state.json").read_text())["base"] == old_pinned
    log = world.log(record)
    assert [entry["op"] for entry in log if entry["phase"] == "done"][:2] == ["bind_issue", "bind_resume"]


# --- a Codex select stage --------------------------------------------------------------------------

class CodexStub:
    """Stands in for `codex_probes.decide` and `start`: the launch argv is the real
    `codex_sandbox.codex_command`, and the started process writes the select output to ``-o``."""

    def __init__(self, codex: str, *, codex_ok: bool = True):
        self.codex, self.codex_ok, self.paths, self.argv, self.prompts = codex, codex_ok, None, None, []

    def decide(self, codex, paths, **options):
        from types import SimpleNamespace
        from src.schedules import codex_sandbox
        self.paths = paths
        self.argv = codex_sandbox.codex_command(codex, paths, model=options["model"], effort=options["effort"])
        return SimpleNamespace(
            abort=False, codex=self.codex_ok, reason=None if self.codex_ok else "nested_sandbox_unavailable",
            launch=SimpleNamespace(argv=["/usr/bin/sandbox-exec", "-f", "profile", *self.argv]),
            known_values=lambda: [],
            to_record=lambda: {"client_requested": "codex", "client_applied": "codex" if self.codex_ok else "claude",
                               "fallback_reason": None if self.codex_ok else "nested_sandbox_unavailable"})

    def start(self, decision, prompt, *, proxy, stdout, stderr, platform):
        self.prompts.append(prompt)
        output = json.dumps(SELECT_OUTPUT)
        script = f"import sys; open(sys.argv[1], 'w').write(sys.argv[2])"
        return subprocess.Popen([sys.executable, "-c", script, str(self.paths.output), output], stdout=stdout,
                                stderr=stderr, start_new_session=True)


def _codex_select(world, *, available=True, codex_ok=True) -> CodexStub:
    config = copy.deepcopy(world.config)
    config["stages"]["select"] = {"client": "codex", "codex": {"model": "gpt-6.1-sol", "effort": "high"}}
    world.save(config)
    fake = world.base / "codex"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    stub = CodexStub(str(fake), codex_ok=codex_ok)
    world.ctx.codex = str(fake)
    world.ctx.codex_decide, world.ctx.codex_start = stub.decide, stub.start
    world.ctx.codex_availability = lambda kind: {"available": True} if available else \
        {"available": False, "reason": "nested_sandbox_unavailable"}
    return stub


def test_codex_select_stage_uses_m_reasoning_effort_and_the_output_schema(world):
    stub = _codex_select(world)
    code, record = world.run("dry-run")
    assert code == 0 and record["status"] == "completed", record.get("reason")
    assert record["selection"]["issue"] == ISSUE and world.stage_seen() == []  # no Claude session
    argv = stub.argv
    assert argv[argv.index("-m") + 1] == "gpt-6.1-sol" and "model_reasoning_effort=high" in argv
    schema = Path(argv[argv.index("--output-schema") + 1])
    assert schema.name == "schema-select.json" and schema.parent == stub.paths.read_roots[0]
    stage = record["stages"]["select"]
    assert stage["client"] == "codex" and stage["fallback_reason"] is None
    assert stage["execution"]["applied"] == {"client": "codex", "model": "gpt-6.1-sol", "effort": "high",
                                             "method": "session", "model_reported": None, "fallback_reason": None}
    assert stub.prompts and stub.prompts[0].startswith("You are the select stage")
    [client] = record["clients"]
    assert client["step"] == "stage-select" and client["client_applied"] == "codex"


def test_codex_stage_unavailable_falls_back_and_records_the_reason(world):
    stub = _codex_select(world, available=False)
    code, record = world.run("dry-run")
    assert code == 0 and record["status"] == "completed"
    assert stub.argv is None and [item["kind"] for item in world.stage_seen()] == ["select"]
    stage = record["stages"]["select"]
    assert stage["client"] == "claude" and stage["requested_client"] == "codex"
    assert stage["fallback_reason"] == "nested_sandbox_unavailable"
    assert stage["execution"]["applied"]["client"] == "claude"
    assert stage["execution"]["applied"]["fallback_reason"] == "nested_sandbox_unavailable"


def test_codex_decision_fallback_runs_the_claude_session(world):
    _codex_select(world, codex_ok=False)
    code, record = world.run("dry-run")
    assert code == 0 and record["stages"]["select"]["client"] == "claude"
    assert record["stages"]["select"]["fallback_reason"] == "nested_sandbox_unavailable"
    assert [item["kind"] for item in world.stage_seen()] == ["select"]


def test_a_blocker_the_broker_refuses_is_replaced_by_a_generic_one(world):
    world.scenario(stages_plan(implement_script=["cat /dev/null"], implement_output=blocked_output("fixes #12 first")))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked" and "closing keywords" in record["block_refused"]
    [block] = [call for call in world.api.calls if call[1] == f"{BASE}/issues/{ISSUE}/comments"
               and "needs the owner" in call[2]["body"]]
    assert sequence.GENERIC_BLOCKED in block[2]["body"] and "fixes #12" not in block[2]["body"]
    labels = [payload for method, path, payload in world.api.calls if path.endswith("/labels") and method == "POST"]
    assert labels[-1] == {"labels": ["autodev:blocked"]}
    assert world.ops(record)[-2:] == ["block", "unclaim"]


def test_a_blocked_submit_posts_only_a_fixed_phrase(world):
    script = [SUBMIT[0], SUBMIT[1]]
    world.scenario(stages_plan(submit_script=script, submit_out={**submit_output("blocked"),
                                                                 "reason": "The base moved; see the conflict."}))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked" and record["pr"] == PR
    assert record["reason"] == f"{sequence.SUBMIT_BLOCKED}: The base moved; see the conflict."
    [block] = [call for call in world.api.calls if call[1] == f"{BASE}/issues/{ISSUE}/comments"
               and "needs the owner" in call[2]["body"]]
    assert block[2]["body"].endswith(f"- {sequence.SUBMIT_BLOCKED}") and "base moved" not in block[2]["body"]
    assert "unclaim" not in world.ops(record)  # the claim stays while the PR is open
    state = [call for call in world.api.calls if call[1] == f"{BASE}/issues/{PR}/comments"]
    assert state and '"review_status": "blocked"' in state[0][2]["body"]


# --- what the runner checks without a session's help (#347 review) ---------------------------------

def test_a_protected_path_without_autodev_next_blocks_after_the_implement_stage(world):
    """The runner's own change check: a protected (not control) path, a valid unblocked output, a
    review and a pre-mortem, no autodev:next: blocked before the submit stage, nothing exported."""
    protected = ["git switch -q -c {branch}", "mkdir -p docs && echo 'notes' > docs/cloud-runs.md",
                 "git add -A && git commit -q -m 'Note the cloud runs'",
                 "git bundle create -q {exchange_dir}/handover.bundle {branch} ^{pinned}"]
    world.scenario(stages_plan(implement_script=protected + REVIEW + PREMORTEM))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked", record.get("reason")
    assert record["blockers"] == [sequence.PROTECTED_PATH]
    assert [item["path"] for item in record["implement"]["change_check"]] == ["docs/cloud-runs.md"]
    assert [item["kind"] for item in world.stage_seen()] == ["select", "implement"] and world.pushes == []
    assert record["quarantined"].endswith("/final") and "exports" not in record
    assert world.ops(record)[-2:] == ["block", "unclaim"]


def test_a_state_comment_that_cannot_be_written_leaves_the_tick_alone(world):
    """The gate trusts only a written state comment, so the tick's next_attempt waits for one."""
    attempt = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
    world.scenario(stages_plan(submit_out=submit_output("waiting_bots", attempt.strftime("%Y-%m-%dT%H:%M:%SZ"))))
    world.api.fail.append(("POST", f"{BASE}/issues/{PR}/comments", None, 422))
    code, record = world.run("run")
    assert code == 0 and record["status"] == "completed" and record["review_status"] == "waiting_bots"
    assert "state_comment_failed" in record
    tick = world.state / "schedules" / "tick.json"
    assert not tick.exists() or "next_attempt" not in json.loads(tick.read_text())


def test_a_block_whose_label_fails_posts_no_second_comment(world):
    """Broker.block wrote its comment and failed on the label: no generic comment follows."""
    world.scenario(stages_plan(implement_script=["cat /dev/null"], implement_output=blocked_output("stop here")))
    world.api.fail.append(("POST", f"{BASE}/issues/{ISSUE}/labels", {"labels": ["autodev:blocked"]}, 422))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked" and "block_failed" in record
    blocks = [call for call in world.api.calls if call[1] == f"{BASE}/issues/{ISSUE}/comments"
              and "needs the owner" in call[2]["body"]]
    assert len(blocks) == 1 and "- stop here" in blocks[0][2]["body"]


def test_a_submit_output_naming_another_pull_request_is_invalid(world):
    """Security review #347: the PR link comes from the broker; a session's other link blocks."""
    other = f"https://github.com/{REPOSITORY}/pull/999"
    world.scenario(stages_plan(submit_out={**submit_output(), "pr_url": other}))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked" and record["pr"] == PR and record["pr_url"] == PR_URL
    assert record["submit_problems"] == ["pr_url: not this run's pull request"]
    assert record["pr_url_reported"] == other and record["review_status"] == "blocked"
    [argv] = world.notifications
    assert "999" not in argv[-1] and PR_URL in argv[-1]
    assert "unclaim" not in world.ops(record)  # the claim stays while the PR is open


def test_a_submit_output_with_another_head_is_invalid_and_the_state_names_the_pushed_head(world):
    world.scenario(stages_plan(submit_out={**submit_output(), "head_sha": "e" * 40}))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked"
    assert record["submit_problems"] == ["head_sha: not the head the broker pushed"]
    head = git(world.trusted, "rev-parse", f"refs/heads/{BRANCH}")
    [state] = [call for call in world.api.calls if call[1] == f"{BASE}/issues/{PR}/comments"
               and "autodev:state" in call[2]["body"]]
    assert f'"head": "{head}"' in state[2]["body"] and "e" * 40 not in state[2]["body"]


def test_a_completed_submit_without_a_pull_request_is_blocked(world):
    world.scenario(stages_plan(submit_script=["cat /dev/null"],
                               submit_out={**submit_output(), "pr_url": None, "head_sha": None}))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked" and record["pr"] is None and record["pr_url"] is None
    assert record["submit_problems"] == ["no pull request was opened"]
    assert world.ops(record)[-2:] == ["block", "unclaim"]


def test_a_push_refused_for_a_control_path_in_the_submit_stage_is_exported(world):
    control = ["mkdir -p src/schedules && echo 'VALUE = 2' > src/schedules/extra.py",
               "git add -A && git commit -q -m 'Touch a control path'",
               "git bundle create -q {exchange_dir}/handover.bundle {branch} ^{pinned}",
               """{worker_cmd} review '{"head": "{head}", "verification": true}'""",
               """{worker_cmd} wait '{"handle": "verify-1"}'""",
               SUBMIT[0]]
    world.scenario(stages_plan(submit_script=control,
                               submit_out={**submit_output("blocked"), "pr_url": None, "head_sha": None}))
    code, record = world.run("run")
    assert code == 1 and record["status"] == "blocked" and record["pr"] is None
    [seen] = [item for item in world.stage_seen() if item["kind"] == "submit"]
    assert seen["codes"][-1] == 1  # the broker refused the push
    refused = [entry for entry in world.log(record) if entry["op"] == "push" and entry["phase"] == "refused"]
    assert refused and "src/schedules/extra.py (control path)" in refused[0]["reason"]
    assert record["blockers"] == [sequence.SUBMIT_BLOCKED, sequence.CONTROL_PATH]
    patch = Path(record["exports"]["patch"])
    assert patch.is_file() and "src/schedules/extra.py" in patch.read_text()
    assert world.pushes == [] and world.ops(record)[-2:] == ["block", "unclaim"]
