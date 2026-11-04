"""The two manifests that decide what a fresh installation gets and what CI checks.

`requirements.txt` is the only thing `python -m venv` plus `pip install -r` puts in
an installation's environment, so a runtime dependency that reaches only
`pyproject.toml` is absent wherever the package itself was not installed. The
workflow triggers decide which changes are tested at all, so an installer that no
`paths:` list names ships without any run of its own tests.
"""

from pathlib import Path
import re
import tomllib

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))
INSTALLERS = sorted((ROOT / "scripts").glob("install_*.py"))
INSTALLER_TESTS = sorted((ROOT / "tests").glob("test_install_*.py"))
NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


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


def triggered_by(path: str, documents: dict) -> list[str]:
    """The workflows a change to `path` starts, by their `paths:` lists alone."""
    started = []
    for name, document in documents.items():
        for entry in trigger_paths(document):
            prefix = entry.rstrip("*")
            if entry == path or (prefix.endswith("/") and path.startswith(prefix)):
                started.append(name)
                break
    return started


def invoked_by(path: str, documents: dict) -> list[str]:
    """The workflows whose own run commands execute `path`."""
    return [name for name, document in documents.items()
            if path in run_commands(document)]


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


def test_an_ignored_path_is_not_a_trigger():
    """`paths-ignore` suppresses a run, so naming an installer there is not cover."""
    ignoring = {"on": {"pull_request": {"paths-ignore": ["scripts/install_x.py"]}}}
    triggering = {"on": {"pull_request": {"paths": ["scripts/install_x.py"]}}}
    assert trigger_paths(ignoring) == set()
    assert trigger_paths(triggering) == {"scripts/install_x.py"}


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


def test_a_directory_glob_triggers_the_paths_below_it():
    """`scripts/templates/**` covers a file under it and nothing beside it."""
    documents = {"w.yml": {"on": {"push": {"paths": ["scripts/templates/**"]}}}}
    assert triggered_by("scripts/templates/review/base.yaml", documents) == ["w.yml"]
    assert triggered_by("scripts/install_x.py", documents) == []


@pytest.mark.parametrize("test", INSTALLER_TESTS, ids=lambda path: path.name)
def test_every_installer_test_is_run_by_a_workflow(test):
    """A test file no workflow invokes proves nothing about the installer it covers."""
    wanted = f"tests/{test.name}"
    assert invoked_by(wanted, DOCUMENTS), f"no workflow's run command invokes {wanted}"
