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
(``Scripts\\python.exe``). On POSIX the first switch of an installation whose
``.venv`` is a real directory renames it to ``.venvs/legacy-<UTC time>``, only so
that a rollback can rename it back, where its scripts are valid again. Windows
cannot rename the directory of a running interpreter, so there that layout refuses
dependency changes; only a ``.venv`` junction already in place is switched. A switch happens only while no process of the installation runs: at a
standalone start under the exclusive installation lease, or in the service's
maintenance. POSIX replaces the link atomically; Windows removes the junction,
then creates the new one, which no process can observe in that state.

The installed packages are the target's ``requirements.txt``, its
``project.dependencies`` (the code declares them; ``requirements.txt`` may lag)
and the extras carried over: an optional extra that both revisions declare and
whose every distribution, by the installed revision's definition, the active
environment has installed, also as a dependency of another package. A carried
extra installs the target's requirements.
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
# Environments being removed: a name neither _ID nor _LEGACY matches, so never complete.
TRASH = ".trash-"

_ID = re.compile(r"\Apy[0-9]+-[0-9a-f]{12}\Z")
_LEGACY = re.compile(r"\Alegacy-[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?\Z")
_TEMPORARY_LINK = re.compile(r"\A\.venv\.[0-9]+\.tmp\Z")
_NAME = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_SPECIFIER = re.compile(r"\A(?:~=|===|==|!=|<=|>=|<|>)[A-Za-z0-9.*+!_-]+\Z")
# A bare URL or VCS reference: no distribution name precedes it.
_URL = re.compile(r"\A(?:[A-Za-z][A-Za-z0-9+.-]*://|(?:git|hg|svn|bzr)\+)")
# A local path, which pip would resolve next to the generated file instead of the checkout.
_LOCAL_PATH = re.compile(r"\A(?:[./~]|[A-Za-z]:)")
_CLAUSE = re.compile(r"\s*(~=|===|==|!=|<=|>=|<|>)\s*([0-9]+(?:\.[0-9]+)*)(\.\*)?\s*\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
# Settings that would point venv or pip at another interpreter, site or target than the new environment.
_FOREIGN_SETTINGS = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PIP_USER", "PIP_TARGET",
                     "PIP_PREFIX")
_PROBE = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
_PROBE_TIMEOUT = 30
# Module-level so tests can exercise the Windows junction path with a stub.
WINDOWS = os.name == "nt"
# The build deadline's clock; tests replace it.
_now = time.monotonic
# The environment this process runs, before any switch can redirect ``.venv``.
RUNNING = os.path.realpath(sys.prefix)


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


def _lexically_same(first, second) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


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


def _child_environment() -> dict:
    """This process's environment without the settings that redirect venv or pip."""
    return {key: value for key, value in os.environ.items() if key not in _FOREIGN_SETTINGS}


def _default_run(args, cwd=None, timeout=None, env=None):
    return subprocess.run(args, cwd=cwd, timeout=timeout, env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, check=False)


# --- what an update needs -----------------------------------------------------------------------

def managed(root, python) -> bool:
    """Whether *python* names the interpreter of the installation's ``.venv``.

    Its environment directory is named ``.venv`` and sits in the installation, which
    may be reached through a link (only that parent is resolved); a path that merely
    resolves there, such as ``.venvs/<id>/bin/python``, does not count: only a process
    started through ``.venv`` follows a switch.
    """
    if not isinstance(python, str) or not os.path.isabs(python):
        return False
    environment = os.path.dirname(os.path.dirname(os.path.abspath(python)))
    return (os.path.normcase(os.path.basename(environment)) == os.path.normcase(LINK)
            and _same(os.path.dirname(environment), root) and os.path.lexists(link_path(root)))


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


def base_python(root, run=None):
    """``(interpreter, version)`` that created the active environment, for a new one beside it.

    The running interpreter's base when this process runs that environment, else
    the ``pyvenv.cfg`` of the environment: its ``executable``, then ``home``'s
    ``python<major>.<minor>``, ``python3`` and ``python``. That one runs once (with
    *run*, ``run(args, cwd=..., timeout=..., env=...)``) to confirm its version.
    """
    environment = os.path.realpath(link_path(root))
    base = getattr(sys, "_base_executable", None)
    if _same(sys.prefix, environment) and base and os.path.isfile(base):
        return base, tuple(sys.version_info[:3])
    try:
        config = _pyvenv(environment)
        recorded = _version(config.get("version_info") or config.get("version"))
    except (OSError, ValueError) as error:
        raise Refused(f"cannot read the base interpreter of {link_path(root)} ({error})") from None
    names = ("python.exe",) if WINDOWS else (f"python{recorded[0]}.{recorded[1]}", "python3", "python")
    candidates = [config.get("executable")] + [os.path.join(config["home"], name)
                                               for name in names if config.get("home")]
    found = []
    for candidate in dict.fromkeys(candidate for candidate in candidates if candidate and os.path.isfile(candidate)):
        try:
            probe = (run or _default_run)([candidate, "-c", _PROBE], cwd=str(root), timeout=_PROBE_TIMEOUT,
                                          env=_child_environment())
            version = _version(probe.stdout) if not probe.returncode else None
        except Exception:  # a probe that cannot run counts as a mismatch, whatever the reason
            version = None
        if version is not None and version[:2] == recorded[:2]:
            return candidate, version
        found.append(f"{candidate} ({'Python ' + '.'.join(map(str, version)) if version else 'does not run'})")
    if not found:
        raise Refused(f"the base interpreter of {link_path(root)} no longer exists")
    raise Refused("no base interpreter of {} runs Python {}.{}, which made it: {}".format(
        link_path(root), recorded[0], recorded[1], "; ".join(found)))


def _pad(version, length):
    return tuple(version) + (0,) * (length - len(version))


def _clause_holds(clause, version) -> Optional[bool]:
    """Whether release *version* (a tuple of ints) satisfies one specifier *clause*; None when this
    cannot evaluate the clause: release segments only, with ``.*`` wildcards for ``==`` and ``!=``."""
    match = _CLAUSE.match(clause)
    if not match:
        return None
    operator, text, wildcard = match.groups()
    wanted = tuple(int(part) for part in text.split("."))
    length = max(len(wanted), len(version))
    have, need = _pad(version, length), _pad(wanted, length)
    if wildcard:
        if operator not in ("==", "!="):
            return None
        prefix = tuple(version[:len(wanted)]) == wanted
        return prefix if operator == "==" else not prefix
    if operator == "~=":
        if len(wanted) < 2:
            return None
        return have >= need and tuple(version[:len(wanted) - 1]) == wanted[:-1]
    return {"==": have == need, "===": have == need, "!=": have != need, "<=": have <= need,
            ">=": have >= need, "<": have < need, ">": have > need}[operator]


def _release(text) -> Optional[tuple]:
    """The release segments of an installed version, such as ``(1, 30, 0)`` for ``1.30.0.post1``."""
    match = re.match(r"\s*v?([0-9]+(?:\.[0-9]+)*)", text or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def python_refusal(specifier, version) -> Optional[str]:
    """Why *version* does not satisfy ``requires-python`` *specifier*, or None.

    Release segments only, which is what ``requires-python`` uses; an operator or
    form this cannot evaluate refuses.
    """
    if not specifier:
        return None
    for clause in specifier.split(","):
        holds = _clause_holds(clause, version)
        if holds is None:
            return f"cannot evaluate requires-python {specifier!r}"
        if not holds:
            return "Python {} does not satisfy requires-python {!r}".format(".".join(map(str, version)), specifier)
    return None


def _normalized(name) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement) -> Optional[str]:
    match = _NAME.match(requirement)
    return _normalized(match.group(1)) if match else None


def unsafe_requirement(line) -> Optional[str]:
    """Why a requirement line cannot go into a generated requirements file, or None.

    A pip option (``-r``, ``-e``, ``--index-url`` …) or a local path would apply
    to, or resolve against, the generated file instead of the checkout.
    """
    if "\n" in line or "\r" in line:
        return f"{line!r} spans several lines"
    stripped = line.strip()
    if stripped.endswith("\\"):
        return f"{stripped!r} continues on the next line"
    if stripped.startswith("-"):
        return f"{stripped!r} is a pip option"
    if _LOCAL_PATH.match(stripped):
        return f"{stripped!r} is a local path"
    return None


def distribution_names(text) -> Optional[set]:
    """Normalized names of the distributions a requirements text names.

    None when a line is a bare URL or VCS reference, whose distribution cannot be
    read from the text. Markers are not evaluated: a line counts whatever it says.
    """
    from src.daemon.update_dependencies import requirement_lines
    names = set()
    for line in requirement_lines(text):
        name = _parse(line)[0]
        if name is None:
            return None
        names.add(name)
    return names


def _installed(environment) -> dict:
    """Normalized name -> version of each distribution installed in *environment*."""
    if WINDOWS:
        sites = [os.path.join(environment, "Lib", "site-packages")]
    else:
        sites = glob.glob(os.path.join(glob.escape(str(environment)), "lib", "python*", "site-packages"))
    return {_normalized(dist.name): dist.version for dist in metadata.distributions(path=sites) if dist.name}


def installed(environment) -> set:
    """Normalized names of the distributions installed in *environment*."""
    return set(_installed(environment))


def unmet(text, environment) -> Optional[list]:
    """The requirements of *text* that *environment* does not satisfy, described; None when unknown.

    A distribution must be installed in a release version that every specifier
    clause accepts (:func:`_clause_holds`). A clause this cannot evaluate, a direct
    URL reference or an unreadable installed version counts as unmet; a bare URL
    line makes the answer unknown. Markers are not evaluated.
    """
    from src.daemon.update_dependencies import requirement_lines
    if distribution_names(text) is None:
        return None
    versions, found = _installed(environment), []
    for line in requirement_lines(text):
        name, clauses = _clauses(line)
        version = versions.get(name)
        release = _release(version) if version else None
        if version is None:
            found.append(f"{name} (not installed)")
        elif clauses is None or release is None:
            found.append(f"{name} {version} (cannot check {line!r})")
        elif unsatisfied := [clause for clause in clauses if not _clause_holds(clause, release)]:
            found.append(f"{name} {version} (needs {','.join(unsatisfied)})")
    return found


def _clauses(line):
    """``(name, specifier clauses)`` of a requirement line: the clauses after an extras group and
    before a marker; None for a direct URL reference. ``(None, None)`` for a bare URL."""
    if _URL.match(line):
        return None, None
    match = _NAME.match(line)
    if not match:
        return None, None
    rest = re.sub(r"\A\[[^\]]*\]", "", line[match.end():].strip()).strip()
    if rest.startswith("@"):
        return _normalized(match.group(1)), None
    return _normalized(match.group(1)), [re.sub(r"\s+", "", clause)
                                         for clause in rest.split(";", 1)[0].split(",") if clause.strip()]


def _parse(line):
    """``(name, specifiers, exact pins)`` of a requirement line.

    *specifiers* is the clause list of a plain ``name<specifiers>`` line, else None
    (extras, a marker or a URL keep the line as written). Exact pins come from any
    named line, also one with extras or a marker. *name* is None for a bare URL or
    VCS reference, which no leading word such as ``git`` names.
    """
    name, clauses = _clauses(line)
    if name is None:
        return None, None, set()
    pins = {re.match(r"\A===?(.+)", clause).group(1) for clause in clauses or ()
            if re.match(r"\A===?[^=]", clause) and not clause.endswith(".*")}
    rest = line[_NAME.match(line).end():].strip()
    plain = clauses is not None and not rest.startswith("[") and ";" not in rest and all(
        _SPECIFIER.match(clause) for clause in clauses)
    return name, clauses if plain else None, pins


def requirements_text(requirements, dependencies, extras) -> str:
    """The requirements an environment installs: ``requirements.txt`` lines, runtime dependencies, carried extras.

    Identical lines are listed once. Plain ``name<specifiers>`` lines of one
    distribution become one line with every specifier, which a specifier set
    intersects; lines with extras, a marker or a URL stay as written. Only two
    different exact pins (``==``, ``===``) of one distribution raise ValueError,
    naming both lines.
    """
    lines, plain, pinned, listed = [], {}, {}, set()

    def add(items):
        for item in items:
            stripped = item.strip()
            name, clauses, pins = _parse(stripped)
            for pin in pins:
                if name in pinned and pinned[name][0] != pin:
                    raise ValueError(f"{pinned[name][1]!r} and {stripped!r} pin {name} to different versions")
                pinned.setdefault(name, (pin, stripped))
            if clauses is None:
                if stripped not in listed:
                    listed.add(stripped)
                    lines.append(stripped)
            elif name in plain:
                plain[name][1].extend(clause for clause in clauses if clause not in plain[name][1])
            else:
                plain[name] = [_NAME.match(stripped).group(1), list(clauses)]
                lines.append(plain[name])

    add(requirements)
    lines.append("")
    lines.append("# project.dependencies of pyproject.toml")
    add(dependencies)
    for name in sorted(extras):
        start = len(lines)
        lines += ["", f"# optional extra {name}, installed in the previous environment"]
        add(extras[name])
        if len(lines) == start + 2:
            del lines[start:]  # every requirement of the extra is listed already
    return "\n".join(line if isinstance(line, str) else line[0] + ",".join(line[1])
                     for line in lines) + "\n"


def needed(root, git, old, target, python, run=None) -> Optional[Needed]:
    """What the update from *old* to *target* installs, or None when the active environment suffices.

    *python* is the interpreter that serves the installation (the stdio server's or
    the service's); *run* runs the base interpreter's version probe
    (:func:`base_python`). Raises :class:`Refused` for a change that needs explicit
    maintenance: see `src.daemon.update_dependencies`, an interpreter other than
    ``.venv``'s, a ``.venv`` that is neither an environment directory nor a link into
    ``.venvs`` (a directory without ``pyvenv.cfg``, and on Windows any directory),
    a missing or different base interpreter, a ``requires-python`` it misses, a pip
    option, a local path or a continued line in the requirements, or two different
    exact pins of one distribution. Git errors propagate.
    """
    from src.daemon.update_dependencies import ENVIRONMENT, REFUSED, classify, inputs, requirement_lines
    change = classify(git, old, target)
    if change.kind == REFUSED:
        raise Refused(change.reason)
    if change.kind != ENVIRONMENT:
        return None
    if not managed(root, python):
        raise Refused(f"{change.reason}, but {python} is not the installation's interpreter "
                      f"{python_path(link_path(root))}; install the dependencies in explicit maintenance")
    kind = state(root)["kind"]
    if kind not in ("link", "directory"):
        raise Refused(f"{change.reason}, but {link_path(root)} is neither an environment directory nor a "
                      f"link into {ENVIRONMENTS}; install the dependencies in explicit maintenance")
    if kind == "directory" and not os.path.isfile(os.path.join(link_path(root), "pyvenv.cfg")):
        raise Refused(f"{change.reason}, but {link_path(root)} has no pyvenv.cfg, so it is no environment this "
                      "updater can replace; install the dependencies in explicit maintenance")
    if WINDOWS and kind == "directory":
        raise Refused(f"{change.reason}, but {link_path(root)} is the directory setup created, which Windows "
                      "cannot rename while its interpreter runs; install the dependencies in explicit "
                      "maintenance with the sessions stopped")
    base, version = base_python(root, run)
    try:
        spec = inputs(git, old, target)
    except (ValueError, TypeError) as error:
        raise Refused(f"{change.reason}; cannot read the target's manifests ({error})") from None
    if reason := python_refusal(spec.requires_python, version):
        raise Refused(f"{change.reason}; {reason}; use explicit maintenance")
    lines = requirement_lines(spec.requirements)
    declared = [*spec.dependencies, *(item for values in spec.extras.values() for item in values)]
    for item in [*lines, *declared]:
        if problem := unsafe_requirement(item):
            raise Refused(f"{change.reason}; {problem}, which an automatic build cannot install; "
                          "use explicit maintenance")
    try:
        present = installed(os.path.realpath(link_path(root)))
    except OSError as error:
        raise Refused(f"cannot list the packages of {link_path(root)} ({error})") from None
    carried = {name: spec.extras[name] for name, before in spec.installed_extras.items()
               if name in spec.extras and before and all(_requirement_name(item) in present for item in before)}
    try:
        text = requirements_text(lines, spec.dependencies, carried)
    except ValueError as error:
        raise Refused(f"{change.reason}; {error}; use explicit maintenance") from None
    return Needed(text, version, base, change.reason)


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
    remaining = deadline - _now()
    if remaining <= 0:
        raise BuildFailed(f"{what} did not start before the environment timeout")
    try:
        result = run(args, cwd=cwd, timeout=max(1, int(remaining)), env=_child_environment())
    except subprocess.TimeoutExpired:
        raise BuildFailed(f"{what} timed out") from None
    except Exception as error:  # the runner's own failure, e.g. undecodable output
        raise BuildFailed(f"{what} could not run ({type(error).__name__}: {error})") from None
    if result.returncode:
        # pip names the failing requirement on stderr; stdout ends with what it last did.
        detail = (result.stderr or "").strip()[-1500:]
        if (result.stdout or "").strip():
            detail += "\n... " + result.stdout.strip()[-300:]
        raise BuildFailed(f"{what} failed (exit code {result.returncode}): {detail}")


def build(root, wanted, *, run, timeout) -> dict:
    """Build (or reuse) the environment *wanted* (:class:`Needed`) describes; its marker record.

    ``run(args, cwd=..., timeout=..., env=...)`` runs a command
    (``self_update._run_command``, which keeps the updater's leases in the child);
    *timeout* bounds the whole build. venv and pip run in the new environment's
    directory, without settings that would redirect them. A failure removes the
    partial environment and raises :class:`BuildFailed`.
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
    deadline = _now() + timeout
    try:
        os.mkdir(environment)
        _step(run, [wanted.base, "-m", "venv", environment], environment, deadline, "python -m venv")
        requirements = os.path.join(environment, REQUIREMENTS)
        with open(requirements, "w", encoding="utf-8") as stream:
            stream.write(wanted.requirements)
        _step(run, [python_path(environment), "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-input", "-r", requirements], environment, deadline, "pip install")
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


def _runs(environment) -> bool:
    """Whether *environment* is the one this process started on (:data:`RUNNING`, not resolved again)."""
    return os.path.normcase(os.path.realpath(environment)) == os.path.normcase(RUNNING)


def ours(root) -> bool:
    """Whether this process started on an environment of the installation: ``.venv`` or one under ``.venvs``.

    As in :func:`managed`, the installation may be reached through a link: its path
    is resolved, the environment's own name is compared as it is.
    """
    parent, name = os.path.dirname(RUNNING), os.path.normcase(os.path.basename(RUNNING))
    return (name == os.path.normcase(LINK) and _same(parent, root)) or _same(parent, environments_dir(root))


def replaced(root) -> bool:
    """Whether this process runs an environment of the installation that ``.venv`` no longer points at."""
    venv = link_path(root)
    return ours(root) and os.path.lexists(venv) and not _runs(venv)


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


def _new_link(root, name) -> str:
    """POSIX: a temporary link to environment *name* beside ``.venv``, ready to replace it."""
    link = link_path(root)
    temporary = f"{link}.{os.getpid()}.tmp"
    if is_link(temporary):
        os.unlink(temporary)
    os.symlink(os.path.join(ENVIRONMENTS, name), temporary, target_is_directory=True)
    return temporary


def _point(root, name, temporary=None) -> None:
    """Make ``.venv`` a link to environment *name*; POSIX replaces a link atomically."""
    link, target = link_path(root), path(root, name)
    if is_link(link) and _same(link, target) and os.path.isdir(target):
        if temporary and is_link(temporary):
            os.unlink(temporary)
        return
    if is_link(target) or not os.path.isdir(target):
        raise FileNotFoundError(f"environment {target} does not exist")
    if os.path.lexists(link) and not is_link(link):
        raise IsADirectoryError(f"{link} is not a link")
    if WINDOWS:
        if os.path.lexists(link):
            _remove_link(link)
        try:
            _junction(os.path.abspath(target), link)
        except BaseException:
            # A failed CreateJunction can leave the empty directory it started from.
            if os.path.isdir(link) and not is_link(link) and not os.listdir(link):
                os.rmdir(link)
            raise
        return
    temporary = temporary or _new_link(root, name)
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

    Only while no process of the installation runs. On POSIX the new link exists
    before the directory moves, so only the two renames remain in between. Raises
    OSError (or ValueError for a malformed plan); :func:`restore` undoes any part
    that happened.
    """
    previous = _check(planned)
    link = link_path(root)
    temporary = None
    if previous["kind"] == "directory" and not is_link(link) and os.path.isdir(link):
        legacy = path(root, previous["legacy"])
        if os.path.lexists(legacy):
            raise FileExistsError(f"{legacy} already exists")
        target = path(root, planned["to"])
        if is_link(target) or not os.path.isdir(target):
            raise FileNotFoundError(f"environment {target} does not exist")
        temporary = None if WINDOWS else _new_link(root, planned["to"])
        try:
            os.rename(link, legacy)
        except BaseException:
            if temporary and is_link(temporary):
                os.unlink(temporary)
            raise
    _point(root, planned["to"], temporary)


def restore(root, planned) -> None:
    """Undo :func:`switch` (complete or partial): ``.venv`` becomes what it was. Idempotent."""
    previous = _check(planned)
    link = link_path(root)
    if previous["kind"] == "link":
        _point(root, previous["name"])
    elif previous["kind"] == "directory":
        legacy = path(root, previous["legacy"])
        if not is_link(link) and os.path.isdir(link) and (
                os.path.isfile(os.path.join(link, "pyvenv.cfg")) or not os.path.lexists(legacy)):
            return  # never renamed, or already restored
        if is_link(legacy) or not os.path.isdir(legacy):
            raise FileNotFoundError(f"the previous environment {legacy} is missing")
        if is_link(link):
            _remove_link(link)
        elif os.path.isdir(link):
            os.rmdir(link)  # an empty directory a failed junction left; anything else is not ours to remove
        elif os.path.lexists(link):
            raise FileExistsError(f"{link} is not a link")
        os.rename(legacy, link)
    elif is_link(link):
        _remove_link(link)


def prune(root, keep=()) -> list:
    """Remove the environments of ``.venvs`` except *keep*, the active one and the one this process
    runs; their names.

    Only names this module creates are touched. A removal first renames the
    environment to a ``.trash-`` name and drops its record, so an interrupted one
    leaves nothing complete; leftovers of that and stale temporary ``.venv`` links
    go too. Call after a successful activation.
    """
    parent = environments_dir(root)
    if not os.path.isdir(parent) or not _unredirected(parent):
        return []
    for name in os.listdir(str(root)):
        candidate = os.path.join(str(root), name)
        if _TEMPORARY_LINK.match(name) and is_link(candidate):
            os.unlink(candidate)
    protected = {name for name in keep if name}
    current = state(root)
    if current["kind"] == "link":
        protected.add(current["name"])
    removed = []
    for name in sorted(os.listdir(parent)):
        candidate = os.path.join(parent, name)
        if name.startswith(TRASH):
            _remove_tree(candidate)
            continue
        if name in protected or not (valid_id(name) or _LEGACY.match(name)) or _runs(candidate):
            continue
        trash = os.path.join(parent, f"{TRASH}{name}-{os.getpid()}")
        os.rename(candidate, trash)
        if not is_link(trash):
            try:
                os.unlink(os.path.join(trash, RECORD))
            except FileNotFoundError:
                pass
        _remove_tree(trash)
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
