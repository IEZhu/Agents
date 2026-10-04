"""Personal flows as Markdown files in the installation's git-ignored ``flows/.user``.

Layout (``AGENTS_USER_FLOWS_DIR`` overrides the root)::

    flows/.user/
      common/<id>.md                 user:<id>, visible in every repository
      common/<id>.meta.json          present only when <id> overrides builtin:<id>
      repos/<repo-key>/<id>.md       repo:<id>, visible only in that repository
      repos/<repo-key>/.repo.json    the group's normalized origin (null: the key
                                     comes from this machine's path)
      repos/<repo-key>/.repo.local.json  this machine's clone path; never shared
      personas/<scope dir>/<id>.json personal agent/component choice for any flow,
                                     builtin/ included (see src.flow_persona)
      .history/<scope dir>/<id>/<UTC timestamp>-<revision>[-deleted].md
      .agents-library.json, .gitignore, .gitattributes
                                     library marker and git settings (src.user_library)

Git ignores every dot-directory in this repository, so nothing here can dirty a
branch, and fast-forward updates of the installation leave it untouched.
Writes are atomic (temporary file + rename) under one lock; every overwrite or
deletion keeps the previous text in ``.history``. Updates carry the revision the
editor started from, so concurrent edits from chat and the browser conflict
instead of overwriting each other. After each write the paths it changed go to the
listeners of ``src.user_library``, also when it failed part way.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from src import flow_persona, user_library
from src.file_lock import file_lock
from src.flows import (FLOW_ID, MAX_FLOW_BYTES, Flow, FlowCatalog, FlowError,
                       flow_title, read_flow)
from src.user_library import REPO_LOCAL, REPO_META, atomic_write as _atomic_write

SCOPES = ("builtin", "user", "repo")
_REFERENCE = re.compile(r"(?:(builtin|user|repo):)?(?:flows/)?([a-z0-9]+(?:-[a-z0-9]+)*)(?:\.md)?")
_REPO_KEY = re.compile(r"[a-z0-9_][a-z0-9._-]*")  # what repo_key() can generate
_VERSION = re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{12}(?:-deleted)?")


def parse_reference(name: str) -> tuple[str | None, str]:
    """Split ``[scope:]id`` (also ``id.md`` and ``flows/id.md``)."""
    match = _REFERENCE.fullmatch(name or "")
    if not match or match[2] == "readme":
        raise FlowError("flow_invalid: use a flow ID from list_flows")
    return match[1], match[2]


def normalize_origin(url: str) -> str | None:
    """``git@host:Owner/Repo.git`` and ``https://user@host/Owner/Repo`` -> ``host/owner/repo``."""
    url = url.strip()
    scp = re.fullmatch(r"[^@/\s]+@([^:/\s]+):(.+)", url)
    if scp:
        host, path = scp.groups()
    else:
        match = re.fullmatch(r"[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.+)", url, re.I)
        if not match:
            return None
        host, path = match.groups()
    path = re.sub(r"\.git/?$", "", path.strip("/"))
    return f"{host}/{path}".lower() if path else None


def _origin(root: Path) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), "config", "--get", "remote.origin.url"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return normalize_origin(result.stdout) if result.returncode == 0 else None


def repo_key(root: Path) -> tuple[str, str | None]:
    """A stable directory name: the normalized origin, else the folder name plus a path hash.

    Clones of the same remote share their repository flows.
    """
    origin = _origin(root)
    if origin:
        key = re.sub(r"[^a-z0-9._-]+", "-", origin).strip("-.")
        if len(key) > 96:
            key = f"{key[:80]}-{hashlib.sha256(origin.encode()).hexdigest()[:8]}"
        return key, origin
    name = re.sub(r"[^a-z0-9._-]+", "-", root.name.lower()).strip("-.") or "repo"
    return f"{name[:60]}-{hashlib.sha256(str(root).encode()).hexdigest()[:8]}", None


def _validate_content(content: str) -> bytes:
    if not isinstance(content, str) or not content.strip():
        raise FlowError("flow_invalid: flow is empty")
    raw = content.encode("utf-8")
    if len(raw) > MAX_FLOW_BYTES:
        raise FlowError("flow_invalid: flow exceeds 256 KiB")
    return raw


def _write_if_changed(path: Path, data: bytes) -> bool:
    """Write ``data`` unless the file already holds exactly it; True when it wrote."""
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    _atomic_write(path, data)
    return True


def _remove(path: Path) -> bool:
    """Delete ``path`` (a symlink itself, never its target); False when it was already gone."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _json_bytes(value) -> bytes:
    return json.dumps(value, indent=2).encode() + b"\n"


def _read_json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def repo_group_meta(directory: Path) -> dict:
    """``origin``, ``path`` and ``local`` of one ``repos/<repo-key>`` group.

    ``.repo.json`` holds only the normalized origin, the same on every machine;
    ``.repo.local.json`` holds this machine's clone path. A group without an
    origin has a key made from that path, so it is ``local``: it means nothing on
    another machine. A ``.repo.json`` written before the split also held ``path``,
    which is still read.
    """
    shared = _read_json_object(directory / REPO_META)
    machine = _read_json_object(directory / REPO_LOCAL)
    origin = shared.get("origin") if isinstance(shared.get("origin"), str) else None
    path = next((value for value in (machine.get("path"), shared.get("path")) if isinstance(value, str)), None)
    return {"origin": origin, "path": path, "local": origin is None}


class FlowLibrary:
    """Built-in, personal (``user:``) and per-repository (``repo:``) flows."""

    def __init__(self, catalog: FlowCatalog | None = None, *, user_dir: str | Path | None = None,
                 repo_root: str | Path | None = None, repo_error: str | None = None,
                 repo_key: str | None = None):
        self.catalog = catalog or FlowCatalog()
        configured = user_dir or os.environ.get("AGENTS_USER_FLOWS_DIR")
        self.user_dir = Path(configured).expanduser().resolve() if configured \
            else self.catalog.directory / ".user"
        self.repo_root = Path(repo_root).resolve() if repo_root else None
        self.repo_error = repo_error if self.repo_root is None else None
        self._repo = None
        # An existing repository's flows can be reached by key alone (the web UI lists
        # every key); only creating one needs a workspace to derive the key from.
        if repo_key is not None and not (isinstance(repo_key, str) and _REPO_KEY.fullmatch(repo_key)):
            raise FlowError("flow_invalid: unknown repository")
        self._key = repo_key if self.repo_root is None else None
        if self._key:
            known = self.user_dir / "repos" / self._key
            if known.is_symlink() or not known.is_dir():
                raise FlowError("flow_not_found: unknown repository")

    # --- locations -----------------------------------------------------------------

    def _stored_origin(self, key: str) -> str | None:
        return repo_group_meta(self.user_dir / "repos" / key)["origin"]

    def repo(self) -> tuple[str, str | None]:
        if self.repo_root is None and self._key:
            return self._key, self._stored_origin(self._key)
        if self.repo_root is None:
            raise FlowError(f"repo_scope_unavailable: {self.repo_error or 'workspace_required'}")
        if self._repo is None:
            self._repo = repo_key(self.repo_root)
        return self._repo

    def _relative(self, scope: str) -> Path:
        if scope == "user":
            return Path("common")
        if scope == "repo":
            return Path("repos") / self.repo()[0]
        raise FlowError("flow_read_only: builtin flows are tracked in git; "
                        "save a local copy with scope user or repo and override=true")

    def _directory(self, scope: str) -> Path:
        directory = (self.user_dir / self._relative(scope))
        resolved = directory.resolve()
        if not resolved.is_relative_to(self.user_dir):
            raise FlowError("flow_invalid: user flow directory escapes the library")
        return resolved

    def _history(self, scope: str, flow_id: str) -> Path:
        return self.user_dir / ".history" / self._relative(scope) / flow_id

    def _persona_dir(self, scope: str) -> Path:
        """``personas/<scope dir>``, relative to the library."""
        return Path("personas") / (Path("builtin") if scope == "builtin" else self._relative(scope))

    def _persona_path(self, scope: str, flow_id: str) -> Path:
        directory = self.user_dir / self._persona_dir(scope)
        try:
            inside = directory.resolve().is_relative_to(self.user_dir.resolve())
        except (OSError, RuntimeError) as error:  # e.g. a symlink loop
            raise FlowError(f"flow_invalid: persona directory cannot be resolved ({error})") from None
        if not inside:
            raise FlowError("flow_invalid: persona directory escapes the library")
        return directory / f"{flow_id}.json"

    def _persona(self, scope: str, flow_id: str, content: str) -> tuple:
        """``(persona, source)``: the personal overlay, else the frontmatter default."""
        path = self._persona_path(scope, flow_id)
        if path.is_symlink():
            raise FlowError("flow_invalid: persona overlays cannot be symlinks")
        try:
            with path.open(encoding="utf-8") as stream:
                overlay = json.load(stream)
        except FileNotFoundError:
            spec = flow_persona.declared(content)
            return spec, "frontmatter" if spec else None
        except (OSError, ValueError) as error:
            raise FlowError(f"flow_invalid: unreadable persona overlay ({error})") from None
        if not isinstance(overlay, dict) or "persona" not in overlay:
            raise FlowError("flow_invalid: the persona overlay must hold a persona field")
        return flow_persona.normalize(overlay["persona"]), "overlay"

    def _with_persona(self, scope: str, flow_id: str, flow: Flow) -> Flow:
        """A persona error stays visible on the flow, which can still be listed and
        repaired with ``set_persona``; ``run_flow`` refuses to run it."""
        try:
            spec, source = self._persona(scope, flow_id, flow.content)
            details = (("persona", spec), ("persona_source", source))
        except FlowError as error:
            details = (("persona", None), ("persona_source", None), ("persona_error", str(error)))
        details = flow.details + details
        return Flow(flow.id, flow.title, flow.source_path, flow.revision, flow.content,
                    flow.source, details)

    # --- reading -------------------------------------------------------------------

    def _meta(self, scope: str, flow_id: str) -> dict:
        try:
            with (self._directory(scope) / f"{flow_id}.meta.json").open(encoding="utf-8") as stream:
                meta = json.load(stream)
            return meta if isinstance(meta, dict) else {}
        except (OSError, ValueError):
            return {}

    def _load(self, scope: str, flow_id: str) -> Flow:
        if scope == "builtin":
            flow = self.catalog.load(flow_id)
            return self._with_persona(scope, flow_id, Flow(
                f"builtin:{flow_id}", flow.title, flow.source_path, flow.revision,
                flow.content, "builtin"))
        directory = self._directory(scope)
        path = directory / f"{flow_id}.md"
        if path.is_symlink():
            raise FlowError("flow_invalid: user flows cannot be symlinks")
        details = []
        meta = self._meta(scope, flow_id)
        if meta.get("overrides") == f"builtin:{flow_id}":
            details.append(("overrides", meta["overrides"]))
            try:
                upstream = self.catalog.load(flow_id).revision
                details.append(("upstream_changed", upstream != meta.get("base_revision")))
            except FlowError:
                details.append(("upstream_changed", True))
        if scope == "repo":
            details.append(("repo", self.repo()[1] or str(self.repo_root or self.repo()[0])))
        return self._with_persona(scope, flow_id, read_flow(
            directory, flow_id, flow_ref=f"{scope}:{flow_id}", source=scope,
            details=tuple(details)))

    def _exists(self, scope: str, flow_id: str) -> bool:
        if scope == "builtin":
            return flow_id in self.catalog.ids()
        try:
            return (self._directory(scope) / f"{flow_id}.md").is_file()
        except FlowError:
            return False

    def resolve(self, name: str) -> Flow:
        """A qualified reference, or a bare ID resolved as ``repo:`` > ``user:`` > ``builtin:``.

        A personal flow can only reuse a built-in ID when it was saved with
        ``override=True``, so a bare name never silently switches to an unrelated flow.
        """
        scope, flow_id = parse_reference(name)
        if scope:
            return self._load(scope, flow_id)
        for candidate in ("repo", "user", "builtin"):
            if self._exists(candidate, flow_id):
                return self._load(candidate, flow_id)
        raise FlowError("flow_not_found: use list_flows to discover available flows")

    def _ids(self, scope: str) -> list[str]:
        try:
            directory = self._directory(scope)
        except FlowError:
            return []
        if not directory.is_dir():
            return []
        return [path.stem for path in sorted(directory.glob("*.md"))
                if FLOW_ID.fullmatch(path.stem) and not path.is_symlink()]

    def list(self, scope: str = "all", *, with_content: bool = False) -> dict:
        if scope not in ("all", *SCOPES):
            raise FlowError("flow_invalid: scope must be all, builtin, user or repo")
        flows, issues = [], []
        wanted = SCOPES if scope == "all" else (scope,)
        repo_info = None
        if "repo" in wanted:
            try:
                key, origin = self.repo()
                repo_info = {"status": "available", "key": key, "origin": origin,
                             "path": str(self.repo_root) if self.repo_root else None}
            except FlowError as error:
                repo_info = {"status": "unavailable", "error": str(error)}
        effective = {}
        for current in ("builtin", "user", "repo"):
            if current not in wanted or (current == "repo" and repo_info["status"] != "available"):
                continue
            ids = self.catalog.ids() if current == "builtin" else self._ids(current)
            for flow_id in ids:
                try:
                    flow = self._load(current, flow_id)
                    entry = flow.metadata()
                    if with_content:
                        entry["content"] = flow.content
                except FlowError as error:
                    issues.append({"id": f"{current}:{flow_id}", "error": str(error)})
                    continue
                if current == "builtin":
                    entry["id"] = flow_id  # Bare built-in IDs stay valid for existing callers.
                    entry["qualified_id"] = f"builtin:{flow_id}"
                flows.append(entry)
                effective[flow_id] = entry
        # Mark built-ins that a bare name no longer reaches.
        for entry in flows:
            if entry["source"] == "builtin":
                winner = effective.get(entry["id"])
                if winner is not entry:
                    entry["overridden_by"] = winner["id"]
        result = {"status": "success", "flows": flows}
        if repo_info:
            result["repo"] = repo_info
        if issues:
            result["issues"] = issues
        return result

    def repositories(self, *, with_content: bool = False) -> list[dict]:
        """Every repository key that holds flows, with a display label and its flows.

        The label is the stored origin, else this machine's path, else the key.
        ``local`` marks a group whose key comes from that path (see ``repo_group_meta``).
        """
        root = self.user_dir / "repos"
        groups = []
        if not root.is_dir():
            return groups
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.is_symlink() or not _REPO_KEY.fullmatch(directory.name):
                continue
            meta = repo_group_meta(directory)
            library = FlowLibrary(self.catalog, user_dir=self.user_dir, repo_key=directory.name)
            listing = library.list("repo", with_content=with_content)
            flows = [dict(entry, repo_key=directory.name) for entry in listing["flows"]]
            if not flows and not listing.get("issues"):
                continue
            label = meta["origin"] or meta["path"] or directory.name
            groups.append({"key": directory.name, "label": str(label),
                           "origin": meta["origin"], "path": meta["path"], "local": meta["local"],
                           "flows": flows, "issues": listing.get("issues", [])})
        groups.sort(key=lambda group: group["label"].lower())
        return groups

    def history(self, scope: str, flow_id: str) -> list[dict]:
        if scope == "builtin":
            return []
        directory = self._history(scope, flow_id)
        if not directory.is_dir():
            return []
        versions = []
        for path in sorted(directory.glob("*.md"), reverse=True):
            if _VERSION.fullmatch(path.stem):
                versions.append({"version": path.stem, "revision_prefix": path.stem.split("-")[1],
                                 "deleted": path.stem.endswith("-deleted")})
        return versions

    def get(self, name: str, version: str | None = None) -> dict:
        scope, flow_id = parse_reference(name)
        if version:
            if not _VERSION.fullmatch(version):
                raise FlowError("flow_invalid: unknown version")
            if scope is None:
                scope = next((s for s in ("repo", "user") if self._history_has(s, flow_id, version)), None)
            if scope in (None, "builtin"):
                raise FlowError("flow_not_found: version not found")
            path = self._history(scope, flow_id) / f"{version}.md"
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise FlowError("flow_not_found: version not found") from None
            return {"status": "success", "flow": {"id": f"{scope}:{flow_id}", "source": scope,
                    "title": flow_title(content, flow_id), "version": version,
                    "revision": hashlib.sha256(content.encode()).hexdigest()},
                    "content": content, "history": self.history(scope, flow_id)}
        flow = self._load(scope, flow_id) if scope else self.resolve(flow_id)
        result = {"status": "success", "flow": flow.metadata(), "content": flow.content,
                  "history": self.history(flow.source, flow_id)}
        if flow.source != "builtin" and self._exists("builtin", flow_id):
            upstream = self.catalog.load(flow_id)
            result["upstream"] = {"id": f"builtin:{flow_id}", "revision": upstream.revision,
                                  "content": upstream.content}
        return result

    def _history_has(self, scope: str, flow_id: str, version: str) -> bool:
        try:
            return (self._history(scope, flow_id) / f"{version}.md").is_file()
        except FlowError:
            return False

    # --- writing -------------------------------------------------------------------

    def _archive(self, scope: str, flow_id: str, raw: bytes, *, deleted: bool = False) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        version = f"{stamp}-{hashlib.sha256(raw).hexdigest()[:12]}" + ("-deleted" if deleted else "")
        _atomic_write(self._history(scope, flow_id) / f"{version}.md", raw)
        return version

    def _history_relative(self, scope: str, flow_id: str, version: str) -> str:
        return (Path(".history") / self._relative(scope) / flow_id / f"{version}.md").as_posix()

    def _write_repo_meta(self, directory: Path, relative: Path, changed: list[str]) -> None:
        """Keep the origin alone in ``.repo.json`` and this machine's path in ``.repo.local.json``.

        A save through a workspace records both. A save by repository key (the web
        editor) only moves the ``path`` out of a ``.repo.json`` written before the split.
        Each written file is added to ``changed`` as soon as it is written.
        """
        if self.repo_root is not None:
            origin, path = self.repo()[1], str(self.repo_root)
        elif "path" in _read_json_object(directory / REPO_META):
            meta = repo_group_meta(directory)
            origin, path = meta["origin"], meta["path"]
        else:
            return
        # The local path first: once .repo.json loses `path`, only .repo.local.json has it.
        if path is not None and _write_if_changed(directory / REPO_LOCAL, _json_bytes({"path": path})):
            changed.append((relative / REPO_LOCAL).as_posix())
        if _write_if_changed(directory / REPO_META, _json_bytes({"origin": origin})):
            changed.append((relative / REPO_META).as_posix())

    def _current(self, scope: str, flow_id: str) -> tuple[Path, bytes | None]:
        path = self._directory(scope) / f"{flow_id}.md"
        if path.is_symlink():
            raise FlowError("flow_invalid: user flows cannot be symlinks")
        try:
            return path, path.read_bytes()
        except FileNotFoundError:
            return path, None

    def _check_revision(self, current: bytes | None, expected: str | None) -> None:
        actual = hashlib.sha256(current).hexdigest() if current is not None else None
        if actual != (expected or None):
            state = f"current revision is {actual}" if actual else "the flow does not exist"
            raise FlowError(f"flow_conflict: {state}; reload it and apply the change again")

    def save(self, name: str, content: str, *, scope: str | None = None,
             expected_revision: str | None = None, override: bool = False) -> dict:
        """Create (``expected_revision=None``) or update a ``user:`` or ``repo:`` flow."""
        named_scope, flow_id = parse_reference(name)
        scope = named_scope or scope or "user"
        if scope not in ("user", "repo"):
            self._relative(scope)  # Raises the read-only error for built-ins.
        raw = _validate_content(content)
        flow_persona.check_known(flow_persona.declared(content))
        builtin = self._exists("builtin", flow_id)
        if builtin and not override:
            raise FlowError(f"flow_shadows_builtin: builtin:{flow_id} exists; pass override=true "
                            "to use a local copy instead of it, or choose another ID")
        if override and not builtin:
            raise FlowError(f"flow_invalid: override=true needs an existing builtin:{flow_id}")
        directory = self._directory(scope)
        relative = self._relative(scope)
        self.user_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        changed = []
        try:
            with file_lock(self.user_dir / ".lock"):
                path, current = self._current(scope, flow_id)
                self._check_revision(current, expected_revision)
                if current != raw:
                    if current is not None:
                        changed.append(self._history_relative(scope, flow_id, self._archive(scope, flow_id, current)))
                    _atomic_write(path, raw)
                    changed.append((relative / path.name).as_posix())
                # Update the override marker even when the text is unchanged.
                meta_path = directory / f"{flow_id}.meta.json"
                if override:
                    # Saving an override acknowledges the built-in text it was edited against.
                    meta = {"overrides": f"builtin:{flow_id}",
                            "base_revision": self.catalog.load(flow_id).revision}
                    if _write_if_changed(meta_path, _json_bytes(meta)):
                        changed.append((relative / meta_path.name).as_posix())
                elif _remove(meta_path):
                    # A plain save is not a copy of a built-in any more (e.g. the built-in was removed).
                    changed.append((relative / meta_path.name).as_posix())
                if scope == "repo":
                    self._write_repo_meta(directory, relative, changed)
                changed += user_library.ensure_root_files(self.user_dir)
        finally:  # also after a partial failure: listeners learn what already changed
            user_library.notify(self.user_dir, changed)
        if current == raw:
            return {"status": "unchanged", "flow": self._load(scope, flow_id).metadata()}
        return {"status": "created" if current is None else "saved",
                "flow": self._load(scope, flow_id).metadata()}

    def delete(self, name: str, *, expected_revision: str, scope: str | None = None) -> dict:
        named_scope, flow_id = parse_reference(name)
        scope = named_scope or scope
        if scope is None:
            scope = next((s for s in ("repo", "user") if self._exists(s, flow_id)), "user")
        self._relative(scope)
        persona_path = self._persona_path(scope, flow_id)  # Validated before any change.
        if persona_path.is_dir() and not persona_path.is_symlink():
            raise FlowError(f"flow_invalid: {persona_path} is a directory; remove it, then delete the flow")
        relative = self._relative(scope)
        changed = []
        try:
            with file_lock(self.user_dir / ".lock"):
                path, current = self._current(scope, flow_id)
                if current is None:
                    raise FlowError("flow_not_found: use list_flows to discover available flows")
                self._check_revision(current, expected_revision)
                version = self._archive(scope, flow_id, current, deleted=True)
                changed.append(self._history_relative(scope, flow_id, version))
                path.unlink()
                changed.append((relative / path.name).as_posix())
                meta_path = self._directory(scope) / f"{flow_id}.meta.json"
                for removed, name in ((meta_path, (relative / meta_path.name).as_posix()),
                                      (persona_path, (self._persona_dir(scope) / persona_path.name).as_posix())):
                    if _remove(removed):
                        changed.append(name)
                changed += user_library.ensure_root_files(self.user_dir)
        finally:
            user_library.notify(self.user_dir, changed)
        return {"status": "deleted", "id": f"{scope}:{flow_id}", "version": version}

    def set_persona(self, name: str, persona, *, reset: bool = False) -> dict:
        """Choose the agent and components for one flow, for this user only.

        ``persona`` replaces the flow's frontmatter declaration (``None`` runs it
        without a persona); ``reset=True`` removes the choice so the frontmatter
        applies again. Works for built-in flows without copying their text.
        A bare name selects the flow a bare ``run_flow`` would run. The flow's
        current persona is not parsed first, so a broken one can be replaced.
        """
        scope, flow_id = parse_reference(name)
        candidates = (scope,) if scope else ("repo", "user", "builtin")
        scope = next((c for c in candidates if self._exists(c, flow_id)), None)
        if scope is None:
            raise FlowError("flow_not_found: use list_flows to discover available flows")
        spec = None
        if not reset:
            spec = flow_persona.normalize(persona)
            flow_persona.check_known(spec)
        path = self._persona_path(scope, flow_id)
        relative = (self._persona_dir(scope) / path.name).as_posix()
        self.user_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        changed = []
        try:
            with file_lock(self.user_dir / ".lock"):
                # A delete may have finished meanwhile: never leave an orphan overlay
                # that a recreated flow would inherit.
                if not self._exists(scope, flow_id):
                    raise FlowError("flow_not_found: use list_flows to discover available flows")
                if path.is_dir() and not path.is_symlink():
                    # Never delete unknown contents recursively.
                    raise FlowError(f"flow_invalid: {path} is a directory; remove it manually")
                if reset:
                    if _remove(path):  # Removes a symlink itself, never its target.
                        changed.append(relative)
                elif path.is_symlink():
                    raise FlowError("flow_invalid: persona overlays cannot be symlinks")
                elif _write_if_changed(path, _json_bytes({"persona": spec})):
                    changed.append(relative)
                changed += user_library.ensure_root_files(self.user_dir)
        finally:
            user_library.notify(self.user_dir, changed)
        return {"status": "reset" if reset else "saved",
                "flow": self._load(scope, flow_id).metadata()}
