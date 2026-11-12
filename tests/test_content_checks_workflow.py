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

What this module reads is a command, not a word and not a path. The reader
finds the command position first, stepping over the prefixes this repository
really writes -- a leading `NAME=value`, which is the form `CLAUDE.md` and
`tests/README.md` prescribe, an `env` with its assignments, and `uv run` or
`uvx` -- and then requires pytest, a path ending in it, or an interpreter whose
`-m` names pytest. After that it separates pytest's options from the paths it
will collect, expands a glob against the tree because the shell does, and asks
whether any of those paths is this file. So `echo pytest x.py` is not evidence,
`--collect-only` and its undashed spellings are not, a whole-file `--deselect`
is not, and `...freshness.py.bak` is not; while a glob, a quoted path and a
`./` spelling all are, because each of them really runs the file.

The schedule is read twice as hard. A cron has to be one GitHub Actions can
schedule, field by field *and* as a whole, because `99 5 * * 1` and
`17 5 31 2 *` both parse as five fields and both run never. And the step has to
be one a scheduled run reaches: not behind an `if:`, not in a job that needs a
job gated to pull requests, not behind an `environment:`, not `continue-on-error`
on either, and not under a `shell:` that prints the script instead of running it.
Every one of those passed this guard until an adversarial pass found it.

Two limits are real and stated rather than solved. A command can run the file
without naming it -- `pytest tests/` collects the directory, and a file list
held in a shell variable expands outside this reader -- and both still fail
here, which is the intended trade: being named is what makes dropping a file
visible. And a static read cannot tell whether a script's pytest line is
reached. A step that defines a shell function called pytest, writes its own
stub on PATH, puts the command in a here-document or after an `exit 0`, or
backgrounds it with `&`, satisfies every check above and runs nothing; closing
that would mean executing the script rather than reading it, which only a real
workflow run can do.
"""

from pathlib import Path
import fnmatch
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
# Both extensions: GitHub accepts `.yml` and `.yaml`, and a workflow saved with
# the other one would have gone unseen here, failing a correct repository.
WORKFLOWS = sorted(
    path
    for pattern in ("*.yml", "*.yaml")
    for path in (ROOT / ".github/workflows").glob(pattern)
)

# The checks over the content library. A new one belongs in this list and in a
# workflow's pytest command, which is the pairing this module exists to hold.
CONTENT_CHECKS = (
    "tests/test_skill_freshness.py",
    "tests/test_skill_invariants.py",
    "tests/test_source_check_classification.py",
)
SCHEDULED_BY_CALENDAR = "tests/test_skill_freshness.py"
# pytest has to be what the command runs, not a word inside it: `echo pytest
# x.py` names both and runs neither. The reader below finds the command position
# first, stepping over the three prefixes this repository and its own
# documentation actually write -- a leading `NAME=value`, an `env` with its
# assignments, and `uv run` or `uvx`.
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PYTEST_EXECUTABLE = re.compile(r"^(?:\S*/)?pytest$")
PYTHON_EXECUTABLE = re.compile(r"^(?:\S*/)?python[\d.]*$")
# Interpreter options that take the next token, so `python -W error -m pytest`
# and `python -X dev -m pytest` are still pytest.
PYTHON_VALUE_OPTIONS = frozenset({"-W", "-X", "-c", "-m"})
# pytest options that take the next token, so their value is not a path pytest
# will collect. An option missing here keeps its value in the path list, which
# can only make this guard readier to believe a file runs, and only if that
# value is itself one of the content checks.
VALUE_OPTIONS = frozenset({
    "-k", "-m", "-p", "-o", "-c", "-W", "-n", "--deselect", "--ignore",
    "--ignore-glob", "--rootdir", "--maxfail", "--durations", "--junitxml",
    "--override-ini", "--confcutdir", "--import-mode", "--tb", "--log-level",
})
# A `#` that begins a token comments out the rest of the line, and each of these
# starts a new command, so a file name after either one is never pytest's argument.
INLINE_COMMENT = re.compile(r"(?:(?<=\s)|^)#.*$")
COMMAND_SEPARATOR = re.compile(r";|&&|\|\||\|")
# Options that make pytest parse the file and run none of its tests: it is
# collected, listed or described, and nothing in it executes. Spelled without
# their dashes, because pytest answers to `--collectonly` and `--setuponly` as
# well as to the hyphenated forms its help prints -- measured on pytest 9.1.1,
# not inferred. `-k` and `-m` are deliberately absent: choosing a subset is what
# a maintainer legitimately does, and the guard does not judge which subset.
NON_RUNNING_OPTIONS = frozenset({
    "collectonly", "co", "setuponly", "setupplan",
    "fixtures", "fixturespertest", "markers",
    "help", "h", "version", "V",
})
# Shells that run a `run:` script as commands. `shell: cat {0}` is a documented
# template and prints the script instead, so a step using one is not evidence.
EXECUTING_SHELLS = frozenset({"bash", "sh", "pwsh", "powershell", "cmd"})
# An install is recognised the same way, by what the command runs rather than by
# the word `install` appearing in it: `python -m pytest tests/x.py -k install`
# runs pytest. The coverage case at the bottom needs the two apart.
PIP_INSTALL = re.compile(
    r"^(?:\S*/)?(?:pip[\d.]*|python[\d.]*\s+(?:-\S+\s+)*-m\s+pip)"
    r"\s+(?:-\S+\s+)*install(?=\s|$)"
)
# GitHub Actions schedules only a five-field POSIX cron expression, and
# documents each field's range and the four operators it accepts: `*`, `,`, `-`
# and `/`. `?`, `L`, `W` and `#` are Quartz extensions it does not take, and
# neither are `@weekly` and friends, which its documentation rejects by name.
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
CRON_NAMES = (
    (),
    (),
    (),
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
    ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"),
)
# February gets 29: a leap year is a real date, so `29 2 *` is a real schedule.
DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


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

    Backslash continuations are joined first, so a step that wraps its arguments
    reads as one command. Then a comment is cut from the first `#` that begins a
    token, and what is left is split on the separators that start a new command:
    `python -m pytest a.py # b.py` and `python -m pytest a.py; echo b.py` both
    name `b.py` in a place pytest never receives it, and a guard that kept the
    whole line would count either as evidence. Whitespace is normalised last,
    which is what lets a folded YAML scalar and a block scalar compare the same
    way. This models those forms rather than parsing a shell: a `#` or a `;`
    inside quotes is treated as if it were not.
    """
    joined = re.sub(r"\\\s*\n\s*", " ", script)
    out = []
    for line in joined.splitlines():
        for piece in COMMAND_SEPARATOR.split(INLINE_COMMENT.sub("", line)):
            command = " ".join(piece.split())
            if command:
                out.append(command)
    return out


def pytest_invocation(command):
    """pytest's own arguments when this command runs pytest, otherwise None.

    The command position decides it. Three prefixes are stepped over first,
    because all three are how pytest is really invoked here and refusing them
    would fail a correct workflow: a leading `NAME=value`, which is the form
    `CLAUDE.md` and `tests/README.md` both prescribe; an `env` with its
    assignments; and `uv run` or `uvx`, with uv already in this CI. What has to
    follow is `pytest`, a path ending in it, or a Python interpreter whose `-m`
    names pytest. `python -m pip install pytest` fails on that `-m`, which is
    the one other place these workflows write the word.
    """
    tokens = command.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if ENV_ASSIGNMENT.match(token) or token == "uvx":
            index += 1
        elif token == "env":
            index += 1
            while index < len(tokens) and ENV_ASSIGNMENT.match(tokens[index]):
                index += 1
        elif token == "uv" and tokens[index + 1 : index + 2] == ["run"]:
            index += 2
        else:
            break
    if index >= len(tokens):
        return None
    if PYTEST_EXECUTABLE.match(tokens[index]):
        return tokens[index + 1 :]
    if not PYTHON_EXECUTABLE.match(tokens[index]):
        return None
    index += 1
    while index < len(tokens) and tokens[index] != "-m":
        if not tokens[index].startswith("-"):
            return None
        index += 2 if tokens[index] in PYTHON_VALUE_OPTIONS else 1
    if tokens[index + 1 : index + 2] != ["pytest"]:
        return None
    return tokens[index + 2 :]


def pytest_paths(args):
    """The arguments pytest will collect: the options and their values removed."""
    paths = []
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("-"):
            option = token.partition("=")[0]
            index += 2 if option in VALUE_OPTIONS and "=" not in token else 1
            continue
        paths.append(token)
        index += 1
    return paths


def names_the_file(path, test_file):
    """True when this pytest argument selects that file: the path, or a glob over it.

    Quotes and a leading `./` are stripped, because the shell removes the quotes
    and both spellings are the same file. A glob is expanded against the tree
    rather than read as a literal: the shell expands it before pytest sees it,
    so the file really is an argument of the command that runs, and
    `.github/workflows/user-sync.yml` already commits that form. A bare
    directory is still not a name -- that is the trade the docstring states.
    """
    cleaned = path.strip("\"'")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned == test_file:
        return True
    if any(character in cleaned for character in "*?["):
        return fnmatch.fnmatch(test_file, cleaned)
    return False


def normalised_option(token):
    """An option's spelling without its dashes.

    pytest answers to `--collectonly` as well as `--collect-only`, and to
    `--setuponly` and `--setupplan`, although `--help` lists only the hyphenated
    forms. Comparing without the dashes covers both spellings of each, and of
    any further alias in that family, instead of chasing them one at a time.
    """
    return token.partition("=")[0].lstrip("-").replace("-", "")


def suppresses_the_run(args):
    """True when an option makes pytest collect or describe and execute nothing."""
    return any(
        normalised_option(arg) in NON_RUNNING_OPTIONS
        for arg in args
        if arg.startswith("-")
    )


def deselects_the_file(args, test_file):
    """True when `--deselect` removes every test in the file.

    pytest deselects every node whose id starts with the value, so the file is
    gone when the value is a prefix of `<file>::`: the bare path, or the path
    with `::`. A longer value such as `<file>::test_one` drops one test and
    leaves the rest running, so it still counts. Measured on pytest 9.1.1
    rather than read off the source: `--deselect <file>` and
    `--deselect <file>::` each deselect the whole file, `<file>::test_one` one.

    `--ignore` is deliberately not here. Measured the same way, it does not
    suppress a file given as an explicit argument, so reading it as an exclusion
    would fail a workflow that really does run the file; where the file appears
    only as `--ignore`'s value it is not one of `pytest_paths` either way.
    """
    node_prefix = test_file + "::"
    for index, token in enumerate(args):
        option, _, inline = token.partition("=")
        if option != "--deselect":
            continue
        value = inline or (args[index + 1] if index + 1 < len(args) else "")
        if value and node_prefix.startswith(value):
            return True
    return False


def runs_as_pytest_argument(commands, test_file):
    """True when some command runs pytest over this file as its own argument.

    Calling pytest is not running its tests: `--collect-only` and its relatives
    parse the file and execute nothing, and a whole-file `--deselect` names it
    in order to drop it. Neither is evidence, however plainly the path is spelt.
    """
    for command in commands:
        args = pytest_invocation(command)
        if args is None:
            continue
        if suppresses_the_run(args) or deselects_the_file(args, test_file):
            continue
        if any(names_the_file(path, test_file) for path in pytest_paths(args)):
            return True
    return False


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
    The trigger itself gets the same treatment: `on: push` and `on: [push]` are
    valid shorthand, and reading `schedule` off a string or a list raised
    instead of saying that the workflow has no calendar.
    """
    triggers = workflow.get("on")
    if triggers is not None and not isinstance(triggers, dict):
        return [], f"`on` is a {type(triggers).__name__}, not a mapping, so it has no schedule"
    entries = ((triggers or {}).get("schedule")) or []
    if not isinstance(entries, list):
        return [], f"schedule is a {type(entries).__name__}, not a list"
    not_mappings = [entry for entry in entries if not isinstance(entry, dict)]
    if not_mappings:
        return [], f"schedule entries are not mappings: {not_mappings!r}"
    return [entry.get("cron") for entry in entries], None


def cron_value(value, low, high, names):
    """True for one number inside its field's range, or a name that field allows."""
    if value.isdigit():
        return low <= int(value) <= high
    return value.upper() in names


def cron_item(item, low, high, names):
    """True for one comma-separated item: `*`, a value, a range, any of them stepped.

    A step divides whatever precedes it, which GitHub documents over a value as
    well as over `*`: its own example is `20/15 * * * *`. A step of zero divides
    by nothing, and a range needs two endpoints, both inside the field.
    """
    base, slash, step = item.partition("/")
    if slash and not (step.isdigit() and int(step) >= 1):
        return False
    if base == "*":
        return True
    ends = base.split("-")
    return len(ends) <= 2 and all(cron_value(end, low, high, names) for end in ends)


def cron_values(field, low, high, names):
    """Every value a validated field selects.

    Only the day and month fields need this, and only for the one question a
    field cannot answer alone: whether the pair can fall on a real date. A step
    counts from the start of its range through the end of the field, which is
    what GitHub's own `20/15 * * * *` means -- minutes 20, 35 and 50 -- and a
    range written backwards wraps rather than selecting nothing, because this is
    not the place to judge it.
    """
    width = high - low + 1
    selected = set()
    for item in field.split(","):
        base, slash, step_text = item.partition("/")
        step = int(step_text) if slash else 1
        if base == "*":
            start, end = low, high
        else:
            ends = [cron_number(e, names, low) for e in base.split("-")]
            start = ends[0]
            end = ends[1] if len(ends) > 1 else (high if slash else start)
        span = (end - start) % width + 1
        selected.update(low + (start - low + i) % width for i in range(0, span, step))
    return selected


def cron_number(value, names, low):
    """One field value as a number, counting a name from where its field starts.

    JAN is month 1 and SUN is weekday 0, so the index alone is wrong for one of
    them: `low` is what tells them apart.
    """
    return int(value) if value.isdigit() else names.index(value.upper()) + low


def reaches_a_date(day, month, weekday):
    """True unless the day and month can never meet, which is a schedule for never.

    `17 5 31 2 *` passes every field and asks for 31 February. The weekday field
    decides whether the pair is binding at all: crontab(5) runs the job when
    *either* the day or the weekday matches, as long as both are restricted, so
    a restricted weekday gives the schedule days of its own and the impossible
    pair stops mattering.

    Restricted is the man page's own test -- a field that does not contain the
    `*` character -- so `*/2` is not restricted, and `17 5 31 2 */2` is refused
    like `17 5 31 2 *`. That reading is disputed between cron implementations
    and GitHub documents neither, so this is a choice: under the other reading
    the expression fires on alternate weekdays in February, which nobody writing
    `31 2` meant either. Refusing it cannot hide a dead schedule, and a weekday
    written `0-6/2` says the intended thing without the ambiguity.
    """
    if "*" not in weekday:
        return True
    days = cron_values(day, 1, 31, ())
    months = cron_values(month, 1, 12, CRON_NAMES[3])
    return any(d <= DAYS_IN_MONTH[m - 1] for d in days for m in months)


def schedulable(cron):
    """True when GitHub Actions can schedule this cron value.

    A non-empty value is not a schedule: `cron: weekly` is accepted by YAML and
    by nothing else, so the workflow would carry a schedule entry and never run.
    Neither is a well-shaped one whose values are out of range -- `99 5 * * 1`
    counts five fields and asks for a minute that does not exist -- nor one whose
    valid fields never coincide, like 31 February. All three are the same silent
    failure, one layer further in each time.
    """
    fields = (cron or "").split()
    if len(fields) != 5:
        return False
    for field, (low, high), names in zip(fields, CRON_RANGES, CRON_NAMES):
        items = field.split(",")
        if not all(items) or not all(cron_item(i, low, high, names) for i in items):
            return False
    return reaches_a_date(fields[2], fields[3], fields[4])


def needed_jobs(jobs, job_name, seen=None):
    """This job and every job it transitively needs."""
    seen = set() if seen is None else seen
    if job_name in seen or job_name not in jobs:
        return seen
    seen.add(job_name)
    needs = jobs[job_name].get("needs") or []
    for other in [needs] if isinstance(needs, str) else needs:
        needed_jobs(jobs, other, seen)
    return seen


def effective_shell(workflow, job, step):
    """The shell a step really runs under, and where it came from.

    A step's own `shell` wins, then the job's `defaults.run.shell`, then the
    workflow's. GitHub documents both halves of that order: `defaults.run`
    "provide[s] default shell and working-directory to all run steps in the
    job", "a default setting defined in a job will override a default setting
    that has the same name defined in a workflow", and a step may "override the
    default shell settings in the runner's operating system and the job's
    default using the shell keyword". Reading only the step's key left
    `defaults: {run: {shell: cat {0}}}` invisible, which prints the script.
    """
    for shell, where in (
        (step.get("shell"), "the step"),
        (((job.get("defaults") or {}).get("run") or {}).get("shell"),
         "the job's `defaults.run`"),
        (((workflow.get("defaults") or {}).get("run") or {}).get("shell"),
         "the workflow's `defaults.run`"),
    ):
        if shell:
            return shell, where
    return None, None


def skip_reasons(workflow, job_name, step):
    """Every reason a scheduled run might not reach this step, or an empty list.

    An `if:` is the obvious one and every one of them disqualifies the step,
    harmless or not, because deciding which conditions exclude the `schedule`
    event is the judgement this guard exists to avoid making. The rest came out
    of attacking it, and each was verified to leave the guard green:

    * `needs:` on a job gated to pull requests takes this job with it, because
      GitHub skips every job that needs a skipped one, so the whole dependency
      chain has to be unconditional and has to exist.
    * `environment:` can hold the run at a deployment approval nobody gives at
      05:17 on a Monday.
    * `continue-on-error`, on the step or the job, lets the calendar alarm fire
      into a green run, which defeats the same purpose one layer later.
    * a `shell:` that is not a shell prints the script instead of running it;
      `shell: cat {0}` is a documented template.

    Two further forms are left alone on purpose. A `runs-on` label no runner
    carries would queue the job for ever, but judging which labels exist is a
    guess this guard should not make. A `strategy.matrix` that expands to
    nothing is unverified on GitHub's side, so it is named here and not refused.

    The whole workflow is the argument, not its jobs, because a `shell` can be
    set three levels up and the step that inherits it carries no key at all.
    """
    jobs = workflow.get("jobs") or {}
    reasons = []
    if "if" in step:
        reasons.append("the step carries an `if:`")
    if step.get("continue-on-error") not in (None, "false"):
        reasons.append("the step is `continue-on-error`, so its failure is not the run's")
    shell, where = effective_shell(workflow, jobs.get(job_name) or {}, step)
    if shell is not None and (shell.split() or [""])[0] not in EXECUTING_SHELLS:
        reasons.append(
            f"`shell: {shell}` from {where} is not a shell, so the script is printed"
        )
    for name in sorted(needed_jobs(jobs, job_name)):
        job = jobs[name]
        where = "its job" if name == job_name else f"`{name}`, which it needs"
        if "if" in job:
            reasons.append(f"{where} carries an `if:`")
        if "environment" in job:
            reasons.append(f"{where} declares an `environment:`, which can hold the run")
        if job.get("continue-on-error") not in (None, "false"):
            reasons.append(f"{where} is `continue-on-error`")
        needs = job.get("needs") or []
        for other in [needs] if isinstance(needs, str) else needs:
            if other not in jobs:
                reasons.append(f"{where} needs `{other}`, which this file does not define")
    return reasons


def is_unconditional(job, step):
    """Kept for the case table below: one job, read without its dependencies."""
    return not skip_reasons({"jobs": {"only": job}}, "only", step)


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
        elif not all(schedulable(cron) for cron in crons):
            reasons.append(
                f"{path.name}: a schedule entry has no usable five-field cron ({crons!r})"
            )
        else:
            workflow = load_workflow(path)
            steps = pytest_steps(path, SCHEDULED_BY_CALENDAR)
            blocked = [
                (job_name, skip_reasons(workflow, job_name, step))
                for job_name, _, step in steps
            ]
            if all(why for _, why in blocked):
                reasons.append(
                    f"{path.name}: a scheduled run may not reach any step running it -- "
                    + "; ".join(f"{job}: {', '.join(why)}" for job, why in blocked)
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
# Review caught six such, each one layer inside the last: the matcher accepted
# the file name anywhere in any run script, so an echo counted as execution; it
# then accepted a `pytest` token anywhere in the command, so `echo pytest x.py`
# counted too; the calendar case accepted an empty schedule, because all([]) is
# true; the cron check read the characters of a field without its range, so
# minute 99 was a schedule; it then read each field without the others, so 31
# February was one; the case below told a pytest run from an install by looking
# for the word `install` anywhere, which made `pytest -k install` an install and
# would have failed a legitimate workflow; and the matcher read the pytest
# command without its options, so `--collect-only` counted as a run.

@pytest.mark.parametrize("command", [
    "python -B -m pytest tests/test_skill_freshness.py -q",
    "pytest tests/test_skill_freshness.py",
    "python -m pytest tests/other.py tests/test_skill_freshness.py tests/more.py -q",
    "/usr/bin/pytest tests/test_skill_freshness.py",
    "python3 -m pytest tests/test_skill_freshness.py -q",
    "python -B -m pytest tests/test_skill_freshness.py -q -p no:cacheprovider",
    ".venv/bin/python -m pytest tests/test_skill_freshness.py",
    ".venv/bin/pytest tests/test_skill_freshness.py",
    "python3.11 -m pytest tests/test_skill_freshness.py",
    "python -m pytest -p tests.ci_shard tests/test_skill_freshness.py -v",
    # Forms review found refused although they run the file. Two are committed
    # in this repository already and one is what its own documentation says.
    "python -B -m pytest tests/test_skill_*.py -q",
    "python -B -m pytest tests/test_skill_fresh???s.py -q",
    "LANGFUSE_TRACING_ENABLED=false python -m pytest tests/test_skill_freshness.py -q",
    "env PYTHONHASHSEED=0 python -B -m pytest tests/test_skill_freshness.py",
    "uv run python -B -m pytest tests/test_skill_freshness.py -q",
    "uvx pytest tests/test_skill_freshness.py",
    "python -m pytest ./tests/test_skill_freshness.py -q",
    "python -m pytest 'tests/test_skill_freshness.py' -q",
    'python -m pytest "tests/test_skill_freshness.py" -q',
    "python -W error -m pytest tests/test_skill_freshness.py",
    "python -X dev -m pytest tests/test_skill_freshness.py",
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
    "echo pytest tests/test_skill_freshness.py",
    "echo /pytest tests/test_skill_freshness.py",
    "echo python -m pytest tests/test_skill_freshness.py",
    "python -m pip install pytest tests/test_skill_freshness.py",
    "ls pytest tests/test_skill_freshness.py",
])
def test_the_matcher_rejects_what_does_not_run_the_file(command):
    """Naming it is not running it: an echo, a neighbouring path, a suffix, another tool."""
    assert not runs_as_pytest_argument([command], "tests/test_skill_freshness.py")


@pytest.mark.parametrize("command", [
    "python -m pytest --collect-only tests/test_skill_freshness.py",
    "python -m pytest --co tests/test_skill_freshness.py -q",
    "python -m pytest --setup-only tests/test_skill_freshness.py",
    "python -m pytest --setup-plan tests/test_skill_freshness.py",
    "python -m pytest --fixtures tests/test_skill_freshness.py",
    "python -m pytest --fixtures-per-test tests/test_skill_freshness.py",
    "python -m pytest --markers tests/test_skill_freshness.py",
    "python -m pytest --help tests/test_skill_freshness.py",
    "python -m pytest --version tests/test_skill_freshness.py",
    "python -m pytest --ignore tests/test_skill_freshness.py tests/other.py",
    "python -m pytest --ignore=tests/test_skill_freshness.py tests/other.py",
    "python -m pytest --deselect tests/test_skill_freshness.py tests/other.py",
    "python -m pytest --deselect=tests/test_skill_freshness.py tests/other.py",
    # pytest answers to the undashed spellings although its help omits them.
    "python -m pytest --collectonly tests/test_skill_freshness.py",
    "python -m pytest --setuponly tests/test_skill_freshness.py",
    "python -m pytest --setupplan tests/test_skill_freshness.py",
    # A node-id prefix ending in `::` takes every test in the file.
    "python -m pytest tests/test_skill_freshness.py --deselect=tests/test_skill_freshness.py::",
    "python -m pytest tests/test_skill_freshness.py --deselect tests/test_skill_freshness.py::",
    # And a glob that misses it is not a name for it either.
    "python -B -m pytest tests/test_source_*.py -q",
])
def test_a_command_that_never_runs_the_file_is_not_evidence(command):
    """Invoking pytest is not running tests, and naming a file is not including it.

    `--collect-only` parses the file and executes nothing; `--ignore` and a
    whole-file `--deselect` spell the path out in order to leave it out. A guard
    that read only the command and the path would count all of these.
    """
    assert not runs_as_pytest_argument([command], "tests/test_skill_freshness.py")


@pytest.mark.parametrize("command", [
    "python -m pytest tests/test_skill_freshness.py -k install",
    "python -m pytest tests/test_skill_freshness.py -m 'not slow'",
    "python -m pytest tests/test_skill_freshness.py -x --maxfail=1",
    "python -m pytest tests/test_skill_freshness.py --durations=15 -q",
    "python -m pytest tests/test_skill_freshness.py --ignore=tests/other.py",
    "python -m pytest tests/test_skill_freshness.py --deselect tests/other.py::test_one",
    "python -m pytest --deselect tests/test_skill_freshness.py::test_one tests/test_skill_freshness.py",
])
def test_a_selector_or_someone_else_s_exclusion_still_counts(command):
    """The other half of the same decision, and the one worth getting wrong-way-round.

    Choosing a subset is legitimate and may legitimately select nothing, so `-k`
    and `-m` are not read as refusals to run. Excluding a different file, or one
    test out of this one, leaves this file running and still counts.
    """
    assert runs_as_pytest_argument([command], "tests/test_skill_freshness.py")


@pytest.mark.parametrize("script, expected", [
    ("# pytest tests/test_skill_freshness.py was here\npython -m pytest tests/other.py",
     ["python -m pytest tests/other.py"]),
    ("python -m pytest \\\n  tests/test_skill_freshness.py -q",
     ["python -m pytest tests/test_skill_freshness.py -q"]),
    ("  python -B   -m  pytest   tests/a.py  ", ["python -B -m pytest tests/a.py"]),
    ("first\n\n# a comment\nsecond", ["first", "second"]),
    ("pytest tests/a.py # pytest tests/b.py", ["pytest tests/a.py"]),
    ("pytest tests/a.py; echo tests/b.py", ["pytest tests/a.py", "echo tests/b.py"]),
    ("pytest tests/a.py && pytest tests/b.py",
     ["pytest tests/a.py", "pytest tests/b.py"]),
    ("pytest tests/a.py | tee log # tests/b.py", ["pytest tests/a.py", "tee log"]),
])
def test_the_parser_reads_a_script_the_way_a_shell_would(script, expected):
    """A comment is not a command, and a continuation is not two of them."""
    assert logical_commands(script) == expected


def test_a_commented_out_pytest_line_is_not_evidence():
    """The whole point of the parser: the file is named, and still not run."""
    script = "# python -m pytest tests/test_skill_freshness.py -q\necho done"
    assert not runs_as_pytest_argument(logical_commands(script), "tests/test_skill_freshness.py")


@pytest.mark.parametrize("script", [
    "python -m pytest tests/other.py # tests/test_skill_freshness.py",
    "python -m pytest tests/other.py; echo tests/test_skill_freshness.py",
    "python -m pytest tests/other.py && echo tests/test_skill_freshness.py",
    "python -m pytest tests/other.py | grep tests/test_skill_freshness.py",
    "python -m pytest tests/other.py\necho tests/test_skill_freshness.py",
])
def test_a_name_outside_the_pytest_command_is_not_evidence(script):
    """A token after a comment or a separator never reaches pytest, whatever precedes it.

    The matcher reads one command, so without the parser splitting these the pytest
    token and the file token would sit in the same string and the guard would pass.
    """
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


@pytest.mark.parametrize("command, installing", [
    ("python -m pip install pytest python-dotenv pyyaml", True),
    ("python3 -m pip install pytest python-dotenv pyyaml", True),
    ("pip install pytest", True),
    ("pip3 install pytest", True),
    (".venv/bin/pip install pytest", True),
    ("python -m pip install --upgrade pytest", True),
    ("python -m pip install -r requirements.txt", True),
    ("python -m pytest tests/test_skill_freshness.py -k install", False),
    ("python -m pytest tests/test_installer_windows.py", False),
    ("echo pip install pytest", False),
    ("pip download pytest", False),
    ("pip list", False),
])
def test_an_install_is_told_from_a_run_by_what_the_command_invokes(command, installing):
    """`pytest -k install` runs pytest, and the word `install` does not say otherwise.

    The oracle in the coverage case below depends on this: it classified by
    searching every argument for `install`, so selecting installation tests
    would have failed a workflow that was perfectly correct.
    """
    assert bool(PIP_INSTALL.match(command)) is installing


@pytest.mark.parametrize("cron, usable", [
    ("17 5 * * 1", True),
    ("0 5 * * 1,4", True),
    ("*/15 * * * *", True),
    ("20/15 * * * *", True),       # a step over a value: GitHub's own example
    ("17 5 * * 1-5", True),
    ("17 5 * * SUN", True),
    ("30 5 * JAN-DEC MON-FRI", True),
    ("0 0 31 12 *", True),         # both ends of the day and month ranges
    ("weekly", False),
    ("", False),
    (None, False),
    ("17 5 * *", False),
    ("17 5 * * 1 2", False),
    ("17 5 * * monday!", False),
    ("every monday at five please", False),   # five fields of prose
    ("99 5 * * 1", False),         # minute is 0-59
    ("17 24 * * 1", False),        # hour is 0-23
    ("17 5 0 * 1", False),         # the month's days start at 1
    ("17 5 32 * 1", False),
    ("17 5 * 13 1", False),
    ("17 5 * * 7", False),         # GitHub documents 0-6; Sunday is 0 or SUN
    ("17 5 29 2 *", True),         # a leap year is a real date
    ("17 5 31 1,2 *", True),       # January has a 31st, so the pair can meet
    ("30 5 31 2 1", True),         # a restricted weekday gives it days of its own
    ("17 5 31 2 *", False),        # 31 February
    ("17 5 30 2 *", False),
    ("17 5 31 4 *", False),        # April has 30 days
    ("17 5 31 2,4 *", False),      # neither month has a 31st
    ("17 5 31 FEB *", False),      # the same date, spelled out
    ("17 5 31 2 */2", False),      # crontab(5) calls a field with `*` unrestricted
    ("17 5 * * */2", True),        # every other weekday is a real schedule
    ("17 5 31 2 0-6/2", True),     # and this is how to say it without the `*`
    ("17 5 31 JAN *", True),
    ("? * * * *", False),          # Quartz again, this time in the minute
    ("*/0 * * * *", False),        # a step of zero divides by nothing
    ("17 5 * * ?", False),         # Quartz operators are not GitHub's
    ("17 5 L * 1", False),
    ("17,, 5 * * 1", False),       # an empty item in a list
    ("17 5 1-2-3 * 1", False),     # a range has two ends
    ("- 5 * * 1", False),
])
def test_only_a_cron_github_can_schedule_counts_as_a_schedule(cron, usable):
    """A schedule entry GitHub cannot parse schedules nothing, so it is not one.

    Shape is not enough: the ranges come from the field diagram in GitHub's
    `schedule` documentation (minute 0-59, hour 0-23, day 1-31, month 1-12 or
    JAN-DEC, weekday 0-6 or SUN-SAT) and the operators from the table under it.
    """
    assert schedulable(cron) is usable


@pytest.mark.parametrize("field, low, high, names, expected", [
    ("20/15", 0, 59, (), {20, 35, 50}),          # GitHub's documented example
    ("*/15", 0, 59, (), {0, 15, 30, 45}),
    ("*", 1, 31, (), set(range(1, 32))),
    ("17", 0, 59, (), {17}),
    ("1,3,5", 1, 12, CRON_NAMES[3], {1, 3, 5}),
    ("JAN,MAR", 1, 12, CRON_NAMES[3], {1, 3}),
    ("FEB", 1, 12, CRON_NAMES[3], {2}),          # a name counts from its field's start
    ("DEC", 1, 12, CRON_NAMES[3], {12}),
    ("NOV-FEB", 1, 12, CRON_NAMES[3], {11, 12, 1, 2}),   # a backwards range wraps
    ("SUN", 0, 6, CRON_NAMES[4], {0}),           # and weekdays start at zero
    ("MON-FRI", 0, 6, CRON_NAMES[4], {1, 2, 3, 4, 5}),
    ("29-31", 1, 31, (), {29, 30, 31}),
])
def test_the_field_expander_selects_the_values_the_spec_names(
    field, low, high, names, expected
):
    """The calendar check rests on these numbers, so they are pinned, not inferred.

    Reading a name as its bare index is the mistake this table exists to catch:
    it makes February January, and `31 FEB *` a schedule again.
    """
    assert cron_values(field, low, high, names) == expected


def test_the_schedule_reader_handles_a_workflow_with_no_schedule():
    """No schedule is not malformed, so it reports no crons and no problem."""
    assert schedule_crons({"on": {"pull_request": None}}) == ([], None)
    assert schedule_crons({}) == ([], None)


@pytest.mark.parametrize("triggers, problem_words", [
    ({"schedule": [{"cron": "17 5 * * 1"}]}, None),
    ({"pull_request": None}, None),
    (None, None),
    ("push", "`on` is a str"),
    (["push"], "`on` is a list"),
    (["push", "pull_request"], "not a mapping"),
])
def test_a_shorthand_trigger_is_reported_instead_of_raising(triggers, problem_words):
    """`on: push` and `on: [push]` are valid GitHub Actions and are not mappings.

    Reading `schedule` off either one raised AttributeError, so the calendar case
    failed with a traceback instead of saying the workflow has no calendar. The
    promise the reader already made about malformed entries covers the entries;
    this covers the trigger that holds them.
    """
    crons, problem = schedule_crons({"on": triggers})
    assert crons == ([] if problem_words or triggers in (None, {"pull_request": None}) else
                     ["17 5 * * 1"])
    if problem_words is None:
        assert problem is None
    else:
        assert problem and problem_words in problem


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


def test_every_pytest_command_in_this_repository_is_recognised():
    """An anchor that rejected a real form would turn every case above into a lie.

    A `pytest` token appears in exactly two kinds of command in these workflows:
    one that runs it, and `pip install pytest`, which does not. Each is read by
    its own reader, over the committed files rather than over examples, so a
    command this file cannot read is counted as neither and the count moves.

    The floor is a floor, not an alarm. With 22 invocations against it, one
    migrated step does not trip it, and the earlier version of this docstring
    claiming otherwise was wrong: review measured seven steps of headroom. What
    the floor catches is a reader that stops reading -- a regression that drops
    the count to nothing, which is how every other case here becomes vacuous.
    The mistake it does catch exactly is the two readers agreeing on one
    command, which is the assertion inside the loop.
    """

    invocations, installs, unreadable = 0, 0, []
    for path in WORKFLOWS:
        for command in run_commands(path):
            tokens = command.split()
            if "pytest" not in tokens and not any(t.endswith("/pytest") for t in tokens):
                continue
            runs = pytest_invocation(command) is not None
            installing = bool(PIP_INSTALL.match(command))
            assert not (runs and installing), (
                f"{path.name}: read as both a pytest run and an install: {command!r}"
            )
            invocations += runs
            installs += installing
            if not (runs or installing):
                unreadable.append(f"{path.name}: {command}")
    assert invocations >= 15 and installs >= 3, (
        "too few pytest commands found to prove anything about the readers: "
        f"{invocations} invocations, {installs} installs. Unread: {unreadable}"
    )


@pytest.mark.parametrize("jobs, step, reason_words", [
    ({"content": {}}, {"run": "pytest x"}, None),
    ({"content": {"if": "github.event_name == 'pull_request'"}}, {"run": "pytest x"},
     "its job carries an `if:`"),
    ({"content": {}}, {"run": "pytest x", "if": "github.event_name != 'schedule'"},
     "the step carries an `if:`"),
    ({"content": {"needs": "gate"}, "gate": {"if": "github.event_name == 'pull_request'"}},
     {"run": "pytest x"}, "`gate`, which it needs carries an `if:`"),
    ({"content": {"needs": ["lint", "gate"]}, "lint": {},
      "gate": {"if": "github.event_name == 'pull_request'"}},
     {"run": "pytest x"}, "`gate`, which it needs carries an `if:`"),
    ({"content": {"needs": "first"}, "first": {"needs": "second"},
      "second": {"if": "always()"}}, {"run": "pytest x"},
     "`second`, which it needs carries an `if:`"),
    ({"content": {"needs": "absent"}}, {"run": "pytest x"},
     "needs `absent`, which this file does not define"),
    ({"content": {"environment": "production"}}, {"run": "pytest x"},
     "declares an `environment:`"),
    ({"content": {"continue-on-error": "true"}}, {"run": "pytest x"},
     "is `continue-on-error`"),
    ({"content": {}}, {"run": "pytest x", "continue-on-error": "true"},
     "the step is `continue-on-error`"),
    ({"content": {}}, {"run": "pytest x", "shell": "cat {0}"},
     "`shell: cat {0}` from the step is not a shell"),
    ({"content": {}}, {"run": "pytest x", "shell": "python"}, "is not a shell"),
    ({"content": {}}, {"run": "pytest x", "shell": "bash"}, None),
    ({"content": {}}, {"run": "pytest x", "shell": "bash -e {0}"}, None),
    ({"content": {"needs": "lint"}, "lint": {}}, {"run": "pytest x"}, None),
    ({"content": {"continue-on-error": "false"}}, {"run": "pytest x"}, None),
])
def test_every_way_a_scheduled_run_can_miss_the_step_is_a_reason(jobs, step, reason_words):
    """An `if:` was never the only way, and each of the others was verified green.

    A dependency on a job gated to pull requests skips this job with it; an
    environment can hold the run for an approval; `continue-on-error` lets the
    calendar alarm land in a green run; and a `shell:` that is not a shell
    prints the command instead of running it. All four passed the guard before.
    """
    reasons = skip_reasons({"jobs": jobs}, "content", step)
    if reason_words is None:
        assert reasons == []
    else:
        assert any(reason_words in reason for reason in reasons), reasons


@pytest.mark.parametrize("workflow, step, expected", [
    ({"jobs": {"content": {}}}, {"run": "pytest x"}, (None, None)),
    ({"jobs": {"content": {}}}, {"run": "pytest x", "shell": "bash"}, ("bash", "the step")),
    ({"defaults": {"run": {"shell": "cat {0}"}}, "jobs": {"content": {}}},
     {"run": "pytest x"}, ("cat {0}", "the workflow's `defaults.run`")),
    ({"jobs": {"content": {"defaults": {"run": {"shell": "cat {0}"}}}}},
     {"run": "pytest x"}, ("cat {0}", "the job's `defaults.run`")),
    # A job's default overrides the workflow's, and a step's key overrides both.
    ({"defaults": {"run": {"shell": "bash"}},
      "jobs": {"content": {"defaults": {"run": {"shell": "cat {0}"}}}}},
     {"run": "pytest x"}, ("cat {0}", "the job's `defaults.run`")),
    ({"defaults": {"run": {"shell": "cat {0}"}},
      "jobs": {"content": {"defaults": {"run": {"shell": "cat {0}"}}}}},
     {"run": "pytest x", "shell": "bash"}, ("bash", "the step")),
    ({"defaults": {"run": {"working-directory": "src"}}, "jobs": {"content": {}}},
     {"run": "pytest x"}, (None, None)),
])
def test_the_shell_is_resolved_the_way_github_resolves_it(workflow, step, expected):
    """A step can inherit its shell from two levels up and carry no key itself.

    Reading only `step["shell"]` left `defaults: {run: {shell: cat {0}}}`
    invisible: the script would be printed, the content checks would never run,
    and the calendar case saw no reason at all.
    """
    job = workflow["jobs"]["content"]
    assert effective_shell(workflow, job, step) == expected


@pytest.mark.parametrize("workflow", [
    {"defaults": {"run": {"shell": "cat {0}"}}, "jobs": {"content": {}}},
    {"jobs": {"content": {"defaults": {"run": {"shell": "cat {0}"}}}}},
    {"defaults": {"run": {"shell": "python"}}, "jobs": {"content": {}}},
])
def test_an_inherited_shell_that_is_not_a_shell_is_a_reason(workflow):
    """And the calendar case has to see it, which is the half that was missing."""
    reasons = skip_reasons(workflow, "content", {"run": "pytest x"})
    assert any("is not a shell" in reason for reason in reasons), reasons


def test_the_parser_reads_the_steps_of_the_workflow_it_is_given():
    """A parser returning nothing would pass every assertion above it."""
    hosts = workflows_running(CONTENT_CHECKS[0])
    assert hosts, "the fixture for this assertion is the first assertion above"
    commands = run_commands(hosts[0])
    assert commands, f"{hosts[0].name} parsed to no run commands"
    assert any(pytest_invocation(cmd) is not None for cmd in commands)
    assert pytest_steps(hosts[0], CONTENT_CHECKS[0]), "the step finder lost the step"
