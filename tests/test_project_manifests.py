"""The two manifests that decide what a fresh installation gets and what CI checks.

`requirements.txt` is the only thing `python -m venv` plus `pip install -r` puts in
an installation's environment, so a runtime dependency that reaches only
`pyproject.toml` is absent wherever the package itself was not installed. The
workflow triggers decide which changes are tested at all, so an installer that no
`paths:` list names ships without any run of its own tests.
"""

from pathlib import Path
import re
import shlex
import tomllib

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))
INSTALLERS = sorted((ROOT / "scripts").glob("install_*.py"))
INSTALLER_TESTS = sorted((ROOT / "tests").glob("test_install_*.py"))
NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
PYTEST = re.compile(r"(^|/)pytest$")
# Options that make pytest collect, inspect or print instead of running the tests
# it selects. Measured on pytest 9.1.1: each one leaves a test body unexecuted,
# while `--setup-show` executes it and so is deliberately not here. The
# de-hyphenated spellings are pytest's own aliases and are accepted, so they
# belong here too; `--fixturespertest` is not one of them -- pytest refuses the
# command, which runs nothing either way, so it is left out rather than implied.
NON_RUNNING = frozenset({
    "--collect-only", "--collectonly", "--co",
    "--setup-only", "--setuponly", "--setup-plan", "--setupplan",
    "--fixtures", "--funcargs", "--fixtures-per-test", "--markers",
    "--help", "-h", "--version", "-V",
})


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


def run_commands(document) -> str:
    # What a job actually executes. Searching the whole file would count a test
    # that appears under `paths:` as one the workflow runs, which is the opposite
    # of what the trigger lists mean.
    commands = []
    for job in (document.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                commands.append(step["run"])
    return "\n".join(commands)


def glob_regex(pattern: str) -> re.Pattern:
    """A path glob as GitHub reads it: `*` stays inside one segment, `**` crosses.

    `fnmatch` lets `*` cross a slash, which would turn `scripts/*` into every
    descendant, and a plain prefix test rejects `scripts/install_*.py`, a shape
    sixteen of this repository's own patterns already use.
    """
    out, index = [], 0
    while index < len(pattern):
        if pattern.startswith("/**/", index):
            out.append("/(?:.*/)?")        # any number of segments, none included
            index += 4
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


def triggered_by(path: str, documents: dict) -> list[str]:
    """The workflows a change to `path` starts, by their `paths:` lists alone."""
    started = []
    for name, document in documents.items():
        for entry in trigger_paths(document):
            # Negation inverts an entry's meaning and this translation does not
            # model it. No pattern here uses one; the day one does, teach the rule
            # rather than let the guard answer wrongly.
            assert "!" not in entry, f"{name} negates in {entry!r}"
            if glob_regex(entry).match(path):
                started.append(name)
                break
    return started


def pytest_arguments(block: str) -> list[str]:
    """The arguments of each pytest invocation in a run block, and nothing else.

    A substring test counts `echo tests/test_x.py`, a shell comment and
    `tests/test_x.py.disabled`, and misses `pytest tests/test_x*.py`, which does
    run the file. Commands are taken one at a time, so an argument of a
    neighbouring command never answers for pytest, and a command that only
    collects contributes nothing: `--collect-only` imports the file and executes
    no test in it, so reading it as a run would report cover that does not exist.
    """
    arguments = []
    for segment in re.split(r"&&|\|\||;|\||\n", block):
        try:
            tokens = shlex.split(segment, comments=True)
        except ValueError:
            continue                       # an unbalanced quote is not an invocation
        for index, token in enumerate(tokens):
            if PYTEST.search(token):
                command = tokens[index + 1:]
                if NON_RUNNING.isdisjoint(command):
                    arguments.extend(command)
                break
    return [argument for argument in arguments if not argument.startswith("-")]


def invoked_by(path: str, documents: dict) -> list[str]:
    """The workflows whose own pytest invocations take `path` as an argument."""
    return [name for name, document in documents.items()
            if any(argument == path or glob_regex(argument).match(path)
                   for argument in pytest_arguments(run_commands(document)))]


DOCUMENTS = {path.name: load(path) for path in WORKFLOWS}


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
    assert triggered_by(wanted, DOCUMENTS), f"no workflow is triggered by a change to {wanted}"
    assert invoked_by(wanted, DOCUMENTS), f"no workflow's run command invokes {wanted}"


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda path: path.name)
def test_every_installer_script_is_a_workflow_trigger(installer):
    """An installer no `paths:` names can change without any workflow running."""
    wanted = f"scripts/{installer.name}"
    assert triggered_by(wanted, DOCUMENTS), f"no workflow is triggered by a change to {wanted}"


@pytest.mark.parametrize("test", INSTALLER_TESTS, ids=lambda path: path.name)
def test_every_installer_test_is_run_by_a_workflow(test):
    """A test file no workflow invokes proves nothing about the installer it covers."""
    wanted = f"tests/{test.name}"
    assert invoked_by(wanted, DOCUMENTS), f"no workflow's run command invokes {wanted}"


def test_an_ignored_path_is_not_a_trigger():
    """`paths-ignore` suppresses a run, so naming an installer there is not cover."""
    ignoring = {"on": {"pull_request": {"paths-ignore": ["scripts/install_x.py"]}}}
    triggering = {"on": {"pull_request": {"paths": ["scripts/install_x.py"]}}}
    assert trigger_paths(ignoring) == set()
    assert trigger_paths(triggering) == {"scripts/install_x.py"}


def test_a_star_stays_inside_one_segment_and_a_double_star_crosses():
    """GitHub's own glob rule, which `fnmatch` would get wrong in both directions."""
    assert glob_regex("scripts/*").match("scripts/install_x.py")
    assert not glob_regex("scripts/*").match("scripts/dev/install_x.py")
    assert glob_regex("scripts/install_*.py").match("scripts/install_x.py")
    assert not glob_regex("scripts/install_*.py").match("scripts/install_x.txt")
    assert glob_regex("**/install_x.py").match("scripts/dev/install_x.py")
    assert glob_regex("scripts/templates/**").match("scripts/templates/review/base.yaml")
    assert glob_regex("tests/test_x*.py").match("tests/test_x_and_y.py")


def test_a_directory_glob_triggers_the_paths_below_it():
    """`scripts/templates/**` covers a file under it and nothing beside it."""
    documents = {"w.yml": {"on": {"push": {"paths": ["scripts/templates/**"]}}}}
    assert triggered_by("scripts/templates/review/base.yaml", documents) == ["w.yml"]
    assert triggered_by("scripts/install_x.py", documents) == []


def test_a_path_entry_is_not_a_run_command():
    """A test named under `paths:` is one that starts the job, not one it runs."""
    documents = {
        "listed-only.yml": {
            "on": {"pull_request": {"paths": ["tests/test_install_x.py"]}},
            "jobs": {"tests": {"steps": [{"run": "pytest tests/test_other.py"}]}}},
        "invoking.yml": {
            "jobs": {"tests": {"steps": [{"uses": "actions/checkout@v7"},
                                         {"run": "pytest tests/test_install_x.py -q"}]}}}}
    assert invoked_by("tests/test_install_x.py", documents) == ["invoking.yml"]
    assert triggered_by("tests/test_install_x.py", documents) == ["listed-only.yml"]


def test_only_a_pytest_argument_counts_as_a_run():
    """Naming a file in a neighbouring command, a comment or a suffix is not a run."""
    def document(command):
        return {"jobs": {"tests": {"steps": [{"run": command}]}}}

    wanted = "tests/test_install_x.py"
    assert invoked_by(wanted, {"w.yml": document("echo tests/test_install_x.py")}) == []
    assert invoked_by(wanted, {"w.yml": document("pytest tests/test_install_x.py.disabled")}) == []
    assert invoked_by(wanted, {"w.yml": document("# pytest tests/test_install_x.py")}) == []
    assert invoked_by(wanted, {"w.yml": document("ls tests\npytest tests/test_other.py")}) == []
    # A glob that does execute the file counts, and so does a pytest behind a path
    # or a module invocation.
    assert invoked_by(wanted, {"w.yml": document("pytest tests/test_install_*.py")}) == ["w.yml"]
    assert invoked_by(wanted, {"w.yml": document("python -B -m pytest " + wanted)}) == ["w.yml"]
    assert invoked_by(wanted, {"w.yml": document(".venv/bin/pytest " + wanted)}) == ["w.yml"]
    assert invoked_by(wanted, {"w.yml": document("ls && pytest " + wanted)}) == ["w.yml"]


def test_a_command_that_only_collects_is_not_a_run():
    """Collection is not execution, so `--collect-only` is not cover for a file.

    The modes are written out here instead of read from `NON_RUNNING`, because a
    case taken from the set under test disappears with the entry it should have
    caught: dropping an alias would remove its own assertion and the test would
    still pass. The last assertion is what keeps the two lists from drifting.
    Measured on pytest 9.1.1, every mode below leaves a test body unexecuted,
    pytest's de-hyphenated aliases included, while `--setup-show` executes it
    and is deliberately absent.
    """
    def document(command):
        return {"jobs": {"tests": {"steps": [{"run": command}]}}}

    modes = ("--collect-only", "--collectonly", "--co",
             "--setup-only", "--setuponly", "--setup-plan", "--setupplan",
             "--fixtures", "--funcargs", "--fixtures-per-test", "--markers",
             "--help", "-h", "--version", "-V")
    wanted = "tests/test_install_x.py"
    for mode in modes:
        assert invoked_by(wanted, {"w.yml": document(f"pytest {mode} {wanted}")}) == [], mode
    # An option that shows more while still running is a run, and a neighbouring
    # command that only collects does not answer for the one that executes.
    assert invoked_by(wanted, {"w.yml": document(f"pytest --setup-show {wanted}")}) == ["w.yml"]
    assert invoked_by(wanted, {"w.yml": document(f"pytest --co {wanted}\npytest {wanted}")}) == ["w.yml"]
    assert set(modes) == NON_RUNNING, "the modes above and NON_RUNNING have drifted apart"
