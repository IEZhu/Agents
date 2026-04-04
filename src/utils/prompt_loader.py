import os
import re
import yaml
from typing import Set, Tuple, Optional

from src.engine.config import REPO_ROOT, SKILLS_DIR, IMPLANTS_DIR, AGENTS_DIR

# Regex to find the closing --- of YAML frontmatter.
# Matches --- only at the start of a line, avoiding --- inside quoted values.
_FRONTMATTER_RE = re.compile(r'^---\s*$', re.MULTILINE)


def split_frontmatter(content: str) -> Tuple[Optional[str], str]:
    """Split MDC content into (frontmatter_yaml, body).

    Returns (None, content) if no valid frontmatter block is found.
    Uses regex to match ``---`` only at the start of a line, so ``---``
    embedded inside quoted YAML values (e.g. ``compiled: "use --- for
    separators"``) won't break the split.
    """
    if not content.startswith("---"):
        return None, content

    # Find all --- at start-of-line; first is the opening, second is the closing.
    matches = list(_FRONTMATTER_RE.finditer(content))
    if len(matches) < 2:
        return None, content

    # Everything between first and second --- markers
    fm_start = matches[0].end()    # right after opening ---\n
    fm_end = matches[1].start()    # right before closing ---
    body_start = matches[1].end()  # right after closing ---\n

    frontmatter_str = content[fm_start:fm_end]
    body = content[body_start:].strip()

    return frontmatter_str, body

def resolve_path(path_ref: str) -> str:
    """
    Resolves a path reference (starting with @ or relative) to an absolute path.
    Prevents path traversal outside REPO_ROOT.
    """
    candidate_path = ""

    if path_ref.startswith("@"):
        clean_ref = path_ref[1:]  # remove @

        # Map known prefixes to their resolved directories
        if clean_ref.startswith("agents/"):
            candidate_path = os.path.join(AGENTS_DIR, clean_ref[len("agents/"):])
        elif clean_ref.startswith("skills/"):
            candidate_path = os.path.join(SKILLS_DIR, clean_ref[len("skills/"):])
        elif clean_ref.startswith("implants/"):
            candidate_path = os.path.join(IMPLANTS_DIR, clean_ref[len("implants/"):])
        else:
            # Fallback: try repo root directly
            candidate_path = os.path.join(REPO_ROOT, clean_ref)
    else:
        candidate_path = os.path.join(REPO_ROOT, path_ref)

    # Security Check: Prevent Path Traversal (realpath resolves symlinks)
    abs_path = os.path.realpath(candidate_path)
    repo_root_real = os.path.realpath(REPO_ROOT)

    try:
        if os.path.commonpath([repo_root_real, abs_path]) != repo_root_real:
            raise ValueError(f"Security Error: Access denied for path '{path_ref}'. Cannot access outside repository.")
    except ValueError:
        # handle edge cases like different drives on Windows, though unlikely in this Linux environment
        raise ValueError(f"Security Error: Path '{path_ref}' is invalid.")

    return abs_path

def load_file_content(path: str) -> str:
    try:
        # Double check existence to avoid race conditions or errors
        if not os.path.exists(path):
             return f"[MISSING FILE: {path}]"

        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
            # specific to MDC files with frontmatter: remove it if present
            _, body = split_frontmatter(content)
            return body if body != content else content
    except FileNotFoundError:
        return f"[MISSING FILE: {path}]"
    except Exception as e:
        return f"[ERROR LOADING FILE: {path} - {str(e)}]"

_skip_inline_cache: dict[str, bool] = {}

def _should_skip_inline(abs_path: str) -> bool:
    """Skip inlining files that are loaded via other mechanisms.

    Results are cached per normalized path to avoid repeated file I/O
    and YAML parsing on every @-reference.
    """
    norm = os.path.normpath(abs_path)

    if norm in _skip_inline_cache:
        return _skip_inline_cache[norm]

    result = _compute_skip_inline(norm)
    _skip_inline_cache[norm] = result
    return result

def _compute_skip_inline(norm: str) -> bool:
    if norm.startswith(os.path.normpath(SKILLS_DIR)):
        return True
    if norm.startswith(os.path.normpath(IMPLANTS_DIR)):
        return True

    if os.path.exists(norm):
        try:
            with open(norm, "r", encoding="utf-8") as f:
                raw = f.read()
            fm_str, _ = split_frontmatter(raw)
            if fm_str is not None:
                fm = yaml.safe_load(fm_str) or {}
                if fm.get("alwaysApply") is True:
                    return True
        except Exception:
            pass
    return False

def process_imports(content: str, seen_files: Set[str] = None) -> str:
    if seen_files is None:
        seen_files = set()

    def replacer(match):
        ref = match.group(0)
        try:
            abs_path = resolve_path(ref)
        except ValueError as e:
            return f"[SECURITY BLOCK: {str(e)}]"

        if abs_path in seen_files:
            return f"[CIRCULAR REFERENCE: {ref}]"

        if _should_skip_inline(abs_path):
            return f"[Loaded separately: {os.path.basename(abs_path)}]"

        seen_files.add(abs_path)
        sub_content = load_file_content(abs_path)
        return process_imports(sub_content, seen_files.copy())

    return re.sub(r'@[\w\./-]+\.mdc', replacer, content)

def get_agent_metadata(agent_name: str) -> dict:
    """
    Reads the frontmatter metadata from the agent's system prompt.
    """
    base_path = os.path.join(AGENTS_DIR, agent_name, "system_prompt.mdc")

    # Security: ensure the resolved path stays within AGENTS_DIR (realpath resolves symlinks)
    abs_path = os.path.realpath(base_path)
    agents_dir_real = os.path.realpath(AGENTS_DIR)
    try:
        if os.path.commonpath([agents_dir_real, abs_path]) != agents_dir_real:
            return {}
    except ValueError:
        return {}

    if not os.path.exists(base_path):
        return {}

    try:
        with open(base_path, 'r', encoding='utf-8') as f:
            content = f.read()
        fm_str, _ = split_frontmatter(content)
        if fm_str is not None:
            return yaml.safe_load(fm_str) or {}
    except Exception:
        pass

    return {}

def load_agent_prompt(agent_name: str) -> str:
    """
    Loads the system prompt for a specific agent, resolving imports.
    """
    base_path = os.path.join(AGENTS_DIR, agent_name, "system_prompt.mdc")

    # Security: ensure the resolved path stays within AGENTS_DIR (realpath resolves symlinks)
    abs_path = os.path.realpath(base_path)
    agents_dir_real = os.path.realpath(AGENTS_DIR)
    try:
        if os.path.commonpath([agents_dir_real, abs_path]) != agents_dir_real:
            raise ValueError(f"Invalid agent name: {agent_name}")
    except ValueError:
        raise ValueError(f"Invalid agent name: {agent_name}")

    if not os.path.exists(base_path):
        # Maybe it's just in the folder
        raise FileNotFoundError(f"Agent prompt not found for '{agent_name}' at {base_path}")

    raw_content = load_file_content(base_path)
    processed_content = process_imports(raw_content)

    return processed_content
