"""Publish the accepted real task to a private Git ref, then materialize its diff.

This optional host action requires a linked codex/ worktree and the original
tracked target preimage (or the exact already-accepted bytes). File replacement is a separate, non-CAS host action.
"""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ..ir import require, text_digest
from .clients import open_client
from .git_sink import GitSink, git
from .workload import BASE, ROOT, TARGET, candidate, source_snapshot


def materialize(repo, output):
    root = Path(repo).resolve()
    require((root / ".git").is_file(), "materialization requires a linked worktree")
    branch = git(root, "branch", "--show-current").stdout.strip()
    require(branch.startswith("codex/"), "materialization requires a task branch")
    require(git(root, "rev-parse", "HEAD").stdout.strip() == BASE, "worktree HEAD differs from recorded task base")
    inputs = source_snapshot(root)
    target = root / TARGET
    allowed_preimages = (inputs["test_guide"], candidate(inputs))
    require(not target.is_symlink() and target.read_text() in allowed_preimages, "target preimage changed")
    preimage = target.read_text()
    sink = GitSink(root, "subagent-testing-v2-reviewed")
    require(sink.tip(optional=True) is None, "one-shot publication ref already exists")
    sink = GitSink(root, "subagent-testing-v2-reviewed", initial=BASE)
    with TemporaryDirectory(prefix="accord-real-task-") as state:
        host, contract = open_client("accord_json", Path(state) / "host.sqlite", sink)
        for name, value in inputs.items():
            host.set_resource(name, value)
        task = host.create_task(contract)
        proposed = host.infer(task, "recorded_agent_edit", candidate)
        receipts = host.check(task)
        accepted = host.accept(task, receipts)
        grant = host.authorize("publish_test_guide", task, accepted, "docs", "test_guide", BASE)
        operation = host.prepare(task, accepted, grant)
        result = host.dispatch(operation)
        require(result["status"] == "confirmed", "Git publication did not confirm")
        artifact = sink.target()
        require(artifact["text"] == proposed["text"], "accepted artifact mismatch")
        require(git(root, "diff", "--name-only", BASE, artifact["version"]).stdout.splitlines() == [TARGET],
                "accepted commit changed another path")
        # The owner must serialize editors during this short materialization step.
        # Rechecking + atomic rename prevents partial writes, not a concurrent-writer CAS.
        require(target.read_text() == preimage, "target changed before materialization")
        with TemporaryDirectory(prefix="accord-materialize-", dir=target.parent) as temporary:
            staged = Path(temporary) / "README.md"
            staged.write_text(artifact["text"], encoding="utf-8")
            staged.chmod(target.stat().st_mode & 0o777)
            staged.replace(target)
        record = {"base_revision": BASE, "branch": branch, "target": TARGET, "publication_ref": sink.ref,
                  "accepted_commit": artifact["version"], "candidate_sha256": text_digest(proposed["text"]),
                  "acceptance": host.inspect("acceptance", accepted),
                  "checks": [host.inspect("check", handle) for handle in receipts],
                  "operation": host.inspect("operation", operation),
                  "adapter": "single-ref outcome chain v2",
                  "adapter_sha256": text_digest((Path(__file__).parent / "git_sink.py").read_text()),
                  "materialization": "Verified preimage then atomic rename; concurrent filesystem CAS is not claimed."}
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = materialize(args.repo, args.output)
    print(json.dumps({"target": result["target"], "accepted_commit": result["accepted_commit"]}, indent=2))


if __name__ == "__main__":
    main()
