"""GitHub workflows grant no write access at the top level, and only the
issue-agent dispatch job reads the routine token, from the environment that
only the default branch may use (docs/autodev-plan.md, phase 0)."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))


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
def test_routine_token_is_read_only_inside_the_issue_agent_environment(path):
    for name, job in load(path).get("jobs", {}).items():
        if "CLAUDE_ROUTINE_TOKEN" in yaml.safe_dump(job):
            assert job.get("environment") == "issue-agent", f"{path.name}: job {name}"
