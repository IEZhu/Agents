"""Line-based merge of text files changed on two machines (``src.user_sync.merge``).

The unit tests check the diff3 rules and compare clean merges with ``git merge-file``; the
machine tests run two libraries against a local bare repository, as in ``test_user_sync``.
"""
from __future__ import annotations

import json
import random
import subprocess
import time

import pytest

from src import user_library
from src.user_sync import merge as merging
from tests.test_user_sync import (Machine, held_open, machine, pair, plain_git, records,  # noqa: F401 (fixtures)
                                  remote)

BASE = "# Shared\n\nintro\n\n## One\n\nfirst\n\n## Two\n\nsecond\n"


def merge(base: str, local: str, remote: str) -> tuple[str | None, int]:
    merged, overlaps = merging.merge_text(base.encode(), local.encode(), remote.encode())
    return (merged.decode() if merged is not None else None), overlaps


# --- the diff3 rules ---------------------------------------------------------------------------


def test_one_sided_and_identical_changes_take_that_text():
    assert merge(BASE, BASE, BASE.replace("first", "1st")) == (BASE.replace("first", "1st"), 0)
    assert merge(BASE, BASE.replace("first", "1st"), BASE) == (BASE.replace("first", "1st"), 0)
    same = BASE.replace("second", "2nd")
    assert merge(BASE, same, same) == (same, 0)


def test_changes_to_different_lines_merge():
    local = BASE.replace("first", "first, edited here")
    remote = BASE.replace("second", "second, edited there") + "\n## Three\n\nadded there\n"
    merged, overlaps = merge(BASE, local, remote)
    assert overlaps == 0
    assert merged == ("# Shared\n\nintro\n\n## One\n\nfirst, edited here\n\n## Two\n\n"
                      "second, edited there\n\n## Three\n\nadded there\n")


def test_deletions_and_insertions_on_separate_lines_merge():
    local = BASE.replace("intro\n\n", "")
    remote = BASE.replace("## Two\n", "## Two\n\nnew paragraph\n")
    assert merge(BASE, local, remote) == ("# Shared\n\n## One\n\nfirst\n\n## Two\n\nnew paragraph\n\nsecond\n", 0)


@pytest.mark.parametrize("local, remote, overlaps", [
    (BASE.replace("first", "A"), BASE.replace("first", "B"), 1),                 # one line, two texts
    (BASE + "local end\n", BASE + "remote end\n", 1),                            # two inserts at one place
    (BASE.replace("first", "A").replace("second", "A2"),
     BASE.replace("first", "B").replace("second", "B2"), 2),                     # two regions
])
def test_overlapping_changes_do_not_merge(local, remote, overlaps):
    assert merge(BASE, local, remote) == (None, overlaps)


def test_changes_on_adjacent_lines_overlap_as_in_git(tmp_path):
    base, local, remote = "x\na\nb\ny\n", "x\nA\nb\ny\n", "x\na\nB\ny\n"
    assert merge(base, local, remote) == (None, 1)
    assert git_merge_file(tmp_path, base, local, remote) is None


def test_line_endings_and_a_missing_final_newline_round_trip():
    base = "a\r\nb\r\nc\r\nd\r\ne"
    assert merge(base, base.replace("a\r", "A\r"), base.replace("e", "E")) == ("A\r\nb\r\nc\r\nd\r\nE", 0)
    # Only \n ends a line: a form feed or a Unicode line separator stays inside its line.
    odd = "a\fx\nb y\nc\nd\n"
    assert merge(odd, odd.replace("a\f", "A\f"), odd.replace("d\n", "D\n")) == ("A\fx\nb y\nc\nD\n", 0)


def test_repeated_lines_are_matched_within_a_bound():
    many = "x\n" * 20000
    started = time.perf_counter()
    assert merge(many, "A\n" + many, many + "B\n") == ("A\n" + many + "B\n", 0)  # edits at both ends
    middle = many[:20000] + "M\n" + many[20000:]
    assert merge(many, middle, many.replace("x", "y", 1))[1] == 0
    # Both sides changed the first and the last line differently: two overlaps, found at once.
    side = "A\n" + "x\n" * 1998 + "Z\n"
    assert merge("x\n" * 2000, side, "B\n" + "x\n" * 1998 + "Y\n") == (None, 2)
    assert merge("x\n" * 2000, side, "x\n" * 2000) == (side, 0)  # one side alone needs no matching
    # Alternating repeats made difflib's matcher cubic (12 s for 1,000 lines).
    alternating = "".join("x\n" if n % 2 else "z\n" for n in range(1000))
    merge("x\n" * 999, alternating, "y\n" + "x\n" * 998)
    # A rewrite of every line on one side: the search gives up within its bound.
    assert merge("x\n" * 50000, "y\n" * 50000, "z\n" * 50000) == (None, 0)
    assert time.perf_counter() - started < 30  # a guard against an unbounded search, not a benchmark


def test_the_line_search_keeps_a_longest_common_subsequence():
    def longest(a, b):
        table = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
        for i in range(len(a) - 1, -1, -1):
            for j in range(len(b) - 1, -1, -1):
                table[i][j] = table[i + 1][j + 1] + 1 if a[i] == b[j] else max(table[i + 1][j], table[i][j + 1])
        return table[0][0]
    generator = random.Random(1986)
    for _ in range(2000):
        a = [generator.choice("xyz") for _ in range(generator.randint(0, 20))]
        b = [generator.choice("xyz") for _ in range(generator.randint(0, 20))]
        pairs = merging._common(a, b, 10 ** 9)
        assert all(a[i] == b[j] for i, j in pairs)
        assert all(i1 < i2 and j1 < j2 for (i1, j1), (i2, j2) in zip(pairs, pairs[1:]))
        assert len(pairs) == longest(a, b), (a, b, pairs)
    assert merging._common(["x"] * 1000, ["y"] * 1000, 1000) is None


@pytest.mark.parametrize("path, base, reason", [
    ("common/shared.md", None, "no_common_base"),
    ("personas/common/shared.json", b"{}\n", "not_text"),
    ("common/shared.meta.json", b"{}\n", "not_text"),
    ("common/shared.md", b"\xff\xfe", "not_text"),
    ("common/shared.md", b"a\0b\n", "not_text"),
    ("common/shared.md", b"x" * (256 * 1024 + 1), "too_large"),
    (".agents-library.json", b"{}\n", "not_text"),
    ("repos/github.com-me-project/history/laptop/2026-10.md", b"a\n", "not_text"),
], ids=["no-base", "persona", "marker", "not-utf8", "nul", "over-256k", "library-file", "history-segment"])
def test_what_is_not_merged_line_by_line(path, base, reason):  # short ids: a 256 KiB one stalled the Windows log
    assert merging.text_merge_refusal(path, base, b"local\n", b"remote\n") == reason


@pytest.mark.parametrize("path", ["common/shared.md", "common/day-close/collect.py",
                                  "repos/github.com-me-project/deploy.md"])
def test_flows_and_scripts_are_merged_line_by_line(path):
    assert merging.text_merge_refusal(path, b"a\n", b"b\n", b"c\n") is None


def git_merge_file(tmp_path, base: str, local: str, remote: str) -> str | None:
    files = []
    for name, text in (("local", local), ("base", base), ("remote", remote)):
        path = tmp_path / name
        path.write_bytes(text.encode())
        files.append(str(path))
    done = subprocess.run(["git", "merge-file", "-p", *files], capture_output=True)
    return done.stdout.decode() if done.returncode == 0 else None


def test_clean_merges_match_git_merge_file(tmp_path):
    """Random edits separated by unchanged lines merge, and to what ``git merge-file`` writes."""
    generator = random.Random(20261010)
    for _ in range(200):
        base = [f"line {n}\n" for n in range(generator.randint(6, 40))]
        local, remote = list(base), list(base)
        # Edit alternating blocks: local takes the even ones, remote the odd ones, a gap between.
        blocks = list(range(0, len(base) - 2, 3))
        for number, start in enumerate(blocks):
            side = local if number % 2 == 0 else remote
            choice = generator.choice(("change", "delete", "insert"))
            if choice == "change":
                side[start] = f"changed {start} on {'local' if side is local else 'remote'}\n"
            elif choice == "delete":
                side[start] = ""
            else:
                side[start] = side[start] + f"inserted after {start}\n"
        texts = ["".join(text) for text in (base, local, remote)]
        merged, overlaps = merge(*texts)
        assert overlaps == 0, texts
        assert merged == git_merge_file(tmp_path, *texts), texts


def test_random_overlaps_never_merge_silently(tmp_path):
    """When both sides change one line differently, the merge refuses, as git does."""
    generator = random.Random(7)
    for _ in range(100):
        base = [f"line {n}\n" for n in range(generator.randint(3, 20))]
        line = generator.randrange(len(base))
        local, remote = list(base), list(base)
        local[line], remote[line] = "local edit\n", "remote edit\n"
        texts = ["".join(text) for text in (base, local, remote)]
        assert merge(*texts)[0] is None
        assert git_merge_file(tmp_path, *texts) is None


# --- two machines -------------------------------------------------------------------------------


@pytest.fixture
def started(pair):
    """Both machines hold ``BASE`` as user:shared, synced."""
    a, b = pair
    a.save("user:shared", BASE)
    a.run()
    b.run()
    assert b.text("common/shared.md") == BASE
    return a, b


def head_message(m: Machine) -> str:
    return plain_git("--git-dir", str(m.remote), "log", "-1", "--format=%B", "main")


def test_a_flow_edited_on_two_machines_merges_on_both(started):
    a, b = started
    a.save("user:shared", BASE.replace("first", "first, edited on A"))
    b.save("user:shared", BASE.replace("second", "second, edited on B"))
    a.run()
    result = b.run()
    both = BASE.replace("first", "first, edited on A").replace("second", "second, edited on B")
    assert result["status"] == "synced" and result["conflicts"] == []
    [merged] = result["merged"]
    assert (merged["path"], merged["flow"]) == ("common/shared.md", "user:shared")
    assert b.text("common/shared.md") == both
    assert b.version_text(merged["local_version"]) == BASE.replace("second", "second, edited on B")
    assert "merged 1 file line by line" in head_message(b)
    assert b.sync.status()["activity"][-1]["merged"] == ["user:shared"]
    a.run()
    assert a.text("common/shared.md") == both
    assert records(a) == records(b) == []
    assert any(item["version"] in merged["local_version"]
               for item in b.flows.get("user:shared")["history"])


def test_a_merge_this_machine_could_not_write_is_not_reported_until_it_lands(started, monkeypatch):
    a, b = started
    a.save("user:shared", BASE.replace("first", "first, edited on A"))
    b.save("user:shared", BASE.replace("second", "second, edited on B"))
    a.run()
    held_open(monkeypatch, {b.lib / "common" / "shared.md"})  # a reader keeps the file open
    result = b.run()
    assert result["merged"] == [] and "merged" not in b.sync.status()["activity"][-1]
    assert b.sync._state()["held_remote"] == ["common/shared.md"]
    assert "merged 1 file line by line" in head_message(b)  # the pushed commit carries the merge
    monkeypatch.undo()
    landed = b.run()
    both = BASE.replace("first", "first, edited on A").replace("second", "second, edited on B")
    assert b.text("common/shared.md") == both and records(b) == []
    assert [item["path"] for item in landed["merged"]] == ["common/shared.md"]  # reported once it lands
    assert b.sync.status()["activity"][-1]["merged"] == ["user:shared"]
    a.run()
    assert a.text("common/shared.md") == both


SCRIPT = "import sys\n\n\ndef one():\n    return 1\n\n\ndef two():\n    return 2\n"


def test_a_held_merge_whose_push_is_rejected_keeps_the_local_text(started, monkeypatch):
    """The merge commit never reached the remote: the next cycle must not settle the file with
    the remote's version as if the merge had landed (review of #277)."""
    a, b = started
    user_library.atomic_write(a.lib / "common/tool/run.py", SCRIPT.encode())
    a.run()
    b.run()
    user_library.atomic_write(a.lib / "common/tool/run.py", SCRIPT.replace("return 1", "return 10").encode())
    user_library.atomic_write(b.lib / "common/tool/run.py", SCRIPT.replace("return 2", "return 20").encode())
    a.run()
    held_open(monkeypatch, {b.lib / "common" / "tool" / "run.py"})
    real_push, calls = b.sync._push, []

    def push_after_a(*args, **kwargs):
        if not calls:
            monkeypatch.undo()  # the reader closes the file; this also drops the wrapper
            a.save("user:other", "# Other\n")
            assert a.run()["pushed"]  # A gets in first: B's merge commit is rejected
        calls.append(1)
        return real_push(*args, **kwargs)

    monkeypatch.setattr(b.sync, "_push", push_after_a)
    b.run()
    b.run()
    a.run()
    [record] = records(b)
    assert (record["path"], record["reason"]) == ("common/tool/run.py", "held_open")
    assert "return 20" in record["local_content"]  # B's edit is kept, and the record says why
    assert "return 10" in b.text("common/tool/run.py")


def test_a_script_edited_on_two_machines_merges(started):
    a, b = started
    script = SCRIPT
    for m in (a,):
        user_library.atomic_write(m.lib / "common/tool/run.py", script.encode())
    a.run()
    b.run()
    user_library.atomic_write(a.lib / "common/tool/run.py", script.replace("return 1", "return 10").encode())
    user_library.atomic_write(b.lib / "common/tool/run.py", script.replace("return 2", "return 20").encode())
    a.run()
    result = b.run()
    assert result["conflicts"] == [] and [m["path"] for m in result["merged"]] == ["common/tool/run.py"]
    assert "local_version" not in result["merged"][0]  # only a flow's text becomes a .history version
    a.run()
    for m in (a, b):
        assert m.text("common/tool/run.py") == script.replace("return 1", "return 10").replace("return 2", "return 20")


def test_overlapping_edits_are_a_conflict_that_says_why(started):
    a, b = started
    a.save("user:shared", BASE.replace("first", "A's first"))
    b.save("user:shared", BASE.replace("first", "B's first").replace("second", "B's second"))
    a.run()
    result = b.run()
    assert result["merged"] == []
    [record] = records(b)
    assert (record["kind"], record["reason"], record["overlaps"]) == ("both_changed", "overlapping_changes", 1)
    assert b.text("common/shared.md") == BASE.replace("first", "A's first")
    assert b.version_text(record["local_version"]) == BASE.replace("first", "B's first").replace("second", "B's second")
    assert "1 conflict" in head_message(b)


def test_a_join_without_a_common_base_says_so(machine):
    a, b = machine("a"), machine("b")
    a.save("user:shared", BASE.replace("first", "on A"))
    a.connect()
    b.save("user:shared", BASE.replace("second", "on B"))
    b.sync.setup(remote=str(b.remote), name="Owner", email="owner@example.com", label="machine-b")
    b.sync.check()
    preview = b.sync.preview(connection_mode="merge")
    assert [(c["path"], c["kind"], c["reason"]) for c in preview["conflicts"]] == \
        [("common/shared.md", "both_changed", "no_common_base")]
    assert preview["merged"] == []
    b.sync.start(preview["hash"], connection_mode="merge")
    [record] = records(b)
    assert record["reason"] == "no_common_base"


def test_a_persona_overlay_changed_on_two_machines_is_not_merged(started):
    a, b = started
    for m, agent in ((a, "software_engineer"), (b, "code_reviewer")):
        m.flows.user_dir.joinpath("personas", "common").mkdir(parents=True, exist_ok=True)
        user_library.atomic_write(m.lib / "personas/common/shared.json",
                                  json.dumps({"persona": {"agent": agent}}, indent=2).encode() + b"\n")
    a.run()
    b.run()
    [record] = records(b)
    assert record["path"] == "personas/common/shared.json" and record["reason"] == "not_text"


def test_the_preview_lists_a_merge_and_its_hash_covers_the_merged_text(started):
    a, b = started
    a.save("user:shared", BASE.replace("first", "first on A"))
    a.run()
    b.save("user:shared", BASE.replace("second", "second on B"))
    preview = b.sync.preview()
    assert [(m["path"], m["flow"]) for m in preview["merged"]] == [("common/shared.md", "user:shared")]
    assert preview["conflicts"] == []
    b.save("user:shared", BASE.replace("second", "second on B, again"))
    assert b.sync.preview()["hash"] != preview["hash"]


def test_flow_tools_report_an_open_conflict_until_it_is_resolved(started, tmp_path):
    from src.flows import execution_bundle

    a, b = started
    a.save("user:shared", BASE.replace("first", "A's first"))
    b.save("user:shared", BASE.replace("first", "B's first"))
    a.run()
    b.run()
    [record] = records(b)
    flow = b.flows.get("user:shared")["flow"]
    [reported] = flow["sync_conflicts"]
    assert (reported["id"], reported["reason"], reported["overlaps"]) == (record["id"], "overlapping_changes", 1)
    assert reported["local_version"] == record["local_version"] and reported["machine"] == "machine-b"
    listed = {entry["id"]: entry for entry in b.flows.list("user")["flows"]}
    assert listed["user:shared"]["sync_conflicts"] == flow["sync_conflicts"]
    bundle = execution_bundle(b.flows.resolve("user:shared"), tmp_path, None, "")
    assert bundle["flow"]["sync_conflicts"] and "flow.sync_conflicts" in bundle["instruction"]
    assert "sync_conflicts" not in a.flows.get("user:shared")["flow"]  # the record reaches A with the next run
    a.run()
    assert a.flows.get("user:shared")["flow"]["sync_conflicts"][0]["id"] == record["id"]

    b.sync.resolve(record["id"], "keep")
    assert "sync_conflicts" not in b.flows.get("user:shared")["flow"]
    b.save("user:other", "# Other\n")
    assert "sync_conflicts" not in b.flows.get("user:other")["flow"]


def test_a_merge_overruled_by_a_retry_is_not_reported(started, monkeypatch):
    """B merges, its push is rejected, and the retry finds an overlap with what A pushed meanwhile:
    the run reports the conflict, not the merge it discarded."""
    a, b = started
    a.save("user:shared", BASE.replace("first", "first, edited on A"))
    b.save("user:shared", BASE.replace("second", "second, edited on B"))
    a.run()
    real_push, calls = b.sync._push, []

    def push_after_a(*args, **kwargs):
        if not calls:
            current = a.flows.get("user:shared")["content"]
            a.save("user:shared", current.replace("second", "second, edited on A too"))
            assert a.run()["pushed"]
        calls.append(1)
        return real_push(*args, **kwargs)

    monkeypatch.setattr(b.sync, "_push", push_after_a)
    result = b.run()
    assert len(calls) == 2 and result["merged"] == []
    [conflict] = result["conflicts"]
    assert (conflict["path"], conflict["reason"]) == ("common/shared.md", "overlapping_changes")


def test_a_merge_after_an_earlier_attempt_s_conflict_is_reported(started, monkeypatch):
    """Attempt 1 records a conflict; while its push runs, the owner edits the file and A pushes
    again; attempt 2 merges cleanly. The run reports both, the merge included (review of #277)."""
    a, b = started
    a.save("user:shared", BASE.replace("first", "first, A"))
    b.save("user:shared", BASE.replace("first", "first, B"))
    a.run()
    real_push, calls = b.sync._push, []

    def push_after_edits(*args, **kwargs):
        if not calls:
            here = b.flows.get("user:shared")["content"]
            b.save("user:shared", here.replace("intro", "intro, B later"))
            a.save("user:shared", a.flows.get("user:shared")["content"].replace("second", "second, A"))
            assert a.run()["pushed"]
        calls.append(1)
        return real_push(*args, **kwargs)

    monkeypatch.setattr(b.sync, "_push", push_after_edits)
    result = b.run()
    text = b.text("common/shared.md")
    assert "intro, B later" in text and "second, A" in text
    assert [item["path"] for item in result["merged"]] == ["common/shared.md"]
    assert [conflict["reason"] for conflict in result["conflicts"]] == ["overlapping_changes"]
