"""What the landing page tells about this installation (#188), for ``GET /ui/api/overview``.

The version, links to the repository the installation came from (its ``origin``, credentials
stripped), the embedding model, the counts of agents, rules, skills, implants and flows, the
directories with their sizes, and which AI apps have an Agents-Core entry in their
configuration. Sizes are measured without following symlinks and kept for ``CACHE_SECONDS``;
the daemon calls ``read`` off the event loop. No answer holds a token or a header value.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
import time
from pathlib import Path

CACHE_SECONDS = 300
INSTALL_ROOT = Path(__file__).resolve().parents[2]
APPS = ("claude-code", "claude-desktop", "codex", "cursor")
_SCP = re.compile(r"[^@/\s]+@([^:/\s]+):(.+)")
_URL = re.compile(r"[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.+)", re.I)
_PART = re.compile(r"[A-Za-z0-9._-]+")


def web_url(origin: str) -> str | None:
    """``https://host/owner/name`` for a git remote; None for one that is not a plain host path.

    Credentials, a port and ``.git`` are dropped, so no token in ``origin`` reaches the page.
    """
    origin = origin.strip()
    match = _SCP.fullmatch(origin) or _URL.fullmatch(origin)
    if not match:
        return None
    host, path = match.groups()
    parts = re.sub(r"\.git/?$", "", path.strip("/")).split("/")
    if not _PART.fullmatch(host) or not all(_PART.fullmatch(part) for part in parts):
        return None
    return f"https://{host.lower()}/" + "/".join(parts)


def repository(root: Path = INSTALL_ROOT) -> dict | None:
    """Links to the installation's repository: its page and, on GitHub, the README, docs and issues."""
    try:
        result = subprocess.run(["git", "-C", str(root), "config", "--get", "remote.origin.url"],
                                capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    url = web_url(result.stdout) if result.returncode == 0 else None
    if url is None:
        return None
    links = {"repository": url}
    if url.startswith("https://github.com/"):
        links.update(readme=url + "#readme", docs=url + "/blob/HEAD/docs/README.md", issues=url + "/issues")
    return links


def size_of(path: Path) -> int | None:
    """Bytes under ``path`` without following symlinks; None when it does not exist."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return info.st_size
    total = 0
    for current, _folders, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                continue
    return total


def configured_apps(directory: Path, home: Path | None = None) -> dict:
    """Per app, whether a configuration this machine has an Agents-Core entry in, and in which scopes."""
    from .audit import inventory
    found: dict[str, set] = {app: set() for app in APPS}
    try:
        entries = inventory(home=home, directory=directory)
    except (OSError, ValueError):
        entries = []
    for entry in entries:
        if not entry.get("agents_core"):
            continue
        scope = entry.get("scope", "")
        app = ("claude-desktop" if scope == "desktop" else "claude-code" if scope.startswith("claude:")
               else "codex" if scope.startswith("codex:") else "cursor" if scope.startswith("cursor:") else None)
        if app:
            found[app].add(scope.split(":local:")[0])
    return {app: {"configured": bool(scopes), "scopes": sorted(scopes)} for app, scopes in found.items()}


def counts() -> dict:
    from src.component_catalog import list_agents, list_components
    from src.user_flows import FlowLibrary
    library = FlowLibrary()
    listing = library.list("all")
    flows = {"builtin": 0, "personal": 0, "repository": 0}
    for flow in listing.get("flows", []):
        if flow.get("source") == "builtin":
            flows["builtin"] += 1
        elif flow.get("source") == "user":
            flows["personal"] += 1
    flows["repository"] = sum(len(group.get("flows", [])) for group in library.repositories())
    return {"agents": len(list_agents()), "rules": len(list_components("rules")),
            "skills": len(list_components("skills")), "implants": len(list_components("implants")),
            "flows": flows}


def model() -> dict:
    from src.engine.config import EMBEDDING_MODEL, FASTEMBED_CACHE_DIR
    from src.engine.embedding_prompts import local_copy
    copy = local_copy(EMBEDDING_MODEL, FASTEMBED_CACHE_DIR)
    if copy and os.path.isdir(copy):
        size = size_of(Path(copy))
    else:
        suffix = EMBEDDING_MODEL.split("/")[-1]
        try:
            folders = [Path(FASTEMBED_CACHE_DIR) / name for name in os.listdir(FASTEMBED_CACHE_DIR)
                       if name.startswith("models--") and name.endswith(suffix)]
        except OSError:
            folders = []
        sizes = [size_of(folder) for folder in folders]
        size = sum(value for value in sizes if value) if folders else None
    return {"name": EMBEDDING_MODEL, "size": size}


def directories(service_directory: Path) -> list[dict]:
    from src.engine.config import FASTEMBED_CACHE_DIR
    from src.user_flows import FlowLibrary
    places = [("installation", "Installation", INSTALL_ROOT),
              ("data", "Vector stores (data/)", INSTALL_ROOT / "data"),
              ("service", "Service state and service.log", Path(service_directory)),
              ("personal_flows", "Personal flows", FlowLibrary().user_dir),
              ("model_cache", "Model cache", Path(FASTEMBED_CACHE_DIR)),
              ("logs", "Logs of stdio servers", INSTALL_ROOT / "logs")]
    return [{"id": key, "label": label, "path": str(path), "size": size_of(path)} for key, label, path in places]


class Overview:
    """The overview. Sizes are measured at most once per ``CACHE_SECONDS``, one measurement at a time;
    the rest is read with each request. ``home`` is the user's home, where the apps' configurations are."""

    def __init__(self, service, clock=time.monotonic, home: Path | None = None):
        self.service = service
        self.clock = clock
        self.home = home
        self._lock = threading.Lock()
        self._sizes: tuple[float, dict] | None = None

    def read(self) -> dict:
        from src.version import agents_core_version
        with self._lock:
            if self._sizes is None or self.clock() - self._sizes[0] >= CACHE_SECONDS:
                self._sizes = (self.clock(), {"model": model(), "directories": directories(self.service.directory)})
            sizes = self._sizes[1]
        return {"version": agents_core_version(), "repository": repository(), "counts": counts(),
                "apps": configured_apps(self.service.directory, self.home), **sizes}
