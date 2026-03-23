import os
import re
import yaml
from typing import Set

from src.engine.config import REPO_ROOT, SKILLS_DIR, IMPLANTS_DIR

def resolve_path(path_ref: str) -> str:
    """
    Resolves a path reference (starting with @ or relative) to an absolute path.
    Prevents path traversal outside REPO_ROOT.
    """
    candidate_path = ""

    if path_ref.startswith("@"):
        # Assuming @ maps to repo root
        # e.g. @.cursor/rules/... -> REPO_ROOT/.cursor/rules/...
        # or @agents/common/... -> REPO_ROOT/.cursor/agents/common/... (maybe?)

        # Let's check common patterns.
        # In the example: @agents/common/core-protocol.mdc
        # Actual path: .cursor/agents/common/core-protocol.mdc
        # In example: @.cursor/implants/...
        # Actual path: .cursor/implants/...

        clean_ref = path_ref[1:] # remove @
        if clean_ref.startswith(".cursor"):
             candidate_path = os.path.join(REPO_ROOT, clean_ref)
        elif clean_ref.startswith("agents"):
             candidate_path = os.path.join(REPO_ROOT, ".cursor", clean_ref)
        else:
             # Fallback: try relative to .cursor or just root
             candidate_1 = os.path.join(REPO_ROOT, ".cursor", clean_ref)
             candidate_2 = os.path.join(REPO_ROOT, clean_ref)

             # We can't easily check existence here if we want to be strict about traversal
             # before checking existence, but let's prioritize the one that exists if possible,
             # OR just pick one logic. The original code prioritized existence of candidate_1.
             if os.path.exists(candidate_1):
                 candidate_path = candidate_1
             else:
                 candidate_path = candidate_2
    else:
        candidate_path = os.path.join(REPO_ROOT, path_ref)

    # Security Check: Prevent Path Traversal
    abs_path = os.path.abspath(candidate_path)

    # os.path.commonpath returns the longest common sub-path
    # We ensure that REPO_ROOT is the prefix of abs_path
    try:
        if os.path.commonpath([REPO_ROOT, abs_path]) != REPO_ROOT:
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
    base_path = os.path.join(REPO_ROOT, ".cursor", "agents", agent_name, "system_prompt.mdc")

    try:
        base_path = resolve_path(os.path.relpath(base_path, REPO_ROOT))
    except ValueError:
        return {}

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
    # Try multiple locations or conventions
    # 1. .cursor/agents/{agent_name}/system_prompt.mdc

    base_path = os.path.join(REPO_ROOT, ".cursor", "agents", agent_name, "system_prompt.mdc")

    # Ensure base_path is safe (though constructed from REPO_ROOT, agent_name could technically be malicious like "../..")
    try:
        base_path = resolve_path(os.path.relpath(base_path, REPO_ROOT))
    except ValueError:
        raise ValueError(f"Invalid agent name: {agent_name}")

    if not os.path.exists(base_path):
        # Maybe it's just in the folder
        raise FileNotFoundError(f"Agent prompt not found for '{agent_name}' at {base_path}")

    raw_content = load_file_content(base_path)
    processed_content = process_imports(raw_content)

    return processed_content
