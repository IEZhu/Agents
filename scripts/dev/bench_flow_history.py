#!/usr/bin/env python3
"""Time opening a flow with the cached library history (#371).

Generates a library with at least 200 history commits in a temporary directory,
or, with ``--library``, copies an existing library there first: the given
library is only read. Prints each timing in milliseconds and checks the budget:
the flow's text (what ``GET /ui/api/flow`` returns) within 300 ms on a cold
cache, and the whole flow with its versions (MCP ``get_flow``) within 150 ms warm.

    .venv/bin/python scripts/dev/bench_flow_history.py
    .venv/bin/python scripts/dev/bench_flow_history.py --library flows/.user --flow user:day-close
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import library_history  # noqa: E402
from src.file_lock import file_lock  # noqa: E402
from src.flows import FlowCatalog  # noqa: E402
from src.library_history import LibraryHistory  # noqa: E402
from src.user_flows import FlowLibrary  # noqa: E402

CONTENT_COLD_MS = 300
OPEN_WARM_MS = 150


def generate(root: Path, commits: int, flows: int) -> str:
    """A library whose history has ``commits`` saves spread over ``flows`` flows."""
    with file_lock(root / ".lock"):
        history = LibraryHistory(root)
        history.ensure()
        texts = {}
        for number in range(commits):
            path = f"common/flow-{number % flows}.md"
            texts[path] = f"# Flow {number % flows}\n\n" + "".join(
                f"- step {line} of revision {number}\n" for line in range(40))
            history._commit({path: texts[path].encode()}, {
                "action": "save", "path": path, "paths": [path], "archive": False, "deleted": False,
                "time": datetime.now(timezone.utc).isoformat(timespec="microseconds")})
        (root / "common").mkdir(exist_ok=True)
        for path, text in texts.items():  # the working files match the history tip
            (root / path).write_text(text, encoding="utf-8")
    return "user:flow-0"


def timed(action) -> float:
    start = time.perf_counter()
    action()
    return (time.perf_counter() - start) * 1000


def measure(root: Path, name: str, repeat: int) -> dict:
    library = lambda: FlowLibrary(FlowCatalog(), user_dir=root)  # a new one per request, as the daemon does

    def cold(action):
        samples = []
        for _ in range(repeat):
            library_history.clear_cache()
            samples.append(timed(action))
        return statistics.median(samples)

    def warm(action):
        action()
        return statistics.median(timed(action) for _ in range(repeat))

    content = lambda: library().get(name, with_history=False)
    versions = lambda: library().versions(name)
    whole = lambda: library().get(name)
    count = len(versions()["history"])
    results = {
        "versions_listed": count,
        "content_cold_ms": cold(content), "content_warm_ms": warm(content),
        "versions_cold_ms": cold(versions), "versions_warm_ms": warm(versions),
        "get_cold_ms": cold(whole), "get_warm_ms": warm(whole),
    }
    with file_lock(root / ".lock"):
        history = LibraryHistory(root)
        history._ensure_repository()
        results["commits"] = len(history._rev_list([oid for _, oid in history._tips() or ()]))
    return results


def save_then_versions(root: Path, name: str) -> float:
    """One more save, then the versions: the cache adds only the new commits."""
    library = FlowLibrary(FlowCatalog(), user_dir=root)
    library.versions(name)
    current = library.get(name, with_history=False)["flow"]["revision"]
    library.save(name, f"# Saved by the benchmark at {time.time()}\n", expected_revision=current)
    return timed(lambda: FlowLibrary(FlowCatalog(), user_dir=root).versions(name))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--library", type=Path, help="time a copy of this library instead of a generated one")
    parser.add_argument("--flow", default=None, help="flow to open (default: user:flow-0, or required with --library)")
    parser.add_argument("--commits", type=int, default=240, help="history commits of the generated library")
    parser.add_argument("--flows", type=int, default=12, help="flows of the generated library")
    parser.add_argument("--repeat", type=int, default=5)
    arguments = parser.parse_args()
    if arguments.library and not arguments.flow:
        parser.error("--library needs --flow")
    with tempfile.TemporaryDirectory(prefix="flow-history-bench-") as directory:
        root = Path(directory) / "library"
        if arguments.library:
            shutil.copytree(arguments.library.expanduser().resolve(), root, symlinks=True)
            name = arguments.flow
        else:
            name = generate(root, arguments.commits, arguments.flows)
        results = measure(root, name, arguments.repeat)
        results["save_then_versions_ms"] = save_then_versions(root, name)
    results = {key: round(value, 1) if isinstance(value, float) else value for key, value in results.items()}
    results["budget"] = {f"content_cold<={CONTENT_COLD_MS}ms": results["content_cold_ms"] <= CONTENT_COLD_MS,
                         f"get_warm<={OPEN_WARM_MS}ms": results["get_warm_ms"] <= OPEN_WARM_MS}
    print(json.dumps({"library": str(arguments.library or "generated"), "flow": name, **results}, indent=2))
    return 0 if all(results["budget"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
