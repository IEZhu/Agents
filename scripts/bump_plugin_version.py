"""Prepare/check one YY.MM.DD.HHMM version for the server, plugin and release.

Run in a contribution worktree after fetching origin/main. Synchronizes VERSION
and the plugin manifest; never commits, pushes, fetches, tags or publishes.
Python package metadata reads VERSION directly.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import calendar_version

MANIFEST = Path("plugins/agents-core/.claude-plugin/plugin.json")


def git(root: Path, *args: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          encoding="utf-8", stdin=subprocess.DEVNULL, timeout=30,
                          check=True, env=environment).stdout.strip()


def baseline(root: Path, sha: str) -> str:
    if git(root, "ls-tree", "--name-only", sha, "--", "VERSION"):
        value = git(root, "show", f"{sha}:VERSION")
        calendar_version.parse(value)
        return value
    # Historical releases had no VERSION. Preserve their existing time policy.
    timestamp, *parents = git(root, "show", "-s", "--format=%cI %P", sha).split()
    moment = datetime.fromisoformat(timestamp)
    if len(parents) == 2:
        previous, promoted = (datetime.fromisoformat(git(root, "show", "-s", "--format=%cI", parent))
                              for parent in parents)
        if promoted >= previous:
            moment = promoted
    return moment.astimezone(timezone.utc).strftime(calendar_version.FORMAT)


def check_package(root: Path) -> None:
    """Keep Python package metadata dynamic so bumps need no dependency resolution."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = project["project"]
    if ("version" in metadata or "version" not in metadata.get("dynamic", [])
            or project.get("tool", {}).get("setuptools", {}).get("dynamic", {}).get("version")
            != {"file": ["VERSION"]}):
        raise ValueError("Python package version must read the canonical VERSION file")
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = [package for package in lock.get("package", []) if package.get("name") == metadata["name"]]
    if len(packages) != 1 or packages[0].get("source") != {"editable": "."} or "version" in packages[0]:
        raise ValueError("Root package in uv.lock must use dynamic versioning; run uv lock")


def prepare(root: Path, base: str, *, check: bool = False, now: datetime | None = None) -> dict:
    path = root / MANIFEST
    original = path.read_text(encoding="utf-8")
    manifest = json.loads(original)
    if not isinstance(manifest, dict) or manifest.get("name") != "agents-core":
        raise ValueError("Expected the agents-core plugin manifest")
    base_sha = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_sha):
        raise ValueError("The release base did not resolve to a commit")
    released = baseline(root, base_sha)
    released_time = calendar_version.parse(released)
    current = calendar_version.read(root)
    if current is not None and calendar_version.parse(current) < released_time:
        raise ValueError("VERSION is behind main; reconcile main into dev through review first")
    if check:
        if current is None or calendar_version.parse(current) <= released_time:
            raise ValueError("VERSION must be greater than main; run python scripts/bump_plugin_version.py")
        if manifest.get("version") != current:
            raise ValueError("Plugin version must match the canonical VERSION file")
        check_package(root)
        return {"version": current, "released_version": released, "base_sha": base_sha,
                "changed": False, "checked": True}
    target = current
    if target is None or calendar_version.parse(target) == released_time:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("Preparation time must have a timezone")
        target = max(moment.astimezone(timezone.utc).replace(second=0, microsecond=0),
                     released_time + timedelta(minutes=1)).strftime(calendar_version.FORMAT)
    # Preserve formatting, and validate the entire write set before writing either file.
    updated, count = re.subn(r'("version"\s*:\s*")[^"\n]*(")',
                             lambda match: match[1] + target + match[2], original)
    if count != 1:
        raise ValueError("Expected exactly one unescaped version field; no files were changed")
    check_package(root)
    changed = current != target or updated != original
    if current != target:
        (root / "VERSION").write_text(target + "\n", encoding="utf-8")
    if updated != original:
        path.write_text(updated, encoding="utf-8")
    return {"version": target, "released_version": released, "base_sha": base_sha,
            "changed": changed, "checked": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="Published baseline ref or pinned SHA")
    parser.add_argument("--check", action="store_true", help="Validate only; never edit files")
    args = parser.parse_args(argv)
    try:
        result = prepare(ROOT, args.base, check=args.check)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Release version: {error}\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
