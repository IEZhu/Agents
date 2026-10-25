"""Dependency gate shared by the staged, in-place and service updaters (#181).

A target's manifests decide what its update needs:

* ``NONE``: the installed environment suffices. Metadata, package data, a new
  optional extra, the order of requirements (in ``pyproject.toml`` and in
  ``requirements.txt``), comments, blank lines and spacing in ``requirements.txt``
  and a ``uv.lock``-only change (installs read ``requirements.txt``) change no
  package.
* ``ENVIRONMENT``: a requirement line of ``requirements.txt`` or the environment
  contract of ``pyproject.toml`` changed: ``project.dependencies``,
  ``project.requires-python``, an existing ``project.optional-dependencies``
  group, or ``build-system``, compared as a whole because a changed backend or
  option may change what an installation needs. The reason names each changed
  field. The staged updaters build the target's environment while the
  installation serves and switch to it at activation (`src.environments`); the
  in-place updater refuses.
* ``REFUSED``: the bridge's Node manifests changed (nothing installs npm
  packages), or a manifest is missing, malformed or dynamic, so the change cannot
  be verified. Such a target needs explicit maintenance.
"""
import re
import tomllib
from typing import NamedTuple, Optional


DEPENDENCIES = ("pyproject.toml", "requirements.txt", "uv.lock", "bridge/package.json", "bridge/package-lock.json")
# The bridge has no npm dependencies today; a change to its manifests is never installed automatically.
BRIDGE = ("bridge/package.json", "bridge/package-lock.json")
# Installs use requirements.txt; the lock file alone changes no installed package.
LOCK_ONLY = ("uv.lock",)

NONE, ENVIRONMENT, REFUSED = "none", "environment", "refused"


class Change(NamedTuple):
    """What a target's update needs: ``kind`` is NONE, ENVIRONMENT or REFUSED; ``reason`` explains the last two."""
    kind: str
    reason: Optional[str] = None


class Inputs(NamedTuple):
    """What the target's environment installs (`src.environments.needed`)."""
    requirements: str          # the target's requirements.txt
    dependencies: list         # the target's project.dependencies
    requires_python: Optional[str]
    extras: dict               # the target's optional dependencies
    installed_extras: dict     # the installed revision's optional dependencies


def _strings(value, field):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be an array of nonempty strings")
    return sorted(value)


def _environment(text):
    manifest = tomllib.loads(text)
    project, build = manifest.get("project"), manifest.get("build-system")
    if not isinstance(project, dict) or not isinstance(build, dict):
        raise ValueError("project and build-system tables are required")
    runtime = _strings(project.get("dependencies", []), "project.dependencies")
    python = project.get("requires-python")
    if python is not None and (not isinstance(python, str) or not python.strip()):
        raise ValueError("project.requires-python must be a nonempty string")
    dynamic = _strings(project.get("dynamic", []), "project.dynamic")
    if {"dependencies", "optional-dependencies", "requires-python"}.intersection(dynamic):
        raise ValueError("dynamic environment requirements cannot be verified")
    extras = project.get("optional-dependencies", {})
    if not isinstance(extras, dict):
        raise ValueError("project.optional-dependencies must be a table")
    extras = {name: _strings(values, f"project.optional-dependencies.{name}") for name, values in extras.items()}
    # Compare the whole build contract, including backend-path and backend options.
    build = {**build, "requires": _strings(build.get("requires"), "build-system.requires")}
    if not isinstance(build.get("build-backend"), str) or not build["build-backend"].strip():
        raise ValueError("build-system.build-backend must be a nonempty string")
    return runtime, python, extras, build


def requirement_lines(text):
    """The requirement lines of a requirements file: comments, blank lines and surrounding space dropped.

    A ``#`` starts a comment at the start of a line or after whitespace, as pip reads it.
    """
    lines = (re.sub(r"(?:^|\s+)#.*$", "", line).strip() for line in text.splitlines())
    return [line for line in lines if line]


def _changed_fields(before, after):
    """The dependency fields of ``pyproject.toml`` that differ between two parsed environments."""
    fields = []
    if before[0] != after[0]:
        fields.append("project.dependencies")
    if before[1] != after[1]:
        fields.append("project.requires-python")
    fields += [f"project.optional-dependencies.{name}" for name, values in sorted(before[2].items())
               if after[2].get(name) != values]
    if before[3] != after[3]:
        fields.append("build-system")
    return fields


def _show(git, revision, name, missing):
    """The file *name* at *revision*; ValueError(*missing*) when it does not exist there."""
    result = git("show", f"{revision}:{name}", check=False)
    if result.returncode:
        raise ValueError(missing)
    return result.stdout


def classify(git, old, target):
    """The :class:`Change` from revision *old* to *target*.

    New extras were unavailable to the old installation, so merely declaring one
    needs no package installation. Existing extras count as environment contract
    even when this controller cannot tell which ones the owner installed. Git reads
    both exact revisions; a missing, deleted or malformed manifest fails closed.
    *git* is called as ``git(*args, check=True)`` and returns a completed process.
    """
    changed = git("diff", "--name-only", old, target, "--", *DEPENDENCIES).stdout.splitlines()
    if not [name for name in changed if name not in LOCK_ONLY]:
        return Change(NONE)
    listed = "Dependency manifests changed: " + ", ".join(changed)
    if [name for name in changed if name in BRIDGE]:
        return Change(REFUSED, listed + "; the bridge's Node packages are not installed automatically; "
                                        "update the environment in explicit maintenance")
    try:
        before, after = (_environment(_show(git, revision, "pyproject.toml",
                                            "pyproject.toml must exist in both revisions"))
                         for revision in (old, target))
        fields = _changed_fields(before, after)
        if "requirements.txt" in changed:
            wanted = requirement_lines(_show(git, target, "requirements.txt",
                                             "requirements.txt must exist in the target"))
            installed = git("show", f"{old}:requirements.txt", check=False)
            if installed.returncode or sorted(requirement_lines(installed.stdout)) != sorted(wanted):
                fields.insert(0, "requirements.txt")
        if fields:  # the environment build installs the target's requirements.txt
            _show(git, target, "requirements.txt", "requirements.txt must exist in the target")
    except (ValueError, TypeError) as error:
        return Change(REFUSED, listed + f"; cannot verify environment compatibility ({error}); "
                                        "use explicit maintenance")
    if not fields:
        return Change(NONE)
    return Change(ENVIRONMENT, listed + "; changed: " + ", ".join(fields))


def inputs(git, old, target):
    """The target's :class:`Inputs` (``requirements`` is the file's text); ValueError when a manifest
    is missing or malformed."""
    requirements = _show(git, target, "requirements.txt", "requirements.txt must exist in the target")
    runtime, python, extras, _ = _environment(_show(git, target, "pyproject.toml",
                                                    "pyproject.toml must exist in the target"))
    installed = _environment(_show(git, old, "pyproject.toml", "pyproject.toml must exist in both revisions"))[2]
    return Inputs(requirements, runtime, python, extras, installed)
