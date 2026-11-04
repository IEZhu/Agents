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
    triggers = document.get("on") or {}
    paths = set()
    if isinstance(triggers, dict):
        for event in triggers.values():
            if isinstance(event, dict):
                for key in ("paths", "paths-ignore"):
                    paths.update(event.get(key) or [])
    return paths


def test_the_manifests_and_workflows_are_found():
    assert WORKFLOWS and INSTALLERS and INSTALLER_TESTS


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


@pytest.mark.parametrize("installer", INSTALLERS, ids=lambda path: path.name)
def test_every_installer_script_is_a_workflow_trigger(installer):
    """An installer no `paths:` names can change without any workflow running."""
    wanted = f"scripts/{installer.name}"
    triggering = [path.name for path in WORKFLOWS
                  if any(entry == wanted or entry.rstrip("*").rstrip("/") and
                         wanted.startswith(entry.rstrip("*"))
                         for entry in trigger_paths(load(path)))]
    assert triggering, f"no workflow is triggered by a change to {wanted}"


@pytest.mark.parametrize("test", INSTALLER_TESTS, ids=lambda path: path.name)
def test_every_installer_test_is_run_by_a_workflow(test):
    """A test file no workflow invokes proves nothing about the installer it covers."""
    wanted = f"tests/{test.name}"
    running = [path.name for path in WORKFLOWS
               if wanted in path.read_text(encoding="utf-8")]
    assert running, f"no workflow runs {wanted}"
