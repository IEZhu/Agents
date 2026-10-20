"""Environment compatibility gate shared by manual and scheduled Git updates."""
import tomllib


DEPENDENCIES = ("pyproject.toml", "requirements.txt", "uv.lock", "bridge/package.json", "bridge/package-lock.json")


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


def dependency_change_reason(git, old, target):
    """Return an actionable refusal, or None when the existing environment suffices.

    New extras were unavailable to the old installation, so merely declaring one
    needs no package installation. Existing extras stay protected even when this
    controller cannot tell which ones the owner installed. Git reads both exact
    revisions; a missing, deleted or malformed manifest fails closed.
    """
    changed = git("diff", "--name-only", old, target, "--", *DEPENDENCIES).stdout.splitlines()
    if not changed:
        return None
    reason = "Dependency manifests changed: " + ", ".join(changed)
    if changed != ["pyproject.toml"]:
        return reason + "; update the environment in explicit maintenance"
    try:
        environments = []
        for revision in (old, target):
            result = git("show", f"{revision}:pyproject.toml", check=False)
            if result.returncode:
                raise ValueError("pyproject.toml must exist in both revisions")
            environments.append(_environment(result.stdout))
        before, after = environments
        if before[:2] == after[:2] and before[3] == after[3] and all(
                name in after[2] and values == after[2][name] for name, values in before[2].items()):
            return None
    except (ValueError, TypeError) as error:
        return reason + f"; cannot verify environment compatibility ({error}); use explicit maintenance"
    return reason + "; runtime, Python, existing extra or build requirements changed; use explicit maintenance"
