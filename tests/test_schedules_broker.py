"""The run-bound GitHub broker (#342): named refusals, with fakes for the network, git and the keychain."""
import json
import subprocess
from pathlib import Path

import pytest

from src.schedules import broker as broker_module
from src.schedules import github
from src.schedules import scan as scan_module
from src.schedules.broker import (MARKER, PREMORTEM_RECORD, REVIEW_RECORD, Broker, BrokerRefused, Credentials,
                                  CredentialsMissing, RiskFinding, Run, check_risk_issue, push_command, register)
from src.schedules.gate import CLAIM_LABEL, OWNER_ID, STALE_CLAIM_COMMENT
from src.schedules.paths import ChangeCheck, Reason, Refusal
from src.schedules.socket_service import Connection, Registry

REPOSITORY = "IEZhu/Agents"
ISSUE = 342
BASE = "a" * 40
HEAD = "b" * 40
OTHER = "c" * 40
SLUG = "broker"
BRANCH = f"claude/autodev-{ISSUE}-{SLUG}"
TOKEN = "fake-broker-token-for-tests"
KEY = "fake deploy key material for tests"
RUN_ID = "20261013T120000Z-abcdef"
SESSION = {"session_root": lambda: 1, "lineage": lambda pid: {pid, 1}}
# The real GraphQL shape (checked on PR #365): a Bot's login has no "[bot]" suffix; REST says coderabbitai[bot].
CODERABBIT = {"__typename": "Bot", "login": "coderabbitai", "databaseId": 136622811}
OWNER = {"__typename": "User", "login": "WonderMr", "databaseId": OWNER_ID}


class FakeGit:
    path = Path("/nonexistent/trusted.git")

    def __init__(self, heads=None, refusals=(), messages="Broker\n\nCloses #342\n",
                 diff="diff --git a/src/x.py b/src/x.py\n+print('ok')\n"):
        self.heads = {BRANCH: HEAD} if heads is None else heads
        self.refusals = tuple(refusals)
        self.messages, self.diff = messages, diff
        self.pushes = []

    def branch_head(self, branch):
        return self.heads.get(branch)

    def check(self, base, head, labels):
        return ChangeCheck(base, head, (head,), self.refusals)

    def outgoing(self, base, head):
        return self.messages, self.diff

    def push(self, argv, env, timeout):
        self.pushes.append((argv, env))
        return subprocess.CompletedProcess(argv, 0, b"", b"")


class FakeGitHub:
    """Answers the broker's REST and GraphQL calls and records them; never touches the network."""

    def __init__(self, threads=(), comments=None, pulls=None, statuses=None, link=None, graphql_errors=False):
        self.calls = []
        self.threads = list(threads)
        self.comments = comments or {}
        self.pulls = pulls or {}
        self.statuses = statuses or {}  # (method, path suffix) -> HTTP status
        self.link = link
        self.graphql_errors = graphql_errors
        self.next_id = 9000

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        payload = json.loads(body) if body else None
        self.calls.append((method, url, payload))
        path = url.removeprefix(github.API).split("?")[0]
        self.next_id += 1
        for (status_method, suffix), status in self.statuses.items():
            if method == status_method and path.endswith(suffix):
                return status, {}, b'{"message": "error"}'
        if path == "/graphql":
            if self.graphql_errors and "resolveReviewThread" in payload["query"]:
                return 200, {}, json.dumps({"data": None, "errors": [{"message": "no"}]}).encode()
            if "resolveReviewThread" in payload["query"]:
                return 200, {}, json.dumps({"data": {"resolveReviewThread": {"thread": {"id": "x"}}}}).encode()
            return 200, {}, json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.threads}}}}}).encode()
        if method == "GET" and "/issues/comments/" in path:
            number = self.comments.get(int(path.rsplit("/", 1)[1]))
            if number is None:
                return 404, {}, b"{}"
            return 200, {}, json.dumps({"issue_url": f"{github.API}/repos/{REPOSITORY}/issues/{number}"}).encode()
        if method == "GET" and path.startswith(f"/repos/{REPOSITORY}/pulls/") and path.rsplit("/", 1)[1].isdigit():
            number = int(path.rsplit("/", 1)[1])
            return 200, {}, json.dumps(self.pulls.get(number) or pull(number)).encode()
        if method == "POST" and path.endswith("/pulls"):
            return 201, {}, json.dumps({"number": 501, "id": self.next_id, "html_url": "https://github.com/x/pull/501"}).encode()
        if method == "POST" and path.endswith("/issues"):
            return 201, {}, json.dumps({"number": 600 + len(self.writes()), "id": self.next_id}).encode()
        if method == "GET":
            return 200, ({"Link": self.link} if self.link else {}), json.dumps([{"id": 1}]).encode()
        return 200, {}, json.dumps({"id": self.next_id}).encode()

    def writes(self):
        return [call for call in self.calls if call[0] != "GET" and not call[1].endswith("/graphql")
                or call[1].endswith("/graphql") and "mutation" in call[2]["query"]]


def pull(number, branch=f"claude/autodev-{ISSUE}-resumed", state="open"):
    return {"number": number, "id": 77, "state": state, "labels": [{"name": "autodev"}],
            "head": {"ref": branch, "repo": {"full_name": REPOSITORY}}}


@pytest.fixture
def credentials(tmp_path):
    key = tmp_path / "keys" / "deploy_key"
    key.parent.mkdir()
    key.write_text(KEY + "\n")
    (key.parent / "github_known_hosts").write_text("github.com ssh-ed25519 placeholder\n")
    return Credentials(key, key.parent / "github_known_hosts", lambda: TOKEN)


def make(tmp_path, credentials, *, mode="run", pr=None, git=None, api=None, reviewed=HEAD, premortem=None,
         earlier_records=(), run_dir=None):
    run = Run(RUN_ID, REPOSITORY, ISSUE, BASE, mode, frozenset({"autodev:ready"}), pr)
    api = api or FakeGitHub()
    found = Broker(run, credentials=credentials, git=git or FakeGit(), run_dir=run_dir or tmp_path, transport=api,
                   reviewed_head=lambda: reviewed, premortem=lambda: premortem or {},
                   earlier_records=lambda: list(earlier_records))
    registry = Registry()
    register(registry, found)
    return found, registry, api


def ask(registry, request_type, args=None):
    line = json.dumps({"type": request_type, "args": args or {}}).encode()
    return registry.handle(line, Connection(4242), **SESSION)


def refused(response, text):
    assert response["ok"] is False, response
    assert text in response["error"], response["error"]


@pytest.fixture
def setup(tmp_path, credentials):
    return make(tmp_path, credentials)


# --- the registry ------------------------------------------------------------------------------------

def test_only_named_operations_are_registered(setup):
    _, registry, _ = setup
    assert registry.types() == sorted(["gh.push", "gh.pr_create", "gh.pr_edit", "gh.comment", "gh.comment_edit",
                                       "gh.label", "gh.threads", "gh.resolve_thread", "gh.read"])


@pytest.mark.parametrize("request_type", ["gh.cleanup", "gh.select", "gh.bind_resume", "gh.risk_issue",
                                          "gh.create_risk_issues", "gh.selection"])
def test_refuses_a_runner_internal_operation_through_the_socket(setup, request_type):
    _, registry, api = setup
    refused(ask(registry, request_type, {}), "unknown request type")
    assert api.calls == []


def test_refuses_a_caller_outside_the_stage_session(setup):
    _, registry, api = setup
    line = json.dumps({"type": "gh.comment", "args": {"number": ISSUE, "body": MARKER}}).encode()
    response = registry.handle(line, Connection(4242), session_root=lambda: 1, lineage=lambda pid: {pid, 7})
    refused(response, "not a process of the current stage session")
    assert api.calls == []


# --- push ----------------------------------------------------------------------------------------------

def test_push_of_the_reviewed_imported_head_succeeds_and_is_logged(setup, tmp_path):
    broker, registry, _ = setup
    response = ask(registry, "gh.push", {"slug": SLUG, "head": HEAD})
    assert response == {"ok": True, "result": {"branch": BRANCH, "head": HEAD}}
    argv, _ = broker.git.pushes[0]
    assert argv[-1] == f"{HEAD}:refs/heads/{BRANCH}" and argv[-2] == f"git@github.com:{REPOSITORY}.git"
    log = [json.loads(line) for line in (tmp_path / "broker.jsonl").read_text().splitlines()]
    assert [(entry["op"], entry["phase"]) for entry in log] == [("push", "intent"), ("push", "done")]
    assert log[-1]["id"] == HEAD


def test_refuses_a_push_to_main_or_another_issues_branch(setup):
    broker, registry, _ = setup
    refused(ask(registry, "gh.push", {"slug": SLUG, "head": HEAD, "branch": "main"}), "unknown argument: 'branch'")
    refused(ask(registry, "gh.push", {"slug": SLUG, "head": HEAD, "issue": 341}), "unknown argument: 'issue'")
    refused(ask(registry, "gh.push", {"slug": "refs/heads/main", "head": HEAD}), "contains characters")
    refused(ask(registry, "gh.push", {"slug": "x:main", "head": HEAD}), "contains characters")
    broker.git.heads[f"claude/autodev-{ISSUE}-other"] = HEAD
    assert ask(registry, "gh.push", {"slug": SLUG, "head": HEAD})["ok"] is True
    refused(ask(registry, "gh.push", {"slug": "other", "head": HEAD}), f"this run pushes only {BRANCH}")
    assert len(broker.git.pushes) == 1


def test_refuses_a_head_without_a_validated_review(tmp_path, credentials):
    for reviewed in (None, OTHER):
        broker, registry, _ = make(tmp_path, credentials, reviewed=reviewed)
        refused(ask(registry, "gh.push", {"slug": SLUG, "head": HEAD}), "no validated review covers head")
        assert broker.git.pushes == []


def test_refuses_commits_not_from_the_trusted_repository(tmp_path, credentials):
    for heads in ({}, {BRANCH: OTHER}):
        broker, registry, _ = make(tmp_path, credentials, git=FakeGit(heads=heads))
        refused(ask(registry, "gh.push", {"slug": SLUG, "head": HEAD}), "is not the imported head")
        assert broker.git.pushes == []


@pytest.mark.parametrize("reason", [Reason.PROTECTED_PATH, Reason.CONTROL_PATH])
def test_refuses_a_protected_or_control_path(tmp_path, credentials, reason):
    git = FakeGit(refusals=[Refusal(HEAD, ".github/autodev-control-paths", reason)])
    broker, registry, _ = make(tmp_path, credentials, git=git)
    refused(ask(registry, "gh.push", {"slug": SLUG, "head": HEAD}), "the change check refuses")
    assert git.pushes == []
    assert broker.log.entries[-1]["phase"] == "refused"


def test_refuses_a_push_whose_diff_holds_a_secret(tmp_path, credentials):
    git = FakeGit(diff=f"+leaked = '{KEY}'\n")
    _, registry, _ = make(tmp_path, credentials, git=git)
    refused(ask(registry, "gh.push", {"slug": SLUG, "head": HEAD}), "secret-like text found")
    assert git.pushes == []


@pytest.mark.parametrize("message", ["Broker\n\nFixes #341\n", "Broker\n\n_Closes #12_\n",
                                     "Broker\n\n__Fixes #12__\n"])
def test_refuses_a_push_whose_commit_closes_another_issue(tmp_path, credentials, message):
    git = FakeGit(messages=message)
    broker, registry, _ = make(tmp_path, credentials, git=git)
    refused(ask(registry, "gh.push", {"slug": SLUG, "head": HEAD}), f"may name only issue #{ISSUE}")
    assert git.pushes == []
    assert broker.log.entries[-1]["phase"] == "refused"


def test_a_diff_with_a_closing_keyword_for_another_issue_is_pushed(tmp_path, credentials):
    """GitHub honours closing keywords in commit messages and the PR text, never in a diff."""
    git = FakeGit(diff='+    ("Fixes #341", "closes #12", "_Closes #12_")\n')
    _, registry, _ = make(tmp_path, credentials, git=git)
    assert ask(registry, "gh.push", {"slug": SLUG, "head": HEAD})["ok"]
    assert len(git.pushes) == 1


@pytest.mark.parametrize("case", ["branch", "imported", "review"])
def test_early_push_refusals_are_logged(tmp_path, credentials, case):
    git = FakeGit(heads={} if case == "imported" else None)
    broker, registry, _ = make(tmp_path, credentials, git=git, reviewed=None if case == "review" else HEAD)
    if case == "branch":
        broker.branch = f"claude/autodev-{ISSUE}-other"
    assert ask(registry, "gh.push", {"slug": SLUG, "head": HEAD})["ok"] is False
    log = [json.loads(line) for line in (tmp_path / "broker.jsonl").read_text().splitlines()]
    assert log[-1]["op"] == "push" and log[-1]["phase"] == "refused" and log[-1]["head"] == HEAD


def test_refuses_a_slug_longer_than_a_branch_takes(setup):
    _, registry, _ = setup
    refused(ask(registry, "gh.push", {"slug": "a" * 61, "head": HEAD}), "longer than 60 characters")
    refused(ask(registry, "gh.push", {"slug": "a" * 60, "head": HEAD}), "is not the imported head")


def test_the_push_command_uses_only_the_deploy_key_and_pinned_host_keys(credentials, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    argv, env = push_command(Path("/runner/trusted.git"), REPOSITORY, HEAD, ISSUE, SLUG, credentials)
    assert argv[-6:] == ["push", "--porcelain", "--no-follow-tags", "--no-signed", f"git@github.com:{REPOSITORY}.git",
                         f"{HEAD}:refs/heads/{BRANCH}"]
    assert "-C" in argv and argv[argv.index("-C") + 1] == "/runner/trusted.git"
    assert env["GIT_SSH_COMMAND"] == (
        f"ssh -F /dev/null -i {credentials.deploy_key} -o IdentitiesOnly=yes -o IdentityAgent=none "
        f"-o UserKnownHostsFile={credentials.known_hosts} -o GlobalKnownHostsFile=/dev/null "
        "-o StrictHostKeyChecking=yes -o BatchMode=yes -o ControlMaster=no -o ControlPath=none")
    assert "SSH_AUTH_SOCK" not in env
    assert env["GIT_CONFIG_GLOBAL"] and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert TOKEN not in json.dumps([argv, env])


@pytest.mark.parametrize("key, hosts", [("/keys dir/deploy_key", "/keys/github_known_hosts"),
                                        ("/keys/deploy_key", "/keys\tx/github_known_hosts")])
def test_the_push_command_refuses_a_path_with_whitespace(key, hosts):
    with pytest.raises(ValueError, match="without whitespace"):
        broker_module.ssh_command(Path(key), Path(hosts))


def test_a_real_run_refuses_a_key_directory_with_whitespace(tmp_path):
    directory = tmp_path / "key dir"
    directory.mkdir()
    (directory / "deploy_key").write_text(KEY + "\n")
    (directory / "github_known_hosts").write_text("github.com ssh-ed25519 placeholder\n")
    spaced = Credentials(directory / "deploy_key", directory / "github_known_hosts", lambda: TOKEN)
    with pytest.raises(CredentialsMissing, match="without whitespace"):
        make(tmp_path, spaced)


def test_trusted_git_reads_the_branch_head_and_the_outgoing_text(tmp_path):
    """The default git adapter on a real temporary repository; it pushes nothing here."""
    repository = tmp_path / "trusted"
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "HOME": str(tmp_path),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "T",
           "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.org"}

    def git(*args):
        return subprocess.run(["git", "-C", str(repository), *args], env=env, check=True, capture_output=True,
                              text=True).stdout.strip()
    repository.mkdir()
    git("init", "-q", "-b", "main")
    (repository / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "Base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", BRANCH)
    (repository / "a.txt").write_text("two\n")
    git("commit", "-q", "-am", "Change\n\nCloses #342")
    head = git("rev-parse", "HEAD")
    trusted = broker_module.TrustedGit(repository)
    assert trusted.branch_head(BRANCH) == head
    assert trusted.branch_head(f"claude/autodev-{ISSUE}-missing") is None
    messages, diff = trusted.outgoing(base, head)
    assert "Closes #342" in messages and "+two" in diff and "-one" in diff and "+two" not in messages


def test_trusted_git_check_reads_the_lists_at_the_pinned_commit(tmp_path):
    """TrustedGit.check on a real repository with control and protected path lists."""
    repository = tmp_path / "trusted"
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "HOME": str(tmp_path),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "T",
           "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.org"}

    def git(*args):
        return subprocess.run(["git", "-C", str(repository), *args], env=env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(name, text, message):
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git("add", name)
        git("commit", "-q", "-m", message)
        return git("rev-parse", "HEAD")
    repository.mkdir()
    git("init", "-q", "-b", "main")
    commit(".github/autodev-protected-paths", "pyproject.toml\n", "Protected")
    commit("pyproject.toml", "[project]\n", "Project")
    base = commit(".github/autodev-control-paths", "CLAUDE.md\n", "Control")
    git("checkout", "-q", "-b", BRANCH)
    allowed = commit("src/x.py", "print('ok')\n", "Code")
    protected = commit("pyproject.toml", "[project]\nname = 'x'\n", "Protected change")
    git("checkout", "-q", "-b", "control", allowed)
    control = commit("CLAUDE.md", "changed\n", "Control change")
    git("checkout", "-q", "-b", "lists", allowed)
    lists = commit(".github/autodev-control-paths", "", "Empty the control list")
    trusted = broker_module.TrustedGit(repository)
    assert trusted.check(base, allowed, {"autodev:ready"}).allowed
    assert [refusal.reason for refusal in trusted.check(base, protected, {"autodev:ready"}).refusals] == [
        Reason.PROTECTED_PATH]
    assert trusted.check(base, protected, {"autodev:ready", "autodev:next"}).allowed
    for head in (control, lists):
        verdict = trusted.check(base, head, {"autodev:next"})
        assert [refusal.reason for refusal in verdict.refusals] == [Reason.CONTROL_PATH]
    assert [refusal.reason for refusal in trusted.check(allowed, base, ()).refusals] == [Reason.NOT_DESCENDANT]
    # A resume run bound to a merge base still reads the lists at pinned (#347): here a later
    # commit whose control list no longer names CLAUDE.md, while the lists at `base` refuse it.
    git("checkout", "-q", "-b", "relisted", base)
    relisted = commit(".github/autodev-control-paths", "OTHER.md\n", "Another control list")
    later = broker_module.TrustedGit(repository, lists_at=relisted)
    assert later.check(base, control, {"autodev:next"}).allowed
    assert not broker_module.TrustedGit(repository).check(base, control, {"autodev:next"}).allowed


# --- PRs -----------------------------------------------------------------------------------------------

def test_pr_create_and_edit_only_this_runs_pr(setup):
    broker, registry, api = setup
    refused(ask(registry, "gh.pr_create", {"title": "Broker", "body": "Closes #342"}), "has pushed no branch")
    assert ask(registry, "gh.push", {"slug": SLUG, "head": HEAD})["ok"]
    created = ask(registry, "gh.pr_create", {"title": "Broker", "body": "Closes #342"})
    assert created["ok"] and created["result"]["number"] == 501 and broker.run.pr == 501
    assert api.calls[-1][2]["head"] == BRANCH and api.calls[-1][2]["base"] == "main"
    refused(ask(registry, "gh.pr_create", {"title": "Again", "body": "x"}), "already has PR #501")
    assert ask(registry, "gh.pr_edit", {"body": "Closes #342\n\nMore."})["ok"]
    assert api.calls[-1][0] == "PATCH" and api.calls[-1][1].endswith(f"/repos/{REPOSITORY}/pulls/501")
    done = broker.log.done("pr_create")
    assert done and done[0]["number"] == 501 and done[0]["id"]


@pytest.mark.parametrize("request_type, args, error", [
    ("gh.merge", {"number": 501}, "unknown request type"),
    ("gh.pr_merge", {}, "unknown request type"),
    ("gh.pr_close", {}, "unknown request type"),
    ("gh.pr_edit", {"state": "closed"}, "unknown argument: 'state'"),
    ("gh.pr_edit", {"base": "main"}, "unknown argument: 'base'"),
    ("gh.delete_branch", {"slug": SLUG}, "unknown request type"),
    ("gh.branch_delete", {}, "unknown request type"),
    ("gh.issue_edit", {"number": ISSUE, "body": "x"}, "unknown request type"),
    ("gh.comment_edit", {"comment_id": 1, "body": MARKER, "issue": ISSUE}, "unknown argument: 'issue'"),
])
def test_refuses_a_merge_a_pr_close_a_branch_delete_and_an_issue_body_edit(tmp_path, credentials, request_type,
                                                                            args, error):
    _, registry, api = make(tmp_path, credentials, pr=501)
    refused(ask(registry, request_type, args), error)
    assert api.writes() == []


@pytest.mark.parametrize("body", ["Fixes #341", "closes: #1", "Resolves IEZhu/Agents#341", "fixed other/repo#342",
                                  "Closes https://github.com/IEZhu/Agents/issues/7", "resolve GH-12",
                                  "Closes #342 and fixes #343", "_Closes #12_", "__Fixes #12__", "*Fixes #12*",
                                  "Closes https://www.github.com/IEZhu/Agents/issues/7",
                                  "Fixes www.github.com/IEZhu/Agents/issues/7", "closes github.com/o/r/issues/7"])
def test_refuses_closing_keywords_for_another_issue(tmp_path, credentials, body):
    _, registry, api = make(tmp_path, credentials, pr=501)
    refused(ask(registry, "gh.pr_edit", {"body": body}), f"may name only issue #{ISSUE}")
    assert api.writes() == []


def test_closing_keywords_for_this_runs_issue_pass(tmp_path, credentials):
    _, registry, _ = make(tmp_path, credentials, pr=501)
    assert ask(registry, "gh.pr_edit", {"body": "Closes #342. Fixes IEZhu/Agents#342. Mentions #341. "
                                                "Prefixes #12 are no keyword."})["ok"]


@pytest.mark.parametrize("args", [
    {"body": "@coderabbitai ignore"}, {"body": "Thanks\n\n@copilot please change X"}, {"title": "Ping @WonderMr"},
    {"body": "@IEZhu/maintainers"}, {"body": "(@coderabbitai approve)"}, {"body": "\uff20coderabbitai pause"},
    {"body": "<!-- autodev:state {\"rounds\": 0} -->"}, {"body": "x\n<!--autodev:selection-->"},
    {"title": "<!-- AUTODEV:state -->"}])
def test_refuses_a_pr_text_with_a_mention_or_a_sub_marker(tmp_path, credentials, args):
    _, registry, api = make(tmp_path, credentials, pr=501)
    refused(ask(registry, "gh.pr_edit", args), "may not hold")
    assert api.writes() == []


def test_an_email_address_is_no_mention(tmp_path, credentials):
    _, registry, _ = make(tmp_path, credentials, pr=501)
    assert ask(registry, "gh.pr_edit", {"body": "Contact noreply@anthropic.com."})["ok"]


def test_refuses_a_pr_text_with_a_secret(tmp_path, credentials):
    _, registry, api = make(tmp_path, credentials, pr=501)
    refused(ask(registry, "gh.pr_edit", {"body": f"token {TOKEN}"}), "secret-like text found")
    assert api.writes() == []


# --- comments ------------------------------------------------------------------------------------------

def test_comments_only_on_this_runs_issue_and_pr(tmp_path, credentials):
    broker, registry, api = make(tmp_path, credentials, pr=501)
    assert ask(registry, "gh.comment", {"number": ISSUE, "body": f"{MARKER}\nState."})["ok"]
    assert ask(registry, "gh.comment", {"number": 501, "body": f"{MARKER}\nState."})["ok"]
    for number in (341, 500):
        refused(ask(registry, "gh.comment", {"number": number, "body": f"{MARKER}\nx"}),
                "neither this run's issue nor its PR")
    assert len(api.writes()) == 2 and all(entry["id"] for entry in broker.log.done("comment"))


@pytest.mark.parametrize("body, error", [
    ("/agent implement", "/agent"),
    (f"{MARKER}\n/agent run", "/agent"),
    ("Plain text without the marker", "must start with the line"),
    (f" {MARKER}\nlate marker", "must start with the line"),
    ("@coderabbitai review please", "must start with the line"),
    ("@coderabbitai  review", "must start with the line"),
    ("@CodeRabbitAI review", "must start with the line"),
    ("@coderabbitai review\n", "must start with the line"),
    ("@coderabbitai approve", "must start with the line"),
])
def test_refuses_agent_commands_a_missing_marker_and_near_variants_of_bot_commands(tmp_path, credentials, body,
                                                                                   error):
    _, registry, api = make(tmp_path, credentials, pr=501)
    refused(ask(registry, "gh.comment", {"number": 501, "body": body}), error)
    assert api.writes() == []


def test_an_exact_bot_command_only_on_this_runs_pr(tmp_path, credentials):
    _, registry, api = make(tmp_path, credentials, pr=501)
    assert ask(registry, "gh.comment", {"number": 501, "body": "@coderabbitai review"})["ok"]
    refused(ask(registry, "gh.comment", {"number": ISSUE, "body": "@coderabbitai review"}), "only on this run's PR")
    assert len(api.writes()) == 1


def test_refuses_a_comment_that_looks_like_a_secret(tmp_path, credentials):
    _, registry, api = make(tmp_path, credentials, pr=501)
    secret = "ghp_" + "abcd" * 9
    refused(ask(registry, "gh.comment", {"number": ISSUE, "body": f"{MARKER}\n{secret}"}), "secret-like text found")
    assert api.writes() == []


@pytest.mark.parametrize("mode", ["run", "dry-run"])
def test_a_decoy_marker_in_outgoing_text_calls_the_tripwire(tmp_path, credentials, mode):
    marker = "Dc" + "x7Kq" * 11
    tripped = []
    run = Run(RUN_ID, REPOSITORY, ISSUE, BASE, mode, frozenset({"autodev:ready"}), 501)
    broker = Broker(run, credentials=credentials, git=FakeGit(), run_dir=tmp_path, transport=FakeGitHub(),
                    reviewed_head=lambda: HEAD, premortem=lambda: {}, earlier_records=lambda: [],
                    decoys=[scan_module.KnownSecret("decoy credentials", marker)], on_decoy=tripped.append)
    with pytest.raises(BrokerRefused, match="secret-like text found"):
        broker._scan("comment", f"{MARKER}\nfound {marker}")
    assert [[hit.name for hit in hits] for hits in tripped] == [["decoy credentials"]]
    with pytest.raises(BrokerRefused):
        broker._scan("comment", "ghp_" + "abcd" * 9)  # a secret, but no decoy: no tripwire
    assert len(tripped) == 1
    assert marker not in (tmp_path / broker_module.LOG_NAME).read_text()


def test_refuses_editing_a_comment_not_in_the_log(tmp_path, credentials):
    broker, registry, api = make(tmp_path, credentials, pr=501)
    refused(ask(registry, "gh.comment_edit", {"comment_id": 12345, "body": f"{MARKER}\nx"}),
            "was not written by this run's gh.comment")
    created = ask(registry, "gh.comment", {"number": ISSUE, "body": f"{MARKER}\nfirst"})["result"]["id"]
    assert ask(registry, "gh.comment_edit", {"comment_id": created, "body": f"{MARKER}\nsecond"})["ok"]
    refused(ask(registry, "gh.comment_edit", {"comment_id": created, "body": "no marker"}), "must start with the line")
    refused(ask(registry, "gh.comment_edit", {"comment_id": created, "body": f"{MARKER}\n@coderabbitai resolve"}),
            "@mention")
    assert [call[0] for call in api.writes()] == ["POST", "PATCH"]
    assert broker.log.entries[-1]["phase"] == "refused"


STATE_BODY = (f"{MARKER}\n<!-- autodev:state {{\"rounds\": 0, \"next_attempt\": \"2000-01-01T00:00:00Z\", "
              f"\"findings\": [], \"head\": \"{HEAD}\"}} -->")


def test_refuses_editing_a_recorded_state_comment_or_the_selection_comment(tmp_path, credentials, monkeypatch):
    """State and selection comments are runner-internal: neither an earlier run's recorded state
    comment nor this run's selection comment may be edited through the socket."""
    monkeypatch.setattr(broker_module.records, "trusted_comment_ids", lambda: {77})
    broker, registry, api = make(tmp_path, credentials, pr=501, api=FakeGitHub(comments={77: 501}))
    refused(ask(registry, "gh.comment_edit", {"comment_id": 77, "body": STATE_BODY}),
            "was not written by this run's gh.comment")
    selection = broker.select(f"{MARKER}\n<!-- autodev:selection -->\nTaking #342.")["id"]
    refused(ask(registry, "gh.comment_edit", {"comment_id": selection, "body": f"{MARKER}\nchanged"}),
            "was not written by this run's gh.comment")
    created = ask(registry, "gh.comment", {"number": 501, "body": f"{MARKER}\nmine"})["result"]["id"]
    refused(ask(registry, "gh.comment_edit", {"comment_id": created, "body": STATE_BODY}), "sub-marker")
    assert [call[0] for call in api.writes()] == ["POST", "POST", "POST"]  # selection, claim label, own comment


@pytest.mark.parametrize("body", [
    STATE_BODY, f"{MARKER}\n<!-- autodev:selection -->\nTaking #342.", f"{MARKER}\n<!--AutoDev:state {{}} -->",
    f"{MARKER}\n@coderabbitai resolve", f"{MARKER}\n@coderabbitai approve", f"{MARKER}\nplease @copilot fix X",
    f"{MARKER}\ncc @WonderMr", f"{MARKER}\n@\u200bcoderabbitai resolve", f"{MARKER}\n\uff20copilot change",
    # Review round 2: a non-ASCII or backtick prefix, an address-like word, an HTML entity.
    f"{MARKER}\n\u00e9@coderabbitai resolve", f"{MARKER}\n`@coderabbitai resolve", f"{MARKER}\nx@coderabbitai resolve",
    f"{MARKER}\n_@copilot fix it", f"{MARKER}\n&#64;coderabbitai resolve", f"{MARKER}\n&#x40;copilot change"])
def test_refuses_a_marked_comment_with_a_sub_marker_or_a_mention(tmp_path, credentials, body):
    _, registry, api = make(tmp_path, credentials, pr=501)
    for number in (ISSUE, 501):
        refused(ask(registry, "gh.comment", {"number": number, "body": body}), "may not hold")
    assert api.writes() == []


# --- labels --------------------------------------------------------------------------------------------

def test_run_labels_on_this_runs_issue_and_pr(tmp_path, credentials):
    _, registry, api = make(tmp_path, credentials, pr=501)
    assert ask(registry, "gh.label", {"number": 501, "label": "autodev", "action": "add"})["ok"]
    assert ask(registry, "gh.label", {"number": ISSUE, "label": "autodev:blocked", "action": "add"})["ok"]
    assert ask(registry, "gh.label", {"number": 501, "label": "autodev:blocked", "action": "remove"})["ok"]
    assert api.writes()[-1][0] == "DELETE" and api.writes()[-1][1].endswith("/501/labels/autodev%3Ablocked")


@pytest.mark.parametrize("args, error", [
    ({"number": 341, "label": "autodev", "action": "add"}, "neither this run's issue nor its PR"),
    ({"number": 341, "label": "autodev:blocked", "action": "remove"}, "neither this run's issue nor its PR"),
    ({"number": 501, "label": "autodev", "action": "remove"}, "may only be added, and only to this run's PR"),
    ({"number": ISSUE, "label": "autodev", "action": "add"}, "may only be added, and only to this run's PR"),
    ({"number": ISSUE, "label": CLAIM_LABEL, "action": "remove"}, "may be set or removed"),
    ({"number": ISSUE, "label": CLAIM_LABEL, "action": "add"}, "may be set or removed"),
    ({"number": 501, "label": CLAIM_LABEL, "action": "remove"}, "may be set or removed"),
    ({"number": ISSUE, "label": "autodev:ready", "action": "remove"}, "may be set or removed"),
    ({"number": ISSUE, "label": "autodev:next", "action": "add"}, "may be set or removed"),
    ({"number": ISSUE, "label": "autodev:skip", "action": "remove"}, "may be set or removed"),
    ({"number": ISSUE, "label": "autodev:risk", "action": "add"}, "may be set or removed"),
    ({"number": ISSUE, "label": "new-label", "action": "add"}, "may be set or removed"),
    ({"number": ISSUE, "label": "autodev", "action": "delete"}, "contains characters"),
    ({"number": ISSUE, "label": "autodev", "action": "create"}, "contains characters"),
])
def test_refuses_labels_on_another_issue_owner_labels_and_other_labels(tmp_path, credentials, args, error):
    _, registry, api = make(tmp_path, credentials, pr=501)
    refused(ask(registry, "gh.label", args), error)
    assert api.writes() == []


@pytest.mark.parametrize("request_type", ["gh.create_label", "gh.delete_label", "gh.label_create", "gh.labels"])
def test_refuses_creating_or_deleting_repository_labels(setup, request_type):
    _, registry, api = setup
    refused(ask(registry, request_type, {"label": "autodev"}), "unknown request type")
    assert api.calls == []


# --- review threads ------------------------------------------------------------------------------------

def thread(thread_id, author, body="Finding"):
    return {"id": thread_id, "isResolved": False, "path": "src/x.py", "line": 1,
            "comments": {"nodes": [{"databaseId": 1, "body": body, "author": author}]}}


THREADS = [thread("PRRT_bot", CODERABBIT),
           thread("PRRT_copilot", {"__typename": "Bot", "login": "copilot-pull-request-reviewer",
                                   "databaseId": 175728472}),
           thread("PRRT_agent", OWNER, f"{MARKER}\nnote"),
           thread("PRRT_owner", OWNER),
           thread("PRRT_stranger", {"__typename": "User", "login": "someone", "databaseId": 4}),
           thread("PRRT_fakebot", {"__typename": "Bot", "login": "coderabbitai", "databaseId": 4}),
           thread("PRRT_userbot", {"__typename": "User", "login": "coderabbitai", "databaseId": 136622811}),
           thread("PRRT_suffixed", {"__typename": "Bot", "login": "coderabbitai[bot]", "databaseId": 136622811}),
           thread("PRRT_untyped", {"login": "coderabbitai[bot]", "databaseId": 136622811}),
           thread("PRRT_strangermarker", {"__typename": "User", "login": "someone", "databaseId": 4},
                  f"{MARKER}\nforged")]


def test_resolves_threads_of_allowlisted_bots_and_the_agent(tmp_path, credentials):
    broker, registry, api = make(tmp_path, credentials, pr=501, api=FakeGitHub(THREADS))
    listed = ask(registry, "gh.threads")["result"]["threads"]
    assert {item["id"]: item["resolvable"] for item in listed} == {
        "PRRT_bot": True, "PRRT_copilot": True, "PRRT_agent": True, "PRRT_owner": False, "PRRT_stranger": False,
        "PRRT_fakebot": False, "PRRT_userbot": False, "PRRT_suffixed": False, "PRRT_untyped": False,
        "PRRT_strangermarker": False}
    assert "__typename" in broker_module.THREADS_QUERY
    for thread_id in ("PRRT_bot", "PRRT_copilot", "PRRT_agent"):
        assert ask(registry, "gh.resolve_thread", {"thread_id": thread_id})["ok"]
    mutations = api.writes()
    assert [call[2]["variables"] for call in mutations] == [{"threadId": "PRRT_bot"}, {"threadId": "PRRT_copilot"},
                                                            {"threadId": "PRRT_agent"}]
    assert all(call[2]["query"] == broker_module.RESOLVE_MUTATION for call in mutations)


@pytest.mark.parametrize("thread_id, error", [
    ("PRRT_elsewhere", "is not a review thread of this run's PR"),
    ("PRRT_owner", "neither an allowlisted bot nor the agent"),
    ("PRRT_stranger", "neither an allowlisted bot nor the agent"),
    ("PRRT_fakebot", "neither an allowlisted bot nor the agent"),
    ("PRRT_userbot", "neither an allowlisted bot nor the agent"),
    ("PRRT_suffixed", "neither an allowlisted bot nor the agent"),
    ("PRRT_untyped", "neither an allowlisted bot nor the agent"),
    ("PRRT_strangermarker", "neither an allowlisted bot nor the agent"),
])
def test_refuses_resolving_a_thread_on_another_pr_or_started_by_the_owner_or_a_stranger(tmp_path, credentials,
                                                                                         thread_id, error):
    _, registry, api = make(tmp_path, credentials, pr=501, api=FakeGitHub(THREADS))
    refused(ask(registry, "gh.resolve_thread", {"thread_id": thread_id}), error)
    assert api.writes() == []
    assert all(call[2]["variables"]["number"] == 501 for call in api.calls)


def test_a_graphql_error_on_resolve_is_logged_as_failed(tmp_path, credentials):
    broker, registry, _ = make(tmp_path, credentials, pr=501, api=FakeGitHub(THREADS, graphql_errors=True))
    refused(ask(registry, "gh.resolve_thread", {"thread_id": "PRRT_bot"}), "GraphQL answered with errors")
    assert [(entry["op"], entry["phase"]) for entry in broker.log.entries] == [
        ("resolve_thread", "intent"), ("resolve_thread", "failed")]


def test_an_http_error_on_a_write_is_logged_as_failed(tmp_path, credentials):
    api = FakeGitHub(statuses={("POST", f"/issues/{ISSUE}/comments"): 422})
    broker, registry, _ = make(tmp_path, credentials, pr=501, api=api)
    refused(ask(registry, "gh.comment", {"number": ISSUE, "body": f"{MARKER}\nx"}), "HTTP 422")
    assert [(entry["op"], entry["phase"]) for entry in broker.log.entries] == [("comment", "intent"),
                                                                               ("comment", "failed")]
    assert TOKEN not in (tmp_path / "broker.jsonl").read_text()


def test_resolving_needs_this_runs_pr(setup):
    _, registry, api = setup
    refused(ask(registry, "gh.resolve_thread", {"thread_id": "PRRT_bot"}), "this run has no PR")
    assert api.calls == []


# --- reads, REST and GraphQL ---------------------------------------------------------------------------

def test_reads_from_the_allowlist(tmp_path, credentials):
    _, registry, api = make(tmp_path, credentials, mode="dry-run")
    answer = ask(registry, "gh.read", {"endpoint": "pull_reviews", "number": 7, "page": 2})
    assert answer == {"ok": True, "result": {"data": [{"id": 1}], "next_page": False}}
    assert ask(registry, "gh.read", {"endpoint": "check_runs", "sha": HEAD})["ok"]
    assert [call[1] for call in api.calls] == [
        f"{github.API}/repos/{REPOSITORY}/pulls/7/reviews?per_page=100&page=2",
        f"{github.API}/repos/{REPOSITORY}/commits/{HEAD}/check-runs"]


def test_a_read_reports_a_next_page_from_the_link_header(tmp_path, credentials):
    link = (f'<{github.API}/repositories/1/pulls/7/comments?per_page=100&page=3>; rel="next", '
            f'<{github.API}/repositories/1/pulls/7/comments?per_page=100&page=9>; rel="last"')
    _, registry, _ = make(tmp_path, credentials, mode="dry-run", api=FakeGitHub(link=link))
    assert ask(registry, "gh.read", {"endpoint": "pull_comments", "number": 7, "page": 2})["result"]["next_page"]
    last = f'<{github.API}/repositories/1/pulls/7/comments?page=1>; rel="prev"'
    _, registry, _ = make(tmp_path, credentials, mode="dry-run", api=FakeGitHub(link=last))
    assert ask(registry, "gh.read", {"endpoint": "pull_comments", "number": 7, "page": 2})["result"]["next_page"] is False


@pytest.mark.parametrize("args, error", [
    ({"endpoint": "contents", "number": 1}, "contains characters"),
    ({"endpoint": "hooks", "number": 1}, "contains characters"),
    ({"endpoint": "issue/../../user"}, "contains characters"),
    ({"endpoint": "/user"}, "contains characters"),
    ({"endpoint": "issue"}, "argument number is required"),
    ({"endpoint": "issue", "number": 1, "path": "/repos/other/repo"}, "unknown argument: 'path'"),
    ({"endpoint": "check_runs", "sha": "main"}, "contains characters"),
])
def test_refuses_reads_outside_the_allowlist(setup, args, error):
    _, registry, api = setup
    refused(ask(registry, "gh.read", args), error)
    assert api.calls == []


def test_the_api_refuses_paths_outside_the_repository(setup):
    broker, _, api = setup
    for path in ("/user", "/repos/other/repo/issues/1", f"/repos/{REPOSITORY}/../other/x",
                 f"/repos/{REPOSITORY}/issues?x=1"):
        with pytest.raises(BrokerRefused, match="outside"):
            broker.api.rest("GET", path)
    assert api.calls == []


@pytest.mark.parametrize("request_type, args, error", [
    ("gh.graphql", {"query": "mutation { mergePullRequest(input: {}) { clientMutationId } }"}, "unknown request type"),
    ("gh.api", {"endpoint": "graphql"}, "unknown request type"),
    ("gh.read", {"endpoint": "graphql"}, "contains characters"),
    ("gh.read", {"endpoint": "issue", "number": 1, "query": "mutation { x }"}, "unknown argument: 'query'"),
    ("gh.read", {"endpoint": "issue", "number": 1, "method": "POST"}, "unknown argument: 'method'"),
    ("gh.threads", {"query": "mutation { x }"}, "unknown argument: 'query'"),
])
def test_refuses_a_raw_graphql_request_and_a_mutation_sent_as_a_read(setup, request_type, args, error):
    _, registry, api = setup
    refused(ask(registry, request_type, args), error)
    assert api.calls == []


def test_only_the_fixed_graphql_documents_are_sent(setup):
    broker, _, api = setup
    copy = "".join(list(broker_module.RESOLVE_MUTATION))
    for document in ("mutation { mergePullRequest(input: {pullRequestId: \"x\"}) { clientMutationId } }", copy):
        with pytest.raises(BrokerRefused, match="fixed GraphQL documents"):
            broker.api.graphql(document, {})
    assert api.calls == []


# --- dry-run and credentials ---------------------------------------------------------------------------

WRITES = [("gh.push", {"slug": SLUG, "head": HEAD}), ("gh.pr_create", {"title": "t", "body": "b"}),
          ("gh.pr_edit", {"body": "b"}), ("gh.comment", {"number": ISSUE, "body": f"{MARKER}\nx"}),
          ("gh.comment_edit", {"comment_id": 1, "body": f"{MARKER}\nx"}),
          ("gh.label", {"number": ISSUE, "label": "autodev", "action": "add"}),
          ("gh.resolve_thread", {"thread_id": "PRRT_bot"})]


@pytest.mark.parametrize("request_type, args", WRITES)
def test_refuses_any_write_in_dry_run(tmp_path, request_type, args):
    broker, registry, api = make(tmp_path, None, mode="dry-run", pr=501)
    refused(ask(registry, request_type, args), "read-only in dry-run")
    assert api.calls == [] and broker.git.pushes == []


def test_refuses_runner_internal_writes_in_dry_run(tmp_path):
    broker, _, api = make(tmp_path, None, mode="dry-run", pr=501, earlier_records=[{"issue": 7}])
    for call in (lambda: broker.cleanup([{"op": "remove_label", "issue": 7, "label": CLAIM_LABEL}]),
                 lambda: broker.select(f"{MARKER}\n<!-- autodev:selection -->\n"),
                 lambda: broker.create_risk_issues(["R1"])):
        with pytest.raises(broker_module.ReadOnly):
            call()
    assert api.calls == []


@pytest.mark.parametrize("missing", ["key", "known_hosts", "token", "everything"])
def test_refuses_a_real_run_with_a_missing_credential(tmp_path, credentials, missing):
    if missing == "key":
        credentials.deploy_key.unlink()
    elif missing == "known_hosts":
        credentials.known_hosts.unlink()
    elif missing == "token":
        def unavailable():
            raise github.TokenUnavailable("the keychain item agents-autodev-github could not be read")
        credentials = Credentials(credentials.deploy_key, credentials.known_hosts, unavailable)
    else:
        credentials = None
    for mode in ("run", "resume"):
        with pytest.raises(CredentialsMissing) as raised:
            make(tmp_path, credentials, mode=mode)
        assert TOKEN not in str(raised.value) and KEY not in str(raised.value)


def test_a_symlinked_deploy_key_counts_as_missing(tmp_path, credentials):
    target = tmp_path / "elsewhere"
    credentials.deploy_key.rename(target)
    credentials.deploy_key.symlink_to(target)
    with pytest.raises(CredentialsMissing, match="the deploy key"):
        make(tmp_path, credentials)


def test_credentials_from_the_configuration_name_the_known_hosts_beside_the_key():
    found = Credentials.from_config({"github": {"deploy_key": "/keys/deploy_key", "token": "keychain:item"}})
    assert found.deploy_key == Path("/keys/deploy_key") and found.known_hosts == Path("/keys/github_known_hosts")


def test_the_log_never_holds_the_token_or_the_key(setup, tmp_path):
    _, registry, _ = setup
    ask(registry, "gh.push", {"slug": SLUG, "head": HEAD})
    ask(registry, "gh.comment", {"number": ISSUE, "body": f"{MARKER}\n{TOKEN}"})
    text = (tmp_path / "broker.jsonl").read_text()
    assert TOKEN not in text and KEY not in text and "refused" in text


# --- runner-internal API -------------------------------------------------------------------------------

def test_cleanup_only_on_issues_of_earlier_run_records(tmp_path, credentials):
    broker, _, api = make(tmp_path, credentials, earlier_records=[{"issue": 7}, {"pr": 8}])
    done = broker.cleanup([{"op": "remove_label", "issue": 7, "label": CLAIM_LABEL, "comment": STALE_CLAIM_COMMENT},
                           {"op": "remove_label", "issue": 8, "label": CLAIM_LABEL, "comment": None}])
    assert done == [{"issue": 7, "label": CLAIM_LABEL}, {"issue": 8, "label": CLAIM_LABEL}]
    assert [call[0] for call in api.writes()] == ["POST", "DELETE", "DELETE"]
    for action, error in ([{"op": "remove_label", "issue": 9, "label": CLAIM_LABEL}], "no earlier run record"),\
                         ([{"op": "remove_label", "issue": 7, "label": "autodev:ready"}], "not a gate action"),\
                         ([{"op": "close", "issue": 7}], "not a gate action"),\
                         ([{"op": "remove_label", "issue": 7, "label": CLAIM_LABEL, "comment": "unmarked"}],
                          "must start with the line"):
        with pytest.raises(BrokerRefused, match=error):
            broker.cleanup(action)
    assert len(api.writes()) == 3


def test_cleanup_treats_an_absent_label_as_done_and_continues(tmp_path, credentials):
    api = FakeGitHub(statuses={("DELETE", "/issues/7/labels/autodev%3Ain-progress"): 404})
    broker, _, _ = make(tmp_path, credentials, api=api, earlier_records=[{"issue": 7}, {"issue": 8}])
    done = broker.cleanup([{"op": "remove_label", "issue": 7, "label": CLAIM_LABEL, "comment": STALE_CLAIM_COMMENT},
                           {"op": "remove_label", "issue": 8, "label": CLAIM_LABEL, "comment": None}])
    assert done == [{"issue": 7, "label": CLAIM_LABEL}, {"issue": 8, "label": CLAIM_LABEL}]
    assert [call[0] for call in api.writes()] == ["POST", "DELETE", "DELETE"]
    absent = [entry for entry in broker.log.done("cleanup") if entry.get("note") == "already absent"]
    assert [entry["target"] for entry in absent] == [7]


def test_cleanup_stops_on_another_label_error(tmp_path, credentials):
    api = FakeGitHub(statuses={("DELETE", "/issues/7/labels/autodev%3Ain-progress"): 403})
    broker, _, _ = make(tmp_path, credentials, api=api, earlier_records=[{"issue": 7}])
    with pytest.raises(BrokerRefused, match="HTTP 403"):
        broker.cleanup([{"op": "remove_label", "issue": 7, "label": CLAIM_LABEL}])
    assert broker.log.entries[-1]["phase"] == "failed"


def test_selection_comment_and_claim_label(tmp_path, credentials):
    broker, _, api = make(tmp_path, credentials)
    result = broker.select(f"{MARKER}\n<!-- autodev:selection -->\nTaking #342.")
    assert result["kind"] == "selection" and result["id"]
    assert [(call[0], call[2]) for call in api.writes()] == [
        ("POST", {"body": f"{MARKER}\n<!-- autodev:selection -->\nTaking #342."}), ("POST", {"labels": [CLAIM_LABEL]})]
    with pytest.raises(BrokerRefused, match="selection comment starts with"):
        broker.select(f"{MARKER}\nno kind")


def test_resume_binds_only_a_self_development_pr_of_this_issue(tmp_path, credentials):
    broker, registry, api = make(tmp_path, credentials, mode="resume")
    assert broker.bind_resume(501) == 501 and broker.run.pr == 501
    assert broker.branch == f"claude/autodev-{ISSUE}-resumed"
    assert ask(registry, "gh.comment", {"number": 501, "body": f"{MARKER}\nresumed"})["ok"]
    unlabeled = {**pull(505), "labels": []}
    for answer in (pull(502, branch="claude/autodev-341-x"), pull(503, state="closed"), pull(504, branch="feature"),
                   unlabeled):
        other, _, _ = make(tmp_path, credentials, mode="resume", api=FakeGitHub(pulls={answer["number"]: answer}))
        with pytest.raises(BrokerRefused, match="no open self-development PR"):
            other.bind_resume(answer["number"])
    with pytest.raises(BrokerRefused, match="only a resume run"):
        make(tmp_path, credentials)[0].bind_resume(501)


def finding(identifier, level="medium", title="Cache may go stale", body="The cache is not refreshed.",
            security=False):
    return RiskFinding(identifier, level, title, body, security)


def test_risk_issues_from_the_premortem_record(tmp_path, credentials):
    premortem = {"R1": finding("R1"), "R2": finding("R2", "high"), "R3": finding("R3", "critical")}
    broker, _, api = make(tmp_path, credentials, pr=501, premortem=premortem)
    created = broker.create_risk_issues(["R1", "R2", "R3"])
    assert [item["finding"] for item in created] == ["R1", "R2", "R3"]
    sent = [call[2] for call in api.writes()]
    assert all(item["labels"] == ["autodev:risk"] and item["body"].startswith(MARKER + "\n")
               and f"https://github.com/{REPOSITORY}/pull/501" in item["body"] for item in sent)
    assert sent[0]["title"] == "Cache may go stale"


@pytest.mark.parametrize("premortem, ids, error", [
    ({"R1": finding("R1", "low")}, ["R1"], "below medium"),
    ({"R1": finding("R1", "info")}, ["R1"], "below medium"),
    ({}, ["R1"], "not in the run's validated pre-mortem record"),
    ({"R1": finding("R1", security=True)}, ["R1"], "security signal"),
    ({"R1": finding("R1", title="Token may leak into logs")}, ["R1"], "security signal"),
    ({"R1": finding("R1", body="The sandbox can be bypassed.")}, ["R1"], "security signal"),
    ({"R1": finding("R1", body=f"value {TOKEN}")}, ["R1"], "security signal"),
])
def test_refuses_a_risk_issue_below_medium_not_in_the_record_or_security(tmp_path, credentials, premortem, ids,
                                                                         error):
    broker, _, api = make(tmp_path, credentials, pr=501, premortem=premortem)
    with pytest.raises(BrokerRefused, match=error):
        broker.create_risk_issues(ids)
    assert api.writes() == []


def test_refuses_a_risk_issue_with_a_secret_in_its_text(tmp_path, credentials):
    secret = "ghp_" + "abcd" * 9
    broker, _, api = make(tmp_path, credentials, pr=501, premortem={"R1": finding("R1", body=f"See {secret}")})
    with pytest.raises(BrokerRefused, match="secret-like text found"):
        broker.create_risk_issues(["R1"])
    assert api.writes() == []


def test_refuses_a_fourth_risk_issue(tmp_path, credentials):
    premortem = {f"R{n}": finding(f"R{n}") for n in range(1, 5)}
    broker, _, api = make(tmp_path, credentials, pr=501, premortem=premortem)
    with pytest.raises(BrokerRefused, match="at most 3 risk issues"):
        broker.create_risk_issues(["R1", "R2", "R3", "R4"])
    assert len(api.writes()) == 3
    with pytest.raises(BrokerRefused, match="at most 3 risk issues"):
        broker.create_risk_issues(["R4"])


def test_the_risk_issue_cap_and_dedup_hold_across_brokers_of_one_run(tmp_path, credentials):
    premortem = {f"R{n}": finding(f"R{n}") for n in range(1, 5)}
    first, _, first_api = make(tmp_path, credentials, pr=501, premortem=premortem)
    with pytest.raises(BrokerRefused, match="already has a risk issue"):
        first.create_risk_issues(["R1", "R1"])
    assert len(first_api.writes()) == 1
    second, _, second_api = make(tmp_path, credentials, pr=501, premortem=premortem)
    with pytest.raises(BrokerRefused, match="already has a risk issue"):
        second.create_risk_issues(["R1"])
    assert second.create_risk_issues(["R2", "R3"])
    with pytest.raises(BrokerRefused, match="at most 3 risk issues"):
        second.create_risk_issues(["R4"])
    third, _, third_api = make(tmp_path, credentials, pr=501, premortem=premortem)
    with pytest.raises(BrokerRefused, match="at most 3 risk issues"):
        third.create_risk_issues(["R4"])
    assert len(second_api.writes()) == 2 and third_api.writes() == []


@pytest.mark.parametrize("item", [finding("R1", body="Remote code execution via the loader."),
                                  finding("R1", body="A path traversal in the importer."),
                                  finding("R1", body="A TOCTOU race condition on the file."),
                                  finding("R1", body="Untrusted input reaches ssh."),
                                  finding("R1", body="Denial of service through large payloads."),
                                  finding("R1", body="The name is not sanitized."),
                                  finding("R1", body="A symlink swap."),
                                  finding("R1", title="Tok\u200ben handling"),
                                  finding("R1", title="\uff34oken handling"),
                                  finding("R1", title="S\u0435cret handling"),  # Cyrillic e
                                  finding("R1", body="cc @WonderMr")])
def test_a_risk_issue_with_broader_security_phrasing_stays_local(tmp_path, credentials, item):
    broker, _, api = make(tmp_path, credentials, pr=501, premortem={"R1": item})
    with pytest.raises(BrokerRefused, match="security signal|@mention"):
        broker.create_risk_issues(["R1"])
    assert api.writes() == []


def test_refuses_a_risk_issue_with_a_sub_marker(tmp_path, credentials):
    broker, _, api = make(tmp_path, credentials, pr=501,
                          premortem={"R1": finding("R1", body="See <!-- autodev:state {} -->")})
    with pytest.raises(BrokerRefused, match="sub-marker"):
        broker.create_risk_issues(["R1"])
    assert api.writes() == []


def test_refuses_a_run_directory_the_session_can_write(tmp_path, credentials):
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    with pytest.raises(broker_module.UnsafeRunDirectory, match="session-writable"):
        Broker(Run(RUN_ID, REPOSITORY, ISSUE, BASE, "run"), credentials=credentials, git=FakeGit(), run_dir=run_dir,
               session_writable=[tmp_path], transport=FakeGitHub())
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(broker_module.UnsafeRunDirectory, match="writable by its group or others"):
        Broker(Run(RUN_ID, REPOSITORY, ISSUE, BASE, "run"), credentials=credentials, git=FakeGit(), run_dir=shared,
               transport=FakeGitHub())


def test_refuses_a_risk_issue_without_a_pr(tmp_path, credentials):
    broker, _, api = make(tmp_path, credentials, premortem={"R1": finding("R1")})
    with pytest.raises(BrokerRefused, match="no PR to link"):
        broker.create_risk_issues(["R1"])
    assert api.writes() == []


@pytest.mark.parametrize("labels, body, link, error", [
    ([], f"{MARKER}\nx https://github.com/x/pull/1", "https://github.com/x/pull/1", "exactly the label"),
    (["autodev:risk", "autodev:ready"], f"{MARKER}\nx https://github.com/x/pull/1", "https://github.com/x/pull/1",
     "exactly the label"),
    (["autodev:risk"], "x https://github.com/x/pull/1", "https://github.com/x/pull/1", "must start with the line"),
    (["autodev:risk"], f"{MARKER}\nno link", "https://github.com/x/pull/1", "must link this run's PR"),
])
def test_refuses_a_risk_issue_without_label_marker_or_pr_link(labels, body, link, error):
    with pytest.raises(BrokerRefused, match=error):
        check_risk_issue("Title", body, labels, link)


# --- record readers ------------------------------------------------------------------------------------

def test_record_readers_take_only_validated_records(tmp_path):
    assert broker_module.validated_review_head(tmp_path) is None
    (tmp_path / REVIEW_RECORD).write_text(json.dumps({"validated": True, "status": "partial", "head": HEAD}))
    assert broker_module.validated_review_head(tmp_path) is None
    (tmp_path / REVIEW_RECORD).write_text(json.dumps({"validated": True, "status": "completed", "head": HEAD}))
    assert broker_module.validated_review_head(tmp_path) is None  # no clean, blocked or findings fields (#345)
    (tmp_path / REVIEW_RECORD).write_text(json.dumps({"validated": True, "status": "completed", "head": HEAD,
                                                      "handle": "review-1", "clean": True, "blocked": False,
                                                      "findings": []}))
    assert broker_module.validated_review_head(tmp_path) is None  # no findings ledger (#345)
    (tmp_path / broker_module.LEDGER_RECORD).write_text(json.dumps({"head": HEAD, "handle": "review-1",
                                                                    "findings": []}))
    assert broker_module.validated_review_head(tmp_path) == HEAD
    (tmp_path / PREMORTEM_RECORD).write_text(json.dumps({"validated": False, "findings": [
        {"id": "R1", "level": "medium", "title": "t", "body": "b", "security": False}]}))
    assert broker_module.read_premortem(tmp_path) == {}
    (tmp_path / PREMORTEM_RECORD).write_text(json.dumps({"validated": True, "findings": [
        {"id": "R1", "level": "medium", "title": "t", "body": "b", "security": False},
        {"id": "R2", "level": "high", "title": "t", "body": "b"}, "junk"]}))
    found = broker_module.read_premortem(tmp_path)
    assert found["R1"].security is False and found["R2"].security is True  # unmarked counts as security


def _gate(tmp_path, findings, decisions=None, ledger=None, **fields):
    """The gate over a review record listing ``findings`` and a ledger holding them (open, under the
    record's handle) unless ``ledger`` gives its entries."""
    record = {"validated": True, "status": "completed", "head": HEAD, "handle": "review-2", "kind": "review",
              "number": 2, "restricted": None, "clean": not findings, "blocked": False, "findings": findings, **fields}
    (tmp_path / REVIEW_RECORD).write_text(json.dumps(record))
    entries = [{"handle": "review-2", "status": "open", **item} for item in findings] if ledger is None else ledger
    (tmp_path / broker_module.LEDGER_RECORD).write_text(json.dumps({"head": HEAD, "handle": "review-2",
                                                                    "findings": entries}))
    if decisions is not None:
        (tmp_path / broker_module.DECISIONS_RECORD).write_text(json.dumps({"decisions": decisions}))
    return broker_module.review_gate(tmp_path)


MEDIUM = {"key": "a" * 16, "level": "medium", "security": False}
DECLINED = {"key": "a" * 16, "decision": "declined", "reason": "Not needed here.", "handle": "review-2"}


@pytest.mark.parametrize("findings, decisions, fields, opens", [
    ([], None, {}, True),  # clean
    ([], None, {"clean": False}, False),
    ([], None, {"blocked": True, "clean": False}, False),  # a restricted review with findings in its scope
    ([], None, {"blocked": None}, False),
    ([MEDIUM], None, {}, False),  # an undecided medium
    ([MEDIUM], [DECLINED], {}, True),  # a declined medium
    ([MEDIUM], [{**DECLINED, "reason": " "}], {}, False),  # declined without a reason
    ([MEDIUM], [{**DECLINED, "decision": "fixed"}], {}, False),  # fixed: the fix needs a new review
    ([MEDIUM], [{**DECLINED, "handle": "review-1"}], {}, False),  # declined before this review
    ([MEDIUM], [{**DECLINED, "decision": "known_risk", "handle": "premortem-1",
                 "history": [{"decision": "declined", "reason": "Not needed.", "handle": "review-2"}]}], {}, True),
    ([{**MEDIUM, "level": "high"}], [DECLINED], {}, False),  # a declined high stays open
    ([{**MEDIUM, "level": "critical"}], [DECLINED], {}, False),
    ([{**MEDIUM, "security": True}], [DECLINED], {}, False),  # a declined security finding stays open
    ([{"key": "a" * 16, "level": "medium"}], [DECLINED], {}, False),  # unmarked counts as security
])
def test_the_push_gate_opens_for_a_clean_or_declined_review(tmp_path, findings, decisions, fields, opens):
    head, why = _gate(tmp_path, findings, decisions, **fields)
    assert (head == HEAD) is opens and (head is None) is not opens, why
    assert broker_module.validated_review_head(tmp_path) == head


EARLIER = {"key": "c" * 16, "level": "critical", "security": False, "handle": "review-1", "status": "open"}


@pytest.mark.parametrize("ledger, decisions, opens, why", [
    # An undecided critical of an earlier review keeps the gate closed after a clean later review.
    ([EARLIER], None, False, "of review-1 stays open"),
    ([{**EARLIER, "status": "resolved", "resolved_by": "review-2"}], None, True, "clean"),
    ([{**EARLIER, "status": "dismissed"}], None, False, "unknown status"),
    ([{**EARLIER, "level": "medium"}], None, False, "neither declined nor resolved"),
    ([{**EARLIER, "level": "medium"}], [{**DECLINED, "key": "c" * 16, "handle": "review-1"}], True,
     "every remaining medium finding was declined"),
    ([{**EARLIER, "level": "medium"}], [{**DECLINED, "key": "c" * 16}], False, "neither declined"),  # wrong handle
    (["junk"], None, False, "lists no findings"),
])
def test_the_push_gate_reads_every_open_finding_of_the_ledger(tmp_path, ledger, decisions, opens, why):
    head, reason = _gate(tmp_path, [], decisions, ledger=ledger)
    assert (head == HEAD) is opens and why in reason, reason


def test_the_push_gate_needs_a_ledger_of_the_same_review(tmp_path):
    assert _gate(tmp_path, [])[0] == HEAD
    ledger = tmp_path / broker_module.LEDGER_RECORD
    for changed in ({"head": "c" * 40}, {"handle": "review-1"}):
        ledger.write_text(json.dumps({"head": HEAD, "handle": "review-2", "findings": [], **changed}))
        assert broker_module.review_gate(tmp_path) == (None, "the findings ledger does not cover this review")
    ledger.unlink()
    assert broker_module.review_gate(tmp_path) == (None, "the findings ledger does not cover this review")
    # A finding of the review that the ledger lacks closes the gate.
    assert _gate(tmp_path, [MEDIUM], [DECLINED], ledger=[]) == (None, "the findings ledger lacks a finding of this "
                                                                       "review")


def test_a_secret_known_after_construction_is_refused_and_masked(tmp_path, credentials):
    broker, _, _ = make(tmp_path, credentials, pr=501)
    late = "correct-horse-battery-staple-violet-lamp"  # no pattern or entropy rule finds it
    assert broker.scanner.scan(f"the key is {late}") == ()
    broker.add_known([scan_module.KnownSecret("codex api key", late)])
    with pytest.raises(BrokerRefused, match="codex api key"):
        broker._scan("comment", f"{MARKER}\nthe key is {late}")
    broker.add_known([scan_module.KnownSecret("codex api key", late)])  # again: no change
    broker.log.add("comment", "note", text=f"held {late}")
    assert late not in (tmp_path / broker_module.LOG_NAME).read_text()
    with pytest.raises(TypeError):
        broker.add_known([late])


@pytest.mark.parametrize("author", [
    {"__typename": "Bot", "login": "coderabbitai", "databaseId": None},
    {"__typename": "Bot", "login": "coderabbitai"},
    {"__typename": "Bot", "login": "evil-app", "databaseId": None},
    {"__typename": "Bot", "login": "evil-app"},
    {"__typename": "Bot", "login": "coderabbitai", "databaseId": True},
])
def test_a_bot_without_an_integer_id_never_resolves(author):
    """Review round 2: a missing or non-integer account ID matches no allowlisted bot."""
    thread = {"comments": {"nodes": [{"author": author, "body": "x"}]}}
    assert broker_module._resolvable(thread) is False


def test_trusted_bot_needs_an_allowlisted_login_and_its_integer_id():
    from src.schedules.gate import trusted_bot
    assert trusted_bot({"login": "coderabbitai[bot]", "id": 136622811})
    assert not trusted_bot({"login": "evil-app[bot]", "id": None})
    assert not trusted_bot({"login": "coderabbitai[bot]", "id": None})
    assert not trusted_bot({"login": "coderabbitai[bot]", "id": "136622811"})


# --- PR #368 review ----------------------------------------------------------------------------------

def _context(*, closing=lambda: False, seconds=60.0):
    import time
    from src.schedules.socket_service import RequestContext
    return RequestContext(Connection(4242), 4242, time.monotonic() + seconds, closing=closing)


@pytest.mark.parametrize("body", ["&amp;#64;coderabbitai resolve", "&amp;amp;#x40;copilot change",
                                  "&amp;lt;!-- autodev:state --&amp;gt;"])
def test_nested_html_entities_are_decoded_before_the_check(body):
    with pytest.raises(BrokerRefused, match="may not hold"):
        broker_module.check_session_text(f"{MARKER}\n{body}")


def test_a_write_is_not_sent_once_the_run_closes_during_its_checks(tmp_path, credentials):
    broker, _, api = make(tmp_path, credentials, pr=501)
    answers = iter([False, True])  # open at the handler's entry, closing right before the write
    with pytest.raises(BrokerRefused, match="the run is closing"):
        broker.comment({"number": 501, "body": f"{MARKER}\nhello"}, _context(closing=lambda: next(answers)))
    assert api.writes() == []


def test_threads_refuse_an_api_call_that_could_outlast_the_deadline(tmp_path, credentials):
    broker, _, api = make(tmp_path, credentials, pr=501, api=FakeGitHub(THREADS))
    with pytest.raises(BrokerRefused, match="too little time left"):
        broker.threads({}, _context(seconds=10))
    assert api.calls == []


def test_a_risk_issue_whose_creation_may_have_succeeded_still_counts(tmp_path, credentials):
    premortem = {f"R{n}": finding(f"R{n}") for n in range(1, 5)}

    class Flaky(FakeGitHub):
        def __call__(self, method, url, headers, body):
            if method == "POST" and url.endswith("/issues"):
                self.calls.append((method, url, json.loads(body)))
                raise github.NetworkError("GitHub is unreachable: TimeoutError")
            return super().__call__(method, url, headers, body)

    broker, _, _ = make(tmp_path, credentials, pr=501, premortem=premortem, api=Flaky())
    with pytest.raises(BrokerRefused, match="unreachable"):
        broker.create_risk_issues(["R1"])
    with pytest.raises(BrokerRefused, match="may have been created"):
        broker.create_risk_issues(["R1"])


def test_a_risk_issue_github_refused_may_be_retried(tmp_path, credentials):
    premortem = {"R1": finding("R1")}
    broker, _, api = make(tmp_path, credentials, pr=501, premortem=premortem,
                          api=FakeGitHub(statuses={("POST", "/issues"): 422}))
    with pytest.raises(BrokerRefused, match="HTTP 422"):
        broker.create_risk_issues(["R1"])
    api.statuses.clear()
    assert broker.create_risk_issues(["R1"])
