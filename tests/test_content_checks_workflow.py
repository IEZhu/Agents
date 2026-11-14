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

One limit is real and stated rather than solved: a command can run the file
without naming it -- `pytest tests/` collects the directory, and a file list
held in a shell variable expands outside this reader -- and both still fail
here, which is the intended trade: being named is what makes dropping a file
visible.

Reading is no longer all this module does. Eight recorded forms satisfied every
check above and ran nothing (#632): a leading `exit 0`, the command in a
here-document, inside an `if` nobody sets, inside a quoted string whose `;` the
reader splits on, after a shell function named pytest, through a stub the step
writes itself, on the unreached side of `||`, and backgrounded with `&`. Seven
are closed by running the step: its script goes to a file and the file to
`bash -e`, in a throwaway tree of this repository's path names, with every
program that could be pytest replaced by a shim that records its argv and exits
0. So the content checks are never executed by the guard that checks them,
nothing is installed and nothing reaches the network, while what pytest was
handed is a recorded fact rather than an inference. The eighth is closed by
reading after all: `&` hands the command to the background, where its failure
can no longer redden the step, so a backgrounded command is not evidence -- and
a single `|` is refused for the same reason under the shell the committed steps
get, where the left side of a pipe loses its exit status.

Executing a step is a trust boundary, and what the boundary is is what the
code refuses: pytest here runs a workflow's own shell on whoever's machine runs
this suite, reachable from a pull-request diff a maintainer checks out. The
sentence that stood here claimed more than the code held, and a pre-mortem
measured three ways out of it, each one leaving the step a clean recorded run:
an absolute program that was not an interpreter, which PATH never saw; a `..`
spelling of a shim path, written 0755 outside the tree before the step started;
and a redirection through `~`, which the reader of the spelling could not place.
So every refusal is read before anything starts, by one reader that the
executor and the assertion over the committed steps share: a command word has
to be a shim or one of the four programs on the
allowlist, a shim path has to stay inside the tree, and a redirection target
has to be a literal path inside it -- which refuses `~`, `$` and a backtick,
because the shell expands those when the step runs and not when this reads.
What bounds the step that does start is a throwaway tree with HOME inside it
and a PATH holding the shims and that allowlist and nothing else. A step this
guard refuses is a verdict about the step and fails; a runner that could not
answer skips. What is still not bounded is what a shimmed program could be made
to do with the tree it is given, what a program on the allowlist can do, or
memory and time beyond the timeout -- and because this is what the half does to
a machine, it is opt-in off CI.

The recorder's own log is the other thing a step could reach: its path used to
travel in the step's environment, so a step could write the line the guard then
read. The paths are baked into each shim per run and the lines are counted
against the invocations, which is arithmetic rather than secrecy -- a shebang
script has to be readable to be executed, so what this stops is a forged line,
not a step that goes looking.

The executing half runs on one CI leg and is opt-in anywhere else
(`confined_to_the_executing_leg`). The steps are platform-neutral and the
recorder's answer does not depend on the host, so three legs paid three times
for one fact; the other two read. Off CI it runs only for whoever asks for it,
because running it is running a workflow's shell on your own machine.

What stays open is named in #632 and not attempted here: a `runs-on` label no
runner carries, a `strategy.matrix` that expands to nothing, and a glob given
to `--ignore-glob`, which is read as the literal it is.
"""

from collections import namedtuple
from functools import lru_cache
from pathlib import Path
import fnmatch
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_EXTENSIONS = ("*.yml", "*.yaml")


def workflow_paths(directory):
    """Every workflow file in this directory, under either extension GitHub takes.

    Both of them: GitHub runs `.yml` and `.yaml` alike, and a workflow saved
    with the other one goes unseen by a guard that globs one -- which made
    `instruction-protocol.yml` invisible to the manifests module under a rename
    (#555, item 4). One reader for both modules, so they cannot drift apart.
    """
    return sorted(path for pattern in WORKFLOW_EXTENSIONS
                  for path in directory.glob(pattern))


WORKFLOWS = workflow_paths(ROOT / ".github/workflows")

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
# A single `&` and a single `|` are in the list for what they say about the
# command they follow, which is why the group captures: `logical_commands` keeps
# either one on that command, and `backgrounded` and `piped` read them back.
COMMAND_SEPARATOR = re.compile(r"(;|&&|\|\||\||&)")
KEPT_SEPARATORS = ("&", "|")
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

    A `&` separates too, and the command before it keeps it: `pytest x &` runs
    pytest in the background, and `backgrounded` below is what reads that. A
    single `|` is kept the same way and for the same reason: the left side of a
    pipe loses its exit status under the shell the committed steps get, so
    `piped` below has to be able to see it.
    """
    joined = re.sub(r"\\\s*\n\s*", " ", script)
    out = []
    for line in joined.splitlines():
        pieces = COMMAND_SEPARATOR.split(INLINE_COMMENT.sub("", line))
        for index in range(0, len(pieces), 2):
            command = " ".join(pieces[index].split())
            if not command:
                continue
            separator = pieces[index + 1] if index + 1 < len(pieces) else ""
            out.append(f"{command} {separator}"
                       if separator in KEPT_SEPARATORS else command)
    return out


def without_separator(command):
    """The command without the `&` or `|` that `logical_commands` kept on it."""
    for separator in KEPT_SEPARATORS:
        if command.endswith(f" {separator}"):
            return command[: -len(separator) - 1]
    return command


def command_position(tokens):
    """The index of the program this command starts, past the prefixes written here.

    Three of them, because all three are how pytest is really invoked in this
    repository and refusing them would fail a correct workflow: a leading
    `NAME=value`, which is the form `CLAUDE.md` and `tests/README.md` both
    prescribe; an `env` with its assignments; and `uv run` or `uvx`.
    """
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
    return index


def command_word(command):
    """The program this command starts, or None when it starts none."""
    tokens = command.split()
    index = command_position(tokens)
    return unquoted(tokens[index]) if index < len(tokens) else None


def pytest_invocation(command):
    """pytest's own arguments when this written command runs pytest, else None.

    Whitespace is the only split a script's text gets, which is the modelling
    this module states rather than a parse of the shell. A recorded line is the
    other caller and knows its own boundaries: `recorded_argv` gives it the
    argv, and `pytest_arguments` is what both of them ask.
    """
    return pytest_arguments(command.split())


def pytest_arguments(tokens):
    """pytest's own arguments when this argv runs pytest, otherwise None.

    The command position decides it. Three prefixes are stepped over first,
    because all three are how pytest is really invoked here and refusing them
    would fail a correct workflow: a leading `NAME=value`, which is the form
    `CLAUDE.md` and `tests/README.md` both prescribe; an `env` with its
    assignments; and `uv run` or `uvx`, with uv already in this CI. What has to
    follow is `pytest`, a path ending in it, or a Python interpreter whose `-m`
    names pytest. `python -m pip install pytest` fails on that `-m`, which is
    the one other place these workflows write the word.
    """
    index = command_position(tokens)
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


def unquoted(token):
    """A token with one matching outer pair of quotes removed, as a shell does.

    Only the outer pair, and only a matching one. A quote character inside the
    value survives expansion and belongs to the path, so
    `--deselect="<file>'"` really hands pytest a name that matches no node id
    and the file still runs. Deleting every quote character instead would read
    that as a whole-file deselection and refuse a command that does run it.
    """
    if len(token) > 1 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def names_the_file(path, test_file):
    """True when this pytest argument selects that file: the path, or a glob over it.

    A leading `./` is stripped, because it is the same file. Quotes are stripped
    too, since the shell removes them, but whether they were there decides what
    the argument means: an unquoted glob reaches pytest already expanded, so the
    file really is an argument of the command that runs, and
    `.github/workflows/user-sync.yml` already commits that form -- while a
    quoted one does not expand at all. Measured, `pytest 'tests/test_skill_*.py'`
    answers `ERROR: file or directory not found` and runs nothing, so it is not
    evidence however much it looks like the form above.

    A partially quoted token such as `tests/'test_skill_'*.py` is left as it is
    and matches nothing, which is a miss rather than a mistake. A bare directory
    is still not a name -- that is the trade the docstring states.
    """
    cleaned = unquoted(path)
    quoted = cleaned is not path
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned == test_file:
        return True
    if not quoted and any(character in cleaned for character in "*?["):
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

    Quotes come off the value first, because the shell removes them before
    pytest is started: measured on pytest 9.1.1, `--deselect='<file>'` and
    `--deselect "<file>"` each deselect the whole file exactly as the bare
    spelling does. That is the opposite of `names_the_file`, where quoting is
    load-bearing because it decides whether the shell expands a glob, and the
    two must not be made to agree. An escaped quote inside a path is not
    modelled, which would be a miss rather than a mistake.

    The `./` is not stripped, because pytest compares the value as written. Review asked for the normalisation that
    `names_the_file` does, and measuring refused it: `--deselect ./<file>`
    deselects nothing, with or without a `./` on the positional, since a node id
    is `<file>::<test>` and `./<file>` is not a prefix of it. Normalising here
    would refuse a command that really does run the file. The asymmetry is the
    point: a path argument and a node-id prefix are different things, and
    `--deselect <file>` after a `./<file>` positional does deselect, which is a
    case below.
    """
    node_prefix = test_file + "::"
    for index, token in enumerate(args):
        option, _, inline = token.partition("=")
        if option != "--deselect":
            continue
        value = inline or (args[index + 1] if index + 1 < len(args) else "")
        value = unquoted(value)
        if value and node_prefix.startswith(value):
            return True
    return False


def backgrounded(command):
    """True when a trailing `&` handed this command to the background.

    A backgrounded pytest does run, which is why the recorder below sees it.
    What it no longer does is decide the step's exit status: `bash -e` returns
    the job's immediate 0 and a failing content check cannot redden the step
    (#632), so the run proves nothing about the check. A `wait` afterwards
    would bind the status again and is deliberately not modelled -- writing the
    command in the foreground says the same thing, and a reader that followed
    `wait` would have to decide which job it waits for.
    """
    return command.split()[-1:] == ["&"]


def piped(command):
    """True when this command's output was handed to another one.

    Which costs it its exit status under the template a step with no `shell` key
    gets: GitHub starts `bash -e {0}`, where `false | cat` exits 0, so a content
    check failing on the left of a pipe cannot redden the step -- measured
    against both shells. It is the same objection the trailing `&` answers to,
    and a pipeline was counted as evidence while the ampersand was refused.
    `shell: bash` is the other answer: GitHub expands it with `-eo pipefail`,
    the failure survives the pipe, and the command is evidence again.
    """
    return command.split()[-1:] == ["|"]


def pipefail_in_effect(shell):
    """True when the shell GitHub starts for this `shell` value sets pipefail.

    From the same table in GitHub's workflow-syntax documentation that
    `SHELL_COMMANDS` comes from: no key is `bash -e {0}`, `shell: bash` is
    `bash --noprofile --norc -eo pipefail {0}`, `shell: sh` is `sh -e {0}`, and
    a value containing `{0}` is used as written, so a maintainer who spells the
    option out gets it. A shell this guard cannot start sets nothing.
    """
    words = (shell or "").split()
    if "{0}" in (shell or ""):
        return any("pipefail" in word for word in words)
    template = SHELL_COMMANDS.get(words[0] if words else None, ())
    return any("pipefail" in word for word in template)


def runs_the_file(argv, test_file):
    """True when this argv is a pytest run of that file, by its own arguments.

    Calling pytest is not running its tests: `--collect-only` and its relatives
    parse the file and execute nothing, and a whole-file `--deselect` names it
    in order to drop it. Neither is evidence, however plainly the path is spelt.
    One reader for a written command and for a recorded one, so the two cannot
    answer differently about the same arguments; what differs is where the argv
    came from, and an argv `recorded_argv` could not read is no argv at all.
    """
    args = None if argv is None else pytest_arguments(argv)
    if args is None:
        return False
    if suppresses_the_run(args) or deselects_the_file(args, test_file):
        return False
    return any(names_the_file(path, test_file) for path in pytest_paths(args))


def runs_as_pytest_argument(commands, test_file, pipefail=False):
    """True when some written command runs pytest over this file as its argument.

    Beyond what `runs_the_file` reads, two commands run and still prove
    nothing: one the shell backgrounds, whose failure the step survives, and
    one on the left of a pipe unless the step's own shell sets pipefail.
    Neither can redden the step. `pipefail` is a keyword because the default is
    the template the committed steps get, and a caller that knows the step's
    shell has to say so.
    """
    for command in commands:
        if backgrounded(command) or (piped(command) and not pipefail):
            continue
        if runs_the_file(without_separator(command).split(), test_file):
            return True
    return False


def steps_passing_the_file(workflow, test_file):
    """Every (job name, job, step) of this parsed workflow that passes the file on.

    A mapping rather than a path, so the assertions below can be asked about a
    constructed workflow as well as about the committed ones -- which is how the
    step a run never reaches got its case.
    """
    found = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not (isinstance(step, dict) and step.get("run")):
                continue
            pipefail = pipefail_in_effect(effective_shell(workflow, job, step)[0])
            if runs_as_pytest_argument(
                    logical_commands(step["run"]), test_file, pipefail=pipefail):
                found.append((job_name, job, step))
    return found


def pytest_steps(path, test_file):
    """Every (job name, job, step) whose run script passes the file to pytest."""
    return steps_passing_the_file(load_workflow(path), test_file)


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


def ascii_number(value):
    """True for a run of ASCII digits, the only numeral a cron field can hold.

    `str.isdigit()` is the wrong test twice over. It is true for `\u0661\u0667`, which
    `int()` happily parses as 17, so a cron GitHub cannot schedule passed the
    range check; and it is true for `\u00b2`, which `int()` refuses, so the same test
    turned a malformed field into a ValueError instead of a verdict -- in a
    reader whose whole contract is to report a problem rather than raise.
    """
    return value.isascii() and value.isdigit()


def cron_value(value, low, high, names):
    """True for one number inside its field's range, or a name that field allows."""
    if ascii_number(value):
        return low <= int(value) <= high
    return value.upper() in names


def cron_item(item, low, high, names):
    """True for one comma-separated item: `*`, a value, a range, any of them stepped.

    A step divides whatever precedes it, which GitHub documents over a value as
    well as over `*`: its own example is `20/15 * * * *`. A step of zero divides
    by nothing, and a range needs two endpoints, both inside the field.
    """
    base, slash, step = item.partition("/")
    if slash and not (ascii_number(step) and int(step) >= 1):
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
    return int(value) if ascii_number(value) else names.index(value.upper()) + low


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


def run_default(workflow, job, step, key):
    """A `defaults.run` key as GitHub resolves it, and where it came from.

    A step's own key wins, then the job's `defaults.run`, then the workflow's.
    GitHub documents both halves of that order: `defaults.run` "provide[s]
    default shell and working-directory to all run steps in the job", "a default
    setting defined in a job will override a default setting that has the same
    name defined in a workflow", and a step may "override the default shell
    settings in the runner's operating system and the job's default using the
    shell keyword". Those two keys are exactly the ones that decide how a step
    is executed, so the recorder below reads both through here.
    """
    for value, where in (
        (step.get(key), "the step"),
        (((job.get("defaults") or {}).get("run") or {}).get(key),
         "the job's `defaults.run`"),
        (((workflow.get("defaults") or {}).get("run") or {}).get(key),
         "the workflow's `defaults.run`"),
    ):
        if value:
            return value, where
    return None, None


def effective_shell(workflow, job, step):
    """The shell a step really runs under, and where it came from.

    Reading only the step's key left `defaults: {run: {shell: cat {0}}}`
    invisible, which prints the script instead of running it.
    """
    return run_default(workflow, job, step, "shell")


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


# Executing the step, because reading it cannot say whether pytest ran. Seven of
# the eight forms recorded in #632 need the script run rather than read, and
# deciding them statically would mean modelling bash control flow, quoting,
# here-documents, function definitions and PATH resolution -- wrong at the
# edges, and a new way to fail a correct workflow, which this file already did
# twice. So the step is executed the way Actions executes one, with every
# program that could be pytest replaced by a shim that records its argv.

# GitHub's own templates, from the `shell` table in its workflow-syntax
# documentation: a non-Windows step with no `shell` key runs `bash -e {0}`,
# `shell: bash` runs `bash --noprofile --norc -eo pipefail {0}`, and `shell: sh`
# runs `sh -e {0}`. The committed steps carry no `shell`, so the first is the one
# that decides this, and `-eo pipefail` is two words to the shell either way.
SHELL_COMMANDS = {
    None: ("bash", "-e"),
    "bash": ("bash", "--noprofile", "--norc", "-eo", "pipefail"),
    "sh": ("sh", "-e"),
}
# Shimmed whether the script names them or not: `uv` and `uvx` because
# `pytest_invocation` steps over them, pip because installing pytest is the
# other thing these workflows do with the word, and the bare interpreter
# spellings because a script may reach one through another program.
ALWAYS_SHIMMED = ("python", "python3", "pytest", "pip", "pip3", "uv", "uvx")
# Executing a step runs a workflow's own shell on whatever machine this suite
# runs on, which is a trust boundary and was an open one: cwd and HOME were
# redirected and the programs that could be pytest were shimmed, while every
# other program on the machine stayed reachable -- measured, `git` and `curl`
# resolved to the real binaries, and a redirection to an absolute path left the
# throwaway tree. So PATH now holds the shim directory and these, and nothing
# else. Each is here because a step or a case in this module really runs it, and
# none of them reaches the network or installs anything. A command word outside
# both lists is refused before the step starts, which is a verdict about the
# step rather than a runner that could not answer; one this reader does not see
# as a command word is still `command not found` under `bash -e`, which stays
# the `problem` this module already models -- never a real execution, and never
# an empty log.
ALLOWED_PROGRAMS = ("cat", "chmod", "ls", "mkdir")
# Shell words that start no program: builtins and the keywords `logical_commands`
# leaves standing on their own. An unlisted one is reported rather than allowed,
# which is the safe direction for a list that decides what may run.
SHELL_WORDS = frozenset({
    ":", ".", "[", "[[", "]]", "{", "}", "(", ")", "!", "break", "case", "cd",
    "command", "continue", "do", "done", "echo", "elif", "else", "esac", "eval",
    "exec", "exit", "export", "false", "fi", "for", "function", "hash", "if",
    "in", "local", "printf", "pwd", "read", "return", "select", "set", "shift",
    "source", "test", "then", "time", "trap", "true", "type", "umask", "unset",
    "until", "wait", "while",
})
# A redirection is the one write a step can make without starting a program, so
# its target is how a script leaves the tree while every program it runs is
# shimmed. `2>&1` is not one of these: its target is a descriptor.
REDIRECTION_TARGET = re.compile(r"(?:^|\s)\d*>>?\s*([^\s|;&<>]+)")
# A here-document's delimiter, in both of the spellings a step writes: `<<WORD`
# and `<<'WORD'`, with `<<-` for the tab-stripping form. `without_here_documents`
# uses it to tell a program from the data one is fed.
HERE_DOCUMENT = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
# A target holding one of these is not a path at all until the shell expands it,
# which happens when the step runs and not when this module reads: `~` is the
# home directory, `$NAME` and `${NAME}` a variable, `$(...)` and a backtick the
# output of a command. `> ~/../x` was measured writing beside the throwaway tree
# while the reader of the spelling saw nothing to refuse.
EXPANDS_AT_RUNTIME = re.compile(r"[~$`]")
# Writing to these touches nothing, and a step that silences output writes one.
HARMLESS_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
PIP_EXECUTABLE = re.compile(r"^(?:\S*/)?pip[\d.]*$")
# The shim records into two files per run whose paths are baked into its own
# text. They used to travel in the step's environment under the fixed name
# `CONTENT_CHECK_RECORDED_ARGV`, which handed the step the one thing it must not
# have: a step could append the very line the guard would then read, and review
# built it -- a `printf` into that variable followed by `true || <a real pytest
# command>`, where the right side satisfies the static reader and bash never
# reaches it. 252 cases passed with nothing started.
#
# Nothing in the environment names either file now. That is not a proof on its
# own: a shebang script has to be readable to be executed, so a determined step
# can read the path out of the shim it just ran. `recorded` below closes that
# with arithmetic instead of secrecy -- one line and one counted invocation per
# call, so a log holding more lines than the shims were invoked times is a log
# with a line no shim wrote, and the run is reported rather than believed.
ARGV_SHIM = (
    "#!/bin/sh\n"
    "# Appends the argv it was called with to a log, counts the call and exits 0\n"
    "# without doing the work, so the script around it reaches its end while\n"
    "# pytest, pip and the network take no part in it. `${{0##*/}}` is the name\n"
    "# it was invoked under, which decides how the recorded line is read\n"
    "# back. The count is written first, so a forged line cannot be the one the\n"
    "# count accounts for.\n"
    "log={log}\n"
    "calls={calls}\n"
    "# One shell-quoted word per argument, because a space-joined line loses the\n"
    "# argument boundaries: `pytest 'a.py b.py'` hands pytest one path that no\n"
    "# tree holds and was recorded as the two paths `a.py b.py`, which read back\n"
    "# as a run of both. A word of safe characters is written as it stands, so an\n"
    "# ordinary command is recorded as it was typed; anything else is single\n"
    "# quoted with its own quotes spliced the way `shlex.quote` splices them,\n"
    "# which is what `shlex.split` reads back exactly. Measured round-tripping\n"
    "# under sh, dash, bash and zsh, including an embedded quote, an empty\n"
    "# argument, a backslash and a tab. A newline inside an argument is the one\n"
    "# shape this format cannot carry: it ends the record, and the count below\n"
    "# then reports the log instead of letting it read as evidence.\n"
    "quote() {{\n"
    "  case ${{1:-}} in\n"
    "    '') printf \"''\" ;;\n"
    "    *[!A-Za-z0-9_@%+=:,./-]*)\n"
    "      rest=$1\n"
    "      printf \"'\"\n"
    "      while :; do\n"
    "        case $rest in\n"
    "          *\\'*) printf \"%s'\\\"'\\\"'\" \"${{rest%%\\'*}}\";"
    " rest=${{rest#*\\'}} ;;\n"
    "          *) break ;;\n"
    "        esac\n"
    "      done\n"
    "      printf \"%s'\" \"$rest\"\n"
    "      ;;\n"
    "    *) printf '%s' \"$1\" ;;\n"
    "  esac\n"
    "}}\n"
    "printf '.' >> \"$calls\"\n"
    "{{ quote \"${{0##*/}}\"\n"
    "  for argument in \"$@\"; do printf ' '; quote \"$argument\"; done\n"
    "  printf '\\n'\n"
    "}} >> \"$log\"\n"
    "exit 0\n"
)


def shim_text(log, calls):
    """The recording shim with this run's own two paths quoted into it."""
    return ARGV_SHIM.format(log=shlex.quote(str(log)), calls=shlex.quote(str(calls)))


def recorded_argv(line):
    """The argv a shim recorded, or None when this line is not one it wrote.

    `shlex` rather than `split()`, because the line holds one shell-quoted word
    per argument and the quoting is the whole point: `split()` reads
    `pytest 'a.py b.py'` as two paths, which is the question the recorder was
    asked and the answer it used to get wrong. A line this cannot parse is a
    line no shim wrote -- every word it writes is balanced -- so it is not an
    argv, and `None` is never evidence of a run.
    """
    try:
        return shlex.split(line)
    except ValueError:
        return None


def recorded(log, calls):
    """The recorded commands, or why this log is not what the shims wrote.

    Each call appends one line to the log and one character to the counter, so
    the two have to agree. They disagree when something other than a shim wrote
    a line, which is the forgery above; and a log that disagrees is not evidence
    of anything, in either direction, so it is returned as a problem.
    """
    lines = log.read_text().splitlines()
    invocations = len(calls.read_text())
    if len(lines) != invocations:
        return [], (
            f"the recorder's log holds {len(lines)} line(s) while the shims were "
            f"invoked {invocations} time(s), so a line no shim wrote reached it "
            "and nothing in this log is evidence"
        )
    return lines, None
# `status` is the shell's exit code, or None when the step was never started;
# `commands` what the shims recorded, in order; `output` the script's own
# output; `problem` why this reader could not answer, which is a different
# verdict from a step that ran no pytest and is never evidence of either; and
# `refused` whether that problem is this guard's own verdict about the step --
# a program, a path or a redirection it does not cover -- rather than this
# machine saying it could not measure. The two were one answer, and the one
# answer skipped: a step that names `grep` turned the executing guard off while
# the pull request stayed green. A refusal fails instead.
StepRun = namedtuple("StepRun", "status commands output problem refused",
                     defaults=(False,))


def refused_step(reason):
    """A step this guard will not execute, with why, and nothing started."""
    return StepRun(None, [], "", f"the step was not executed: {reason}", True)
# Executing a step is the expensive half and the answer does not depend on the
# host: every candidate step runs a platform-neutral `python -m pytest`, and
# what the recorder sees on macOS it sees on ubuntu. Measured, it cost +3.3 s on
# every pull request here and +22.6 s on each of the three legs of the manifests
# guard's matrix. So one leg executes and the others read, which is also what
# stops the Windows leg running a `pwsh` step under `bash`. Cost is not the only
# reason it is one leg: executing a step runs a workflow's shell on the machine
# that runs this suite, so off CI it is nobody's default.
EXECUTING_LEG = "linux"
FORCE_EXECUTION = "CONTENT_GUARD_EXECUTE"
# What says this run is CI's rather than somebody's own machine. `CI` is the
# marker this repository already reads for that (`tests/test_installer_windows.py`),
# and `GITHUB_RUN_ID` is one of the variables its own workflows consume from a
# step's environment (`.github/workflows/issue-agent-bridge.yml`). Either is
# enough; neither is set by running the suite at a prompt.
CI_MARKERS = ("CI", "GITHUB_RUN_ID")
CONFINED_REASON = (
    "the assertions that execute a workflow's shell run on the CI leg whose "
    f"platform is {EXECUTING_LEG}, so the repository pays for them once, and "
    "they are opt-in anywhere else because running one runs that shell on this "
    f"machine; set {FORCE_EXECUTION}=1 to run them here as well"
)


def confined_to_the_executing_leg(platform=None, environment=None):
    """True when this leg reads the workflows instead of executing their steps.

    Two things decide it and both have to hold: the platform, because that is
    what a matrix leg is, and a CI marker, because executing a step runs a
    workflow's own shell on whatever machine the suite runs on. The platform
    alone used to decide, which put the half that touches the host on for a
    Linux contributor, a Linux container and a downstream consumer who did
    nothing but run the suite, and off on darwin, where this repository is
    developed and where nobody was testing it. The variable overrides both
    directions: a maintainer working on this harness sets it and gets every
    assertion wherever they work, and an explicit 0 turns it off on the
    executing leg too.
    """
    platform = sys.platform if platform is None else platform
    environment = os.environ if environment is None else environment
    if FORCE_EXECUTION in environment:
        return environment[FORCE_EXECUTION] not in ("1", "true", "yes")
    if not platform.startswith(EXECUTING_LEG):
        return True
    return not any(environment.get(marker) for marker in CI_MARKERS)


def tracked_names(root=None):
    """`git_ls_files` for this root, with the default resolved before the cache.

    Resolved here rather than inside, because `tracked_names()` and
    `tracked_names(ROOT)` are two keys to an `lru_cache` and would be two runs
    of git for one answer.
    """
    return git_ls_files(ROOT if root is None else root)


@lru_cache(maxsize=4)
def git_ls_files(root):
    """Every path git tracks here, or why git could not say: `(names, problem)`.

    Tracked rather than walked, because `data/`, a dangling `.venv` symlink and
    other worktrees also live in this directory and none of them is part of
    what a runner checks out. The names are the whole point -- the copy below
    holds empty files -- so a glob expands as it would on a runner, `cd docs`
    succeeds, and a path that does not exist still does not. A smaller tree
    would quietly change what the recorder sees, so a failure is never a
    smaller tree.

    It used to be a raised CalledProcessError: `check=True` with nothing around
    it, which made a work tree a hard, undiagnosed dependency of both guards.
    Measured in an export of this repository with no `.git`, 15 cases here and
    11 in the manifests module failed with a traceback, where the same export of
    `dev` passed all 224. git is named in the problem because git is what the
    reader of that message has to go and look at.
    """
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root, capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip().splitlines()
        return (), (
            f"`git ls-files` in {root} exited {error.returncode}, so the tree a "
            "step would run against is unknown: "
            + (detail[-1] if detail else "no output")
        )
    except OSError as error:
        return (), (
            f"`git ls-files` could not be started in {root}, so the tree a step "
            f"would run against is unknown: {error}"
        )
    names = tuple(name for name in listing.stdout.split("\0") if name)
    if not names:
        return (), (
            f"`git ls-files` in {root} listed no files, so there is no tree to "
            "run a step against"
        )
    return names, None


def write_tree(directory, root=None):
    """The tracked path names as empty files under this directory, or git's problem."""
    names, problem = tracked_names(root)
    if problem:
        return problem
    for name in names:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return None


def why_steps_cannot_be_executed():
    """The one reason the executing assertions cannot run here, or None.

    Two of them, asked in this order so the cheap one answers first: the leg
    reads instead of executing, or git cannot say what the tree holds. The
    second is what an export of this repository with no `.git` is -- a source
    tarball, a vendored copy -- where these cases used to fail 26 times over a
    missing work tree. Neither is a verdict about the workflows, so neither is
    a failure.
    """
    if confined_to_the_executing_leg():
        return CONFINED_REASON
    problem = tracked_names()[1]
    if problem:
        return f"a step is executed against the tree git lists, and {problem}"
    return None


UNEXECUTABLE = why_steps_cannot_be_executed()
EXECUTES_STEPS = pytest.mark.skipif(
    UNEXECUTABLE is not None, reason=UNEXECUTABLE or "")


def leaves_the_tree(path):
    """True when this spelling does not stay inside the tree it is joined to.

    Lexically, because this answers before the tree exists and before anything
    is written: an absolute path replaces the tree it is joined to, and a `..`
    that `normpath` cannot cancel climbs out of it. `leaves_the_tree("a/../b")`
    is False and `leaves_the_tree("a/../../b")` is True, which is the
    difference the head of this branch did not make -- it asked whether the
    spelling started with `/`, so every `..` spelling counted as a path inside
    the tree. The real path is checked again where a file is written, which is
    the answer that also covers a symlink no reading can see.
    """
    return path.startswith("/") or os.path.normpath(path).split(os.sep)[0] == ".."


def write_shim(tree, path, text):
    """Put this recording shim inside this tree, or say why it was not written.

    The containment is checked here because this is the line that writes: a
    parents `mkdir`, a `write_text` and a `chmod 0o755` at `tree / path`, with
    nothing between them at the head of this branch. A `..` spelling of a shim
    path resolves outside the tree, and one was measured leaving a 0755
    `#!/bin/sh` stub beside it that survived the tree's cleanup -- anywhere the
    user can write, over an existing file of that name, a real interpreter
    included. `shim_plan` refuses the same spelling by reading before anything
    is created; this is what the resolved path says, so a symlink answers too.
    """
    destination = tree / path
    if not destination.resolve().is_relative_to(tree.resolve()):
        return f"`{path}` resolves outside {tree}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)
    destination.chmod(0o755)
    return None


def shim_plan(script):
    """Which program names, paths inside the tree and paths outside it a shim covers.

    Read by the same expressions as the static reader, so the recorder covers
    exactly what that reader would call pytest, plus pip. A bare name is
    answered by a shim directory at the front of PATH. A path inside the tree,
    such as `.venv/bin/pytest`, is written at that path in the copied tree,
    because PATH does not decide a path. A path that leaves the tree cannot be
    covered at all and is returned so the caller refuses the step: an absolute
    one would start the real program -- the real pytest over the real content
    checks, from inside the guard that checks them -- and a `..` spelling named
    a file to create outside the tree, which is what it was measured doing.
    """
    names, inside, outside = set(ALWAYS_SHIMMED), set(), set()
    for command in logical_commands(script):
        for token in command.split():
            bare = unquoted(token)
            if not (PYTEST_EXECUTABLE.match(bare) or PYTHON_EXECUTABLE.match(bare)
                    or PIP_EXECUTABLE.match(bare)):
                continue
            if leaves_the_tree(bare):
                outside.add(bare)
            elif "/" in bare:
                inside.add(bare)
            else:
                names.add(bare)
    return sorted(names), sorted(inside), sorted(outside)


def without_here_documents(script):
    """This script with every here-document body and its delimiter line removed.

    A here-document is data a program is fed, not a sequence of commands, and
    `logical_commands` reads one as commands on purpose: a path named inside it
    has to count as named, because hiding the command there and leaving the
    path visible is one of the eight forms #632 recorded. The reader that
    decides what may run needs the other answer -- `cat <<'PLAN'` ends with a
    line reading `PLAN`, which starts no program, and the lines between it
    start none either.
    """
    kept, delimiter = [], None
    for line in re.sub(r"\\\s*\n\s*", " ", script).splitlines():
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        kept.append(line)
        opened = HERE_DOCUMENT.search(line)
        if opened:
            delimiter = opened.group(1)
    return "\n".join(kept) + "\n"


def unconfined_programs(script):
    """The programs this script starts that are neither shimmed nor allowed.

    Read with the same expressions as everything else here: `shim_plan` says
    what a shim covers, and a word that is a shell builtin or a keyword starts
    nothing. What is left is what would run for real. This used to be asked by
    an assertion over the committed steps and by nothing else -- not by the
    executor, and not by the CI job that executes steps for the manifests
    guard, which does not run this module at all -- so a step whose command
    word was `/usr/bin/touch` was started and counted as a clean recorded run.
    The executor asks it now, through `why_this_guard_refuses`.

    Three words at a command position start nothing and are not reported, each
    of them a place where this reader is reading something that is not a
    command: a here-document's own lines, a `name()` function definition, and a
    word beginning with `-`, which is an option and means the split landed
    inside a command rather than at the start of one. What a word this reader
    misses falls back to is the old answer and not an execution: PATH holds the
    shims and the allowlist and nothing else, so the program is `command not
    found`. This reading is what makes that boundary a loud refusal instead of
    a status 127 nobody reads.
    """
    names, inside, outside = shim_plan(script)
    shimmed = {*names, *inside, *outside}
    found = []
    for command in logical_commands(without_here_documents(script)):
        word = command_word(command)
        if word is None or word in shimmed or word in SHELL_WORDS:
            continue
        if word in ALLOWED_PROGRAMS or word.endswith("()") or word.startswith("-"):
            continue
        found.append(word)
    return sorted(set(found))


def writes_outside_the_tree(script):
    """Every redirection target of this script that is not a literal path inside it.

    Accepted the other way round, because the spelling is all this has: a
    target is a path this guard can place only when it holds no expansion and
    stays inside the tree. An absolute path is outside the copy by
    construction, `..` climbs out of it, and `~`, `$NAME`, `$(...)` and a
    backtick are not paths yet at all -- the shell expands them when the step
    runs, long after this answer is given. The first two were measured
    succeeding and `> ~/../x` was measured writing beside the tree, each with
    the guard still recording and still passing, because the reader asked only
    whether the spelling began with `/` or `..`. `/dev/null` and the standard
    streams are the exception, because writing there touches nothing and a step
    that silences output writes one.
    """
    escaping = []
    for command in logical_commands(script):
        for target in REDIRECTION_TARGET.findall(command):
            target = unquoted(target)
            if target in HARMLESS_TARGETS:
                continue
            if EXPANDS_AT_RUNTIME.search(target) or leaves_the_tree(target):
                escaping.append(target)
    return sorted(set(escaping))


def why_this_guard_refuses(script):
    """Every reason this guard will not execute this script, read before it runs.

    One reader for the executor and for the assertion over the committed steps,
    so the line the executor holds is the line a workflow edit is measured
    against, and on every leg rather than only where steps are executed. Three
    reasons, each of them a way out of the tree that was measured working: a
    program neither a shim nor the allowlist covers, which PATH never decides
    for an absolute path and which `command not found` only reports after the
    fact; a shim path that does not stay inside the tree, which the harness
    wrote there 0755 before the step started; and a redirection target this
    reader cannot place as a literal path inside the tree.

    A reason here is a verdict about the step, which fails. It is not the same
    answer as a runner that could not measure, which skips.
    """
    reasons = []
    outside = shim_plan(script)[2]
    if outside:
        reasons.append(
            "it runs " + ", ".join(f"`{path}`" for path in outside)
            + ", which PATH cannot intercept and no shim inside the throwaway "
            "tree can stand in for")
    unconfined = unconfined_programs(script)
    if unconfined:
        reasons.append(
            "it starts " + ", ".join(f"`{word}`" for word in unconfined)
            + ", which is neither shimmed nor on the allowlist, so running it "
            "would run the real program")
    escaping = writes_outside_the_tree(script)
    if escaping:
        reasons.append(
            "it writes to " + ", ".join(f"`{path}`" for path in escaping)
            + ", which is not a literal path inside the throwaway tree")
    return reasons


def shell_argv(shell, script_file):
    """The argv GitHub would start for this `shell` value, or why this cannot.

    A value is either a name GitHub expands with its own template or a command
    line containing `{0}`, which it uses as written. Only `bash` and `sh` are
    started here: `pwsh`, `powershell` and `cmd` are real shells this machine
    may not have, and `cat {0}` is a shell at all only in the sense that
    `skip_reasons` already refuses. Saying so is the honest answer, because a
    step whose script was never executed must not be read as a step that ran
    no pytest.
    """
    words = (shell or "").split()
    name = words[0] if words else None
    if name is not None and name not in SHELL_COMMANDS:
        return None, (
            f"`shell: {shell}` is not a shell this guard can start, so the step "
            "was not executed"
        )
    if "{0}" in (shell or ""):
        return [word.replace("{0}", script_file) for word in words], None
    return [*SHELL_COMMANDS[name], script_file], None


def step_environment(workflow, job, step):
    """The `env` a step runs with: the workflow's, the job's and its own, merged.

    Merged rather than overridden, which is how GitHub's three `env` maps
    combine, with the innermost winning a shared name. It matters for one of
    the recorded forms: a command inside `if [ "${RUN_CONTENT_CHECKS:-0}" = "1" ]`
    runs nothing while nothing sets the variable, and a workflow that really
    does set it runs the content checks and has to keep passing. A `${{ }}`
    expression is passed through as the text it is, because nothing here
    evaluates one.
    """
    merged = {}
    for holder in (workflow, job, step):
        env = holder.get("env")
        if isinstance(env, dict):
            merged.update({k: v for k, v in env.items() if isinstance(v, str)})
    return merged


def execute_step(script, *, shell=None, env=None, working_directory=None, timeout=120):
    """Run one step's script the way Actions runs one, and report what it started.

    The script goes to a file and the file to the shell, which is how Actions
    runs a `run:` block and why `exit 0` on its first line ends the step. It
    runs in a throwaway copy of this repository's path names, so a glob expands
    and `cd docs` succeeds while nothing the script writes reaches the real
    tree, and with HOME inside that copy. Every program that could be pytest is
    replaced by a shim that appends its argv to a log and exits 0, so no check
    is collected, nothing is installed and nothing reaches the network: a
    `pip install` records instead of resolving, an action that would install a
    toolchain is a `uses:` step this never executes, and a step's own pytest
    line records instead of running. PATH and HOME are set after the workflow's
    `env`, so a workflow cannot redirect either; the recorder's own paths are in
    no variable at all, because the guard against a workflow redirecting the log
    was never the guard against a workflow writing it.

    What the script may do at all is decided before any of that, by
    `why_this_guard_refuses`, and a script it names a reason for is not
    started: the refusals are the boundary, and three of them were measured
    missing. A program this guard does not cover and the machine does not have
    still stops the script under `bash -e` when the reader did not see it as a
    command word; that non-zero status is returned as a `problem` with the last
    line of output, rather than as an empty log, because "could not run this"
    and "ran no pytest" are different answers -- and both of them differ again
    from "this guard will not run that", which is what `refused` carries.
    """
    refusals = why_this_guard_refuses(script)
    if refusals:
        return refused_step("; ".join(refusals))
    names, inside, _ = shim_plan(script)
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        tree = scratch / "tree"
        problem = write_tree(tree)
        if problem:
            return StepRun(None, [], "", problem)
        shims = scratch / "shims"
        shims.mkdir()
        allowed = scratch / "allowed"
        allowed.mkdir()
        for program in ALLOWED_PROGRAMS:
            on_this_machine = shutil.which(program)
            if on_this_machine:
                (allowed / program).symlink_to(on_this_machine)
        recorder = scratch / "recorder"
        recorder.mkdir()
        log, calls = recorder / "recorded-argv", recorder / "invocations"
        log.touch()
        calls.touch()
        text = shim_text(log, calls)
        for name in names:
            problem = write_shim(shims, name, text)
            if problem:
                return refused_step(problem)
        for path in inside:
            problem = write_shim(tree, path, text)
            if problem:
                return refused_step(problem)
        script_file = scratch / "step.sh"
        script_file.write_text(script)
        argv, problem = shell_argv(shell, str(script_file))
        if problem:
            return StepRun(None, [], "", problem)
        # Absolute, because PATH below holds only the shims and the allowlist and
        # the shell is not one of them. A machine without it says so as a problem.
        interpreter = shutil.which(argv[0])
        if interpreter is None:
            return StepRun(None, [], "", (
                f"`{argv[0]}` is not on this machine's PATH, so the step was not "
                "executed"
            ))
        argv = [interpreter, *argv[1:]]
        directory = tree if working_directory is None else tree / working_directory
        if not directory.is_dir():
            return StepRun(None, [], "", (
                f"`working-directory: {working_directory}` is not a directory of "
                "this repository, so the step was not executed"
            ))
        environment = dict(env or {})
        environment.update({
            "PATH": f"{shims}{os.pathsep}{allowed}",
            "HOME": str(tree),
        })
        try:
            finished = subprocess.run(
                argv, cwd=directory, env=environment,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return StepRun(None, [], "", f"the script did not finish in {timeout}s")
        except OSError as error:
            return StepRun(None, [], "", f"`{argv[0]}` could not be started: {error}")
        commands, forged = recorded(log, calls)
        output = (finished.stdout + finished.stderr).strip()
        if forged:
            return StepRun(finished.returncode, [], output, forged)
        if finished.returncode == 0:
            return StepRun(0, commands, output, None)
        last = output.splitlines()[-1] if output else "no output"
        return StepRun(finished.returncode, commands, output, (
            f"the script exited {finished.returncode} under the shims, so what it "
            f"would have run is unknown: {last}"
        ))


@lru_cache(maxsize=None)
def recorded_step(script, shell, environment, working_directory):
    """`execute_step` for one step, answered once however many files ask.

    The committed step runs all three content checks in one command, so without
    this it was executed once per file -- three identical runs of one script for
    one recorded log. The environment arrives as a sorted tuple because that is
    what makes the call hashable; everything else is the step as written.
    """
    return execute_step(script, shell=shell, env=dict(environment),
                        working_directory=working_directory)


def recorded_pytest_runs(commands, test_file):
    """The recorded commands that really handed this file to pytest.

    The same reader, over the argv the shims saw instead of over the script's
    text. The same refusals apply for the same reasons: a recorded
    `--collect-only` collected the file and ran none of it, and a recorded
    whole-file `--deselect` dropped it. What the shell did with the command --
    an `&`, a pipe -- is not in the argv it handed over, so it is read from the
    script's text instead, where `runs_as_pytest_argument` is what reads it.
    """
    return [command for command in commands
            if runs_the_file(recorded_argv(command), test_file)]


def executed_verdict(test_file, documents):
    """Execute every step that names this file and say what the recorder saw.

    Returns `(evidence, measured, reasons, refusals, disqualified)`: whether
    some step really handed the file to pytest, how many steps gave an answer
    either way, why the rest gave none, which of them this guard refused to
    execute at all, and which the workflow's own configuration disables. The
    five are what `require_a_recorded_run` decides on, and taking the workflows
    as an argument is what lets a constructed one be asked the same question as
    the committed ones.

    Three of them are kept apart because they are three different answers. A
    refusal is a verdict about the step and fails. A disqualification is a
    verdict about the configuration and fails too: a step a run never reaches
    is one the repository wrote that way, which no runner can mend. Only a
    reason may be this machine saying it could not measure, and only that
    skips. The three were one list, which is how a lone `if: ${{ false }}`
    produced no measurement and no complaint.
    """
    reasons, refusals, disqualified, measured = [], [], [], 0
    for name, workflow in documents.items():
        for job_name, job, step in steps_passing_the_file(workflow, test_file):
            skipped = skip_reasons(workflow, job_name, step)
            if skipped:
                disqualified.append(
                    f"{name} ({job_name}): a run may not reach this step -- "
                    + ", ".join(skipped))
                continue
            run = recorded_step(
                step["run"],
                effective_shell(workflow, job, step)[0],
                tuple(sorted(step_environment(workflow, job, step).items())),
                run_default(workflow, job, step, "working-directory")[0],
            )
            if run.problem:
                where = f"{name} ({job_name}): {run.problem}"
                (refusals if run.refused else reasons).append(where)
            elif recorded_pytest_runs(run.commands, test_file):
                return True, measured + 1, reasons, refusals, disqualified
            else:
                measured += 1
                reasons.append(
                    f"{name} ({job_name}): the script started "
                    f"{len(run.commands)} recorded command(s) and none of them ran "
                    f"{test_file}: {run.commands}")
    return False, measured, reasons, refusals, disqualified


def require_a_recorded_run(test_file, measured, problems, refusals=(), disqualified=()):
    """Pass, skip or fail on what executing the candidate steps answered.

    Five answers, not two. A step this guard will not execute is a verdict
    about the step -- it names a program, a path or a redirection the harness
    does not cover -- so it fails: a workflow edit that reaches outside what
    the throwaway tree bounds has to break the build rather than quietly stop
    measuring, which is what it did when `grep` in a step read as a runner that
    could not answer. A step a run never reaches is a verdict about the
    configuration and fails as well: it is disabled in the repository's own
    text, so no runner is going to answer differently tomorrow, and a lone
    `if: ${{ false }}` over the only candidate used to leave this with nothing
    measured and nothing to say. A step that was executed and ran no pytest
    over the file is the bypass this module exists to catch, and it fails. A
    candidate set whose every answer was a problem is a measurement nobody
    made -- no bash on PATH, a temp filesystem mounted `noexec`, a shim shebang
    the kernel refuses, pytest refusing a committed argv, the timeout on a
    contended runner -- and an unusable runner must not read as a bypass, so it
    skips carrying those reasons. Nothing naming the file at all is the
    original failure and stays one.
    """
    if refusals:
        pytest.fail(
            f"a step that names {test_file} is one this guard will not execute, "
            "so whether a run really hands the file to pytest was never "
            "measured: " + "; ".join(refusals)
        )
    if problems and not measured and not disqualified:
        pytest.skip(
            f"this guard could not measure whether a step really runs {test_file}: "
            + "; ".join(problems)
        )
    told = [*problems, *disqualified]
    pytest.fail(
        f"no step that names {test_file} really handed it to pytest when run. "
        + ("; ".join(told) if told else "No workflow names it at all.")
    )


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


@pytest.mark.parametrize("test_file", CONTENT_CHECKS)
@EXECUTES_STEPS
def test_running_the_step_really_hands_the_file_to_pytest(test_file):
    """And the step is run, because eight recorded forms read as if they ran it.

    The assertion above reads the script; this one executes it with a recording
    shim in place of every program that could be pytest, and asks what pytest
    was actually handed. A step whose script this guard cannot execute is
    reported as that, not as a step that ran nothing -- the two answers must not
    be allowed to look alike.

    A step a run never reaches is not evidence either, which this asked nothing
    about until review moved the three files into a second step carrying
    `if: ${{ false }}` and watched 252 cases pass while CI ran neither file. So
    every candidate goes through `skip_reasons` first, the same reader the
    calendar case has always used, and a step it disqualifies is its own answer:
    the configuration's, which fails rather than waiting for a runner that will
    never answer it.
    """
    evidence, measured, reasons, refusals, disqualified = executed_verdict(
        test_file, {path.name: load_workflow(path) for path in WORKFLOWS})
    if evidence:
        return
    require_a_recorded_run(test_file, measured, reasons, refusals, disqualified)


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
    # A quoted glob is not expanded by the shell, so pytest collects nothing:
    # measured, `ERROR: file or directory not found`.
    "python -B -m pytest 'tests/test_skill_*.py' -q",
    'python -B -m pytest "tests/test_skill_*.py" -q',
    # A node-id prefix that does match, after a `./` spelling of the positional.
    "python -m pytest ./tests/test_skill_freshness.py --deselect tests/test_skill_freshness.py",
    # Quoted exactly as the shell would hand it over bare: measured, each of
    # these deselects the whole file.
    "python -m pytest tests/test_skill_freshness.py --deselect='tests/test_skill_freshness.py'",
    'python -m pytest tests/test_skill_freshness.py --deselect="tests/test_skill_freshness.py"',
    "python -m pytest tests/test_skill_freshness.py --deselect 'tests/test_skill_freshness.py'",
    "python -m pytest tests/test_skill_freshness.py --deselect='tests/test_skill_freshness.py::'",
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
    # Measured: `--deselect ./<file>` deselects nothing, because a node id is
    # `<file>::<test>` and `./<file>` is not a prefix of it. The file runs.
    "python -m pytest ./tests/test_skill_freshness.py --deselect ./tests/test_skill_freshness.py",
    "python -m pytest ./tests/test_skill_freshness.py --deselect './tests/test_skill_freshness.py'",
    # Quote removal keeps the `./` distinction: the value still is not a node-id
    # prefix, so the file still runs and still counts.
    "python -m pytest tests/test_skill_freshness.py --deselect='./tests/test_skill_freshness.py'",
    # An inner quote belongs to the path: the shell removes only the outer pair,
    # so pytest gets a name matching no node id and the file still runs.
    "python -m pytest tests/test_skill_freshness.py --deselect=\"tests/test_skill_freshness.py'\"",
    "python -m pytest tests/test_skill_freshness.py --deselect='tests/test_skill_freshness.py\"'",
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
    ("pytest tests/a.py | tee log # tests/b.py", ["pytest tests/a.py |", "tee log"]),
    # A `&` separates like the rest, and the command before it keeps it.
    ("pytest tests/a.py &", ["pytest tests/a.py &"]),
    ("pytest tests/a.py & echo b", ["pytest tests/a.py &", "echo b"]),
    ("pytest tests/a.py &\npytest tests/b.py",
     ["pytest tests/a.py &", "pytest tests/b.py"]),
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
    ("\u0661\u0667 5 * * 1", False),       # Arabic-Indic digits: int() parses them, cron does not
    ("\u06f1\u06f7 \u06f5 * * \u06f1", False),      # the same field by field
    ("\u00b2 5 * * 1", False),         # isdigit() is true and int() raises on this one
    ("*/\u0662 * * * *", False),       # and in a step
    ("1-\u0665 5 * * 1", False),       # and in a range
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


# The eight forms recorded in #632. Each satisfies every reader above while the
# content checks never execute, so each was verified by applying it to the
# committed workflow, running this guard, and then running the step's script
# the way Actions runs one with argv-recording shims in place of python and
# pytest. `closed_by` says which half of this module refuses it: `running` for
# the seven whose pytest line is never reached, and `reading` for the one that
# does reach it and still is not evidence.
CONTROL_COMMAND = (
    "python -B -m pytest"
    " tests/test_skill_freshness.py"
    " tests/test_skill_invariants.py"
    " tests/test_source_check_classification.py"
    " -q -p no:cacheprovider"
)
RECORDED_BYPASSES = (
    ("a leading `exit 0`", "exit 0\n" + CONTROL_COMMAND + "\n", "running"),
    ("a here-document, where the command is data",
     "cat <<'PLAN'\n" + CONTROL_COMMAND + "\nPLAN\n", "running"),
    ("an `if` nobody sets",
     'if [ "${RUN_CONTENT_CHECKS:-0}" = "1" ]; then\n  '
     + CONTROL_COMMAND + "\nfi\n", "running"),
    ("a quoted string whose `;` the reader splits on",
     'echo "nothing to do; ' + CONTROL_COMMAND + '"\n', "running"),
    ("a shell function named pytest",
     "pytest() { echo skipped; }\n"
     "pytest tests/test_skill_freshness.py tests/test_skill_invariants.py"
     " tests/test_source_check_classification.py -q\n", "running"),
    ("a stub the step writes itself",
     "mkdir -p .tools\n"
     "printf '#!/bin/sh\\necho stubbed\\n' > .tools/pytest\n"
     "chmod +x .tools/pytest\n"
     ".tools/pytest tests/test_skill_freshness.py tests/test_skill_invariants.py"
     " tests/test_source_check_classification.py -q\n", "running"),
    ("the unreached side of `||`", "cd docs || " + CONTROL_COMMAND + "\n", "running"),
    ("a trailing `&`", CONTROL_COMMAND + " &\n", "reading"),
)


@pytest.mark.parametrize("name, script, closed_by", RECORDED_BYPASSES)
def test_a_recorded_bypass_hands_nothing_to_pytest(name, script, closed_by):
    """None of the eight is evidence, and which half refuses it is part of the case.

    The seven marked `running` are asserted to satisfy the static reader first.
    That is not a formality: it is what keeps the recorder honest about its own
    cases, and a case that some later reading rule catches by itself has to be
    moved to `reading` rather than left looking like proof that the recorder
    works. Then the script is executed and its log holds no pytest over the
    file. The eighth runs pytest and is refused by reading, because the step no
    longer waits for it.
    """
    read_as_evidence = runs_as_pytest_argument(
        logical_commands(script), SCHEDULED_BY_CALENDAR
    )
    if closed_by == "reading":
        assert not read_as_evidence, name
        return
    if UNEXECUTABLE:
        pytest.skip(UNEXECUTABLE)
    assert read_as_evidence, (
        f"{name} no longer needs the recorder: reading the script already refuses "
        "it, so this case belongs to `reading`"
    )
    run = execute_step(script)
    assert run.problem is None, run.problem
    assert recorded_pytest_runs(run.commands, SCHEDULED_BY_CALENDAR) == [], (
        f"{name} really did run pytest over {SCHEDULED_BY_CALENDAR}: {run.commands}"
    )


@EXECUTES_STEPS
def test_the_recorder_sees_the_command_a_real_step_runs():
    """Every bypass above ends in an empty log, so an empty log has to mean something.

    The control is the committed step's own command. Executed under the shims it
    records exactly once, and the recorded line reads as a pytest run over each
    of the three content checks -- with nothing installed, nothing collected and
    no network touched to get there. A recorder that recorded nothing would make
    all seven cases above vacuous, which is the mistake this pins.
    """
    run = execute_step(CONTROL_COMMAND + "\n")
    assert run.problem is None, run.problem
    assert run.status == 0
    assert run.commands == [CONTROL_COMMAND], run.output
    for test_file in CONTENT_CHECKS:
        assert recorded_pytest_runs(run.commands, test_file) == [CONTROL_COMMAND]


@EXECUTES_STEPS
def test_the_recording_keeps_the_argument_boundaries_the_shell_made():
    """One quoted path is not two paths, and the log spelt them the same way.

    The shim joined its argv with spaces, so `pytest 'a.py b.py'` -- one
    argument naming a file no tree holds, which pytest answers with `file or
    directory not found` -- was recorded as `pytest a.py b.py` and read back as
    a run of both. Everything downstream reads that line: this module's own
    verdict and the manifests guard, which hands the argv to a real pytest. One
    shell-quoted word per argument now, which `shlex` reads back exactly.
    """
    joined = f"{SCHEDULED_BY_CALENDAR} tests/test_skill_invariants.py"
    run = execute_step(f"python -B -m pytest '{joined}' -q\n")
    assert run.problem is None, run.problem
    [line] = run.commands
    assert recorded_argv(line) == ["python", "-B", "-m", "pytest", joined, "-q"], line
    assert recorded_pytest_runs(run.commands, SCHEDULED_BY_CALENDAR) == [], (
        f"one quoted argument was read as two paths: {line}")
    # The two real arguments are still a run of both, or the case above would
    # pass by refusing everything.
    separate = execute_step(f"python -B -m pytest {joined} -q\n")
    assert separate.problem is None, separate.problem
    for test_file in (SCHEDULED_BY_CALENDAR, "tests/test_skill_invariants.py"):
        assert recorded_pytest_runs(separate.commands, test_file) == separate.commands
    # And a word the shell kept whole survives the log however it is spelt:
    # the quoting is only around what needs it, so an ordinary command is
    # recorded as it was written.
    assert separate.commands == [f"python -B -m pytest {joined} -q"]
    quoted = execute_step("python -B -m pytest \"it's a path\" -q\n")
    assert quoted.problem is None, quoted.problem
    assert recorded_argv(quoted.commands[0])[4] == "it's a path", quoted.commands


@EXECUTES_STEPS
def test_the_tree_the_script_runs_in_is_this_repository_s_own_names():
    """A glob has to expand in it, and a directory the script enters has to exist.

    Both decide a case above. A committed workflow writes the glob spelling, so
    an empty tree would stop the recorder seeing a form that really runs the
    file; and `cd docs || <pytest>` skips its right side only because `cd docs`
    succeeds, which in an empty tree it would not. The files are empty, so
    nothing a script reads there is the repository's real content.
    """
    expanded = execute_step("python -B -m pytest tests/test_skill_*.py -q\n")
    assert expanded.problem is None, expanded.problem
    [recorded] = expanded.commands
    assert "*" not in recorded, f"the glob was not expanded: {recorded}"
    assert recorded_pytest_runs([recorded], SCHEDULED_BY_CALENDAR) == [recorded]
    entered = execute_step("cd docs\n" + CONTROL_COMMAND + "\n")
    assert entered.problem is None, entered.problem
    assert entered.commands == [CONTROL_COMMAND], "`cd docs` did not reach a directory"


@EXECUTES_STEPS
def test_the_step_runs_with_the_env_its_workflow_gives_it():
    """`if [ "${RUN_CONTENT_CHECKS:-0}" = "1" ]` is a bypass only while nothing sets it.

    A workflow that really does set the variable runs the content checks and has
    to keep passing, so the recorder runs the step under the three `env` maps
    merged the way GitHub merges them, innermost first.
    """
    assert step_environment(
        {"env": {"RUN_CONTENT_CHECKS": "0", "KEPT": "workflow"}},
        {"env": {"RUN_CONTENT_CHECKS": "1"}},
        {"run": "", "env": {"EXPRESSION": "${{ github.sha }}"}},
    ) == {
        "RUN_CONTENT_CHECKS": "1",
        "KEPT": "workflow",
        "EXPRESSION": "${{ github.sha }}",
    }
    gated = 'if [ "${RUN_CONTENT_CHECKS:-0}" = "1" ]; then\n  ' + CONTROL_COMMAND + "\nfi\n"
    assert execute_step(gated).commands == []
    assert execute_step(gated, env={"RUN_CONTENT_CHECKS": "1"}).commands == [CONTROL_COMMAND]


@EXECUTES_STEPS
@pytest.mark.parametrize("script, shell, words", [
    (CONTROL_COMMAND, "pwsh", "not a shell this guard can start"),
    (CONTROL_COMMAND, "cat {0}", "not a shell this guard can start"),
    ("/usr/bin/python3 -m pytest tests/test_skill_freshness.py", None,
     "which PATH cannot intercept"),
])
def test_a_step_this_guard_cannot_execute_is_reported_not_read_as_an_empty_log(
    script, shell, words
):
    """"Could not run this" and "ran no pytest" are different answers.

    A shell this machine may not have, and an absolute interpreter path PATH
    cannot intercept, both end with no recorded command -- which is exactly
    what a bypass looks like. Returning a problem instead keeps the assertion
    above from reporting a step it never executed as a step that ran nothing,
    and keeps the recorder from starting the real pytest over the real content
    checks from inside the guard that checks them.
    """
    run = execute_step(script + "\n", shell=shell)
    assert run.commands == []
    assert run.problem and words in run.problem, run.problem


@pytest.mark.parametrize("script, evidence", [
    ("python -m pytest tests/test_skill_freshness.py -q", True),
    ("python -m pytest tests/test_skill_freshness.py -q &", False),
    ("python -m pytest tests/test_skill_freshness.py -q & echo done", False),
    ("python -m pytest tests/test_skill_freshness.py -q && echo done", True),
    # Refused under the template the committed steps get: see `piped`, and the
    # cases above it that give the same script both verdicts, one key apart.
    ("python -m pytest tests/test_skill_freshness.py -q | tee log", False),
    # Refused although a `wait` would bind the status again: see `backgrounded`.
    ("python -m pytest tests/test_skill_freshness.py -q &\nwait", False),
])
def test_a_backgrounded_command_is_not_evidence(script, evidence):
    """The one of the eight that reading closes, and `&&` is not `&`.

    A backgrounded pytest does run, so the recorder sees it; what the step no
    longer does is wait for it, so `bash -e` returns 0 whatever the content
    checks say. `&&` and the other separators have to keep counting, which is
    the half of this worth getting wrong-way-round. A single `|` is the one that
    moved: under `bash -e` it discards the left side's status exactly as `&`
    does, and the cases above decide it by the step's own shell.
    """
    assert runs_as_pytest_argument(
        logical_commands(script), SCHEDULED_BY_CALENDAR
    ) is evidence


# The recorder's own integrity, because the step it judges can write files. The
# log path used to travel in the step's environment under a fixed variable name,
# and review built the step that appends the line the guard then reads: the
# printf below, with a right-hand side that satisfies the static reader and a
# shell that never reaches it. 252 cases passed with nothing started.
FORGED_RECORD = (
    "printf 'pytest tests/test_skill_freshness.py tests/test_skill_invariants.py"
    " tests/test_source_check_classification.py\\n'"
    ' >> "${CONTENT_CHECK_RECORDED_ARGV:-/dev/null}"\n'
    "true || python -B -m pytest tests/test_skill_freshness.py"
    " tests/test_skill_invariants.py tests/test_source_check_classification.py\n"
)


@EXECUTES_STEPS
def test_the_step_cannot_name_the_recorder_s_log():
    """The forged construction, verbatim, and the two answers that stop it.

    Reading cannot refuse the construction: the right side of `||` is a real
    pytest command over all three files, so the static half says yes and must
    -- bash never reaches it, which is exactly what executing the step is for.
    Two things stop it. Its redirection target is a variable, which is not a
    path this guard can place inside the throwaway tree, so the step is not
    executed at all. And the step would have no way to address the log even if
    it were: the paths are baked into each shim's own text per run and no
    variable in the step's environment holds either of them, which the second
    step below is what measures.
    """
    assert runs_as_pytest_argument(
        logical_commands(FORGED_RECORD), SCHEDULED_BY_CALENDAR
    ), "the static half is supposed to be satisfied; that is the premise here"
    run = execute_step(FORGED_RECORD)
    assert run.commands == [], (
        f"a step forged the recorder's log: {run.commands}")
    assert run.refused and "CONTENT_CHECK_RECORDED_ARGV" in run.problem, run.problem
    assert recorded_pytest_runs(run.commands, SCHEDULED_BY_CALENDAR) == []
    told = execute_step(
        'printf "%s\\n" "${CONTENT_CHECK_RECORDED_ARGV:-unset}"\n')
    assert told.problem is None, told.problem
    assert told.output == "unset", (
        f"the step's environment still names the recorder: {told.output}")


@EXECUTES_STEPS
def test_a_recorded_line_no_shim_wrote_is_refused(tmp_path):
    """And if a line reaches the log anyway, the count is what catches it.

    The shim's own text is readable by the step that runs it -- a shebang
    script has to be readable to be executed -- so baking the path in moves it
    out of the environment without putting it beyond a determined script. Two
    answers stand between that script and the log. The script below reads the
    path out of the shim with shell builtins and appends through `$log`, and a
    redirection target this reader cannot place as a literal path inside the
    tree is refused before the step starts, so it is never executed. Should a
    line reach the log by some route this reader does not see, the arithmetic
    is what reports it: each shim records one line and counts one invocation,
    so a log holding more lines than the shims were invoked times is a log with
    a line no shim wrote, and the whole run is reported as a measurement rather
    than read as evidence. That half is asked of `recorded` directly, on the
    two files a run leaves behind, because no step can reach them any more.
    """
    script = (
        'shim="$(command -v pytest)"\n'
        'log=""\n'
        "while IFS= read -r line; do\n"
        '  case "$line" in log=*) log="${line#log=}"; log="${log%\\\'}";'
        ' log="${log#\\\'}";; esac\n'
        'done < "$shim"\n'
        '[ -n "$log" ] || { echo "the shim no longer names its log"; exit 9; }\n'
        "printf 'pytest tests/test_skill_freshness.py\\n' >> \"$log\"\n"
        "pytest tests/test_skill_freshness.py\n"
    )
    run = execute_step(script)
    assert run.commands == [], run.commands
    assert run.refused and "$log" in run.problem, run.problem
    log, calls = tmp_path / "recorded-argv", tmp_path / "invocations"
    log.write_text("pytest tests/test_skill_freshness.py\n")
    calls.write_text(".")
    assert recorded(log, calls) == (["pytest tests/test_skill_freshness.py"], None)
    log.write_text("pytest forged.py\npytest tests/test_skill_freshness.py\n")
    lines, forged = recorded(log, calls)
    assert lines == [] and forged and "no shim wrote" in forged, (lines, forged)


@EXECUTES_STEPS
def test_an_unshimmed_program_is_refused_before_it_is_command_not_found():
    """The trust boundary: executing a step means running a diff as shell here.

    A maintainer who runs this suite over a pull request runs that branch's
    workflow scripts on their own machine. cwd and HOME are redirected and the
    programs that could be pytest are shimmed, which left every other program
    on the machine reachable -- measured, `git` and `curl` resolved to the real
    binaries. PATH holds the shim directory and a small allowlist and nothing
    else, and a command word outside both is refused by reading before the
    script is started at all, which is a verdict about the step.

    `command not found` is what is left for a program this reader does not see
    as a command word -- here behind `eval`, which starts nothing itself. That
    one is still a `problem`: `bash -e` stops, nothing was recorded, and the
    answer is that this runner could not measure rather than that the step is
    one the guard refuses.
    """
    run = execute_step("git --version\n" + CONTROL_COMMAND + "\n")
    assert run.commands == [], (
        f"the step reached {run.commands} after an unshimmed program")
    assert run.refused is True, run.problem
    assert run.problem and "`git`" in run.problem, run.problem
    hidden = execute_step("eval 'git --version'\n" + CONTROL_COMMAND + "\n")
    assert hidden.commands == [], hidden.commands
    assert hidden.refused is False, hidden.problem
    assert hidden.problem and "exited" in hidden.problem, hidden.problem
    assert "not found" in hidden.output, hidden.output


@EXECUTES_STEPS
def test_a_step_writing_outside_the_throwaway_tree_is_refused():
    """A redirection to an absolute path left the tree, and was measured doing it.

    What the throwaway tree bounds is a literal path that stays inside it and
    nothing else. An absolute target is outside it by construction, so the step
    is refused before it is started -- the same answer as a program path PATH
    cannot intercept, and for the same reason: running it would do something to
    this machine the guard cannot take back. A target a reader of the spelling
    cannot place at all is the case below. `/dev/null` and the standard streams
    are the exception, because writing there touches nothing.
    """
    outside = Path(tempfile.gettempdir()) / "content-guard-escaped-the-tree"
    outside.unlink(missing_ok=True)
    run = execute_step(f"echo forged > {outside}\n" + CONTROL_COMMAND + "\n")
    assert run.commands == []
    assert run.problem and str(outside) in run.problem, run.problem
    assert not outside.exists(), f"the step wrote {outside} outside the tree"
    assert execute_step("ls tests > /dev/null\n").problem is None


@EXECUTES_STEPS
def test_a_shim_path_that_climbs_out_of_the_tree_is_refused(tmp_path, monkeypatch):
    """`../x/pytest` never was inside the tree, and the harness wrote it there.

    A token counted as absolute only when it started with `/`, so a `..`
    spelling was a relative path -- and the relative paths are exactly the ones
    a shim is written at, by a parents `mkdir`, a `write_text` and a
    `chmod 0o755` under the throwaway tree with nothing in between. Measured at
    the head of this branch: a 0755 `#!/bin/sh` stub appeared beside the tree,
    from a step whose only unusual feature was that spelling, and survived the
    tree's cleanup. Any line of any workflow holding such a token would have
    done it, anywhere the user can write, an interpreter included. A spelling
    that does not stay inside the tree is refused exactly as an absolute one.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    escaped = tmp_path / "escaped" / "pytest"
    run = execute_step("echo ../../escaped/pytest\n" + CONTROL_COMMAND + "\n")
    assert not escaped.exists(), (
        f"a shim was written at {escaped}, outside the throwaway tree")
    assert run.commands == [], run.commands
    assert run.problem and "../../escaped/pytest" in run.problem, run.problem


def test_a_shim_is_never_written_outside_the_tree_it_is_given(tmp_path):
    """The check at the line that writes, where nothing stood at all.

    `shim_plan` refuses the `..` spelling above by reading, so this is the
    answer for a path that arrives here anyway: a spelling that reader does not
    model, or a symlink inside the tree, which no reading of a workflow's text
    can see. The write is a parents `mkdir`, a `write_text` and a `chmod 0o755`
    -- nothing to take back afterwards -- so containment is decided before it
    rather than regretted after.
    """
    tree = tmp_path / "tree"
    tree.mkdir()
    (tmp_path / "beside").mkdir()
    (tree / "out").symlink_to(tmp_path / "beside")
    for path in ("../escaped/pytest", "out/pytest"):
        problem = write_shim(tree, path, ARGV_SHIM)
        assert problem and path in problem, (path, problem)
    assert not (tmp_path / "escaped").exists(), "a shim was written beside the tree"
    assert list((tmp_path / "beside").iterdir()) == [], "a symlink led out of the tree"
    # A path inside the tree is still written, and still executable.
    assert write_shim(tree, ".venv/bin/pytest", ARGV_SHIM) is None
    assert (tree / ".venv/bin/pytest").stat().st_mode & 0o777 == 0o755


@EXECUTES_STEPS
def test_an_absolute_program_this_guard_cannot_shim_is_refused(tmp_path):
    """The refusal covered the three interpreter spellings and nothing else.

    `shim_plan` reads a token only when the pytest, python or pip expression
    matches it, so an absolute path to any other program entered no plan and
    was never refused -- while PATH, which holds the shims and the allowlist,
    is not consulted for an absolute path at all. Measured at the head of this
    branch: a step whose command word was `/usr/bin/touch` created the file it
    was given, and the step was reported as a clean recorded run. What refuses
    it now is the same reading the committed steps are asserted against.
    """
    marker = tmp_path / "absolute-program-ran"
    run = execute_step(f"/usr/bin/touch {marker}\n" + CONTROL_COMMAND + "\n")
    assert not marker.exists(), f"an absolute program ran and wrote {marker}"
    assert run.commands == [], run.commands
    assert run.problem and "/usr/bin/touch" in run.problem, run.problem


@EXECUTES_STEPS
def test_a_redirection_target_that_expands_at_runtime_is_refused(tmp_path, monkeypatch):
    """`~`, `$VAR` and `$(...)` are not paths this reader can place.

    A target was refused only when it started with `/` or its first segment was
    `..`, which reads the spelling and not the path: `~`, a variable and a
    command substitution are expanded by the shell when the step runs, long
    after this answer is given. Measured at the head of this branch:
    `> ~/../x` wrote beside the throwaway tree and the step was a clean
    recorded run. A target is accepted now only when it is a literal path that
    stays inside the tree.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    escaped = tmp_path / "escaped-by-tilde"
    run = execute_step(
        "echo forged > ~/../../escaped-by-tilde\n" + CONTROL_COMMAND + "\n")
    assert not escaped.exists(), f"the step wrote {escaped}, outside the tree"
    assert run.commands == [], run.commands
    assert run.problem and "escaped-by-tilde" in run.problem, run.problem
    for target in ("~/x", "$HOME/x", "${HOME}/x", "$(pwd)/x", "`pwd`/x", "a/../../x"):
        assert writes_outside_the_tree(f"echo forged > {target}\n") == [target], target
    # A literal path inside the tree is still a write this guard allows.
    assert writes_outside_the_tree("echo fine > build/log\n") == []


@EXECUTES_STEPS
def test_a_program_this_guard_does_not_cover_is_a_failure_not_a_skip():
    """`command not found` is not an unusable runner, and it read as one.

    A program that is neither shimmed nor on the allowlist stopped the script
    under `bash -e` with status 127, which the executor returned as a problem
    -- the answer kept for a runner that cannot measure. Nothing was measured,
    so the assertion skipped: a maintainer who adds `grep`, `tee`, `jq`,
    `coverage` or `timeout` to a step, or a redirection to an unset
    `$GITHUB_STEP_SUMMARY`, turns this guard off while the pull request stays
    green. The two answers are separated now. A step this guard will not
    execute is a verdict about the step and fails; a runner that cannot answer
    still skips.
    """
    script = "grep -q pytest tests/test_skill_invariants.py\n" + CONTROL_COMMAND + "\n"
    run = execute_step(script)
    assert run.commands == [], run.commands
    assert run.problem and "grep" in run.problem, run.problem
    verdict = executed_verdict(
        SCHEDULED_BY_CALENDAR,
        {"w.yml": {"jobs": {"content": {"steps": [{"run": script}]}}}})
    assert verdict[0] is False
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as outcome:
        require_a_recorded_run(SCHEDULED_BY_CALENDAR, *verdict[1:])
    assert isinstance(outcome.value, pytest.fail.Exception), (
        "a step naming a program this guard does not cover has to fail rather "
        f"than skip: {outcome.value}")
    # A runner that really could not answer still skips, or the split is one-sided.
    with pytest.raises(pytest.skip.Exception):
        require_a_recorded_run(SCHEDULED_BY_CALENDAR, 0,
                               ["w.yml (content): the script did not finish in 120s"])


def test_a_configuration_that_disables_the_only_candidate_fails():
    """Nothing measured and nothing wrong with the runner is still a verdict.

    Moving the three files into a step carrying `if: ${{ false }}` is what
    review did to this guard, and reading the `if:` was only half the answer. A
    disqualified step gave a reason, a candidate set of nothing but reasons is
    an unusable runner, and an unusable runner skips -- so the hole closed by
    reading the `if:` reopened one line later, with the suite green and CI
    running neither file. The configuration's own verdict is now its own
    answer: no runner is going to reach that step tomorrow either.
    """
    gated = {"jobs": {"content": {"steps": [
        {"run": CONTROL_COMMAND, "if": "${{ false }}"}]}}}
    verdict = executed_verdict(SCHEDULED_BY_CALENDAR, {"gated.yml": gated})
    assert verdict[0] is False
    assert verdict[1] == 0, "a step a run never reaches must not be a measurement"
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as outcome:
        require_a_recorded_run(SCHEDULED_BY_CALENDAR, *verdict[1:])
    assert isinstance(outcome.value, pytest.fail.Exception), (
        "a configuration that disables the only candidate has to fail rather "
        f"than skip: {outcome.value}")
    assert "may not reach this step" in str(outcome.value), outcome.value
    # And the answer it must not swallow: a runner that really could not measure
    # still skips, or this has traded one silent pass for a permanent failure.
    with pytest.raises(pytest.skip.Exception):
        require_a_recorded_run(SCHEDULED_BY_CALENDAR, 0,
                               ["w.yml (content): the script did not finish in 120s"])


def test_every_step_this_guard_executes_stays_inside_what_it_confines():
    """And the line is held by an assertion on every leg, not where steps run.

    Both guards execute every step whose script mentions pytest, so what the
    executor refuses is a claim about those steps. One third of that claim was
    asserted here -- the programs -- and the other two thirds nowhere; and the
    CI job that executes steps for the manifests guard does not run this module
    at all, so even that third was asserted on one leg only. This reads the
    committed steps with the executor's own reader, on every leg and without
    starting anything, so a workflow edit that reaches outside the tree breaks
    the build loudly instead of turning a guard into a skip. Today every such
    step starts only `python` or `python3`, which are shimmed.
    """
    refused = {}
    for path in WORKFLOWS:
        for job in (load_workflow(path).get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not (isinstance(step, dict) and isinstance(step.get("run"), str)
                        and "pytest" in step["run"]):
                    continue
                reasons = why_this_guard_refuses(step["run"])
                if reasons:
                    refused[f"{path.name}: {step.get('name', step['run'][:40])}"] = reasons
    assert refused == {}, (
        "a step this guard executes reaches outside what the throwaway tree, the "
        f"shim PATH and the allowlist confine: {refused}"
    )
    # The reader really answers, or the assertion above is a tautology. One
    # case per reason, each of them measured escaping at the head of this branch.
    for script in (
        "grep -q x tests/test_skill_invariants.py\n" + CONTROL_COMMAND + "\n",
        "/usr/bin/touch escaped\n" + CONTROL_COMMAND + "\n",
        "echo ../../escaped/pytest\n" + CONTROL_COMMAND + "\n",
        CONTROL_COMMAND + " >> $GITHUB_STEP_SUMMARY\n",
    ):
        assert why_this_guard_refuses(script), script


@pytest.mark.parametrize("platform, environment, confined", [
    ("linux", {"CI": "true"}, False),
    ("linux", {"GITHUB_RUN_ID": "17"}, False),
    # Off CI the leg is opt-in on every platform, this one included: a Linux
    # contributor, a Linux container and a downstream consumer used to execute
    # a workflow's shell by running the suite.
    ("linux", {}, True),
    ("darwin", {}, True),
    ("darwin", {"CI": "true"}, True),
    ("win32", {"CI": "true"}, True),
    ("darwin", {"CONTENT_GUARD_EXECUTE": "1"}, False),
    ("win32", {"CONTENT_GUARD_EXECUTE": "1"}, False),
    ("linux", {"CONTENT_GUARD_EXECUTE": "1"}, False),
    # Both directions: an explicit 0 turns it off on the executing leg too,
    # which is what a maintainer reaches for when a runner is contended.
    ("linux", {"CI": "true", "CONTENT_GUARD_EXECUTE": "0"}, True),
])
def test_the_executing_assertions_are_confined_to_one_leg(platform, environment, confined):
    """Executing a step is what costs, and the repository pays for it once.

    Measured before this: +3.3 s on every pull request for the content file and
    +22.6 s on each of three matrix legs for the manifests file, which runs on
    ubuntu, macOS and Windows. The steps are platform-neutral `python -m pytest`
    commands and the recorder's answer does not depend on the host, so running
    them three times buys nothing; what the other two legs are for is the rest
    of these files, which read rather than run. So the executing assertions ask
    this predicate and skip where it says so, and the Windows leg stops
    executing `pwsh` steps under `bash` as a side effect.

    The override is for the machine where the work happens: a maintainer
    changing the harness runs it with `CONTENT_GUARD_EXECUTE=1` and gets every
    assertion, on any platform.
    """
    assert confined_to_the_executing_leg(platform, environment) is confined


def test_the_switch_that_runs_the_host_touching_leg_is_documented():
    """A variable that makes a suite run a workflow's shell has to be findable.

    The leg is opt-in off CI, which means the only way to run it on a
    maintainer's machine is this variable, and it was named in no workflow and
    no README: the half of this module that touches the host was reachable only
    by reading the module. `tests/README.md` is where this repository documents
    what a selection does to the machine it runs on, so the name and the effect
    belong there.
    """
    readme = (ROOT / "tests/README.md").read_text(encoding="utf-8")
    assert FORCE_EXECUTION in readme, (
        f"{FORCE_EXECUTION} turns on the assertions that run a workflow's shell "
        "on this machine and tests/README.md does not mention it")


@pytest.mark.parametrize("measured, problems, outcome, words", [
    (1, [], "fail", "really handed"),
    (0, [], "fail", "No workflow names it at all"),
    (1, ["a.yml (job): the script did not finish in 120s"], "fail", "really handed"),
    (0, ["a.yml (job): the script did not finish in 120s"], "skip", "did not finish"),
    (0, ["a.yml (job): `bash` is not on this machine's PATH"], "skip",
     "could not measure"),
])
def test_an_unusable_runner_is_unmeasured_and_not_a_bypass(
    measured, problems, outcome, words
):
    """The third answer, which was being thrown in with the second.

    The problem and the no are kept apart per step and were then collapsed:
    the assertion fired when every answer was a problem, which is what no bash
    on PATH, a temp filesystem mounted `noexec`, a shim shebang the kernel will
    not run, and the 120 s timeout on a contended runner all produce. Each of
    them would have reported this repository's wiring as broken on a runner
    that simply could not answer. So the failure now needs one step that really
    was executed and really ran nothing, and a candidate set whose every answer
    is a problem skips carrying those reasons.
    """
    raises = pytest.skip.Exception if outcome == "skip" else pytest.fail.Exception
    with pytest.raises(raises, match=words):
        require_a_recorded_run(SCHEDULED_BY_CALENDAR, measured, problems)


@EXECUTES_STEPS
def test_a_step_a_run_never_reaches_is_not_evidence():
    """`if: ${{ false }}` is a step CI never runs, and it satisfied both halves.

    Review left the calendar file in the committed step and moved the other two
    into a second step carrying that condition: 252 cases passed while a run
    executed neither file. The step was executed here because nothing asked
    whether a run reaches it -- `skip_reasons` was applied to the calendar case
    alone. Every candidate goes through it now, so the condition disqualifies
    the step and the step is not a measurement of anything. What that answer
    then decides is the case beside this one.
    """
    gated = {"jobs": {"content": {"steps": [
        {"run": CONTROL_COMMAND, "if": "${{ false }}"}]}}}
    evidence, measured, reasons, _, disqualified = executed_verdict(
        SCHEDULED_BY_CALENDAR, {"gated.yml": gated})
    assert evidence is False
    assert measured == 0, "a step a run never reaches measures nothing"
    assert reasons == [], reasons
    assert any("may not reach this step" in entry for entry in disqualified), (
        disqualified)
    # The same step without the condition is evidence, or the case above is vacuous.
    reachable = {"jobs": {"content": {"steps": [{"run": CONTROL_COMMAND}]}}}
    assert executed_verdict(SCHEDULED_BY_CALENDAR, {"open.yml": reachable})[0] is True


@pytest.mark.parametrize("shell, pipefail", [
    (None, False),                      # the committed steps: `bash -e {0}`
    ("bash", True),                     # GitHub expands this one with pipefail
    ("sh", False),
    ("bash -eo pipefail {0}", True),    # a command line given as written
    ("pwsh", False),
])
def test_whether_a_pipeline_can_redden_the_step_comes_from_its_shell(shell, pipefail):
    """`bash -e` is not `bash --noprofile --norc -eo pipefail`, measured on both.

    A step with no `shell` key gets the first, where `false | cat` exits 0, so a
    content check that fails on the left of a pipe cannot redden the step --
    which is the same reason a trailing `&` is refused, and the pipe was pinned
    as evidence while the ampersand was refused. `shell: bash` gets the second,
    where the pipeline carries the failure, and GitHub's own table is what says
    which template a step gets.
    """
    assert pipefail_in_effect(shell) is pipefail


@pytest.mark.parametrize("script, pipefail, evidence", [
    ("python -m pytest tests/test_skill_freshness.py -q", False, True),
    ("python -m pytest tests/test_skill_freshness.py -q | tee log", False, False),
    ("python -m pytest tests/test_skill_freshness.py -q | tee log", True, True),
    ("python -m pytest tests/test_skill_freshness.py -q | cat | cat", False, False),
    ("ls | python -m pytest tests/test_skill_freshness.py -q", False, True),
    ("python -m pytest tests/test_skill_freshness.py -q && echo done", False, True),
    ("python -m pytest tests/test_skill_freshness.py -q &", True, False),
])
def test_a_pipeline_is_evidence_only_where_a_failure_survives_it(
    script, pipefail, evidence
):
    """The left of a pipe is the position that loses its exit status, not the right.

    `ls | pytest <file>` is the last command of the pipeline, so its failure is
    the pipeline's and the step's under either template. A backgrounded command
    stays refused with pipefail, because `&` is not `|`: nothing waits for it.
    """
    assert runs_as_pytest_argument(
        logical_commands(script), SCHEDULED_BY_CALENDAR, pipefail=pipefail
    ) is evidence


def test_the_committed_template_and_a_pipefail_step_reach_different_verdicts():
    """The same script, one step key apart, and the guard must not agree with itself.

    This is what the finding was: the module already resolves a step's shell
    three levels up for the calendar case, and the reader that decides evidence
    ignored it, so `| tee log` counted under a template where the content
    check's failure is discarded.
    """
    piped = CONTROL_COMMAND + " | tee log"
    bare = {"jobs": {"content": {"steps": [{"run": piped}]}}}
    with_pipefail = {"jobs": {"content": {"steps": [{"run": piped, "shell": "bash"}]}}}
    inherited = {"defaults": {"run": {"shell": "bash"}},
                 "jobs": {"content": {"steps": [{"run": piped}]}}}
    assert steps_passing_the_file(bare, SCHEDULED_BY_CALENDAR) == [], (
        "a failing content check on the left of a pipe cannot redden a `bash -e` step")
    assert steps_passing_the_file(with_pipefail, SCHEDULED_BY_CALENDAR), (
        "under `shell: bash` the pipeline carries the failure, so it is evidence")
    assert steps_passing_the_file(inherited, SCHEDULED_BY_CALENDAR), (
        "and the shell is resolved from `defaults.run` like everything else here")


def test_git_failing_is_the_harness_s_own_problem_and_names_git(tmp_path):
    """A work tree was a hard, undiagnosed dependency of both guards.

    `git ls-files` ran with `check=True`, so in an export of this repository
    with no `.git` -- a source tarball, a vendored copy, an unpacked artifact --
    15 cases here and 11 in the manifests module failed with a
    CalledProcessError, where the same export of `dev` passed all 224. The names
    are not optional: they are the tree a step's script runs against. What is
    optional is raising, so it is reported like every other thing this harness
    cannot do, with git named in it.
    """
    names, problem = tracked_names(tmp_path)
    assert names == ()
    assert problem and "git ls-files" in problem, problem
    assert write_tree(tmp_path / "tree", root=tmp_path) == problem
    # Names and a problem are exclusive, which is what keeps the case above from
    # being a reading of an empty answer; and where there is a work tree, the
    # names are this repository's own.
    here, here_problem = tracked_names()
    assert (here_problem is None) == bool(here)
    if here_problem is None:
        assert "tests/test_content_checks_workflow.py" in here


def test_a_step_is_not_executed_when_git_cannot_say_what_the_tree_holds(monkeypatch):
    """Because a smaller tree would quietly change what the recorder sees.

    A glob expands against these names and `cd docs` succeeds because of them,
    so a tree built from a failed listing would answer a different question than
    the one asked. The step is not executed at all, and the problem is git's.
    """
    monkeypatch.setattr(
        "tests.test_content_checks_workflow.write_tree",
        lambda directory, root=None: "`git ls-files` in /x failed: not a git repository",
    )
    run = execute_step(CONTROL_COMMAND + "\n")
    assert run.commands == []
    assert run.problem and "git ls-files" in run.problem, run.problem


@EXECUTES_STEPS
def test_the_shim_list_covers_every_program_that_could_start_pytest():
    """Emptying this list left all 252 cases passing, which is the hole in them.

    `shim_plan`'s token scan finds pytest, python and pip by regular expression,
    so those three are covered twice over; `uv` and `uvx` come from the list
    alone, and `pytest_invocation` steps over both as prefixes. uv is on PATH on
    a developer machine, and `uv run pytest tests/x.py` under the real uv
    resolves an environment over the network from inside the guard that exists
    to prove nothing reaches the network. The recorded log is the proof it does
    not: the shim answers, and what it recorded reads back as the pytest run it
    was.
    """
    names, _inside, _outside = shim_plan("echo nothing to shim here\n")
    for program in ("python", "python3", "pytest", "pip", "pip3", "uv", "uvx"):
        assert program in names, f"{program} is not shimmed, so a step starts the real one"
    run = execute_step("uv run pytest tests/test_skill_freshness.py\n"
                       "uvx pytest tests/test_skill_freshness.py\n")
    assert run.problem is None, run.problem
    assert run.commands == ["uv run pytest tests/test_skill_freshness.py",
                            "uvx pytest tests/test_skill_freshness.py"], run.output
    assert recorded_pytest_runs(run.commands, SCHEDULED_BY_CALENDAR) == run.commands
