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
pytest, so this module starts subprocesses where it used to split strings. A
step this guard cannot execute, and an argv pytest refuses, are reported as
measurements that could not be made -- never as a file that does not run.
"""

from collections import namedtuple
from functools import lru_cache
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib

import pytest
import yaml

from tests.test_content_checks_workflow import (  # the #632 harness, not a second one
    effective_shell,
    execute_step,
    pytest_invocation,
    run_default,
    step_environment,
    tracked_names,
    write_tree,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))
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
# pytest's own exit code for a command it refuses, including a path no checkout
# of this repository holds and an option no installed plugin provides.
USAGE_ERROR = 4
PYTEST_TIMEOUT = 120
# `files` is what really executed, `problem` why pytest could not be asked.
Executed = namedtuple("Executed", "files problem")
# `hosts` the workflows that really run the file; `problems` the steps and
# commands this guard could not measure, which is a third answer and not a no.
Invocation = namedtuple("Invocation", "hosts problems")
SCRATCH = []


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


@lru_cache(maxsize=None)
def recorded_step(script, shell, environment, working_directory):
    """What the recording shims saw when this step's script was executed.

    `execute_step` is the harness of `tests/test_content_checks_workflow.py`: the
    script goes to a file and the file to the shell GitHub documents for it, in a
    throwaway tree of tracked path names, with every program that could be pytest
    replaced by a shim that records its argv. Cached because one step answers for
    every file this module asks about and executing it is the expensive half.
    """
    return execute_step(script, shell=shell, env=dict(environment),
                        working_directory=working_directory)


@lru_cache(maxsize=1)
def stub_tree() -> Path:
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
    write_tree(tree)
    (tree / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
    return tree


@lru_cache(maxsize=None)
def executed_files(arguments, stubs) -> Executed:
    """Which of these files the real pytest executes a test in, given this argv.

    pytest is the one asked. Each file is written with a single test that records
    having run, the arguments go to `python -m pytest` unchanged, and what comes
    back is a fact about pytest 9.1.1 rather than a model of it: `--ignore` and
    `--deselect` exclude, `-k` and `-m` select, `--collect-only` and its
    relatives collect and execute nothing, and an option that consumes its value
    consumes it, with no table of such options anywhere here.

    A usage error is a measurement this guard could not make and is reported as
    that: an argument naming a path no checkout holds, an option from a plugin
    this interpreter lacks, or a node id whose class an empty stub does not hold
    -- `workspace-bindings.yml` commits one, and pytest refuses the whole command
    over it. Reading any of those as a file that did not run would turn "could
    not ask" into "no", which is the distinction #632 insisted on.
    """
    tree = stub_tree()
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
            cwd=tree, capture_output=True, text=True, timeout=PYTEST_TIMEOUT,
        )
    finally:
        for name in stubs:
            if name in tracked_names():
                (tree / name).write_text("", encoding="utf-8")
            else:
                (tree / name).unlink()
    executed = log.read_text(encoding="utf-8").split() if log.exists() else []
    if finished.returncode == USAGE_ERROR and not executed:
        output = (finished.stdout + finished.stderr).strip().splitlines()
        return Executed(frozenset(), (
            "pytest refused `" + " ".join(arguments) + "`, so what it would have run "
            "is unknown: " + (output[-1] if output else "no output")))
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
    hosts, problems = [], []
    for name, document in documents.items():
        for job_name, job, step in pytest_candidates(document):
            run = recorded_step(
                step["run"],
                effective_shell(document, job, step)[0],
                tuple(sorted(step_environment(document, job, step).items())),
                run_default(document, job, step, "working-directory")[0],
            )
            if run.problem:
                problems.append(f"{name} ({job_name}): {run.problem}")
                continue
            ran, unmeasured = False, []
            for command in run.commands:
                arguments = pytest_invocation(command)
                if arguments is None:
                    continue               # pip, or an interpreter running something else
                measured = executed_files(
                    tuple(arguments), RECORDED_FILES | frozenset({path}))
                if path in measured.files:
                    ran = True
                    break
                if measured.problem:
                    unmeasured.append(f"{name} ({job_name}): {measured.problem}")
            if ran:
                hosts.append(name)
                break
            problems.extend(unmeasured)
    return Invocation(hosts, problems)


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


def test_this_guard_is_itself_triggered_and_run():
    """A guard no workflow runs is a guard that holds only on the author's machine."""
    wanted = f"tests/{Path(__file__).name}"
    assert triggered_by(wanted, DOCUMENTS), (
        f"no workflow's `paths:` names {wanted}; {NAME_IT}")
    run = invocation(wanted, DOCUMENTS)
    assert run.hosts, (
        f"no workflow's run command handed {wanted} to pytest when executed. "
        + ("; ".join(run.problems) if run.problems else "No step names it at all."))


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda path: path.name)
def test_every_installer_script_is_a_workflow_trigger(installer):
    """An installer no `paths:` names can change without any workflow running."""
    wanted = f"scripts/{installer.name}"
    assert triggered_by(wanted, DOCUMENTS), (
        f"no workflow's `paths:` names {wanted}; {NAME_IT}")


@pytest.mark.parametrize("test", INSTALLER_TESTS, ids=lambda path: path.name)
def test_every_installer_test_is_run_by_a_workflow(test):
    """A test file no workflow invokes proves nothing about the installer it covers."""
    wanted = f"tests/{test.name}"
    run = invocation(wanted, DOCUMENTS)
    assert run.hosts, (
        f"no workflow's run command handed {wanted} to pytest when executed. "
        + ("; ".join(run.problems) if run.problems else "No step names it at all."))


def test_an_ignored_path_is_not_a_trigger():
    """`paths-ignore` suppresses a run, so naming an installer there is not cover."""
    ignoring = {"on": {"pull_request": {"paths-ignore": ["scripts/install_x.py"]}}}
    triggering = {"on": {"pull_request": {"paths": ["scripts/install_x.py"]}}}
    assert trigger_paths(ignoring) == set()
    assert trigger_paths(triggering) == {"scripts/install_x.py"}


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


def test_a_real_invocation_counts_however_it_is_spelt():
    """The guard has to pass the commands this repository really writes.

    A guard of this kind fails by blocking correct work as readily as by missing
    a bypass, so each form below is one a workflow here commits or `tests/README.md`
    prescribes, and each is executed rather than recognised: the shell expands the
    glob against real names, `python -B -m pytest` reaches pytest through the
    interpreter, a leading assignment is the documented form, and `&&` runs what
    follows it.
    """
    wanted = "tests/test_install_setup_options.py"
    for command in (
        "pytest tests/test_install_*.py",
        f"python -B -m pytest {wanted} -q -p no:cacheprovider",
        f"LANGFUSE_TRACING_ENABLED=false pytest {wanted}",
        f"ls tests > /dev/null && pytest {wanted}",
        f"pytest -k install {wanted}",
    ):
        assert invoked_by(wanted, {"w.yml": step(command)}) == ["w.yml"], command


def test_a_command_that_only_collects_is_not_a_run():
    """Collection is not execution, and pytest is what says which is which.

    The set of modes that collect or describe instead of running used to live
    here, measured once against pytest 9.1.1 and then fixed in the file while
    pytest went on evolving. Each mode below is measured on every run now, by the
    pytest that is installed, so a spelling it drops or adds shows up as this
    test rather than as a guard that quietly believes a file ran.
    """
    wanted = "tests/test_install_setup_options.py"
    for mode in ("--collect-only", "--collectonly", "--co", "--setup-only",
                 "--setup-plan", "--fixtures", "--markers", "--help", "--version"):
        assert invoked_by(wanted, {"w.yml": step(f"pytest {mode} {wanted}")}) == [], mode
    # An option that shows more while still running is a run, and so is the file
    # beside a whole-file `--deselect` of another one.
    assert invoked_by(wanted, {"w.yml": step(f"pytest --setup-show {wanted}")}) == ["w.yml"]
    assert invoked_by(wanted, {"w.yml": step(f"pytest --deselect {wanted} {wanted}")}) == []


def test_a_measurement_this_guard_cannot_make_is_not_a_no():
    """A step that was never executed, and an argv pytest refused, say so.

    Both are the distinction #632 insisted on: "could not ask" and "ran nothing"
    must not be allowed to look alike, because the first one silently passing as
    the second is how a guard stops guarding.
    """
    wanted = "tests/test_install_setup_options.py"
    absolute = invocation(wanted, {"w.yml": step(f"/usr/bin/pytest {wanted}")})
    assert absolute.hosts == []
    assert absolute.problems and "was not executed" in absolute.problems[0]
    refused = invocation(wanted, {"w.yml": step(f"pytest --no-such-option {wanted}")})
    assert refused.hosts == []
    assert refused.problems and "pytest refused" in refused.problems[0]
