"""Check tracked Markdown navigation and the public MCP tool index without starting services.

Uses markdown-it-py from the project environment. External URLs are not fetched;
Markdown links/images and GitHub-style heading/HTML anchors are checked locally.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN = MarkdownIt("commonmark").enable("table")
BADGE_PATH = Path("docs/badges/mcp-api-docs.svg")


class AnchorParser(HTMLParser):
    """Collect rendered HTML anchors, excluding comments and raw-text content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors: set[str] = set()

    def handle_starttag(self, tag, attrs):
        self.anchors.update(value for key, value in attrs
                            if value is not None and (key == "id" or tag == "a" and key == "name"))


def parse_document(text: str) -> tuple[set[str], list[str]]:
    """Return rendered anchors and link destinations; fenced examples are ignored."""
    tokens = MARKDOWN.parse(text)
    anchors: set[str] = set()
    links: list[str] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            inline = tokens[index + 1]
            title = "".join(child.content for child in inline.children or []
                            if child.type in {"text", "code_inline", "image"})
            slug = "".join(char for char in title.lower()
                           if char in "_- " or unicodedata.category(char)[0] in "LN").replace(" ", "-")
            candidate, suffix = slug, 0
            while candidate in anchors:
                suffix += 1
                candidate = f"{slug}-{suffix}"
            anchors.add(candidate)
        for child in [token, *(token.children or [])]:
            if child.type == "link_open":
                links.append(child.attrGet("href") or "")
            elif child.type == "image":
                links.append(child.attrGet("src") or "")
            elif child.type in {"html_block", "html_inline"}:
                parser = AnchorParser()
                parser.feed(child.content)
                anchors.update(parser.anchors)
    return anchors, links


def check_links(root: Path, documents: list[Path]) -> list[str]:
    cache: dict[Path, tuple[set[str], list[str]]] = {}

    def parsed(path: Path):
        if path not in cache:
            cache[path] = parse_document(path.read_text(encoding="utf-8"))
        return cache[path]

    errors = []
    for document in documents:
        for destination in parsed(document)[1]:
            url = urlsplit(destination)
            if url.scheme or url.netloc:
                continue
            raw_path = unquote(url.path)
            target = ((root / raw_path.lstrip("/")) if raw_path.startswith("/")
                      else (document.parent / raw_path if raw_path else document)).resolve()
            label = f"{document.relative_to(root)}: {destination}"
            if not target.is_relative_to(root.resolve()):
                errors.append(f"{label}: escapes repository")
            elif not target.exists():
                errors.append(f"{label}: missing path")
            elif url.fragment and target.suffix.lower() in {".md", ".mdc"}:
                if unquote(url.fragment) not in parsed(target)[0]:
                    errors.append(f"{label}: missing anchor")
    return errors


def declared_tools(root: Path) -> set[str]:
    """Read production registrations as AST, without importing the server or models."""
    names: set[str] = set()
    tree = ast.parse((root / "src/server.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name)
                        and decorator.func.value.id == "mcp" and decorator.func.attr == "tool"):
                    name = next((kw.value.value for kw in decorator.keywords
                                 if kw.arg == "name" and isinstance(kw.value, ast.Constant)), node.name)
                    names.add(name)
    # These two registrars use add_tool in a loop, not decorators in server.py.
    for filename, variable in [("src/local_inference/tools.py", "TOOL_NAMES"),
                               ("src/artifacts/service.py", "REQUEST_MODELS")]:
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == variable
                                                    for t in node.targets):
                values = node.value.keys if isinstance(node.value, ast.Dict) else node.value.elts
                names.update(ast.literal_eval(value) for value in values)
    return names


def documented_tools(root: Path) -> set[str]:
    text = (root / "docs/mcp-tools.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| `([a-z][a-z0-9_]*)` \|", text, re.MULTILINE))


def check_tool_index(root: Path) -> list[str]:
    expected = declared_tools(root)
    documented = documented_tools(root)
    return ([f"MCP tool missing from docs/mcp-tools.md: {name}" for name in sorted(expected - documented)]
            + [f"Unknown MCP tool in docs/mcp-tools.md: {name}" for name in sorted(documented - expected)])


def tool_badge(root: Path) -> str:
    expected = declared_tools(root)
    covered = len(expected & documented_tools(root))
    total = len(expected)
    message = f"{covered}/{total}"
    color = "#4c1" if total and covered == total else "#fe7d37" if covered else "#e05d44"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="142" height="20" role="img" aria-label="MCP API docs: {message}">
  <title>MCP API references: {covered} of {total} registered tools</title>
  <clipPath id="round"><rect width="142" height="20" rx="3"/></clipPath>
  <g clip-path="url(#round)">
    <path fill="#555" d="M0 0h92v20H0z"/>
    <path fill="{color}" d="M92 0h50v20H92z"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">
    <text x="46" y="14">MCP API docs</text>
    <text x="117" y="14">{message}</text>
  </g>
</svg>
'''


def check_tool_badge(root: Path) -> list[str]:
    path = root / BADGE_PATH
    if not path.is_file() or path.read_text(encoding="utf-8") != tool_badge(root):
        return [f"{BADGE_PATH}: stale or missing; run python scripts/check_docs.py --update-badge"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Markdown paths relative to the repository; default: tracked .md files")
    parser.add_argument("--skip-tool-index", action="store_true", help="Check only local navigation")
    parser.add_argument("--update-badge", action="store_true", help="Regenerate the source-derived MCP API reference badge")
    args = parser.parse_args(argv)
    if args.skip_tool_index and args.update_badge:
        parser.error("--update-badge requires the tool-index check")
    if args.paths:
        documents = [(ROOT / path).resolve() for path in args.paths]
        if any(not path.is_relative_to(ROOT) or not path.is_file() for path in documents):
            parser.error("paths must be existing files inside the repository")
    else:
        paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
        documents = [ROOT / path for path in paths if path.endswith(".md")]
    if args.update_badge:
        path = ROOT / BADGE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tool_badge(ROOT), encoding="utf-8")
    errors = check_links(ROOT, documents)
    if not args.skip_tool_index:
        errors.extend(check_tool_index(ROOT))
        errors.extend(check_tool_badge(ROOT))
    for error in errors:
        print(error)
    print(f"Checked {len(documents)} Markdown files; {len(errors)} navigation/tool-index errors.")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
