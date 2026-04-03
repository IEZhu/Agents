import os
import re
import yaml
from typing import Set

from src.engine.config import REPO_ROOT, SKILLS_DIR, IMPLANTS_DIR, AGENTS_DIR

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
        elif clean_ref.startswith(".cursor"):
            # Legacy references — try resolving from repo root
            candidate_path = os.path.join(REPO_ROOT, clean_ref)
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
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip()
            return content
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
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) >= 2:
                    fm = yaml.safe_load(parts[1]) or {}
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

    if not os.path.exists(base_path):
        return {}

    try:
        with open(base_path, 'r', encoding='utf-8') as f:
            content = f.read()
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 2:
                    return yaml.safe_load(parts[1]) or {}
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
    if os.path.commonpath([os.path.realpath(AGENTS_DIR), abs_path]) != os.path.realpath(AGENTS_DIR):
        raise ValueError(f"Invalid agent name: {agent_name}")

    if not os.path.exists(base_path):
        # Maybe it's just in the folder
        raise FileNotFoundError(f"Agent prompt not found for '{agent_name}' at {base_path}")

    raw_content = load_file_content(base_path)
    processed_content = process_imports(raw_content)

    return processed_content
