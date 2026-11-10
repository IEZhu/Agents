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
"""

from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))

# The checks over the content library. A new one belongs in this list and in a
# workflow's run command, which is the pairing this module exists to hold.
CONTENT_CHECKS = (
    "tests/test_skill_freshness.py",
    "tests/test_skill_invariants.py",
    "tests/test_source_check_classification.py",
)
SCHEDULED_BY_CALENDAR = "tests/test_skill_freshness.py"


def run_commands(path):
    """Every `run:` script in a workflow, as one text per step."""
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    out = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("run"):
                out.append(" ".join(step["run"].split()))
    return out


def workflows_running(test_file):
    return [p for p in WORKFLOWS if any(test_file in cmd for cmd in run_commands(p))]


def test_the_test_files_exist():
    for name in CONTENT_CHECKS:
        assert (ROOT / name).is_file(), f"{name} is listed here but not in the tree"


@pytest.mark.parametrize("test_file", CONTENT_CHECKS)
def test_a_workflow_names_the_file_in_a_run_command(test_file):
    """Naming it is the only proof. A `paths:` entry runs the workflow, not the file."""
    hosts = workflows_running(test_file)
    assert hosts, (
        f"no workflow runs {test_file}; a paths filter is not enough, "
        "the file has to appear in a run command"
    )


def test_the_calendar_check_has_a_schedule():
    """Its failure comes from the date, so no change triggers it."""
    for path in workflows_running(SCHEDULED_BY_CALENDAR):
        triggers = yaml.load(path.read_text(), Loader=yaml.BaseLoader).get("on") or {}
        if "schedule" in triggers:
            crons = [entry.get("cron") for entry in triggers["schedule"]]
            assert all(crons), f"{path.name} has a schedule with no cron"
            return
    pytest.fail(
        f"no workflow both runs {SCHEDULED_BY_CALENDAR} and carries a schedule; "
        "a path filter cannot reach a file that nobody edits"
    )


def test_the_helper_reads_a_run_command_it_is_pointed_at():
    """Guard the guard: a parser that finds nothing would pass every case above."""
    hosts = workflows_running(CONTENT_CHECKS[0])
    assert hosts, "the fixture for this assertion is the assertion above"
    commands = run_commands(hosts[0])
    assert commands, f"{hosts[0].name} parsed to no run commands"
    assert any(re.search(r"\bpytest\b", cmd) for cmd in commands), (
        f"{hosts[0].name} runs the file without pytest, which this module did not expect"
    )
