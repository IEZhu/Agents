"""Only manifest changes compatible with the existing environment may activate."""
from types import SimpleNamespace

import pytest

from src.daemon.update_dependencies import DEPENDENCIES, dependency_change_reason


MANIFEST = '''[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "agents"
version = "1.0"
requires-python = ">=3.11"
dependencies = ["mcp==1.28.1", "python-dotenv"]

[project.optional-dependencies]
local-inference = ["httpx>=0.28,<0.29"]
'''


def gate(before=MANIFEST, after=MANIFEST, *, changed=("pyproject.toml",), missing=None):
    def git(*args, check=True):
        if args[0] == "diff":
            assert args[1:] == ("--name-only", "old", "target", "--", *DEPENDENCIES)
            return SimpleNamespace(stdout="\n".join(changed), returncode=0)
        assert args[0] == "show" and check is False
        revision, name = args[1].split(":", 1)
        assert name == "pyproject.toml"
        return SimpleNamespace(stdout=before if revision == "old" else after,
                               returncode=128 if revision == missing else 0)
    return dependency_change_reason(git, "old", "target")


def test_metadata_package_data_and_a_new_extra_need_no_environment_update():
    after = MANIFEST.replace('version = "1.0"', 'version = "2.0"\ndescription = "new metadata"')
    after += '''thread-status = ["claude-agent-sdk==0.2.168"]

[tool.setuptools.package-data]
"src.context_compaction" = ["cursor_ui.js"]
'''
    assert gate(after=after) is None


def test_requirement_order_and_formatting_do_not_change_the_environment():
    after = MANIFEST.replace('["setuptools>=68", "wheel"]', '["wheel", "setuptools>=68"]')
    after = after.replace('["mcp==1.28.1", "python-dotenv"]', '["python-dotenv", "mcp==1.28.1"]')
    assert gate(after=after) is None
    assert gate(changed=()) is None


@pytest.mark.parametrize("before, after", [
    (MANIFEST, MANIFEST.replace("mcp==1.28.1", "mcp==2")),
    (MANIFEST, MANIFEST.replace('dependencies = ["mcp==1.28.1", "python-dotenv"]',
                                'dependencies = ["mcp==1.28.1", "python-dotenv", "new-package"]')),
    (MANIFEST, MANIFEST.replace('"python-dotenv"', '"python-dotenv>=2"')),
    (MANIFEST, MANIFEST.replace('requires-python = ">=3.11"', 'requires-python = ">=3.12"')),
    (MANIFEST, MANIFEST.replace("setuptools>=68", "setuptools>=80")),
    (MANIFEST, MANIFEST.replace("setuptools.build_meta", "other.backend")),
    (MANIFEST, MANIFEST.replace("httpx>=0.28,<0.29", "httpx>=1")),
    (MANIFEST, MANIFEST.replace('local-inference = ["httpx>=0.28,<0.29"]', "")),
    (MANIFEST, MANIFEST.replace('version = "1.0"', 'version = "1.0"\ndynamic = ["dependencies"]')),
])
def test_real_environment_changes_require_explicit_maintenance(before, after):
    reason = gate(before, after)
    assert "Dependency manifests changed: pyproject.toml" in reason
    assert "explicit maintenance" in reason


@pytest.mark.parametrize("malformed", [
    "not valid TOML !", "[project]\nname = 'x'\n",
    MANIFEST.replace('["mcp==1.28.1", "python-dotenv"]', "42"),
    MANIFEST.replace('["httpx>=0.28,<0.29"]', "[42]"),
    MANIFEST.replace('["setuptools>=68", "wheel"]', '"setuptools"'),
])
@pytest.mark.parametrize("revision", ["old", "target"])
def test_malformed_manifest_on_either_side_fails_closed(malformed, revision):
    reason = gate(malformed, MANIFEST) if revision == "old" else gate(MANIFEST, malformed)
    assert "cannot verify environment compatibility" in reason


@pytest.mark.parametrize("revision", ["old", "target"])
def test_added_or_removed_manifest_fails_closed(revision):
    assert "must exist in both revisions" in gate(missing=revision)


@pytest.mark.parametrize("path", [name for name in DEPENDENCIES if name != "pyproject.toml"])
def test_other_dependency_manifests_always_require_maintenance(path):
    assert path in gate(changed=(path,))
    assert "explicit maintenance" in gate(changed=("pyproject.toml", path))
