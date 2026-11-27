import logging
import os
import sys
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from src.runtime_paths import installation_root, runtime_root

logger = logging.getLogger(__name__)

ENGINE_DIR = os.path.dirname(__file__)

# --- Install root ------------------------------------------------------------
# Where the Agents-Core content ships: agents/, skills/, implants/, capabilities/
# registry, and vector indexes keyed against those files (router_cache,
# skills_store, implants_store, .router_cache_model). Shared across all client
# repos that reach this install through a global MCP registration.
INSTALL_ROOT = str(installation_root())
INSTALL_DATA_DIR = os.path.join(INSTALL_ROOT, "data")
AGENTS_DIR = os.path.join(INSTALL_ROOT, "agents")
SKILLS_DIR = os.path.join(INSTALL_ROOT, "skills")
IMPLANTS_DIR = os.path.join(INSTALL_ROOT, "implants")
RULES_DIR = os.path.join(INSTALL_ROOT, "rules")
FLOWS_DIR = os.path.join(INSTALL_ROOT, "flows")

# --- Client repo root (per-session, per-repo memory artifacts) ---------------
# Where the serving MCP session's journal lives: history.md, history/ archive,
# managed CLAUDE.md section, data/memory/.describe_hash, and stdio AGENTS_DEBUG
# logs. History vector indexes live in private daemon state (HTTP) or the
# installation's leased data/stdio/ slot, not in the client repo. Resolved
# lazily so a single install serving many client repos keeps their memory
# isolated (issue #36).

_CLIENT_ROOT_MARKERS = (".git", "CLAUDE.md", "AGENTS.md")


class ClientRootError(RuntimeError):
    """No safe client repo root can be inferred for per-repo memory or flows.

    `code` is the protocol error code: `workspace_required` when no project
    could be inferred, `workspace_unsafe` when the candidate is a system or
    home directory.
    """

    def __init__(self, message: str, code: str = "workspace_required"):
        super().__init__(message)
        self.code = code


def _find_marker_upwards(start: Path) -> Optional[Path]:
    """Walk up from *start* until a directory containing any of
    `_CLIENT_ROOT_MARKERS` is found. Returns that directory, or None."""
    try:
        current = start.resolve(strict=False)
    except OSError:
        return None
    profiles = {Path(os.environ.get(name) or Path.home() / default).expanduser().resolve()
                for name, default in (("CODEX_HOME", ".codex"), ("CLAUDE_CONFIG_DIR", ".claude"))}
    for candidate in (current, *current.parents):
        if _unsafe_client_root_reason(candidate) is not None:
            break
        if any((candidate / marker).exists() for marker in _CLIENT_ROOT_MARKERS
               if candidate not in profiles or marker not in ("AGENTS.md", "CLAUDE.md")):
            return candidate
    return None


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_directory() -> Optional[Path]:
    """`%SystemRoot%` (normally C:\\Windows) on Windows, otherwise None."""
    if not _is_windows():
        return None
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir")
    return Path(os.path.realpath(system_root)) if system_root else None


def _real(path: Optional[str]) -> Optional[Path]:
    return Path(os.path.realpath(path)) if path else None


def _inside(root: Path, base: Path) -> bool:
    """`root` is `base` or below it; case-insensitive on macOS, whose volumes are."""
    if sys.platform == "darwin":
        root, base = Path(str(root).casefold()), Path(str(base).casefold())
    return root.is_relative_to(base)


def _home_directory() -> Optional[Path]:
    try:
        return _real(os.path.expanduser("~"))
    except (OSError, RuntimeError):
        return None


# POSIX directories that hold no project themselves. Exact entries refuse only
# the directory itself (projects below them are fine); subtree entries refuse
# everything under them. /private/tmp, /private/var/folders (macOS TMPDIR and
# pytest tmp_path) and /usr/local stay allowed.
_POSIX_UNSAFE_EXACT = (
    "/usr", "/var", "/private", "/private/var", "/Users", "/home", "/Library",
)
_POSIX_UNSAFE_SUBTREES = (
    "/System", "/bin", "/sbin", "/etc", "/private/etc", "/private/var/db",
    "/private/var/root", "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/libexec",
    "/usr/share",
)


def _unsafe_client_root_reason(root: Path) -> Optional[str]:
    """Why an inferred *root* must not be used as a client repo, or None.

    Clients may start stdio servers outside any project. The Claude desktop
    app starts them in C:\\Windows\\System32, where an unfiltered administrator
    token let history.md be appended without an error.
    """
    if root.parent == root:
        return "it is a filesystem root"
    windows_dir = _windows_directory()
    if windows_dir is not None and root.is_relative_to(windows_dir):
        return f"it is inside the Windows directory {windows_dir}"
    if _is_windows():
        for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
            base = _real(os.environ.get(name))
            if base is not None and root.is_relative_to(base):
                return f"it is inside %{name}% {base}"
        users = _real(os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Users"))
        if users is not None and root == users:
            return f"it is the users directory {users}"
    else:
        for path in _POSIX_UNSAFE_SUBTREES:
            base = _real(path)
            if base is not None and _inside(root, base):
                return f"it is inside the system directory {path}"
        for path in _POSIX_UNSAFE_EXACT:
            base = _real(path)
            if base is not None and _inside(root, base) and _inside(base, root):
                return f"it is the system directory {path}"
    home = _home_directory()
    if home is not None and _inside(root, home) and _inside(home, root):
        return f"it is the home directory {home}"
    return None


def _unsafe_launch_root_reason(root: Path) -> Optional[str]:
    """A launch directory is a project only outside installation and app runtimes."""
    reason = _unsafe_client_root_reason(root)
    if reason is not None:
        return reason
    for base in (Path(INSTALL_ROOT).resolve(), runtime_root().resolve()):
        if _inside(root, base):
            return f"it is inside the Agents-Core installation/runtime directory {base}"
    parts = tuple(part.casefold() for part in root.parts)
    if any(part.endswith(".app") and child == "contents" for part, child in zip(parts, parts[1:])):
        return "it is inside an application runtime"
    extension_hosts = {".vscode", ".vscode-insiders", ".vscode-server", ".cursor", ".cursor-server"}
    if any(host in extension_hosts and child == "extensions" for host, child in zip(parts, parts[1:])):
        return "it is inside an editor extension runtime"
    return None


def get_client_repo_root(*, allow_install_fallback: bool = True) -> str:
    """Resolve the client repo that owns this MCP session's per-repo memory.

    Resolution order:
      1. `AGENTS_CLIENT_REPO_ROOT` env var — authoritative override.
      2. Walk up from the start directory to the nearest directory containing
         `.git` or `CLAUDE.md`.
      3. Use the safe existing session directory itself, including empty folders.

    The start directory is `CLAUDE_PROJECT_DIR` when it names an existing
    directory (Claude Code exports it to the stdio servers it spawns), else
    `os.getcwd()`. Every step raises `ClientRootError` instead of returning a
    filesystem root, a system or home directory (`workspace_unsafe`).

    An unavailable cwd never selects the installation as a project. The legacy
    allow_install_fallback argument is retained for callers' compatibility.
    Memoized for the process lifetime; failures are not cached. Tests reset
    via `_reset_client_repo_root_cache()`. A tool call that names its own
    `workspace` bypasses this process-wide root (`client_root_from_workspace`).
    """
    return get_client_repo_root_info(allow_install_fallback=allow_install_fallback)[0]


def get_client_repo_root_info(*, allow_install_fallback: bool = True) -> tuple[str, str]:
    """Like `get_client_repo_root()`, plus how the root was found.

    The source is `env`, `CLAUDE_PROJECT_DIR`, `cwd` or `install` (fallback for
    a deleted cwd).
    """
    root, used_install_fallback, source = _resolve_client_repo_root()
    if source != "env":
        path = _existing_directory(root, "session workspace")
        if str(path) != root:
            raise ClientRootError("session workspace changed; reconnect the client", code="workspace_invalid")
    if used_install_fallback and not allow_install_fallback:
        raise OSError("workspace_required: cwd unavailable; set AGENTS_CLIENT_REPO_ROOT")
    return root, source


@lru_cache(maxsize=1)
def _resolve_client_repo_root() -> tuple[str, bool, str]:
    """Pin one identity for both memory and flows: (root, install fallback, source)."""
    override = os.environ.get("AGENTS_CLIENT_REPO_ROOT")
    if override:
        resolved = client_root_from_override(override)
        _log_resolution(resolved, "env")
        return resolved, False, "env"

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir and os.path.isdir(project_dir):
        start, source = Path(project_dir), "CLAUDE_PROJECT_DIR"
    else:
        try:
            start, source = Path(os.getcwd()), "cwd"
        except (FileNotFoundError, OSError) as err:
            raise ClientRootError("cwd unavailable; reconnect the client in its workspace") from err

    path = _existing_directory(str(start), source)
    reason = (_unsafe_launch_root_reason(path) if source == "cwd" else _unsafe_client_root_reason(path))
    if reason is not None:
        raise ClientRootError(f"refusing {path} (from {source} {start}) as the client repo root: {reason}",
                              code="workspace_unsafe")

    root = _find_marker_upwards(start)
    if root is None:
        try:
            root = start.resolve()
        except OSError as err:
            raise ClientRootError(f"workspace_invalid: cannot resolve {source}", code="workspace_invalid") from err

    reason = _unsafe_launch_root_reason(root) if source == "cwd" else _unsafe_client_root_reason(root)
    if reason is not None:
        raise ClientRootError(
            f"refusing {root} (from {source} {start}) as the client repo root for "
            f"per-repo memory and flows: {reason}. Start the MCP server in the "
            "project directory, as Claude Code does, or set AGENTS_CLIENT_REPO_ROOT.",
            code="workspace_unsafe",
        )
    _log_resolution(str(root), source)
    return str(root), False, source


def _root_uri_path(uri: str) -> Optional[Path]:
    """Local path of a `file:` root URI; None for another scheme or a remote host."""
    parts = urllib.parse.urlsplit(uri)
    if parts.scheme != "file":
        return None
    if parts.netloc and parts.netloc.lower() != "localhost":
        if not _is_windows():
            return None
        # file://server/share/dir is a UNC path, as Node's pathToFileURL writes it.
        return Path(urllib.request.url2pathname(f"//{parts.netloc}{parts.path}"))
    return Path(urllib.request.url2pathname(parts.path))


def _main_checkout(root: Path) -> Path:
    """The main checkout of the git worktree at *root*, else *root* itself.

    Claude Code gives a worktree session's servers the main checkout as
    CLAUDE_PROJECT_DIR, and a worktree is deleted after its branch lands, so
    its turns belong to the main checkout's history.
    """
    dot_git = root / ".git"
    if not dot_git.is_file():
        return root
    try:
        pointer = dot_git.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir:"):
            return root
        gitdir = root / pointer[len("gitdir:"):].strip()
        # Only a worktree's git directory has commondir; a submodule's does not.
        common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except (OSError, UnicodeDecodeError, ValueError):
        return root
    main = common.parent
    return main if common.name == ".git" and common.is_dir() and main != root else root


def client_root_from_workspace(workspace: str, root_uris: Iterable[str]) -> str:
    """The project of a tool call that names its `workspace` (stdio).

    A server process may serve several sessions: the Claude desktop app starts
    one for all its Code sessions, in C:\\Windows\\System32, and answers
    roots/list with the folders of every open session. The caller names its
    own directory; it must lie inside one of the client's MCP roots, the
    directories the client lets servers work in, so a model cannot point
    memory at an arbitrary repository. The directory then resolves as
    CLAUDE_PROJECT_DIR does (nearest `.git` or `CLAUDE.md` at or above it,
    else itself), a git worktree to its main checkout, and an unsafe result is
    refused. Raises `ClientRootError` (`workspace_invalid` or
    `workspace_unsafe`).
    """
    path = _existing_directory(workspace, "workspace")
    roots = [root.resolve() for root in map(_root_uri_path, root_uris) if root is not None]
    if not any(_inside(path, root) for root in roots):
        named = ", ".join(str(root) for root in roots) or "none"
        raise ClientRootError(
            f"workspace {path} is outside the client's MCP roots ({named})", code="workspace_invalid",
        )
    return _project_root(path, "workspace")


def client_root_from_override(value: str) -> str:
    """The project an `AGENTS_CLIENT_REPO_ROOT` names, used as named after its safety check.

    A stdio server reads it from its environment; the service's bridge reports it (#266).
    """
    resolved = Path(os.path.realpath(os.path.expanduser(value)))
    # Validate before any filesystem side effect: a mistaken `~` or `C:\\`
    # would otherwise collect every project's history.
    reason = _unsafe_client_root_reason(resolved)
    if reason is not None:
        raise ClientRootError(
            f"refusing {resolved} (from AGENTS_CLIENT_REPO_ROOT) as the client repo "
            f"root for per-repo memory and flows: {reason}. Set "
            "AGENTS_CLIENT_REPO_ROOT only to one project's directory, and only "
            "on a per-project registration.",
            code="workspace_unsafe",
        )
    return str(resolved)


def client_root_from_directory(directory: str, *, launch_directory: bool = True) -> str:
    """The project of the directory a client started a session's process in.

    The shared service's bridge (`bridge/stdio.mjs`) reports the
    CLAUDE_PROJECT_DIR or cwd its client gave it, the input a stdio server
    trusts. Empty session directories need no marker; automatic launch inputs
    must additionally stay outside installation and application runtimes.
    """
    path = _existing_directory(directory, "directory")
    reason = _unsafe_launch_root_reason(path) if launch_directory else _unsafe_client_root_reason(path)
    if reason is not None:
        raise ClientRootError(
            f"refusing {path} (from the bridge's cwd) as the client repo root for per-repo "
            f"memory and flows: {reason}", code="workspace_unsafe",
        )
    root = _project_root(path, "directory")
    if launch_directory and (reason := _unsafe_launch_root_reason(Path(root))) is not None:
        raise ClientRootError(f"refusing {root}: {reason}", code="workspace_unsafe")
    return root


def _existing_directory(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ClientRootError(f"{label} {value!r} is not an absolute path", code="workspace_invalid")
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        path = None
    if path is None or not path.is_dir():
        raise ClientRootError(f"{label} {value!r} is not an existing directory", code="workspace_invalid")
    return path


def _project_root(path: Path, label: str) -> str:
    """Nearest `.git` or `CLAUDE.md` at or above *path* (else *path*), a worktree to its main checkout."""
    marked = _find_marker_upwards(path)
    root = _main_checkout(marked if marked is not None else path)
    reason = _unsafe_client_root_reason(root)
    if reason is not None:
        raise ClientRootError(
            f"refusing {root} (from {label} {path}) as the client repo root for "
            f"per-repo memory and flows: {reason}",
            code="workspace_unsafe",
        )
    return str(root)


def _log_resolution(root: str, source: str) -> None:
    """Log the resolved root once per process (the result is memoized)."""
    logger.info("client-repo-root: %s (source=%s, pid=%d)", root, source, os.getpid())


def _reset_client_repo_root_cache() -> None:
    """Clear the memoization of `get_client_repo_root()`. Test-only."""
    _resolve_client_repo_root.cache_clear()


def get_client_data_dir() -> str:
    """`{client_repo_root}/data` — holds `memory/.describe_hash`.

    `memory/` is also the legacy default `HistoryStore` data directory; the
    server passes its own directory in daemon state or the stdio slot.
    """
    return os.path.join(get_client_repo_root(), "data")


def get_debug_log_dir() -> str:
    """Per-call JSON debug logs (when `AGENTS_DEBUG=1`).

    `{client_repo_root}/logs` for stdio; `debug/` in daemon state over HTTP.
    """
    if os.environ.get("AGENTS_TRANSPORT") == "http":
        from src.daemon.state import state_dir
        return str(state_dir() / "debug")
    return os.path.join(get_client_repo_root(), "logs")


# --- Embedding / model config ------------------------------------------------

# The default lives in src/model_migration.py, which moves installed models to it once.
from src.model_migration import DEFAULT_MODEL as DEFAULT_EMBEDDING_MODEL  # noqa: E402

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "").strip() or DEFAULT_EMBEDDING_MODEL

# fastembed cache — persistent by default (macOS launchd wipes /tmp during long downloads).
# A blank value (the uncommented `FASTEMBED_CACHE_DIR=` line from env.example)
# counts as unset; otherwise os.makedirs("") fails when the model loads.
FASTEMBED_CACHE_DIR = os.path.expanduser(os.getenv("FASTEMBED_CACHE_DIR", "").strip() or "~/.cache/fastembed")


def _float_env(name: str, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Parse a float from an env var, falling back to *default* on bad values or out-of-range."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid value for %s=%r, using default %.2f", name, raw, default)
        return default
    if not (lo <= value <= hi):
        logger.warning("Out-of-range value for %s=%.4f (expected %.1f–%.1f), using default %.2f", name, value, lo, hi, default)
        return default
    return value


def _int_env(name: str, default: int, lo: int = 0, hi: Optional[int] = None) -> int:
    """Parse an int from an env var, falling back to *default* on bad values or out-of-range.

    Mirrors :func:`_float_env`; ``hi=None`` means no upper bound (used for
    timeouts / intervals that have no natural ceiling).
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid value for %s=%r, using default %d", name, raw, default)
        return default
    if value < lo or (hi is not None and value > hi):
        logger.warning("Out-of-range value for %s=%d, using default %d", name, value, default)
        return default
    return value


# Documents per fastembed inference batch. fastembed pads a batch to its longest
# document, so activation memory grows with batch size × 512 tokens: its default
# of 256 sent a whole 163-entry history as one batch and kept ~11 GB (#157).
EMBEDDING_BATCH_SIZE = _int_env("EMBEDDING_BATCH_SIZE", 4, lo=1, hi=256)


def _choice_env(name: str, default: str, choices: tuple[str, ...]) -> str:
    """Read a lower-cased enum env var, falling back to *default* on unknown values.

    Mirrors :func:`_float_env`: a typo or a not-yet-implemented mode must not
    silently run the default while the operator believes a gate is on.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value not in choices:
        logger.warning("Unknown value for %s=%r (expected one of %s), using default %r",
                       name, raw, "|".join(choices), default)
        return default
    return value


ROUTER_SIMILARITY_THRESHOLD = _float_env("ROUTER_SIMILARITY_THRESHOLD", 0.95)

# Keyword boosting: minimum keyword hits to consider overriding a cache decision
KEYWORD_OVERRIDE_MIN_HITS = 1
# Top agent must have >= this ratio vs second-best to auto-override
# (otherwise falls through to ROUTE_REQUIRED for LLM re-evaluation)
KEYWORD_UNIQUENESS_RATIO = 2.0
# On a cache miss ROUTE_REQUIRED lists at most this many agents, universal_agent
# always among them, ranked by SemanticRouter.cold_ranking: by similarity to each
# agent's embedded catalog card once those are built, else by keyword hits, and a
# query no keyword matches then still gets the whole catalog. 0 always lists the
# whole catalog. Every installation starts with an empty cache, so without this each
# cold activation sent all of it (46 agents, about 7,000 characters) (#734).
ROUTER_SHORTLIST_SIZE = _int_env("ROUTER_SHORTLIST_SIZE", 8, lo=0)
if ROUTER_SHORTLIST_SIZE == 1:
    # universal_agent takes one place, so 1 would list no ranked agent at all.
    logger.warning("ROUTER_SHORTLIST_SIZE=1 leaves no place beside universal_agent, using default 8")
    ROUTER_SHORTLIST_SIZE = 8
# Headless profile only: on a cache miss, load the top agent of the embedding ranking
# directly, without the ROUTE_REQUIRED round trip, when its cosine similarity to the
# request leads the runner-up's by at least this much; 0 turns it off. Interactive
# sessions never resolve a miss themselves, and keyword hits alone never resolve one:
# a single incidental keyword can lead ("regression" is a data_analyst keyword, so
# "add a regression test" leads with data_analyst 1:0). The default is provisional:
# the measurement for #734 had only the 72-query Russian labelled set
# (evals/datasets/routing_ru.jsonl), where 0.05 was the stricter of the gaps it
# reported and resolved fewer queries at a higher precision than 0.02; re-measure on
# a larger set before lowering it.
ROUTER_COLD_RESOLVE_MARGIN = _float_env("ROUTER_COLD_RESOLVE_MARGIN", 0.05, lo=0.0, hi=2.0)
# Cosine distance thresholds (1 - similarity). Calibrated for
# paraphrase-multilingual-MiniLM-L12-v2; typical distances:
#   skills  0.39–0.63  → threshold 0.75 (comfortable margin)
#   implants 0.52–0.72 → threshold 0.85 (implant descriptions are more
#                         abstract, so distances run ~0.1 higher)
# With the default harrier-oss-v1-270m (median top-1 skill distance 0.56–0.59)
# and e5-large (0.19), almost every gold skill and implant passes both cutoffs,
# so they rarely gate; measured in issue #160.
SKILLS_RELEVANCE_THRESHOLD = _float_env("SKILLS_RELEVANCE_THRESHOLD", 0.75)
IMPLANTS_RELEVANCE_THRESHOLD = _float_env("IMPLANTS_RELEVANCE_THRESHOLD", 0.85)
MAX_PREFERRED_IMPLANTS = 5
IMPLANTS_DEEP_TIER_DEFAULT = 3

# Implant layer sensitivity, tuned separately from skills
# (docs/layer-sensitivity-plan.md). Measured 2026-09 on the current embedder,
# query-to-implant distances sit in ~0.12-0.24, far below the absolute threshold
# above, so that threshold never gates; the z-score gate compares each implant
# with this query's own distance distribution instead.
#   IMPLANT_INDEX_MODE: "legacy" embeds description + body (the technique text,
#     which matches queries by topic); "triggers" embeds description + user-side
#     `triggers` + "When to Use" (matches by task shape).
#   IMPLANT_GATING: "legacy" = top-N under the absolute threshold; "zscore" =
#     keep only implants at least IMPLANT_GATE_Z standard deviations closer than
#     the query's mean distance, so a query may load none.
# Unknown values warn and fall back to the default (_choice_env).
IMPLANT_INDEX_MODE = _choice_env("IMPLANT_INDEX_MODE", "legacy", ("legacy", "triggers"))
IMPLANT_GATING = _choice_env("IMPLANT_GATING", "legacy", ("legacy", "zscore"))
IMPLANT_GATE_Z = _float_env("IMPLANT_GATE_Z", 1.5, lo=0.0, hi=5.0)
# Distance multiplier when one of the implant's `triggers` occurs in the query.
IMPLANT_TRIGGER_BOOST = _float_env("IMPLANT_TRIGGER_BOOST", 0.85)
# Whether the query needs any implant, decided for the implant layer alone.
#   "off"    — legacy: every standard/deep query loads implants.
#   "intent" — also require classify_intent(query).implant_budget > 0, without
#              letting the classifier change the tier, skills or persona format
#              (INTENT_CLASSIFIER_ENABLED switches all of those at once).
# Per-query enrichment only (server._load_and_enrich, used by the eval harnesses):
# the persona bundle is built once per session, so persona_bundle.py does not apply it.
# Measured on the implant labels (evals/scripts/implant_need_gate.py): utility
# +0.149 [95% CI +0.056, +0.242] vs production, implants per query 2.07 → 1.36.
IMPLANT_NEED_GATE = _choice_env("IMPLANT_NEED_GATE", "off", ("off", "intent"))

# --- Intent classifier (src/engine/intent.py) --------------------------------
# Replaces the length+regex `infer_tier` heuristic with a two-axis TaskProfile.
# Default OFF: the legacy rule stays authoritative until an A/B on
# evals/datasets/routing.jsonl says otherwise (baseline there is 67/110 = 60.9%).
INTENT_CLASSIFIER_ENABLED = os.getenv("INTENT_CLASSIFIER_ENABLED", "0").lower() in ("1", "true")
# Structural points needed to promote a mode's default tier by one step. A mode's
# default tier is a floor and is never lowered; read from env so the A/B can
# sweep the promotion threshold without a code change.
# lo=3 is an invariant, not a taste: length contributes at most 2 points, so any
# threshold below 3 would let size alone promote a tier — the exact defect this
# module replaces.
INTENT_DEEP_AT = _int_env("INTENT_DEEP_AT", 5, lo=3)
# Length buckets. Length contributes at most 2 points, never a tier on its own —
# `len > 300` alone is what drove 35 of 40 bench queries into the deep tier.
INTENT_LONG_CHARS = _int_env("INTENT_LONG_CHARS", 150, lo=1)
INTENT_VERY_LONG_CHARS = _int_env("INTENT_VERY_LONG_CHARS", 500, lo=1)
# A greeting lexicon hit only means "converse" below this length, so a long task
# spec that opens with "Hi" is not misread as small talk.
INTENT_CONVERSE_MAX_CHARS = _int_env("INTENT_CONVERSE_MAX_CHARS", 60, lo=1)

# How long a retrieval tool waits for the background initializer before it
# answers ``warming_up`` (store load, embedding model and rules warmup).
WARMUP_WAIT_SECONDS = _int_env("WARMUP_WAIT_SECONDS", 20, lo=1, hi=600)

SESSION_CACHE_MAX_SIZE = 128
SESSION_CACHE_TTL_SECONDS = 600

# Debug logging — set AGENTS_DEBUG=1 in .env to write per-call JSON files to
# get_debug_log_dir().
AGENTS_DEBUG = os.getenv("AGENTS_DEBUG", "").lower() in ("1", "true")

# Rules layer — universal directives loaded into every enriched prompt.
# Disable with RULES_ENABLED=0 to compare behavior with/without the layer.
RULES_ENABLED = os.getenv("RULES_ENABLED", "1").lower() in ("1", "true")

# --- Client profile (#734) ---------------------------------------------------
# "interactive" (default) keeps today's behaviour. "headless" is for one-shot runs
# with no chat UI and no peer sessions (benchmarks, scheduled runs) whose
# installation already put the managed protocol section in place; there every
# token added early in a run is re-read by each later model call. Changes that
# cut that overhead consult headless_profile() and leave interactive sessions
# unchanged. The setting is per server process: set it in the env of a
# standalone stdio server entry, not in a shared daemon that also serves
# interactive sessions.
CLIENT_PROFILE = _choice_env("AGENTS_CLIENT_PROFILE", "interactive", ("interactive", "headless"))


def headless_profile() -> bool:
    """Whether this server runs the headless client profile.

    Reads the module attribute at call time so tests can monkeypatch
    ``config.CLIENT_PROFILE``.
    """
    return CLIENT_PROFILE == "headless"


# A headless run whose client records each turn itself, through the Claude Code Stop hook
# `python -m src.memory.turn_log_hook` (#734). Logging through log_interaction makes the model
# write its whole answer a second time and adds a final call that re-reads the context. Only
# together with the headless profile does the server tell every client to leave logging to that
# hook, and only then does the hook write; either switch alone changes nothing. An interactive
# Claude Code session needs neither: its hook registration carries `--interactive`, and the server
# decides for that client alone (client_instructions.stop_hook_logs, #701).
TURN_LOG_HOOK = os.getenv("AGENTS_TURN_LOG_HOOK", "").strip().lower() in ("1", "true")


def turn_log_hook() -> bool:
    """Whether the client's Stop hook, not the model, logs each turn.

    True only with ``AGENTS_TURN_LOG_HOOK=1`` and the headless profile; reads the module
    attributes at call time so tests can monkeypatch them.
    """
    return TURN_LOG_HOOK and headless_profile()

# --- Auto-update (background self-update) ------------------------------------
# The server can keep itself current by pulling its own git repo (INSTALL_ROOT)
# in a daemon thread — non-blocking — and preparing the vector stores; the new
# code takes effect at the next start with no active readers. It acts ONLY when the
# checked-out branch is AUTO_UPDATE_BRANCH and the tree is clean, fast-forward
# only. On any other branch (feature branches / local development) it is a
# no-op. Opt out with AGENTS_AUTO_UPDATE=0.
AUTO_UPDATE_ENABLED = os.getenv("AGENTS_AUTO_UPDATE", "1").lower() in ("1", "true")
AUTO_UPDATE_REMOTE = os.getenv("AGENTS_AUTO_UPDATE_REMOTE", "origin")
AUTO_UPDATE_BRANCH = os.getenv("AGENTS_AUTO_UPDATE_BRANCH", "main")
# Seconds allotted to each git CLI/network op (status/fetch/merge/...).
AUTO_UPDATE_GIT_TIMEOUT = _int_env("AGENTS_AUTO_UPDATE_TIMEOUT", 30, lo=1)
# Throttle: skip the network fetch if the last check was within this many
# seconds (stdio servers respawn frequently). 0 disables throttling.
AUTO_UPDATE_MIN_INTERVAL = _int_env("AGENTS_AUTO_UPDATE_INTERVAL", 900, lo=0)
# The reindex subprocess loads the embedding model and re-embeds changed stores.
# The shared daemon's updater runs at the LaunchAgent's Background priority: on
# 2026-10-04 the skills store took about six minutes there (28 s at normal
# priority), and a model switch rebuilding both stores came close to the former
# 600 s. A build that hits the ceiling fails and is retried every run, so on a
# slower machine a short ceiling means an update that never lands; this one only
# stops a hung build.
AUTO_UPDATE_REINDEX_TIMEOUT = _int_env("AGENTS_AUTO_UPDATE_REINDEX_TIMEOUT", 3600, lo=1)
# A target that changes the dependencies gets a new Python environment under .venvs/
# (src/environments.py): `python -m venv` plus `pip install` of its requirements,
# downloads included, while the installation keeps serving. A build past this many
# seconds fails and is retried by the next update.
AUTO_UPDATE_ENVIRONMENT_TIMEOUT = _int_env("AGENTS_AUTO_UPDATE_ENVIRONMENT_TIMEOUT", 1800, lo=1)
# Two-phase staged update (the default). When on, the background daemon does NOT
# mutate the live install: it prepares the new version + freshly-built indexes in
# an isolated git worktree under AUTO_UPDATE_STAGING_DIR and writes a marker; the
# next idle start activates it with a local ff-merge + atomic file move (see
# src/self_update.py, Phase A/B). Set to 0 for synchronous in-place fast-forward
# + reindex at idle startup, also under the exclusive installation lease.
AUTO_UPDATE_STAGING = os.getenv("AGENTS_AUTO_UPDATE_STAGING", "1").lower() in ("1", "true")
# Parent dir for per-sha staging worktrees. MUST share a filesystem with
# INSTALL_DATA_DIR so the activation move (os.replace) is atomic; under data/
# (gitignored) by default so it never dirties the live tree. `or` (not a getenv
# default) so an empty AGENTS_AUTO_UPDATE_STAGING_DIR= line in .env falls back
# to the default. Relative values (including the default) are anchored under
# INSTALL_DATA_DIR so the path never depends on the process CWD — the MCP
# server is spawned with an arbitrary working directory.
_staging_dir_env = os.getenv("AGENTS_AUTO_UPDATE_STAGING_DIR") or ".prepared"
AUTO_UPDATE_STAGING_DIR = (
    _staging_dir_env
    if os.path.isabs(_staging_dir_env)
    else os.path.join(INSTALL_DATA_DIR, _staging_dir_env)
)

# --- Deprecated name aliases (PEP 562) ---------------------------------------
# Issue #36: `REPO_ROOT`/`DATA_DIR`/`DEBUG_LOG_DIR` used to conflate the Agents
# install directory with the client repo whose memory we write to. They now
# split into INSTALL_ROOT + get_client_repo_root()/get_debug_log_dir(). These
# aliases keep external `from src.engine.config import REPO_ROOT` callsites
# working without churn; prefer the explicit names in new code.
def __getattr__(name: str):
    if name == "REPO_ROOT":
        return INSTALL_ROOT
    if name == "DATA_DIR":
        return INSTALL_DATA_DIR
    if name == "DEBUG_LOG_DIR":
        return get_debug_log_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
