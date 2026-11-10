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

Three things here are strict because review found each of them loose. A file
counts only inside a command that invokes `pytest`, so an `echo` or a shell
comment naming it is not evidence. It must appear as a whole argument, so
`...freshness.py.bak` does not stand in for `...freshness.py`. And the schedule
must carry a usable cron *and* reach a step no condition can skip, because a
workflow can have a schedule and still never run this file on it.

The cost is one false negative: a workflow running `pytest tests/` would
execute these files without naming them and would still fail here. That is the
intended trade, because being named is the property worth holding.
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


def load_workflow(path):
    """Parse a workflow, keeping GitHub's keys as written.

    Not `yaml.safe_load`: that resolves the bare `on` key to the boolean True
    and the trigger lookup below stops finding it. `BaseLoader` also builds
    nothing but strings, lists and dicts, so it constructs no Python objects --
    which is the reason a static-analysis warning about `yaml.load` is wrong
    here. No linter runs in this repository, so there is no marker to add.
    """
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


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


def runs_as_pytest_argument(commands, test_file):
    """True when some command calls pytest and passes this file as its own argument."""
    whole_argument = re.compile(rf"(?<!\S){re.escape(test_file)}(?!\S)")
    return any(PYTEST_CALL.search(cmd) and whole_argument.search(cmd) for cmd in commands)


def pytest_steps(path, test_file):
    """Every (job name, job, step) whose run script passes the file to pytest."""
    found = []
    for job_name, job in (load_workflow(path).get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if not (isinstance(step, dict) and step.get("run")):
                continue
            if runs_as_pytest_argument(logical_commands(step["run"]), test_file):
                found.append((job_name, job, step))
    return found


def run_commands(path):
    """Every command of every `run:` step in a workflow."""
    out = []
    for job in (load_workflow(path).get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("run"):
                out.extend(logical_commands(step["run"]))
    return out


def workflows_running(test_file):
    """Every workflow with at least one step that passes this file to pytest."""
    return [p for p in WORKFLOWS if pytest_steps(p, test_file)]


def schedule_crons(workflow):
    """The cron expressions of a workflow's schedule, or why it has none.

    Returns `(crons, problem)`. `BaseLoader` hands back exactly what was
    written, so a schedule given as bare strings instead of mappings would make
    a plain `entry.get("cron")` raise AttributeError; it is reported instead.
    """
    entries = ((workflow.get("on") or {}).get("schedule")) or []
    if not isinstance(entries, list):
        return [], f"schedule is a {type(entries).__name__}, not a list"
    not_mappings = [entry for entry in entries if not isinstance(entry, dict)]
    if not_mappings:
        return [], f"schedule entries are not mappings: {not_mappings!r}"
    return [entry.get("cron") for entry in entries], None


def is_unconditional(job, step):
    """A step a scheduled run cannot skip: no `if:` on the job or on the step."""
    return "if" not in job and "if" not in step


def test_the_test_files_exist():
    """A name in CONTENT_CHECKS that no longer exists would make its case vacuous."""
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


def test_the_calendar_check_runs_on_a_schedule_nothing_can_skip():
    """Its failure comes from the date, so no change triggers it.

    A schedule trigger alone is not enough twice over: its entries have to
    carry a cron, and the step running this file has to be one a scheduled run
    reaches rather than one behind an `if:`.
    """
    reasons = []
    for path in workflows_running(SCHEDULED_BY_CALENDAR):
        crons, problem = schedule_crons(load_workflow(path))
        if problem:
            reasons.append(f"{path.name}: {problem}")
        elif not crons:
            reasons.append(f"{path.name}: runs it but has no schedule entry")
        elif not all(crons):
            reasons.append(f"{path.name}: a schedule entry has no cron ({crons!r})")
        elif not any(
            is_unconditional(job, step)
            for _, job, step in pytest_steps(path, SCHEDULED_BY_CALENDAR)
        ):
            reasons.append(
                f"{path.name}: every step running it sits behind an `if:`, "
                "so a scheduled run may skip it"
            )
        else:
            return
    pytest.fail(
        f"nothing runs {SCHEDULED_BY_CALENDAR} on a schedule a run cannot skip; "
        "a path filter never reaches a file nobody edits. "
        + ("Candidates: " + "; ".join(reasons) if reasons else "No workflow runs it at all.")
    )


# Guard the guard. Every assertion above rests on these helpers, and one that
# said yes to anything, or no to everything, would make them all vacuous.
# Review caught two such: the matcher accepted the file name anywhere in any run
# script, so an echo counted as execution; and the calendar case accepted an
# empty schedule, because all([]) is true.

@pytest.mark.parametrize("command", [
    "python -B -m pytest tests/test_skill_freshness.py -q",
    "pytest tests/test_skill_freshness.py",
    "python -m pytest tests/other.py tests/test_skill_freshness.py tests/more.py -q",
    "/usr/bin/pytest tests/test_skill_freshness.py",
])
def test_the_matcher_accepts_a_real_pytest_invocation(command):
    """The forms this repository actually writes: python -m, bare, mid-list, absolute."""
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
    """Naming it is not running it: an echo, a neighbouring path, a suffix, another tool."""
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


@pytest.mark.parametrize("schedule, crons, problem_words", [
    ([{"cron": "17 5 * * 1"}], ["17 5 * * 1"], None),
    ([{"cron": "0 5 * * 1"}, {"cron": "0 5 * * 4"}], ["0 5 * * 1", "0 5 * * 4"], None),
    ([], [], None),
    ([{"cron": ""}], [""], None),
    ([{}], [None], None),
    (["17 5 * * 1"], [], "not mappings"),
    ("17 5 * * 1", [], "not a list"),
])
def test_the_schedule_reader_reports_a_malformed_entry_instead_of_raising(
    schedule, crons, problem_words
):
    """BaseLoader returns what was written, so bare strings must not explode."""
    got, problem = schedule_crons({"on": {"schedule": schedule}})
    assert got == crons
    if problem_words is None:
        assert problem is None
    else:
        assert problem and problem_words in problem


def test_the_schedule_reader_handles_a_workflow_with_no_schedule():
    """No schedule is not malformed, so it reports no crons and no problem."""
    assert schedule_crons({"on": {"pull_request": None}}) == ([], None)
    assert schedule_crons({}) == ([], None)


@pytest.mark.parametrize("job, step, expected", [
    ({"steps": []}, {"run": "pytest x"}, True),
    ({"if": "github.event_name == 'pull_request'"}, {"run": "pytest x"}, False),
    ({}, {"run": "pytest x", "if": "github.event_name != 'schedule'"}, False),
    ({"if": "always()"}, {"run": "pytest x", "if": "always()"}, False),
])
def test_a_condition_on_the_job_or_the_step_disqualifies_it(job, step, expected):
    """An `if:` may be harmless, but it may also exclude the scheduled event.

    The guard refuses to judge which, because getting that wrong is the silent
    failure it exists to prevent: a workflow that looks scheduled and never
    runs this file on the calendar.
    """
    assert is_unconditional(job, step) is expected


def test_the_parser_reads_the_steps_of_the_workflow_it_is_given():
    """A parser returning nothing would pass every assertion above it."""
    hosts = workflows_running(CONTENT_CHECKS[0])
    assert hosts, "the fixture for this assertion is the first assertion above"
    commands = run_commands(hosts[0])
    assert commands, f"{hosts[0].name} parsed to no run commands"
    assert any(PYTEST_CALL.search(cmd) for cmd in commands)
    assert pytest_steps(hosts[0], CONTENT_CHECKS[0]), "the step finder lost the step"
