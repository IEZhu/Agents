"""Local Git outcome chain: one ref CAS binds the document and its receipt.

The broker exclusively owns an append-only refs/accord-eval/<namespace>/target.
An outcome commit carries both the document tree and operation metadata; denial
commits preserve the tree. No multi-ref transaction is assumed atomic. It never
touches HEAD, the user's index, working files, hooks or a remote. This is a
trusted adapter, not a sandbox; external rewinds/deletion violate its contract.
"""

import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from ..ir import canonical, digest, identifier, require, text_digest


def git(repo, *args, data=None, extra_env=None, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "Accord experiment", "GIT_AUTHOR_EMAIL": "accord@example.invalid",
                "GIT_COMMITTER_NAME": "Accord experiment", "GIT_COMMITTER_EMAIL": "accord@example.invalid",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"})
    env.update(extra_env or {})
    return subprocess.run(["git", "-C", str(repo), "-c", f"core.hooksPath={os.devnull}",
                           "-c", "commit.gpgsign=false", *args], input=data, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=check, timeout=20)


def oid(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is not None,
            "expected full Git object ID")
    return value


class GitSink:
    idempotent = True

    def __init__(self, repo, namespace, *, initial=None):
        self.repo = Path(repo).resolve()
        identifier(namespace, revision=True)
        self.prefix = f"refs/accord-eval/{namespace}"
        self.ref = self.prefix + "/target"
        self.path = "tests/README.md"
        common = git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        self.identity = digest("git-outcome-chain/2", [common, self.prefix, self.path])
        if initial is not None and self.tip(optional=True) is None:
            initial = oid(initial)
            result = git(self.repo, "update-ref", "--no-deref", self.ref, initial, "0" * len(initial), check=False)
            require(result.returncode == 0 or self.tip() == initial, "cannot initialize publication ref")

    def tip(self, *, optional=False):
        result = git(self.repo, "show-ref", "--verify", "--hash", self.ref, check=False)
        if optional and result.returncode != 0:
            return None
        require(result.returncode == 0, "publication ref unavailable")
        return oid(result.stdout.strip())

    def lookup(self, operation):
        return self._lookup_at(operation, self.tip())

    def _lookup_at(self, operation, tip):
        require(re.fullmatch(r"[0-9a-f]{64}", operation) is not None, "invalid operation ID")
        matches = git(self.repo, "log", "--first-parent", "--format=%H", "--fixed-strings",
                      f'--grep="operation":"{operation}"', oid(tip)).stdout.splitlines()
        for commit in matches:
            body = git(self.repo, "cat-file", "commit", oid(commit)).stdout.split("\n\n", 1)[1]
            try:
                value = json.loads(body)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("protocol") == "accord.git-outcome/2" and value.get("operation") == operation:
                require(value["status"] in ("confirmed", "denied"), "invalid Git outcome")
                return value | {"target_version": commit}
        return None

    def target(self):
        commit = self.tip()
        return {"version": commit, "text": git(self.repo, "show", f"{commit}:{self.path}").stdout}

    def _commit(self, parent, text, observation):
        with TemporaryDirectory(prefix="accord-index-") as temporary:
            env = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            git(self.repo, "read-tree", parent, extra_env=env)
            if text is not None:
                blob = git(self.repo, "hash-object", "-w", "--stdin", data=text).stdout.strip()
                git(self.repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{self.path}", extra_env=env)
            tree = git(self.repo, "write-tree", extra_env=env).stdout.strip()
        return git(self.repo, "commit-tree", tree, "-p", parent,
                   data=canonical(observation).decode() + "\n").stdout.strip()

    def apply(self, operation, intent, text):
        require(intent["sink_identity"] == self.identity and intent["target"] == "test_guide", "wrong Git target")
        require(intent["output"] == text_digest(text), "payload digest mismatch")
        expected = oid(intent["expected_version"])
        fingerprint = digest("accord.intent/0.1", intent)
        # One ref publishes both the artifact tree and its outcome. A lost response
        # is resolved from the same chain before any retry; no separate receipt gap.
        # Dedup lookup and CAS must use one immutable ancestry to avoid recording a
        # second outcome when the same operation wins between lookup and publication.
        for _ in range(3):
            current = self.tip()
            prior = self._lookup_at(operation, current)
            if prior:
                require(prior["intent"] == fingerprint, "operation reused for different intent")
                return prior
            status = "confirmed" if current == expected else "denied"
            observation = {"protocol": "accord.git-outcome/2", "operation": operation,
                           "intent": fingerprint, "status": status, "observed_tip": current}
            commit = self._commit(current, text if status == "confirmed" else None, observation)
            result = git(self.repo, "update-ref", "--no-deref", self.ref, commit, current, check=False)
            if result.returncode == 0:
                return self.lookup(operation)
        raise RuntimeError("Git transaction did not settle; reconcile before retry")

    def publication_count(self, base):
        messages = git(self.repo, "log", "--first-parent", "--format=%B", f"{oid(base)}..{self.tip()}").stdout
        count = 0
        for line in messages.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("protocol") == "accord.git-outcome/2" and value.get("status") == "confirmed":
                count += 1
        return count


def seed_repository(directory, inputs, paths):
    """Disposable test repository of actual frozen files, not a managed worktree."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    for name, path in paths.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(inputs[name], encoding="utf-8")
    git(root, "add", "--", *paths.values())
    git(root, "commit", "-q", "-m", "Frozen repository task inputs")
    return git(root, "rev-parse", "HEAD").stdout.strip()
