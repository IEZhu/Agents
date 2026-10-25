"""Manifest changes the existing environment covers activate as is; environment changes get a new one."""
from types import SimpleNamespace

import pytest

from src.daemon.update_dependencies import (DEPENDENCIES, ENVIRONMENT, NONE, REFUSED, classify, inputs,
                                            requirement_lines)


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
REQUIREMENTS = "mcp==1.28.1\npython-dotenv\n"
# The target's requirements.txt by default: one more requirement line than REQUIREMENTS.
TARGET_REQUIREMENTS = REQUIREMENTS + "numpy>=1.24\n"


def fake_git(before=MANIFEST, after=MANIFEST, *, changed=("pyproject.toml",), missing=None,
             requirements=TARGET_REQUIREMENTS, installed_requirements=REQUIREMENTS):
    """Git over two revisions: *missing* names a revision without pyproject.toml, or "requirements"
    (the target's requirements.txt)."""
    def git(*args, check=True):
        if args[0] == "diff":
            assert args[1:] == ("--name-only", "old", "target", "--", *DEPENDENCIES)
            return SimpleNamespace(stdout="\n".join(changed), returncode=0)
        assert args[0] == "show" and check is False
        revision, name = args[1].split(":", 1)
        if name == "requirements.txt":
            if revision == "old":
                return SimpleNamespace(stdout=installed_requirements, returncode=0)
            return SimpleNamespace(stdout=requirements, returncode=128 if missing == "requirements" else 0)
        assert name == "pyproject.toml"
        return SimpleNamespace(stdout=before if revision == "old" else after,
                               returncode=128 if revision == missing else 0)
    return git


def gate(before=MANIFEST, after=MANIFEST, **kwargs):
    return classify(fake_git(before, after, **kwargs), "old", "target")


def test_metadata_package_data_and_a_new_extra_need_no_environment_update():
    after = MANIFEST.replace('version = "1.0"', 'version = "2.0"\ndescription = "new metadata"')
    after += '''thread-status = ["claude-agent-sdk==0.2.168"]

[tool.setuptools.package-data]
"src.context_compaction" = ["cursor_ui.js"]
'''
    assert gate(after=after).kind == NONE


def test_requirement_order_and_formatting_do_not_change_the_environment():
    after = MANIFEST.replace('["setuptools>=68", "wheel"]', '["wheel", "setuptools>=68"]')
    after = after.replace('["mcp==1.28.1", "python-dotenv"]', '["python-dotenv", "mcp==1.28.1"]')
    assert gate(after=after).kind == NONE
    assert gate(changed=()).kind == NONE


def test_a_lock_file_change_alone_needs_no_environment():
    """Installs read requirements.txt: uv.lock alone changes no installed package (#181)."""
    assert gate(changed=("uv.lock",)) == (NONE, None)
    assert gate(changed=("uv.lock", "requirements.txt")).kind == ENVIRONMENT


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
])
def test_environment_contract_changes_need_a_new_environment(before, after):
    change = gate(before, after)
    assert change.kind == ENVIRONMENT
    assert "Dependency manifests changed: pyproject.toml" in change.reason


@pytest.mark.parametrize("after, field", [
    (MANIFEST.replace("mcp==1.28.1", "mcp==2"), "project.dependencies"),
    (MANIFEST.replace('requires-python = ">=3.11"', 'requires-python = ">=3.12"'), "project.requires-python"),
    (MANIFEST.replace("httpx>=0.28,<0.29", "httpx>=1"), "project.optional-dependencies.local-inference"),
    (MANIFEST.replace('local-inference = ["httpx>=0.28,<0.29"]', ""), "project.optional-dependencies.local-inference"),
    (MANIFEST.replace("setuptools.build_meta", "other.backend"), "build-system"),
])
def test_the_reason_names_the_changed_field(after, field):
    """#364: the reason says which dependency field changed, not only which manifest."""
    assert gate(after=after).reason.endswith("; changed: " + field)


def test_a_requirements_change_needs_a_new_environment():
    change = gate(changed=("requirements.txt",))
    assert change.kind == ENVIRONMENT and change.reason.endswith("; changed: requirements.txt")
    both = gate(after=MANIFEST.replace("mcp==1.28.1", "mcp==2"), changed=("pyproject.toml", "requirements.txt"))
    assert both.reason.endswith("; changed: requirements.txt, project.dependencies")


@pytest.mark.parametrize("edited", [
    "# runtime\n" + REQUIREMENTS,
    REQUIREMENTS.replace("python-dotenv", "python-dotenv   # loads .env") + "\n\n",
    "  mcp==1.28.1  \n\npython-dotenv\n",
])
def test_comments_blank_lines_and_spacing_in_requirements_change_nothing(edited):
    """#364: only requirement lines count; an edited comment needs no environment."""
    assert gate(changed=("requirements.txt",), requirements=edited) == (NONE, None)


def test_requirement_lines_drop_comments_like_pip():
    text = "# header\nmcp==2  # pinned\nurl @ https://x/y#egg=z\n\n  numpy  \n"
    assert requirement_lines(text) == ["mcp==2", "url @ https://x/y#egg=z", "numpy"]


@pytest.mark.parametrize("malformed", [
    "not valid TOML !", "[project]\nname = 'x'\n",
    MANIFEST.replace('["mcp==1.28.1", "python-dotenv"]', "42"),
    MANIFEST.replace('["httpx>=0.28,<0.29"]', "[42]"),
    MANIFEST.replace('["setuptools>=68", "wheel"]', '"setuptools"'),
    MANIFEST.replace('version = "1.0"', 'version = "1.0"\ndynamic = ["dependencies"]'),
])
@pytest.mark.parametrize("revision", ["old", "target"])
@pytest.mark.parametrize("changed", [("pyproject.toml",), ("requirements.txt",)])
def test_malformed_manifest_on_either_side_fails_closed(malformed, revision, changed):
    change = gate(malformed, MANIFEST, changed=changed) if revision == "old" else \
        gate(MANIFEST, malformed, changed=changed)
    assert change.kind == REFUSED and "cannot verify environment compatibility" in change.reason


@pytest.mark.parametrize("revision", ["old", "target"])
def test_added_or_removed_manifest_fails_closed(revision):
    assert "must exist in both revisions" in gate(missing=revision).reason


def test_a_target_without_requirements_fails_closed():
    change = gate(changed=("requirements.txt",), missing="requirements")
    assert change.kind == REFUSED and "requirements.txt must exist in the target" in change.reason


@pytest.mark.parametrize("path", ["bridge/package.json", "bridge/package-lock.json"])
def test_bridge_package_manifests_always_require_maintenance(path):
    for changed in ((path,), ("pyproject.toml", path), ("requirements.txt", path)):
        change = gate(changed=changed)
        assert change.kind == REFUSED and path in change.reason and "explicit maintenance" in change.reason


def test_inputs_name_what_the_target_environment_installs():
    after = MANIFEST.replace('["mcp==1.28.1", "python-dotenv"]', '["python-dotenv", "mcp==2"]')
    found = inputs(fake_git(MANIFEST, after, requirements="mcp==2\n"), "old", "target")
    assert found.requirements == "mcp==2\n" and found.dependencies == ["mcp==2", "python-dotenv"]
    assert found.requires_python == ">=3.11"
    assert found.extras == found.installed_extras == {"local-inference": ["httpx>=0.28,<0.29"]}
