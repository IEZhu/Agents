"""GitHub workflows grant no write access at the top level, and only the
issue-agent dispatch job reads the routine token, from the environment that
only the default branch may use (docs/autodev-plan.md, phase 0)."""

from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))
ENVIRONMENT = "issue-agent"
# Any of these hands the routine token, or every secret, to the code that runs.
SECRET_EXPOSURE = re.compile(r"CLAUDE_ROUTINE_TOKEN|toJSON\(\s*secrets\s*\)|secrets:\s*inherit", re.IGNORECASE)


def load(path):
    # BaseLoader keeps GitHub's `on` key instead of turning it into True.
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def test_workflows_are_found():
    assert WORKFLOWS


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_top_level_permissions_are_declared_and_read_only(path):
    permissions = load(path).get("permissions")
    assert permissions is not None, f"{path.name} must declare top-level permissions"
    assert isinstance(permissions, dict), f"{path.name} must list permissions, not {permissions!r}"
    assert "write" not in permissions.values(), f"{path.name} grants write access to every job"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_secrets_reach_only_jobs_in_the_issue_agent_environment(path):
    workflow = load(path)
    # Workflow-level env and defaults are visible to every job.
    for key in ("env", "defaults"):
        assert not SECRET_EXPOSURE.search(yaml.safe_dump(workflow.get(key, {}))), f"{path.name}: top-level {key}"
    for name, job in workflow.get("jobs", {}).items():
        if SECRET_EXPOSURE.search(yaml.safe_dump(job)):
            assert job.get("environment") == ENVIRONMENT, f"{path.name}: job {name}"


@pytest.mark.parametrize("text", [
    "env:\n  T: ${{ secrets.CLAUDE_ROUTINE_TOKEN }}\n",
    "jobs:\n  j:\n    steps:\n      - run: echo '${{ toJSON(secrets) }}'\n",
    "jobs:\n  j:\n    uses: ./.github/workflows/x.yml\n    secrets: inherit\n",
])
def test_secret_exposure_pattern_catches_known_forms(text):
    assert SECRET_EXPOSURE.search(text)
