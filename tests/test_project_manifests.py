"""The two manifests that decide what a fresh installation gets and what CI checks.

`requirements.txt` is the only thing `python -m venv` plus `pip install -r` puts in
an installation's environment, so a runtime dependency that reaches only
`pyproject.toml` is absent wherever the package itself was not installed. The
workflow triggers decide which changes are tested at all, so an installer that no
`paths:` list names ships without any run of its own tests.

The second invariant used to answer that by reimplementing two parsers that
belong to someone else -- GitHub's path-filter globs and enough of a shell and of
pytest's command line to tell an invocation from a mention -- and three review
rounds each found another gap in the emulation, the fix for one round becoming
the subject of the next (#555). Neither parser is here any more.

A `paths:` entry now has to name the file verbatim. No glob is interpreted, so
no glob can be interpreted wrongly; a file covered only by a glob fails with the
remedy in the message, which is to name it beside the glob -- what a reader of
the workflow wants anyway. A `run` command is executed instead of parsed, by the
harness `tests/test_content_checks_workflow.py` built for #632: the script goes
to `bash -e` in a throwaway tree of this repository's tracked path names, with
every program that could be pytest replaced by a shim that records its argv and
exits 0. What that shim recorded then goes to the real pytest in a tree of the
same shape, where the files in question are the only ones holding a test, and
"does this command run the file" is answered by pytest running it or not.

So `echo pytest x.py` records nothing because `echo` is not the shim; `--ignore
x.py` is pytest's own exclusion rather than a value this file forgot to drop;
`--collect-only` collects and runs nothing because that is what pytest does with
it; and a glob is expanded by the shell against real names. What is left to get
wrong is not a table of options or a translation of a glob syntax, because
neither exists here: the two external contracts are read by the two programs
that own them.

The cost is the point of the trade and worth naming: each distinct step is
executed once under the shims and each distinct pytest argv once under the real
pytest, so this module starts subprocesses where it used to split strings. It
pays for the steps that could reach the file under test, not for every step in
the repository that mentions pytest, and only on the CI leg that executes --
measured, the two together were +22.6 s on each of three matrix legs.

An argv pytest refuses and a step a run never reaches are reported as
measurements that could not be made, never as a file that does not run; a
candidate set where every answer is one of those is an unusable runner rather
than a bypass, and skips. A step the recording harness refuses to execute is
the other answer and fails, because it is a verdict about the step: a
maintainer who adds an ordinary program to a step used to turn this guard into
a silent skip on a green pull request.

Handing a recorded argv to a real program is a trust boundary of its own, and
at first it was an open one: the argv went to `python -m pytest` with no `env`
and no bound on what a path argument could name, so a workflow's own text
decided what the inner pytest deleted (`--basetemp` is removed recursively
before it is recreated), where it wrote (`--junitxml`), what configuration it
read (`-c`, `--rootdir`, `-o`) and what it imported (`-p`), while the session's
own `PYTEST_ADDOPTS` could decide the verdict. The inner run gets a built
environment now, every path argument has to resolve inside the stub tree, and
an argv carrying one of those options is a measurement this guard does not
make.

Where the stubs stop is stated too: they share the path and nothing else,
holding one test called `test_executed` and no marks, so an argv that selects
with `-k` or `-m` asks about the real file's names and markers and is
unmeasurable here. A path reaches pytest as itself; anything inside the file
does not.
"""

from collections import namedtuple
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import warnings

import pytest
import yaml

from tests.test_content_checks_workflow import (  # the #632 harness, not a second one
    EXECUTES_STEPS,
    effective_shell,
    pytest_invocation,
    recorded_step,
    run_default,
    skip_reasons,
    step_environment,
    tracked_names,
    workflow_paths,
    write_tree,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = workflow_paths(ROOT / ".github/workflows")
INSTALLERS = sorted((ROOT / "scripts").glob("install_*.py"))
INSTALLER_TESTS = sorted((ROOT / "tests").glob("test_install_*.py"))
NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
# The files this module asks about. Every inner pytest run writes a recording
# test into all of them at once, so one run answers for all and the answers are
# cached by the argv alone.
RECORDED_FILES = frozenset(
    [f"tests/{Path(__file__).name}"] + [f"tests/{path.name}" for path in INSTALLER_TESTS])
RECORD = "executed-files"
RECORDING_TEST = (
    "# Written by tests/test_project_manifests.py. Records that pytest executed a\n"
    "# test in this file, which is the only question that module asks of pytest.\n"
    "def test_executed():\n"
    "    with open({log}, 'a', encoding='utf-8') as log:\n"
    "        log.write({name} + '\\n')\n"
)
# pytest's own exit codes that answer the question this module puts to it: tests
# ran and passed, tests ran and failed, or nothing was collected -- in each case
# the log says what executed. Anything else is a run that did not answer: a
# usage error (4 today), an interruption, an internal error, a signal. Reading
# the list of answers rather than asserting the number of one refusal is what
# keeps a pytest release from redefining this guard's verdict.
ANSWERING_EXIT_CODES = frozenset({0, 1, 5})
# Options whose value selects part of what a file holds, rather than naming the
# file. The stub shares only the path with the file it stands for -- one test
# called `test_executed`, no marks -- so pytest's answer about a selection is an
# answer about the stub, and it was measured wrong in both directions. Neither
# is read as a verdict now; both are the measurement this guard cannot make.
SELECTING_OPTIONS = frozenset({"-k", "-m"})
# Options that reach outside the tree the inner run is given, whatever its path
# arguments say, so an argv carrying one is not handed to pytest at all. Read in
# the pinned pytest's own source rather than recalled: a given `--basetemp` that
# exists is removed recursively before it is recreated
# (`_pytest/tmpdir.py`, `TempPathFactory.getbasetemp`), `--junitxml` writes its
# report wherever it is told, `-c` and `--rootdir` move the configuration a run
# reads, and `-o` sets any ini option, `addopts` included. The trigger is
# ordinary: a workflow step that records `--basetemp=<dir>`, or a `--junitxml`
# a maintainer types.
UNBOUNDED_OPTIONS = frozenset({"--basetemp", "--junitxml", "--rootdir", "-c", "-o"})
# `-p` is the sixth and the one with a measurable exception. `-p NAME` imports a
# module as a plugin, which is arbitrary code; `-p no:NAME` only blocks one and
# imports nothing (`_pytest/config/__init__.py`, `consider_pluginarg`). The
# committed steps of this repository write `-p no:cacheprovider` and nothing
# else, and refusing that spelling would turn the guards over
# `tests/test_project_manifests.py`, `tests/test_install_review_config.py` and
# `tests/test_install_setup_options.py` into permanent skips -- which is the
# failure the paragraph above is about, bought to close a hole that spelling
# does not open.
BLOCKS_A_PLUGIN = "no:"
# What the inner pytest keeps of this process's environment, which on a POSIX
# host is nothing: `sys.executable` finds its own interpreter and site-packages
# without help, and `PYTEST_ADDOPTS` alone was enough to decide every verdict
# this module gives -- measured, `--collect-only` in it turned "this file runs"
# into "this file does not run". Windows needs these to start a process and to
# seed its random generator at all.
KEPT_FROM_THE_HOST = ("SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT")
PYTEST_TIMEOUT = 120
# The modes that make pytest parse a file and run none of its tests, each one
# measured on every run by the pytest that is installed rather than listed here
# from a reading of its help. Six were lost when the two parsers were deleted:
# `--setuponly`, `--setupplan`, `--funcargs`, `--fixtures-per-test`, `-h` and
# `-V`, four of them added deliberately for pytest's de-hyphenated aliases.
# `-k` and `-m` are not modes: they select, and `SELECTING_OPTIONS` has them.
NON_RUNNING_MODES = (
    "--collect-only", "--collectonly", "--co",
    "--setup-only", "--setuponly", "--setup-plan", "--setupplan",
    "--fixtures", "--funcargs", "--fixtures-per-test",
    "--markers", "--help", "-h", "--version", "-V",
)
# `files` is what really executed, `problem` why pytest could not be asked.
Executed = namedtuple("Executed", "files problem")
# `hosts` the workflows that really run the file; `problems` the steps and
# commands this guard could not measure, which is a third answer and not a no;
# `measured` how many of them pytest did answer, because a candidate set that
# answered nothing at all is an unusable runner rather than a bypass; and
# `refusals` the steps the recording harness will not execute, which is a
# verdict about the step and fails rather than skipping.
Invocation = namedtuple("Invocation", "hosts problems measured refusals")
SCRATCH = []


class UnmeasuredCandidate(UserWarning):
    """A candidate step or argv this guard could not measure, on a passing run.

    A problem that another workflow's positive answer made irrelevant to the
    assertion is still a workflow this guard cannot see into, and it was being
    discarded in silence: `workspace-bindings.yml` commits an argv pytest
    refuses on every run and nothing ever printed it. pytest collects these in
    its warnings summary, so the gap is visible without failing a repository
    that is wired correctly.
    """


def normalise(name: str) -> str:
    # PEP 503: the name pip resolves, so PyYAML and pyyaml are one dependency.
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_names(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        match = NAME.match(line)
        if match:
            names.add(normalise(match.group(1)))
    return names


def load(path: Path):
    # BaseLoader keeps GitHub's `on` key instead of turning it into the boolean True.
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def trigger_paths(document) -> set[str]:
    # `paths` only: `paths-ignore` suppresses a run, so a workflow that ignores an
    # installer's path would otherwise satisfy the assertion that names it.
    triggers = document.get("on") or {}
    paths = set()
    if isinstance(triggers, dict):
        for event in triggers.values():
            if isinstance(event, dict):
                paths.update(event.get("paths") or [])
    return paths


def triggered_by(path: str, documents: dict) -> list[str]:
    """The workflows a change to `path` starts, by a verbatim `paths:` entry.

    Verbatim, because the glob syntax of a `paths:` filter is GitHub's and a
    translation of it here has to agree with GitHub about every spelling: `**/`
    matching zero directories, `*` stopping at a slash, a character class, a
    negation. The translation that stood here got the first one wrong (#555),
    and the next review round would have found the one after it.

    So no entry is interpreted. A glob is not cover and a file covered only by
    one fails the assertions below, where the message says to name it beside the
    glob. An entry that begins with `!` is an exclusion and never equals a path,
    which is the right answer rather than an unmodelled case.
    """
    return [name for name, document in documents.items()
            if path in trigger_paths(document)]


def pytest_candidates(document) -> list[tuple]:
    """Every `(job name, job, step)` whose script mentions pytest at all.

    A mention narrows which steps are worth executing and decides nothing: `echo
    pytest x.py` mentions it and runs nothing, and reading a mention as a run was
    the second gap of #555. A step that never writes the word cannot call the
    shim under any of the eight bypasses #632 recorded.
    """
    found = []
    for job_name, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if (isinstance(step, dict) and isinstance(step.get("run"), str)
                    and "pytest" in step["run"]):
                found.append((job_name, job, step))
    return found


def mentions_the_file(script: str, path: str) -> bool:
    """True when some token of this script could reach this path.

    What it decides is which steps are worth executing, and nothing else: a
    step that cannot be read as naming the file cannot hand it to pytest under
    any of the eight bypasses #632 recorded, because all eight hide the command
    and none hides the path. So the reading is generous on purpose -- a glob, a
    directory, a `./` spelling, a quoted token and the bare basename all count,
    and so does a name inside a here-document, since catching that is what the
    recorder is for. A candidate wrongly dropped would be a bypass; a candidate
    wrongly kept is only a second of someone's time.

    Without this the candidate set was every step whose script mentions pytest
    anywhere under `.github/workflows` -- 26 steps and 22 inner pytest runs to
    answer about one path, growing with every pytest step the repository adds.
    """
    name = Path(path).name
    for token in script.replace(";", " ").split():
        token = token.strip("'\"")
        cleaned = token[2:] if token.startswith("./") else token
        if cleaned in (path, name, ".", "./"):
            return True
        if any(character in cleaned for character in "*?[") and (
                fnmatch(path, cleaned) or fnmatch(name, cleaned)):
            return True
        directory = cleaned.rstrip("/")
        if directory and path.startswith(f"{directory}/"):
            return True
    return False


@lru_cache(maxsize=1)
def stub_tree() -> tuple:
    """A tree of this repository's tracked path names, kept for the session.

    Built once: the names are what a glob expands against and what pytest finds
    at the end of a path argument, and all of them are empty but the recording
    stubs written per run. The repository's own `pyproject.toml` is copied over
    the empty one, because its `[tool.pytest.ini_options]` decides what a run
    collects on the runner too. `tests/conftest.py` is deliberately left empty:
    importing the real one would run this repository's fixtures inside a
    measurement of someone else's command.
    """
    holder = tempfile.TemporaryDirectory(prefix="manifest-guard-")
    SCRATCH.append(holder)                 # removed when the interpreter exits
    tree = Path(holder.name)
    problem = write_tree(tree)
    if problem:
        return None, problem              # no work tree, so no tree of its names
    (tree / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
    return tree, None


def selects_a_subset(arguments) -> list[str]:
    """The selecting options in this argv, in the order pytest would read them."""
    return sorted({argument.partition("=")[0] for argument in arguments
                   if argument.partition("=")[0] in SELECTING_OPTIONS})


def unbounded_options(arguments) -> list[str]:
    """The options in this argv that let a run reach outside the tree it is given.

    Both of pytest's spellings of `-p` are read, the separate word and the
    glued one, because it is the one option here with a value that decides the
    answer: `no:NAME` blocks a plugin and anything else imports a module. A
    long option that merely begins with `-p`, such as `--pdb`, is not it.
    """
    found = []
    for index, argument in enumerate(arguments):
        option = argument.partition("=")[0]
        if option in UNBOUNDED_OPTIONS:
            found.append(option)
        elif argument.startswith("-p") and not argument.startswith("--"):
            value = argument[2:] or (
                arguments[index + 1] if index + 1 < len(arguments) else "")
            if not value.startswith(BLOCKS_A_PLUGIN):
                found.append("-p")
    return sorted(set(found))


def outside_the_stub_tree(arguments, tree) -> list[str]:
    """Every argument of this argv that spells a path the stub tree does not hold.

    A path is what an inner run can reach, and the argv is a workflow's own
    text: an argument pytest collects is a path, the value of an option written
    with `=` can be one, and a node id's path is the part before `::`. The
    answer comes from the resolved path rather than from the spelling, so an
    absolute path, a `..` that climbs out and a symlink are one question; a
    value that is no path at all, such as the `short` of `--tb=short`, names
    something inside the tree and is left alone.
    """
    root = tree.resolve()
    found = []
    for argument in arguments:
        spelled = argument.partition("=")[2] if argument.startswith("-") else argument
        spelled = spelled.partition("::")[0]
        if not spelled:
            continue
        if not (root / spelled).resolve().is_relative_to(root):
            found.append(argument)
    return sorted(set(found))


def inner_environment(tree) -> dict:
    """The environment the inner pytest gets: built, not inherited.

    The step executor already builds one, for the same reason and against the
    same risk, and this one is built the same way: nothing of the session's own
    environment on a POSIX host, with the temporary directory inside the tree
    so that what the run writes for itself is written there too.
    """
    temporary = tree / ".tmp"
    temporary.mkdir(exist_ok=True)
    environment = {
        "PATH": os.defpath,
        "HOME": str(tree),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    environment.update({name: os.environ[name] for name in KEPT_FROM_THE_HOST
                        if name in os.environ})
    return environment


@lru_cache(maxsize=None)
def executed_files(arguments, stubs) -> Executed:
    """Which of these files the real pytest executes a test in, given this argv.

    pytest is the one asked. Each file is written with a single test that records
    having run, the arguments go to `python -m pytest` unchanged, and what comes
    back is a fact about pytest 9.1.1 rather than a model of it: `--ignore` and
    `--deselect` exclude, `-k` and `-m` select, `--collect-only` and its
    relatives collect and execute nothing, and an option that consumes its value
    consumes it, with no table of such options anywhere here.

    What pytest is handed is bounded, because the argv is a workflow's own text
    and pytest is a real program: an argv carrying one of `UNBOUNDED_OPTIONS`
    or `-p` with a module to import is not run at all, every path it spells has
    to resolve inside the stub tree, and the environment is built rather than
    inherited. Each of those was a way for a recorded line to act on the
    machine running this suite while the guard reported a pass -- measured, an
    absolute `--junitxml` wrote its report outside the tree and the file still
    counted as run -- and each of them is now the third answer rather than a
    subprocess.

    What pytest is not asked is a selection. `-k` and `-m` choose by test name
    and by marker, which are properties of the real file's contents, and the
    stub holds one test called `test_executed` and no marks -- so the answer
    would be about the stub. It is reported as unmeasured instead, which is the
    stub-fidelity limit of this module stated in one place: a path reaches
    pytest as itself, and everything inside the file is a name this tree does
    not have.

    A run that did not answer is a measurement this guard could not make and is
    reported as that: an argument naming a path no checkout holds, an option
    from a plugin this interpreter lacks, or a node id whose class an empty stub
    does not hold -- `workspace-bindings.yml` commits one, and pytest refuses
    the whole command over it. Reading any of those as a file that did not run
    would turn "could not ask" into "no", which is the distinction #632 insisted
    on. Which exit codes answer is the list above, not the one number a usage
    error happens to carry in this release.
    """
    selecting = selects_a_subset(arguments)
    if selecting:
        return Executed(frozenset(), (
            "`" + " ".join(arguments) + "` selects a subset with "
            + ", ".join(f"`{option}`" for option in selecting)
            + ", which is a question about the real file's test names and markers; "
            "this guard's stub shares only its path, so what ran is unknown"))
    unbounded = unbounded_options(arguments)
    if unbounded:
        return Executed(frozenset(), (
            "`" + " ".join(arguments) + "` carries "
            + ", ".join(f"`{option}`" for option in unbounded)
            + ", which lets a run delete, write, configure or import outside the "
            "tree it is given, so this guard does not hand it to pytest"))
    tree, problem = stub_tree()
    if problem:
        return Executed(frozenset(), problem)
    outside = outside_the_stub_tree(arguments, tree)
    if outside:
        return Executed(frozenset(), (
            "`" + " ".join(arguments) + "` names "
            + ", ".join(f"`{argument}`" for argument in outside)
            + ", which is not a path inside the tree this guard gives pytest, so "
            "it does not hand it to pytest"))
    log = tree / RECORD
    log.unlink(missing_ok=True)
    for name in stubs:
        stub = tree / name
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(
            RECORDING_TEST.format(log=repr(str(log)), name=repr(name)), encoding="utf-8")
    try:
        finished = subprocess.run(
            [sys.executable, "-m", "pytest", *arguments],
            cwd=tree, env=inner_environment(tree),
            capture_output=True, text=True, timeout=PYTEST_TIMEOUT,
        )
    finally:
        for name in stubs:
            if name in tracked_names()[0]:
                (tree / name).write_text("", encoding="utf-8")
            else:
                (tree / name).unlink()
    executed = log.read_text(encoding="utf-8").split() if log.exists() else []
    if finished.returncode not in ANSWERING_EXIT_CODES and not executed:
        output = (finished.stdout + finished.stderr).strip().splitlines()
        return Executed(frozenset(), (
            "pytest refused `" + " ".join(arguments) + "` and exited "
            f"{finished.returncode}, so what it would have run is unknown: "
            + (output[-1] if output else "no output")))
    return Executed(frozenset(executed), None)


def invocation(path: str, documents: dict) -> Invocation:
    """The workflows that really hand `path` to pytest, and what went unmeasured.

    Two stages, each run by the program that owns the contract it reads: the step
    is executed under the recording shims, which is the only way a mention, a
    here-document, an `exit 0` or a stub the step writes itself can be told from
    an invocation; then each recorded argv is handed to the real pytest, which is
    the only thing that knows which of its arguments select a file. The reading
    left between them is which recorded program was started, by the same
    `pytest_invocation` the #632 guard uses on the same log lines.
    """
    hosts, problems, refusals, measured = [], [], [], 0
    for name, document in documents.items():
        for job_name, job, step in pytest_candidates(document):
            # The narrowing: a step that names no spelling of this path is not
            # executed, which is where this module's cost used to come from.
            if not mentions_the_file(step["run"], path):
                continue
            skipped = skip_reasons(document, job_name, step)
            if skipped:
                problems.append(
                    f"{name} ({job_name}): a run may not reach this step -- "
                    + ", ".join(skipped))
                continue
            run = recorded_step(
                step["run"],
                effective_shell(document, job, step)[0],
                tuple(sorted(step_environment(document, job, step).items())),
                run_default(document, job, step, "working-directory")[0],
            )
            if run.problem:
                where = f"{name} ({job_name}): {run.problem}"
                (refusals if run.refused else problems).append(where)
                continue
            ran, answered, unmeasured = False, 0, []
            for command in run.commands:
                arguments = pytest_invocation(command)
                if arguments is None:
                    continue               # pip, or an interpreter running something else
                answer = executed_files(
                    tuple(arguments), RECORDED_FILES | frozenset({path}))
                if answer.problem:
                    unmeasured.append(f"{name} ({job_name}): {answer.problem}")
                    continue
                answered += 1
                if path in answer.files:
                    ran = True
                    break
            if ran:
                hosts.append(name)
                break
            # A step that was executed and recorded no pytest at all is a
            # measurement -- it ran nothing -- and so is one whose argv pytest
            # answered. A step whose every recorded argv pytest refused is not.
            if answered or not unmeasured:
                measured += 1
            problems.extend(unmeasured)
    return Invocation(hosts, problems, measured, refusals)


def require_a_host(path: str, run: Invocation) -> None:
    """Pass, skip or fail on what the candidate steps answered about this path.

    The same four answers the #632 harness makes: a step that was executed and
    whose argv pytest did answer, with the file not among what ran, is the
    failure this module exists for; a step the recording harness will not
    execute is a verdict about the step and fails too, because a workflow edit
    that reaches outside what the harness bounds has to break the build rather
    than quietly stop measuring; and a candidate set where every answer was a
    problem is a measurement that was not made, where an unusable runner -- no
    bash, a `noexec` temp filesystem, a shim the kernel refuses, a 120 s timeout
    on a contended runner, an argv pytest rejects or one this guard does not
    hand it -- must not read as a bypass.
    """
    unresolved = [*run.problems, *run.refusals]
    if run.hosts:
        if unresolved:
            warnings.warn(
                f"{len(unresolved)} candidate(s) for {path} could not be "
                "measured; another workflow answered instead: "
                + "; ".join(unresolved),
                UnmeasuredCandidate, stacklevel=2)
        return
    if run.refusals:
        pytest.fail(
            f"a step that names {path} is one this guard will not execute, so "
            "whether a run really hands the file to pytest was never measured: "
            + "; ".join(run.refusals))
    if run.problems and not run.measured:
        pytest.skip(
            f"this guard could not measure whether a workflow runs {path}: "
            + "; ".join(run.problems))
    pytest.fail(
        f"no workflow's run command handed {path} to pytest when executed. "
        + ("; ".join(run.problems) if run.problems else "No step names it at all."))


def invoked_by(path: str, documents: dict) -> list[str]:
    """The workflows whose own pytest invocations really run `path`."""
    return invocation(path, documents).hosts


def step(command):
    """A one-step workflow document that runs this script."""
    return {"jobs": {"tests": {"steps": [{"run": command}]}}}


DOCUMENTS = {path.name: load(path) for path in WORKFLOWS}
NAME_IT = ("a `paths:` glob is not cover: the entry has to name the file, "
           "beside the glob if the glob is wanted too")


def test_the_manifests_and_workflows_are_found():
    assert DOCUMENTS and INSTALLERS and INSTALLER_TESTS


def test_every_core_dependency_is_installable_from_requirements():
    """A core dependency missing here is missing from every installation's venv."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = {normalise(NAME.match(entry).group(1))
            for entry in declared["project"]["dependencies"]}
    installed = requirement_names((ROOT / "requirements.txt").read_text(encoding="utf-8"))
    missing = sorted(core - installed)
    assert not missing, (
        "pyproject.toml lists these as runtime dependencies while requirements.txt, "
        f"the only manifest an installation installs, omits them: {missing}")
    # The reverse is deliberate: requirements.txt also carries pytest and
    # pytest-asyncio, which the package declares under its extras instead.


def test_this_guard_is_itself_a_workflow_trigger():
    """A change to it has to start something; reading that needs no execution."""
    wanted = f"tests/{Path(__file__).name}"
    assert triggered_by(wanted, DOCUMENTS), (
        f"no workflow's `paths:` names {wanted}; {NAME_IT}")


@EXECUTES_STEPS
def test_this_guard_is_itself_run_by_the_workflow_its_change_triggers():
    """A guard no workflow runs is a guard that holds only on the author's machine.

    One workflow has to do both. Asserted separately, a `paths:` entry in one
    workflow and a pytest command in another satisfied the pair while no run
    ever both started on a change to this file and executed it (#555, item 5),
    so what is asserted is the intersection.
    """
    wanted = f"tests/{Path(__file__).name}"
    run = invocation(wanted, DOCUMENTS)
    require_a_host(wanted, run)
    both = sorted(set(triggered_by(wanted, DOCUMENTS)) & set(run.hosts))
    assert both, (
        f"the workflows a change to {wanted} starts and the workflows that run it "
        f"are disjoint: triggered by {sorted(triggered_by(wanted, DOCUMENTS))}, run "
        f"by {sorted(run.hosts)}, so no single run both starts on a change to it "
        "and executes it")


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda path: path.name)
def test_every_installer_script_is_a_workflow_trigger(installer):
    """An installer no `paths:` names can change without any workflow running."""
    wanted = f"scripts/{installer.name}"
    assert triggered_by(wanted, DOCUMENTS), (
        f"no workflow's `paths:` names {wanted}; {NAME_IT}")


@pytest.mark.parametrize("test", INSTALLER_TESTS, ids=lambda path: path.name)
@EXECUTES_STEPS
def test_every_installer_test_is_run_by_a_workflow(test):
    """A test file no workflow invokes proves nothing about the installer it covers."""
    wanted = f"tests/{test.name}"
    require_a_host(wanted, invocation(wanted, DOCUMENTS))


def test_an_ignored_path_is_not_a_trigger():
    """`paths-ignore` suppresses a run, so naming an installer there is not cover."""
    ignoring = {"on": {"pull_request": {"paths-ignore": ["scripts/install_x.py"]}}}
    triggering = {"on": {"pull_request": {"paths": ["scripts/install_x.py"]}}}
    assert trigger_paths(ignoring) == set()
    assert trigger_paths(triggering) == {"scripts/install_x.py"}


@EXECUTES_STEPS
def test_a_path_entry_is_not_a_run_command():
    """A test named under `paths:` is one that starts the job, not one it runs."""
    wanted = "tests/test_install_setup_options.py"
    neighbour = "tests/test_installer_profiles.py"
    documents = {
        "listed-only.yml": {
            "on": {"pull_request": {"paths": [wanted]}},
            "jobs": {"tests": {"steps": [{"run": f"pytest {neighbour}"}]}}},
        "invoking.yml": {
            "jobs": {"tests": {"steps": [{"uses": "actions/checkout@v7"},
                                         {"run": f"pytest {wanted} -q"}]}}}}
    assert invoked_by(wanted, documents) == ["invoking.yml"]
    assert triggered_by(wanted, documents) == ["listed-only.yml"]


MATCHING_GLOBS = (
    "scripts/install_*.py",
    "scripts/*",
    "scripts/**",
    "**/install_x.py",
)


def test_a_glob_under_paths_is_not_cover_for_a_file_it_matches():
    """#555, first gap: no `paths:` glob is interpreted, so none can be misread.

    The four entries below all cover `scripts/install_x.py` as GitHub reads
    them, and a translation of that syntax had to agree with GitHub about each.
    The literal rule gives them one answer instead -- a glob is not cover, name
    the file -- and the answer no longer depends on the spelling, which is what
    the gap was: `**/` matches zero directories at GitHub and the translation
    kept its slash mandatory, so a root-level file was rejected while
    `scripts/install_*.py` was accepted.
    """
    for entry in MATCHING_GLOBS:
        documents = {"w.yml": {"on": {"push": {"paths": [entry]}}}}
        assert triggered_by("scripts/install_x.py", documents) == [], entry
    # The bullet's own case, whose answer the literal rule does not change: it
    # is no longer an answer about `**/` at all.
    documents = {"w.yml": {"on": {"push": {"paths": ["**/install_x.py"]}}}}
    assert triggered_by("install_x.py", documents) == []
    # Named verbatim, it is cover, or the rule above would hold vacuously.
    documents = {"w.yml": {"on": {"push": {"paths": ["scripts/install_x.py"]}}}}
    assert triggered_by("scripts/install_x.py", documents) == ["w.yml"]
    assert "glob_regex" not in globals(), "a glob translation is back in this module"


@EXECUTES_STEPS
def test_a_mention_of_pytest_is_not_a_run_because_the_step_is_executed():
    """#555, second gap: only an invocation reaches the recorder, a word does not.

    `echo pytest <file>` names both and runs neither, and a reader that took any
    token called pytest for the command answered that the file ran. Executing
    the step answers it instead: `echo` is not the recording shim, so nothing is
    recorded, and the same holds for a command the shell never reaches.
    """
    wanted = "tests/test_install_setup_options.py"
    assert invoked_by(wanted, {"w.yml": step(f"echo pytest {wanted}")}) == []
    assert invoked_by(wanted, {"w.yml": step(f"exit 0\npytest {wanted}")}) == []
    assert invoked_by(wanted, {"w.yml": step(f"pytest {wanted}")}) == ["w.yml"]


@EXECUTES_STEPS
def test_an_option_s_value_is_not_read_as_a_test_path():
    """#555, third gap: pytest decides what its arguments select, not this file.

    `--ignore <file>` consumes the file as the option's value, and a reader that
    dropped the option but kept the value credited the file pytest was told to
    exclude. The arguments now go to the real pytest, so the only list of
    value-taking options involved is pytest's own.
    """
    wanted = "tests/test_install_setup_options.py"
    neighbour = "tests/test_installer_profiles.py"
    assert invoked_by(wanted, {"w.yml": step(f"pytest --ignore {wanted} {neighbour}")}) == []
    assert invoked_by(wanted, {"w.yml": step(f"pytest -p no:cacheprovider {wanted}")}) == ["w.yml"]


@EXECUTES_STEPS
def test_a_real_invocation_counts_however_it_is_spelt():
    """The guard has to pass the commands this repository really writes.

    A guard of this kind fails by blocking correct work as readily as by missing
    a bypass, so each form below is one a workflow here commits or `tests/README.md`
    prescribes, and each is executed rather than recognised: the shell expands the
    glob against real names, `python -B -m pytest` reaches pytest through the
    interpreter, a leading assignment is the documented form, and `&&` runs what
    follows it.

    `pytest -k install <file>` used to be here, asserted to count. It is neither
    refused nor counted now: the stub cannot answer a selection, so the case
    moved to `test_an_argv_that_selects_a_subset_is_a_measurement_this_guard_cannot_make`
    below, where it is unmeasured rather than decided.
    """
    wanted = "tests/test_install_setup_options.py"
    for command in (
        "pytest tests/test_install_*.py",
        f"python -B -m pytest {wanted} -q -p no:cacheprovider",
        f"LANGFUSE_TRACING_ENABLED=false pytest {wanted}",
        f"ls tests > /dev/null && pytest {wanted}",
    ):
        assert invoked_by(wanted, {"w.yml": step(command)}) == ["w.yml"], command


@EXECUTES_STEPS
def test_a_command_that_only_collects_is_not_a_run():
    """Collection is not execution, and pytest is what says which is which.

    The set of modes that collect or describe instead of running used to live
    here, measured once against pytest 9.1.1 and then fixed in the file while
    pytest went on evolving. Each mode below is measured on every run now, by the
    pytest that is installed, so a spelling it drops or adds shows up as this
    test rather than as a guard that quietly believes a file ran.
    """
    wanted = "tests/test_install_setup_options.py"
    for mode in NON_RUNNING_MODES:
        assert invoked_by(wanted, {"w.yml": step(f"pytest {mode} {wanted}")}) == [], mode
    # An option that shows more while still running is a run, and so is the file
    # beside a whole-file `--deselect` of another one.
    assert invoked_by(wanted, {"w.yml": step(f"pytest --setup-show {wanted}")}) == ["w.yml"]
    assert invoked_by(wanted, {"w.yml": step(f"pytest --deselect {wanted} {wanted}")}) == []


@EXECUTES_STEPS
def test_a_measurement_this_guard_cannot_make_is_not_a_no():
    """A step that was never executed, and an argv pytest refused, say so.

    Both are the distinction #632 insisted on: "could not ask" and "ran
    nothing" must not be allowed to look alike, because the first one silently
    passing as the second is how a guard stops guarding. They are not the same
    answer as each other either. An absolute interpreter path is a step the
    recording harness refuses, which is a verdict about the step and fails; an
    option no installed plugin provides is pytest refusing the command, which
    nobody can do anything about from here and reads as unmeasured.
    """
    wanted = "tests/test_install_setup_options.py"
    absolute = invocation(wanted, {"w.yml": step(f"/usr/bin/pytest {wanted}")})
    assert absolute.hosts == []
    assert absolute.problems == []
    assert absolute.refusals and "was not executed" in absolute.refusals[0]
    refused = invocation(wanted, {"w.yml": step(f"pytest --no-such-option {wanted}")})
    assert refused.hosts == []
    assert refused.refusals == []
    assert refused.problems and "pytest refused" in refused.problems[0]


@EXECUTES_STEPS
def test_a_candidate_set_whose_every_answer_is_a_problem_is_unmeasured():
    """An unusable runner reads as unmeasured rather than as a bypass.

    Both documents below are candidates that name the file and neither can be
    measured: the first hands pytest an option no installed plugin provides, so
    pytest refuses the whole command, and the second carries `--junitxml`, which
    writes wherever it is told and is therefore an argv this guard does not hand
    to pytest at all. The guard used to fail on exactly this, which is what a
    runner without bash, with a `noexec` temp filesystem or with a contended
    120 s timeout looks like.
    """
    wanted = "tests/test_install_setup_options.py"
    run = invocation(wanted, {
        "refused.yml": step(f"pytest --no-such-option {wanted}"),
        "unbounded.yml": step(f"pytest --junitxml=report.xml {wanted}"),
    })
    assert run.hosts == []
    assert run.measured == 0, "neither of these could be measured"
    assert len(run.problems) == 2, run.problems
    with pytest.raises(pytest.skip.Exception, match="could not measure"):
        require_a_host(wanted, run)
    # And the bypass it must still catch: a step that really was executed and
    # really ran nothing fails, because that measurement was made.
    ran_nothing = invocation(wanted, {"w.yml": step(f"echo pytest {wanted}")})
    assert ran_nothing.measured >= 1 and ran_nothing.hosts == []
    with pytest.raises(pytest.fail.Exception, match="handed"):
        require_a_host(wanted, ran_nothing)


@EXECUTES_STEPS
def test_a_candidate_step_a_run_never_reaches_is_not_a_host():
    """The same condition on this half, which answered with the workflow's name.

    `pytest_candidates` reads no `if:`, so a lone `if: ${{ false }}` step was
    executed and credited; the reader the other module has always used for the
    calendar case now runs over these candidates too.
    """
    wanted = "tests/test_install_setup_options.py"
    gated = {"jobs": {"tests": {"steps": [
        {"run": f"pytest {wanted}", "if": "${{ false }}"}]}}}
    run = invocation(wanted, {"gated.yml": gated})
    assert run.hosts == []
    assert run.measured == 0
    assert any("may not reach this step" in problem for problem in run.problems), (
        run.problems)
    assert invoked_by(wanted, {"open.yml": step(f"pytest {wanted}")}) == ["open.yml"]


@EXECUTES_STEPS
def test_an_argv_that_selects_a_subset_is_a_measurement_this_guard_cannot_make():
    """`-k` and `-m` ask about the real file's test names and markers.

    The stub shares only the path with the file it stands for: it holds one test
    called `test_executed` and no marks. So a selection answers about the stub,
    and it was measured answering wrongly in both directions -- a module-level
    `slow` mark on `tests/test_install_review_config.py` makes the committed
    command really collect 86 tests and run none under this repository's
    `addopts`, while the unmarked stub ran and the guard passed; and `-k
    detection` on that command really runs 4 tests while the guard reported the
    file as not run. Both are now the third answer, which is the one this module
    already has for a question it cannot put to pytest.
    """
    wanted = "tests/test_install_setup_options.py"
    for command in (
        f"pytest -k install {wanted}",
        f"pytest -m 'not slow' {wanted}",
        f"pytest {wanted} -k detection",
        f"pytest {wanted} -m slow -q",
    ):
        run = invocation(wanted, {"w.yml": step(command)})
        assert run.hosts == [], command
        assert run.measured == 0, command
        assert any("selects a subset" in problem for problem in run.problems), (
            command, run.problems)
    # The same command without the selector is measured, or the cases above say
    # nothing about selection.
    assert invoked_by(wanted, {"w.yml": step(f"pytest {wanted}")}) == ["w.yml"]


@EXECUTES_STEPS
def test_the_inner_pytest_does_not_inherit_this_session_s_environment(monkeypatch):
    """The environment alone can decide what the inner run collects.

    The recorded argv went to `python -m pytest` with no `env` argument, so the
    inner run inherited whatever started the suite. `PYTEST_ADDOPTS` is enough
    on its own: measured at the head of this branch, `--collect-only` in it
    turned every answer from "this file runs" into "this file does not run",
    which is the verdict this module exists to give and the one direction that
    reads as a bypass. The inner pytest gets a built environment now, the way
    the step executor already builds one.
    """
    wanted = f"tests/{Path(__file__).name}"
    stubs = RECORDED_FILES | frozenset({wanted})
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    answer = executed_files((wanted, "-q", "--tb=line"), stubs)
    assert answer.problem is None, answer.problem
    assert wanted in answer.files, (
        "the session's own PYTEST_ADDOPTS decided what the inner pytest ran: "
        f"{sorted(answer.files)}")


@EXECUTES_STEPS
def test_an_argv_that_reaches_outside_the_stub_tree_is_never_handed_to_pytest(tmp_path):
    """A recorded argv is a workflow's text, and it went to a real program unfiltered.

    The only refusal was a `-k` or `-m` selection; everything else ran, with no
    bound on what a path argument could name. pytest removes a given
    `--basetemp` recursively before recreating it, writes `--junitxml` wherever
    it is told, reads its configuration from `-c` and `--rootdir`, sets any ini
    option from `-o`, and imports a module named by `-p`. Measured at the head
    of this branch: `--junitxml` wrote a report outside the stub tree and the
    guard reported the file as run. Such an argv is a measurement this guard
    does not make, which is the answer it already has for a question it cannot
    put to pytest.
    """
    wanted = f"tests/{Path(__file__).name}"
    stubs = RECORDED_FILES | frozenset({wanted})
    report = tmp_path / "escaped-junit.xml"
    for arguments in (
        (f"--junitxml={report}", wanted),
        (f"--basetemp={tmp_path / 'basetemp'}", wanted),
        ("--rootdir=/", wanted),
        ("-c", "/dev/null", wanted),
        ("-o", "addopts=--collect-only", wanted),
        ("-p", "tests.ci_shard", wanted),
        ("-pno_such_plugin", wanted),
        ("--ignore=../..", wanted),
        ("/etc", wanted),
        (wanted, "../.."),
    ):
        answer = executed_files(arguments, stubs)
        assert answer.files == frozenset(), arguments
        assert answer.problem, arguments
    assert not report.exists(), f"the inner pytest wrote {report}, outside the stub tree"
    # The spelling every committed step writes stays measurable, or this refuses
    # the repository's own wiring: `-p no:NAME` blocks a plugin and imports nothing.
    measured = executed_files((wanted, "-q", "-p", "no:cacheprovider"), stubs)
    assert measured.problem is None, measured.problem
    assert wanted in measured.files, sorted(measured.files)


@EXECUTES_STEPS
def test_a_step_this_guard_will_not_execute_fails_instead_of_skipping():
    """The skip that a maintainer's ordinary edit used to buy.

    A program the recording harness does not cover stops the script with status
    127, which arrives here as a problem, and a candidate set of nothing but
    problems skips -- so adding `grep` to a step left the assertion silent and
    the pull request green. A step the harness refuses is a verdict about the
    step and fails now; a runner that could not answer still skips.
    """
    wanted = "tests/test_install_setup_options.py"
    run = invocation(wanted, {"w.yml": step(
        f"grep -q pytest {wanted}\npytest {wanted}\n")})
    assert run.hosts == [], run.hosts
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as outcome:
        require_a_host(wanted, run)
    assert isinstance(outcome.value, pytest.fail.Exception), (
        f"a step this guard will not execute has to fail rather than skip: "
        f"{outcome.value}")


@EXECUTES_STEPS
def test_a_step_that_names_another_file_is_never_executed():
    """The cost scaled with the repository instead of with the guard.

    Every step whose script merely mentions pytest was a candidate, anywhere
    under `.github/workflows`: 26 steps and 22 inner pytest runs to answer about
    one path, so each new pytest step anywhere added a bash run and up to one
    pytest run to all three legs for ever. A step that names some other file
    cannot hand this one to pytest under any of the eight bypasses -- all eight
    hide the command, not the path -- so it is not executed at all.

    The reading that narrows is deliberately generous, because a candidate
    wrongly dropped is a bypass: a glob, a directory, a `./` spelling and the
    bare basename all count, and the file named inside a here-document counts
    too, since that is one of the forms the recorder exists to catch.
    """
    wanted = "tests/test_install_setup_options.py"
    elsewhere = {"w.yml": step("/usr/bin/pytest tests/test_other_thing.py")}
    run = invocation(wanted, elsewhere)
    assert (run.hosts, run.problems, run.measured) == ([], [], 0), (
        "a step naming another file was executed anyway")
    naming = {"w.yml": step(f"/usr/bin/pytest {wanted}")}
    assert invocation(wanted, naming).refusals, (
        "the same step naming this file has to reach the harness, or the case "
        "above is about nothing")
    for command in (
        f"pytest {wanted}",
        "pytest tests/test_install_*.py",
        f"pytest ./{wanted}",
        "pytest tests/",
        f"cat <<'PLAN'\n{wanted}\nPLAN\npytest {wanted}",
        f'pytest "{wanted}"',
    ):
        assert mentions_the_file(command, wanted), command
    assert not mentions_the_file("pytest tests/test_flows.py", wanted)
    assert not mentions_the_file("pytest tests/test_install_x.py", wanted)


@EXECUTES_STEPS
def test_a_candidate_this_guard_cannot_measure_is_reported_even_on_success():
    """A real "could not measure" was being discarded whenever anything else said yes.

    `workspace-bindings.yml` commits a node-id argv that pytest refuses with its
    usage exit code on every run, and the problem was returned while another
    workflow supplied the positive answer, so the suite went green and the gap
    was never printed once. A workflow this guard permanently cannot measure is
    worth seeing, so an unresolved problem is warned about even when the
    assertion passes.
    """
    wanted = "tests/test_install_setup_options.py"
    run = invocation(wanted, {
        "refused.yml": step(f"pytest --no-such-option {wanted}"),
        "answering.yml": step(f"pytest {wanted}"),
    })
    assert run.hosts == ["answering.yml"]
    assert run.problems and "no-such-option" in run.problems[0], run.problems
    with pytest.warns(UnmeasuredCandidate, match="no-such-option"):
        require_a_host(wanted, run)
    # Nothing to report is nothing to warn about.
    import warnings as _warnings
    with _warnings.catch_warnings():
        _warnings.simplefilter("error", UnmeasuredCandidate)
        require_a_host(wanted, invocation(wanted, {"only.yml": step(f"pytest {wanted}")}))


def test_the_pytest_contract_this_guard_reads_is_pinned_to_a_range():
    """Every verdict here is pytest's, so pytest's version is part of the guard.

    This module asks the installed pytest which of its arguments select a file
    and reads its exit code, and `requirements.txt` asked for `pytest>=7.0.0`,
    which is nine spellings and an exit code taken on trust from whatever a
    runner resolves on the day. A release that renamed `--collectonly` or moved
    the usage exit code would fail a repository that is wired correctly, and the
    failure would be a dependency bump wearing this guard's message.

    So the range has an upper bound, and the installed pytest is inside it --
    the second half matters, because a pin nothing satisfies pins nothing.
    """
    lines = [line.strip()
             for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if re.match(r"^pytest\s*[<>=!]", line.strip())]
    assert len(lines) == 1, f"one pytest requirement, not {lines}"
    [requirement] = lines
    assert ">=" in requirement and "<" in requirement, (
        f"`{requirement}` has no upper bound, so the behaviour this module "
        "measures is whatever the next release decides")
    floor = re.search(r">=\s*([\d.]+)", requirement).group(1)
    ceiling = re.search(r"<\s*([\d.]+)", requirement).group(1)
    installed = tuple(int(part) for part in pytest.__version__.split(".")[:2])
    assert _version(floor) <= installed < _version(ceiling), (
        f"pytest {pytest.__version__} is outside `{requirement}`, so the "
        "measurements below were taken against a version this repository does "
        "not install")


def _version(text: str) -> tuple:
    parts = tuple(int(part) for part in text.split(".")[:2])
    return parts if len(parts) == 2 else (parts[0], 0)


@EXECUTES_STEPS
def test_an_exit_code_this_guard_does_not_model_is_unmeasured_without_naming_a_number():
    """And the other half of the same dependency: the exit code was read as a 4.

    Which it is today -- pytest's usage error -- and the guard asserted that
    number. What it needs to know is narrower and does not move: whether the run
    answered the question. Nothing collected is an answer, the file simply did
    not run; an interruption, an internal error, a usage error or a signal are
    not, whatever number carries them.
    """
    refused = executed_files(("--no-such-option", "tests/t.py"), frozenset({"tests/t.py"}))
    assert refused.files == frozenset()
    assert refused.problem and "pytest refused" in refused.problem, refused.problem
    assert "exited" in refused.problem, (
        f"the problem has to name what pytest did: {refused.problem}")
    wanted = "tests/test_install_setup_options.py"
    collected_nothing = executed_files(
        ("-p", "no:cacheprovider", "--deselect", wanted, wanted), RECORDED_FILES)
    assert collected_nothing.problem is None, (
        "nothing collected is an answer, not a measurement that failed")
    assert wanted not in collected_nothing.files


def test_both_extensions_github_accepts_are_read(tmp_path):
    """A `.yaml` workflow was invisible to every assertion in this module.

    GitHub runs both spellings. Renaming `instruction-protocol.yml` to `.yaml`
    gave 3 failed and made the workflow invisible to every trigger and run
    assertion here, which is item 4 of #555 and was untouched. The other module
    already globs both; this one globbed `*.yml` alone, and the two now share
    one reader so they cannot drift apart again.
    """
    (tmp_path / "a.yml").write_text("", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("", encoding="utf-8")
    (tmp_path / "c.yml.bak").write_text("", encoding="utf-8")
    assert [path.name for path in workflow_paths(tmp_path)] == ["a.yml", "b.yaml"]
    assert WORKFLOWS == workflow_paths(ROOT / ".github/workflows")


@EXECUTES_STEPS
def test_every_collect_only_spelling_is_one_pytest_still_knows():
    """The loop above passes for two different reasons, and only one is the point.

    A mode pytest no longer knows is a usage error, and a usage error is `hosts
    == []` -- the same answer as a mode that collects without running. So the
    assertion that the file does not run would survive pytest renaming every one
    of these, which is the vacuity this pins: each mode has to be one pytest
    answered, `measured == 1`, and an option it does not know has to be the
    third answer instead.

    Six spellings were also dropped when the two parsers were deleted:
    `--setuponly`, `--setupplan`, `--funcargs`, `--fixtures-per-test`, `-h` and
    `-V`. Four of them were added deliberately for pytest's de-hyphenated
    aliases, and each costs one cached inner run.
    """
    wanted = "tests/test_install_setup_options.py"
    for mode in NON_RUNNING_MODES:
        run = invocation(wanted, {"w.yml": step(f"pytest {mode} {wanted}")})
        assert run.hosts == [], mode
        assert run.measured == 1, (mode, run.problems)
    bogus = invocation(wanted, {"w.yml": step(f"pytest --collect-onlyy {wanted}")})
    assert (bogus.hosts, bogus.measured) == ([], 0), (
        "an option pytest does not know has to be unmeasured, or every mode "
        "above could be a typo and this loop would not notice")
    assert bogus.problems
