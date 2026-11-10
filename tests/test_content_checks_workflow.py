"""The content checks are wired to a workflow that actually runs them.

Written because they were not. `tests/test_skill_invariants.py`,
`tests/test_skill_freshness.py` and `tests/test_source_check_classification.py`
were committed with 246 cases and no workflow naming any of them, so they were
green on one machine and never in CI (#608). Every workflow here runs an
explicit list of files, which means a path filter alone proves nothing: a glob
runs the workflow, not your test.

The freshness check needs more than a path filter. It fails on the earlier of a
skill's rolling window and its pinned `review_on` date, so the file nobody
touches is the one it exists to catch, and only a schedule reaches that file.

The match is deliberately strict in two ways, both of them from review. A file
counts only inside a command that invokes `pytest`, so an `echo` or a shell
comment naming it is not evidence; and it must appear as a whole argument, so
`...freshness.py.bak` does not satisfy the guard for `...freshness.py`. The
cost is one false negative: a workflow that ran `pytest tests/` would execute
these files without naming them and would still fail here. That is the
intended trade, because naming them is the property worth holding.
"""

from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))

# The checks over the content library. A new one belongs in this list and in a
# workflow's pytest command, which is the pairing this module exists to hold.
CONTENT_CHECKS = (
    "tests/test_skill_freshness.py",
    "tests/test_skill_invariants.py",
    "tests/test_source_check_classification.py",
)
SCHEDULED_BY_CALENDAR = "tests/test_skill_freshness.py"
PYTEST_CALL = re.compile(r"(?:^|\s|/)pytest(?:\s|$)")


def logical_commands(script):
    """Split one `run:` script into commands, as a shell would read them.

    Backslash continuations are joined first, then comment-only lines dropped:
    three steps in this repository use a continuation and four carry a comment,
    so neither can be skipped. Whitespace is normalised last, which is what
    lets a folded YAML scalar and a block scalar compare the same way.
    """
    joined = re.sub(r"\\\s*\n\s*", " ", script)
    out = []
    for line in joined.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(" ".join(line.split()))
    return out


def run_commands(path):
    """Every command of every `run:` step in a workflow."""
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    out = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("run"):
                out.extend(logical_commands(step["run"]))
    return out


def runs_as_pytest_argument(commands, test_file):
    """True when some command calls pytest and passes this file as its own argument."""
    whole_argument = re.compile(rf"(?<!\S){re.escape(test_file)}(?!\S)")
    return any(PYTEST_CALL.search(cmd) and whole_argument.search(cmd) for cmd in commands)


def workflows_running(test_file):
    return [p for p in WORKFLOWS if runs_as_pytest_argument(run_commands(p), test_file)]


def test_the_test_files_exist():
    for name in CONTENT_CHECKS:
        assert (ROOT / name).is_file(), f"{name} is listed here but not in the tree"


@pytest.mark.parametrize("test_file", CONTENT_CHECKS)
def test_a_workflow_passes_the_file_to_pytest(test_file):
    """Naming it in a pytest command is the only proof; a `paths:` entry is not."""
    hosts = workflows_running(test_file)
    assert hosts, (
        f"no workflow passes {test_file} to pytest; a paths filter is not enough, "
        "the file has to be an argument of a pytest command"
    )


def test_the_calendar_check_has_a_schedule_with_an_entry():
    """Its failure comes from the date, so no change triggers it."""
    for path in workflows_running(SCHEDULED_BY_CALENDAR):
        triggers = yaml.load(path.read_text(), Loader=yaml.BaseLoader).get("on") or {}
        crons = [entry.get("cron") for entry in (triggers.get("schedule") or [])]
        if crons and all(crons):
            return
    pytest.fail(
        f"no workflow both runs {SCHEDULED_BY_CALENDAR} and carries a schedule with a "
        "cron; a path filter cannot reach a file that nobody edits"
    )


# Guard the guard. Every assertion above rests on runs_as_pytest_argument, and a
# matcher that said yes to anything, or no to everything, would make them all
# vacuous. Review caught the first of those: the original matched the file name
# anywhere in any run script, so an echo would have counted as execution.

@pytest.mark.parametrize("command", [
    "python -B -m pytest tests/test_skill_freshness.py -q",
    "pytest tests/test_skill_freshness.py",
    "python -m pytest tests/other.py tests/test_skill_freshness.py tests/more.py -q",
    "/usr/bin/pytest tests/test_skill_freshness.py",
])
def test_the_matcher_accepts_a_real_pytest_invocation(command):
    assert runs_as_pytest_argument([command], "tests/test_skill_freshness.py")


@pytest.mark.parametrize("command", [
    "echo tests/test_skill_freshness.py",
    "python -m pytest tests/test_skill_freshness.py.bak -q",
    "python -m pytest tests/test_skill_freshness.pyc -q",
    "python -m pytest extra/tests/test_skill_freshness.py -q",
    "python -m pytest tests/test_skill_invariants.py -q",
    "ruff check tests/test_skill_freshness.py",
])
def test_the_matcher_rejects_what_does_not_run_the_file(command):
    assert not runs_as_pytest_argument([command], "tests/test_skill_freshness.py")


@pytest.mark.parametrize("script, expected", [
    ("# pytest tests/test_skill_freshness.py was here\npython -m pytest tests/other.py",
     ["python -m pytest tests/other.py"]),
    ("python -m pytest \\\n  tests/test_skill_freshness.py -q",
     ["python -m pytest tests/test_skill_freshness.py -q"]),
    ("  python -B   -m  pytest   tests/a.py  ", ["python -B -m pytest tests/a.py"]),
    ("first\n\n# a comment\nsecond", ["first", "second"]),
])
def test_the_parser_reads_a_script_the_way_a_shell_would(script, expected):
    """A comment is not a command, and a continuation is not two of them."""
    assert logical_commands(script) == expected


def test_a_commented_out_pytest_line_is_not_evidence():
    """The whole point of the parser: the file is named, and still not run."""
    script = "# python -m pytest tests/test_skill_freshness.py -q\necho done"
    assert not runs_as_pytest_argument(logical_commands(script), "tests/test_skill_freshness.py")


def test_the_parser_reads_the_steps_of_the_workflow_it_is_given():
    """A parser returning nothing would pass every assertion above it."""
    hosts = workflows_running(CONTENT_CHECKS[0])
    assert hosts, "the fixture for this assertion is the first assertion above"
    commands = run_commands(hosts[0])
    assert commands, f"{hosts[0].name} parsed to no run commands"
    assert any(PYTEST_CALL.search(cmd) for cmd in commands)
