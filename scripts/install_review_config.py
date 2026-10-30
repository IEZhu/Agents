#!/usr/bin/env python3
"""Install the shared review configuration and review discipline in a repository."""
import argparse
from collections import Counter
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Sequence

if sys.version_info < (3, 11):
    print("ERROR: Python 3.11 or newer is required. Rerun this command with a supported Python.",
          file=sys.stderr)
    sys.exit(1)

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "_helpers"))

from inject_claude_md import write_with_backup  # noqa: E402

TEMPLATE_DIR = SCRIPT_DIR / "templates" / "review"
BASE_TEMPLATE = TEMPLATE_DIR / "coderabbit-base.yaml"
INSTRUCTION_DIR = TEMPLATE_DIR / "path-instructions"
DISCIPLINE_TEMPLATE = TEMPLATE_DIR / "review-discipline.md"

CONFIG_NAME = ".coderabbit.yaml"
MANAGED_MARK = "# Managed by Agents-Core scripts/install_review_config.py"
SECTION_BEGIN = "<!-- >>> Agents-Core Review Discipline (managed by install_review_config) >>> -->"
SECTION_END = "<!-- <<< Agents-Core Review Discipline (managed by install_review_config) <<< -->"

# Always first, so the shared classes frame the stack-specific guidance.
UNIVERSAL_STACK = "universal"
PROFILES = ("quiet", "chill", "assertive")
LANGUAGE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")

# A stack is selected when the target holds at least this many of its files.
DEFAULT_MIN_FILES = 2
STACK_SUFFIXES: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "go": (".go",),
    "shell": (".sh", ".bash"),
    "windows-scripts": (".bat", ".cmd", ".ps1", ".psm1", ".nsi"),
    "rust": (".rs",),
    "c-embedded": (".c", ".h"),
    "php": (".php",),
    "csharp": (".cs",),
    "bsl-1c": (".bsl",),
    "frontend": (".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs", ".vue", ".svelte",
                 ".html", ".css", ".scss"),
    "sql": (".sql",),
    "iac-ci": (".yml", ".yaml", ".nix", ".tf", ".tfvars", ".toml"),
    "docs-ai-instructions": (".md", ".mdc", ".rst"),
}
# One file is decisive for a stack that no linter covers or that never appears
# incidentally.
STACK_MIN_FILES: dict[str, int] = {"bsl-1c": 1, "sql": 1}
# Git variables that redirect a command away from the directory it is given.
GIT_LOCATION_VARIABLES = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CEILING_DIRECTORIES",
})
# Dependency and build trees, skipped when the target is not a Git repository and
# the walk would otherwise count vendored files as the repository's own stack.
VENDOR_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "node_modules", "vendor", "target", "dist", "build",
    "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", "site-packages",
})


class InstallError(Exception):
    """A condition the caller must resolve before the installer may write."""


class _BlockDumper(yaml.SafeDumper):
    """Emit multi-line instructions as literal blocks so the result stays readable."""


def _represent_str(dumper: yaml.SafeDumper, value: str):
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_BlockDumper.add_representer(str, _represent_str)


def available_stacks() -> list[str]:
    """Stack names that have an instruction fragment, excluding the universal set."""
    names = sorted(path.stem for path in INSTRUCTION_DIR.glob("*.yaml"))
    return [name for name in names if name != UNIVERSAL_STACK]


def repository_files(repo: Path) -> list[str]:
    """Tracked paths when the target is a Git repository, else every walked path."""
    # Git reads these from the environment, so an inherited value would make
    # ls-files report another repository's files for this target.
    environment = {key: value for key, value in os.environ.items()
                   if key not in GIT_LOCATION_VARIABLES}
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z"],
            capture_output=True, check=True, text=True, env=environment)
    except (OSError, subprocess.CalledProcessError):
        return [str(item.relative_to(repo)) for item in repo.rglob("*")
                if item.is_file() and not set(item.parts) & VENDOR_DIRECTORIES]
    return [entry for entry in completed.stdout.split("\0") if entry]


def detect_stacks(paths: Sequence[str], min_files: int = DEFAULT_MIN_FILES) -> list[str]:
    """Select the stacks whose file count reaches the threshold for that stack."""
    counts: Counter[str] = Counter()
    for path in paths:
        suffix = Path(path).suffix.lower()
        if not suffix:
            continue
        for stack, suffixes in STACK_SUFFIXES.items():
            if suffix in suffixes:
                counts[stack] += 1
    known = set(available_stacks())
    selected = [stack for stack, count in counts.items()
                if stack in known and count >= STACK_MIN_FILES.get(stack, min_files)]
    return sorted(selected)


def load_instructions(stacks: Iterable[str]) -> list[dict]:
    """Read the universal fragment and each selected fragment, in a stable order."""
    entries: list[dict] = []
    for stack in [UNIVERSAL_STACK, *stacks]:
        fragment = INSTRUCTION_DIR / f"{stack}.yaml"
        if not fragment.is_file():
            raise InstallError(f"No instruction fragment for stack '{stack}': {fragment}")
        loaded = yaml.safe_load(fragment.read_text(encoding="utf-8"))
        if not isinstance(loaded, list) or not loaded:
            raise InstallError(f"{fragment} must hold a non-empty YAML sequence")
        for item in loaded:
            if not isinstance(item, dict) or set(item) != {"path", "instructions"}:
                raise InstallError(
                    f"{fragment} entries must carry exactly the keys path and instructions")
            if not str(item["path"]).strip() or not str(item["instructions"]).strip():
                raise InstallError(f"{fragment} entries must carry non-empty values")
            entries.append({"path": item["path"], "instructions": item["instructions"]})
    return entries


def guideline_patterns(repo: Path, patterns: Sequence[str]) -> list[str]:
    """Keep the guideline patterns that match a file present in the target."""
    kept = []
    for pattern in patterns:
        if "*" in pattern:
            if next(repo.glob(pattern), None) is not None:
                kept.append(pattern)
        elif (repo / pattern).is_file():
            kept.append(pattern)
    return kept


def base_branch_patterns(names: Sequence[str], regexes: Sequence[str]) -> list[str]:
    """Anchor literal branch names, and pass explicit patterns through unchanged."""
    anchored = [f"^{re.escape(name)}$" for name in names]
    return list(dict.fromkeys(anchored + list(regexes)))


def compose_config(repo: Path, stacks: Sequence[str], language: str | None,
                   profile: str | None, base_branches: Sequence[str]) -> str:
    """Render the complete .coderabbit.yaml text for the target repository."""
    config = yaml.safe_load(BASE_TEMPLATE.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise InstallError(f"{BASE_TEMPLATE} must hold a YAML mapping")
    if language:
        config["language"] = language
    if profile:
        config["reviews"]["profile"] = profile
    if base_branches:
        config["reviews"]["auto_review"]["base_branches"] = list(base_branches)
    config["reviews"]["path_instructions"] = load_instructions(stacks)
    kept = guideline_patterns(repo, config["knowledge_base"]["code_guidelines"]["filePatterns"])
    if kept:
        config["knowledge_base"]["code_guidelines"]["filePatterns"] = kept
    else:
        del config["knowledge_base"]["code_guidelines"]["filePatterns"]

    header = "\n".join([
        MANAGED_MARK,
        "# Do not edit by hand: rerun the installer after changing the templates.",
        f"# Stacks: {', '.join([UNIVERSAL_STACK, *stacks])}",
        "# Reference: https://docs.coderabbit.ai/reference/configuration",
        "",
    ])
    body = yaml.dump(config, Dumper=_BlockDumper, sort_keys=False, allow_unicode=True,
                     width=88, default_flow_style=False)
    return header + body


def render_section(content: bytes, section: bytes, path: Path) -> bytes:
    """Replace only the managed discipline section, preserving every other byte."""
    block = (SECTION_BEGIN.encode() + b"\n\n" + section.strip(b"\r\n") + b"\n\n"
             + SECTION_END.encode())
    begin, end = SECTION_BEGIN.encode(), SECTION_END.encode()
    if begin in content or end in content:
        if content.count(begin) != 1 or content.count(end) != 1:
            raise InstallError(
                f"Expected one complete review-discipline marker pair in {path}; "
                "fix the markers manually")
        start, stop = content.index(begin), content.index(end)
        if stop < start:
            raise InstallError(f"End marker precedes begin marker in {path}; fix it manually")
        return content[:start] + block + content[stop + len(end):]
    if not content:
        return block + b"\n"
    tail = content if content.endswith(b"\n") else content + b"\n"
    if not tail.endswith(b"\n\n"):
        tail += b"\n"
    return tail + block + b"\n"


def install_config(repo: Path, text: str, force: bool, dry_run: bool) -> str:
    """Write the composed configuration, refusing an unmanaged existing file."""
    target = repo / CONFIG_NAME
    if target.exists() and not target.is_symlink():
        current = target.read_text(encoding="utf-8", errors="replace")
        if MANAGED_MARK not in current and not force:
            raise InstallError(
                f"{target} exists and was not written by this installer. Review it, then "
                "rerun with --force to replace it.")
        if current == text:
            return "already current"
    if dry_run:
        return "would write"
    write_with_backup(target, text.encode("utf-8"))
    return "written"


def install_section(md_path: Path, create: bool, dry_run: bool) -> str:
    """Inject or refresh the discipline section in the target instruction file."""
    if not md_path.exists() and not create:
        raise InstallError(
            f"{md_path} does not exist. Rerun with --create-agents-md to create it, or with "
            "--no-agents-md to skip the section.")
    section = DISCIPLINE_TEMPLATE.read_bytes()
    content = md_path.read_bytes() if md_path.exists() else b""
    rendered = render_section(content, section, md_path)
    if rendered == content:
        return "already current"
    if dry_run:
        return "would write"
    write_with_backup(md_path, rendered)
    return "written"


def parse_list(value: str) -> list[str]:
    """Split a comma-separated option into non-empty, de-duplicated entries."""
    items = [item.strip() for item in value.split(",")]
    return list(dict.fromkeys(item for item in items if item))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, help="Target repository root.")
    parser.add_argument("--stacks", type=parse_list, default=None,
                        help="Comma-separated stacks, overriding detection.")
    parser.add_argument("--list-stacks", action="store_true",
                        help="Print the available stacks and exit.")
    parser.add_argument("--language", default=None,
                        help="Review language as an ISO code, for example ru-RU.")
    parser.add_argument("--profile", choices=PROFILES, default=None,
                        help="CodeRabbit review profile.")
    parser.add_argument("--base-branch", type=parse_list, default=None, dest="base_branches",
                        help="Literal pull-request target branches other than the repository "
                             "default; each becomes an anchored regex.")
    parser.add_argument("--base-branch-regex", type=parse_list, default=None,
                        dest="base_branch_regexes",
                        help="Unanchored regexes for target branches, for example '.*' to "
                             "review every base branch.")
    parser.add_argument("--agents-md", type=Path, default=None,
                        help="Instruction file for the discipline section, relative to the "
                             "target repository unless absolute (default AGENTS.md).")
    parser.add_argument("--create-agents-md", action="store_true",
                        help="Create the instruction file when it is missing.")
    parser.add_argument("--no-agents-md", action="store_true",
                        help="Install only the configuration, without the section.")
    parser.add_argument("--force", action="store_true",
                        help="Replace an existing .coderabbit.yaml this installer did not write.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the actions without writing.")
    parser.add_argument("--print", action="store_true", dest="print_config",
                        help="Print the composed configuration instead of installing it.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_stacks:
        print("\n".join(available_stacks()))
        return 0
    if args.repo is None:
        print("ERROR: --repo is required unless --list-stacks is given.", file=sys.stderr)
        return 1
    if args.language and not LANGUAGE_RE.match(args.language):
        print(f"ERROR: --language {args.language} is not an ISO language code.", file=sys.stderr)
        return 1

    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        print(f"ERROR: {repo} is not a directory.", file=sys.stderr)
        return 1

    try:
        stacks = args.stacks if args.stacks is not None else detect_stacks(repository_files(repo))
        unknown = sorted(set(stacks) - set(available_stacks()))
        if unknown:
            raise InstallError(
                f"Unknown stacks: {', '.join(unknown)}. Run --list-stacks for the full set.")
        branches = base_branch_patterns(args.base_branches or [], args.base_branch_regexes or [])
        text = compose_config(repo, stacks, args.language, args.profile, branches)
        if args.print_config:
            print(text, end="")
            return 0
        config_state = install_config(repo, text, args.force, args.dry_run)
        section_state = "skipped"
        if not args.no_agents_md:
            md_path = args.agents_md or Path("AGENTS.md")
            # Expand but do not resolve: a symlinked instruction file must reach
            # write_with_backup as a symlink, which refuses to replace it. A
            # relative path belongs to the target, not to the working directory.
            md_path = md_path.expanduser()
            if not md_path.is_absolute():
                md_path = repo / md_path
            section_state = install_section(md_path, args.create_agents_md, args.dry_run)
    except InstallError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Stacks: {', '.join([UNIVERSAL_STACK, *stacks])}")
    print(f"{CONFIG_NAME}: {config_state}")
    print(f"Review discipline section: {section_state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
