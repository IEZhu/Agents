"""Versioned Python environments of an installation (#181).

An update whose target changes the dependencies (`src.daemon.update_dependencies`)
gets an environment of its own, built while the installation serves:
``<root>/.venvs/<id>``, where ``<id>`` is ``py<major><minor>-`` and the first 12 hex
digits of the SHA-256 of the requirements it installs. An environment is never
moved after it is created, so its console-script shebangs and ``pyvenv.cfg`` stay
valid. ``<id>/.agents-env.json``, written last, marks it complete; an environment
without it is removed before a rebuild.

``<root>/.venv`` is a link to the active environment: a symbolic link on POSIX, a
directory junction on Windows (no administrator rights needed). Client
configurations and the service keep naming ``<root>/.venv/bin/python``
(``Scripts\\python.exe``). The first switch of an installation whose ``.venv`` is a
real directory renames it to ``.venvs/legacy-<UTC time>``, only so that a rollback
can rename it back, where its scripts are valid again. A switch happens only while
no process of the installation runs: at a standalone start under the exclusive
installation lease, or in the service's maintenance.

The installed packages are the target's ``requirements.txt``, its
``project.dependencies`` (the code declares them; ``requirements.txt`` may lag) and
every optional extra whose packages the active environment has installed.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from importlib import metadata
from typing import NamedTuple, Optional

ENVIRONMENTS = ".venvs"
LINK = ".venv"
RECORD = ".agents-env.json"
# The requirements an environment was built from, kept next to its record.
REQUIREMENTS = ".agents-requirements.txt"

_ID = re.compile(r"\Apy[0-9]+-[0-9a-f]{12}\Z")
_LEGACY = re.compile(r"\Alegacy-[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?\Z")
# Requirements files that name other files resolve them next to the generated file, not the checkout.
_INCLUDES = re.compile(r"\A\s*(?:-r|-c|-e|--requirement|--constraint|--editable)\b")
_NAME = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_CLAUSE = re.compile(r"\s*(~=|===|==|!=|<=|>=|<|>)\s*([0-9]+(?:\.[0-9]+)*)(\.\*)?\s*\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
# Module-level so tests can exercise the Windows junction path with a stub.
WINDOWS = os.name == "nt"


class Refused(Exception):
    """The target's dependencies cannot be delivered automatically; the reason says why."""


class BuildFailed(Exception):
    """Building the target's environment failed; nothing else changed."""


class Needed(NamedTuple):
    """What an environment build installs, with which interpreter, and why it is needed."""
    requirements: str
    version: tuple
    base: str
    reason: str


def environments_dir(root) -> str:
    return os.path.join(str(root), ENVIRONMENTS)


def link_path(root) -> str:
    return os.path.join(str(root), LINK)


def path(root, name) -> str:
    return os.path.join(environments_dir(root), name)


def python_path(environment) -> str:
    """The interpreter of *environment* (a venv directory or the ``.venv`` link)."""
    if WINDOWS:
        return os.path.join(str(environment), "Scripts", "python.exe")
    return os.path.join(str(environment), "bin", "python")


def valid_id(name) -> bool:
    return isinstance(name, str) and bool(_ID.match(name))


def is_link(candidate) -> bool:
    """A symbolic link, or any Windows reparse point such as a junction."""
    try:
        status = os.lstat(candidate)
    except OSError:
        return False
    return stat.S_ISLNK(status.st_mode) or bool(getattr(status, "st_file_attributes", 0) & _REPARSE_POINT)


def _same(first, second) -> bool:
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def _unredirected(directory) -> bool:
    absolute = os.path.abspath(directory)
    return not is_link(absolute) and os.path.normcase(os.path.realpath(absolute)) == os.path.normcase(absolute)


def _remove_link(link) -> None:
    """Remove a link itself, never its target (a directory link or junction on Windows needs rmdir)."""
    try:
        os.unlink(link)
    except (IsADirectoryError, PermissionError):
        os.rmdir(link)


def _remove_tree(directory) -> None:
    """Remove an environment directory without following a link at or below it."""
    if is_link(directory):
        _remove_link(directory)
    elif os.path.isdir(directory):
        shutil.rmtree(directory)
    elif os.path.lexists(directory):
        os.unlink(directory)


def _junction(target, link) -> None:  # pragma: no cover - Windows
    import _winapi
    _winapi.CreateJunction(target, link)


# --- what an update needs -----------------------------------------------------------------------

def managed(root, python) -> bool:
    """Whether *python* is an interpreter of the installation's ``.venv``.

    Compared through the environment directory, not the interpreter: a venv's
    ``bin/python`` is usually a link to the base interpreter.
    """
    if not isinstance(python, str) or not os.path.isabs(python):
        return False
    link = link_path(root)
    environment = os.path.dirname(os.path.dirname(os.path.abspath(python)))
    return os.path.lexists(link) and _same(environment, link)


def _pyvenv(environment) -> dict:
    found = {}
    with open(os.path.join(environment, "pyvenv.cfg"), encoding="utf-8") as stream:
        for line in stream:
            key, separator, value = line.partition("=")
            if separator:
                found[key.strip().lower()] = value.strip()
    return found


def _version(text) -> tuple:
    match = re.match(r"\s*([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", text or "")
    if not match:
        raise ValueError(f"unreadable Python version {text!r}")
    return tuple(int(part or 0) for part in match.groups())


def base_python(root):
    """``(interpreter, version)`` that created the active environment, for a new one beside it.

    The running interpreter's base when this process runs that environment, else
    the ``pyvenv.cfg`` of the environment.
    """
    environment = os.path.realpath(link_path(root))
    base = getattr(sys, "_base_executable", None)
    if _same(sys.prefix, environment) and base and os.path.isfile(base):
        return base, tuple(sys.version_info[:3])
    try:
        config = _pyvenv(environment)
        version = _version(config.get("version_info") or config.get("version"))
    except (OSError, ValueError) as error:
        raise Refused(f"cannot read the base interpreter of {link_path(root)} ({error})") from None
    names = ("python.exe",) if WINDOWS else ("python3", "python")
    candidates = [config.get("executable")] + [os.path.join(config["home"], name)
                                               for name in names if config.get("home")]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate, version
    raise Refused(f"the base interpreter of {link_path(root)} no longer exists")


def _pad(version, length):
    return tuple(version) + (0,) * (length - len(version))


def python_refusal(specifier, version) -> Optional[str]:
    """Why *version* does not satisfy ``requires-python`` *specifier*, or None.

    Release segments only, which is what ``requires-python`` uses; an operator or
    form this cannot evaluate refuses.
    """
    if not specifier:
        return None
    for clause in specifier.split(","):
        match = _CLAUSE.match(clause)
        if not match:
            return f"cannot evaluate requires-python {specifier!r}"
        operator, text, wildcard = match.groups()
        wanted = tuple(int(part) for part in text.split("."))
        length = max(len(wanted), len(version))
        have, need = _pad(version, length), _pad(wanted, length)
        if wildcard:
            if operator not in ("==", "!="):
                return f"cannot evaluate requires-python {specifier!r}"
            prefix = tuple(version[:len(wanted)]) == wanted
            satisfied = prefix if operator == "==" else not prefix
        elif operator == "~=":
            if len(wanted) < 2:
                return f"cannot evaluate requires-python {specifier!r}"
            satisfied = have >= need and tuple(version[:len(wanted) - 1]) == wanted[:-1]
        else:
            satisfied = {"==": have == need, "===": have == need, "!=": have != need, "<=": have <= need,
                         ">=": have >= need, "<": have < need, ">": have > need}[operator]
        if not satisfied:
            return "Python {} does not satisfy requires-python {!r}".format(".".join(map(str, version)), specifier)
    return None


def _normalized(name) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement) -> Optional[str]:
    match = _NAME.match(requirement)
    return _normalized(match.group(1)) if match else None


def installed(environment) -> set:
    """Normalized names of the distributions installed in *environment*."""
    if WINDOWS:
        sites = [os.path.join(environment, "Lib", "site-packages")]
    else:
        sites = glob.glob(os.path.join(glob.escape(str(environment)), "lib", "python*", "site-packages"))
    return {_normalized(dist.name) for dist in metadata.distributions(path=sites) if dist.name}


def requirements_text(requirements, dependencies, extras) -> str:
    """The requirements an environment installs: ``requirements.txt``, runtime dependencies, carried extras."""
    lines = [requirements.rstrip("\n"), "", "# project.dependencies of pyproject.toml", *dependencies]
    for name in sorted(extras):
        lines += ["", f"# optional extra {name}, installed in the previous environment", *extras[name]]
    return "\n".join(lines) + "\n"


def needed(root, git, old, target, python) -> Optional[Needed]:
    """What the update from *old* to *target* installs, or None when the active environment suffices.

    *python* is the interpreter that serves the installation (the stdio server's or
    the service's). Raises :class:`Refused` for a change that needs explicit
    maintenance: see `src.daemon.update_dependencies`, an interpreter outside the
    installation's ``.venv``, a ``.venv`` that is neither an environment directory
    nor a link into ``.venvs``, or a ``requires-python`` the base interpreter misses.
    Git errors propagate.
    """
    from src.daemon.update_dependencies import ENVIRONMENT, REFUSED, classify, inputs
    change = classify(git, old, target)
    if change.kind == REFUSED:
        raise Refused(change.reason)
    if change.kind != ENVIRONMENT:
        return None
    if not managed(root, python):
        raise Refused(f"{change.reason}, but {python} is not an interpreter of the installation's {LINK}; "
                      "install the dependencies in explicit maintenance")
    if state(root)["kind"] not in ("link", "directory"):
        raise Refused(f"{change.reason}, but {link_path(root)} is neither an environment directory nor a "
                      f"link into {ENVIRONMENTS}; install the dependencies in explicit maintenance")
    base, version = base_python(root)
    try:
        spec = inputs(git, old, target)
    except (ValueError, TypeError) as error:
        raise Refused(f"{change.reason}; cannot read the target's manifests ({error})") from None
    if reason := python_refusal(spec.requires_python, version):
        raise Refused(f"{change.reason}; {reason}; use explicit maintenance")
    if any(_INCLUDES.match(line) for line in spec.requirements.splitlines()):
        raise Refused(f"{change.reason}; requirements.txt names other files, which an automatic "
                      "build cannot resolve; use explicit maintenance")
    try:
        present = installed(os.path.realpath(link_path(root)))
    except OSError as error:
        raise Refused(f"cannot list the packages of {link_path(root)} ({error})") from None
    carried = {name: spec.extras[name] for name, before in spec.installed_extras.items()
               if name in spec.extras and before and all(_requirement_name(item) in present for item in before)}
    return Needed(requirements_text(spec.requirements, spec.dependencies, carried), version, base, change.reason)


# --- building ---------------------------------------------------------------------------------------

def environment_id(requirements, version) -> str:
    digest = hashlib.sha256(requirements.encode("utf-8")).hexdigest()
    return f"py{version[0]}{version[1]}-{digest[:12]}"


def complete(environment, digest=None) -> bool:
    """Whether *environment* is a finished build (of the requirements with SHA-256 *digest*)."""
    if is_link(environment) or not os.path.isdir(environment):
        return False
    try:
        with open(os.path.join(environment, RECORD), encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, ValueError):
        return False
    return (isinstance(record, dict) and (digest is None or record.get("requirements_sha256") == digest)
            and os.path.exists(python_path(environment)))


def _step(run, args, cwd, deadline, what):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BuildFailed(f"{what} did not start before the environment timeout")
    try:
        result = run(args, cwd=cwd, timeout=max(1, int(remaining)))
    except subprocess.TimeoutExpired:
        raise BuildFailed(f"{what} timed out") from None
    except OSError as error:
        raise BuildFailed(f"{what} could not run ({error})") from None
    if result.returncode:
        detail = ((result.stderr or "") + (result.stdout or "")).strip()[-1000:]
        raise BuildFailed(f"{what} failed (exit code {result.returncode}): {detail}")


def build(root, wanted, *, run, timeout) -> dict:
    """Build (or reuse) the environment *wanted* (:class:`Needed`) describes; its marker record.

    ``run(args, cwd=..., timeout=...)`` runs a command (``self_update._run_command``,
    which keeps the updater's leases in the child); *timeout* bounds the whole build.
    A failure removes the partial environment and raises :class:`BuildFailed`.
    """
    digest = hashlib.sha256(wanted.requirements.encode("utf-8")).hexdigest()
    name = environment_id(wanted.requirements, wanted.version)
    parent = environments_dir(root)
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as error:
        raise BuildFailed(f"cannot create {parent} ({error})") from None
    if not _unredirected(parent):
        raise BuildFailed(f"{parent} is a link or lies below one")
    environment = path(root, name)
    record = {"id": name, "path": environment, "python": python_path(environment), "requirements_sha256": digest}
    if complete(environment, digest):
        return record
    if os.path.lexists(environment):
        if is_link(link_path(root)) and _same(link_path(root), environment):
            raise BuildFailed(f"the active environment {environment} is incomplete; repair it in explicit maintenance")
        try:
            _remove_tree(environment)
        except OSError as error:
            raise BuildFailed(f"cannot remove the incomplete environment {environment} ({error})") from None
    deadline = time.monotonic() + timeout
    try:
        _step(run, [wanted.base, "-m", "venv", environment], str(root), deadline, "python -m venv")
        requirements = os.path.join(environment, REQUIREMENTS)
        with open(requirements, "w", encoding="utf-8") as stream:
            stream.write(wanted.requirements)
        _step(run, [python_path(environment), "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-input", "-r", requirements], str(root), deadline, "pip install")
        temporary = os.path.join(environment, RECORD + ".tmp")
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump({"requirements_sha256": digest, "python": ".".join(map(str, wanted.version)),
                       "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, stream)
        os.replace(temporary, os.path.join(environment, RECORD))
    except BaseException as error:
        try:
            _remove_tree(environment)
        except OSError:
            pass  # incomplete without its record: the next build removes it
        if isinstance(error, OSError):
            raise BuildFailed(f"cannot write {environment} ({error})") from None
        raise
    return record


# --- switching --------------------------------------------------------------------------------------

def state(root) -> dict:
    """What ``.venv`` is: ``link`` (with the environment ``name``), ``directory``, ``missing``,
    ``foreign`` (a link elsewhere) or ``file``."""
    link = link_path(root)
    if is_link(link):
        real = os.path.realpath(link)
        name = os.path.basename(real)
        if _same(os.path.dirname(real), environments_dir(root)) and (valid_id(name) or _LEGACY.match(name)):
            return {"kind": "link", "name": name}
        return {"kind": "foreign"}
    if os.path.isdir(link):
        return {"kind": "directory"}
    return {"kind": "file" if os.path.lexists(link) else "missing"}


def plan(root, name, now=None) -> dict:
    """The switch of ``.venv`` to environment *name* and what it replaces, for the update journal.

    A ``.venv`` directory gets the name it will be renamed to under ``.venvs``.
    """
    current = state(root)
    if current["kind"] in ("foreign", "file"):
        raise Refused(f"{link_path(root)} is neither an environment directory nor a link into {ENVIRONMENTS}")
    previous = dict(current)
    if current["kind"] == "directory":
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        legacy, suffix = f"legacy-{stamp}", 0
        while os.path.lexists(path(root, legacy)):
            suffix += 1
            legacy = f"legacy-{stamp}-{suffix}"
        previous["legacy"] = legacy
    return {"to": name, "previous": previous}


def _point(root, name) -> None:
    """Make ``.venv`` a link to environment *name*; POSIX replaces a link atomically."""
    link, target = link_path(root), path(root, name)
    if is_link(link) and _same(link, target) and os.path.isdir(target):
        return
    if is_link(target) or not os.path.isdir(target):
        raise FileNotFoundError(f"environment {target} does not exist")
    if os.path.lexists(link) and not is_link(link):
        raise IsADirectoryError(f"{link} is not a link")
    if WINDOWS:
        if os.path.lexists(link):
            _remove_link(link)
        _junction(os.path.abspath(target), link)
        return
    temporary = f"{link}.{os.getpid()}.tmp"
    if is_link(temporary):
        os.unlink(temporary)
    os.symlink(os.path.join(ENVIRONMENTS, name), temporary, target_is_directory=True)
    try:
        os.replace(temporary, link)
    except BaseException:
        if is_link(temporary):
            os.unlink(temporary)
        raise


def _check(planned) -> dict:
    previous = planned.get("previous") if isinstance(planned, dict) else None
    if not isinstance(previous, dict) or not valid_id(planned.get("to")) or previous.get("kind") not in (
            "link", "directory", "missing") or (previous["kind"] == "link" and not (
            valid_id(previous.get("name")) or _LEGACY.match(str(previous.get("name"))))) or (
            previous["kind"] == "directory" and not _LEGACY.match(str(previous.get("legacy")))):
        raise ValueError(f"malformed environment switch {planned!r}")
    return previous


def switch(root, planned) -> None:
    """Point ``.venv`` at ``planned["to"]`` (:func:`plan`); a ``.venv`` directory is renamed first.

    Only while no process of the installation runs. Raises OSError (or ValueError
    for a malformed plan); :func:`restore` undoes any part that happened.
    """
    previous = _check(planned)
    link = link_path(root)
    if previous["kind"] == "directory" and not is_link(link) and os.path.isdir(link):
        legacy = path(root, previous["legacy"])
        if os.path.lexists(legacy):
            raise FileExistsError(f"{legacy} already exists")
        os.rename(link, legacy)
    _point(root, planned["to"])


def restore(root, planned) -> None:
    """Undo :func:`switch` (complete or partial): ``.venv`` becomes what it was. Idempotent."""
    previous = _check(planned)
    link = link_path(root)
    if previous["kind"] == "link":
        _point(root, previous["name"])
    elif previous["kind"] == "directory":
        if not is_link(link) and os.path.isdir(link):
            return  # never renamed, or already restored
        legacy = path(root, previous["legacy"])
        if is_link(legacy) or not os.path.isdir(legacy):
            raise FileNotFoundError(f"the previous environment {legacy} is missing")
        if os.path.lexists(link):
            if not is_link(link):
                raise IsADirectoryError(f"{link} is not a link")
            _remove_link(link)
        os.rename(legacy, link)
    elif is_link(link):
        _remove_link(link)


def prune(root, keep=()) -> list:
    """Remove environments of ``.venvs`` except *keep*, the active one and this process's; their names.

    Only names this module creates are touched. Call after a successful activation.
    """
    parent = environments_dir(root)
    if not os.path.isdir(parent) or not _unredirected(parent):
        return []
    protected = {name for name in keep if name}
    current = state(root)
    if current["kind"] == "link":
        protected.add(current["name"])
    removed = []
    for name in sorted(os.listdir(parent)):
        candidate = os.path.join(parent, name)
        if name in protected or not (valid_id(name) or _LEGACY.match(name)) or _same(candidate, sys.prefix):
            continue
        _remove_tree(candidate)
        removed.append(name)
    return removed


def activation_refusal(root, environment) -> Optional[str]:
    """Why a prepared update's *environment* record cannot be switched to now, or None."""
    if not isinstance(environment, dict) or not valid_id(environment.get("id")) or \
            not isinstance(environment.get("requirements_sha256"), str):
        return "the prepared environment record is malformed"
    parent = environments_dir(root)
    if not _unredirected(parent):
        return f"{parent} is a link or lies below one"
    if not complete(path(root, environment["id"]), environment["requirements_sha256"]):
        return f"the prepared environment {environment['id']} is missing or incomplete"
    if state(root)["kind"] not in ("link", "directory", "missing"):
        return f"{link_path(root)} is neither an environment directory nor a link into {ENVIRONMENTS}"
    return None
