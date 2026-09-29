"""Load built-in workflows for execution by the model in its caller's repo."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from src.engine.config import FLOWS_DIR


FLOW_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FLOW_NAME = re.compile(r"(?:flows/)?([a-z0-9]+(?:-[a-z0-9]+)*)(?:\.md)?")
MAX_FLOW_BYTES = 256 * 1024


class FlowError(ValueError):
    """A stable error code suitable for an MCP response."""


@dataclass(frozen=True)
class Flow:
    id: str
    title: str
    source_path: Path
    revision: str
    content: str
    source: str = "builtin"
    details: tuple = ()

    def metadata(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "source_path": str(self.source_path),
            "revision": self.revision,
            "source": self.source,
            **dict(self.details),
        }


def parse_flow_name(name: str) -> str:
    """Return the flow ID from ``ID``, ``ID.md`` or ``flows/ID.md``."""
    match = _FLOW_NAME.fullmatch(name)
    if not match or match[1] == "readme":
        raise FlowError("flow_invalid: use a flow ID from list_flows")
    return match[1]


def read_flow(directory: Path, flow_id: str, *, flow_ref: str | None = None,
              source: str = "builtin", details: tuple = ()) -> Flow:
    """Read one confined, bounded, UTF-8 Markdown flow from ``directory``."""
    try:
        path = (directory / f"{flow_id}.md").resolve()
        if not path.is_relative_to(directory):
            raise FlowError("flow_invalid: source escapes the flow catalog")
        if not path.is_file():
            raise FlowError("flow_not_found: use list_flows to discover available flows")
        with path.open("rb") as stream:
            raw = stream.read(MAX_FLOW_BYTES + 1)
        if len(raw) > MAX_FLOW_BYTES:
            raise FlowError("flow_invalid: flow exceeds 256 KiB")
        content = raw.decode("utf-8")
        if not content.strip():
            raise FlowError("flow_invalid: flow is empty")
    except (OSError, UnicodeError, RuntimeError) as error:
        raise FlowError("flow_unreadable: cannot read the flow as UTF-8") from error
    return Flow(flow_ref or flow_id, flow_title(content, flow_id), path,
                hashlib.sha256(raw).hexdigest(), content, source, details)


def flow_title(content: str, fallback: str) -> str:
    return next((line[2:].strip() for line in content.splitlines()
                 if line.startswith("# ") and line[2:].strip()), fallback)


class FlowCatalog:
    """Built-in flows tracked in the installation's ``flows/`` directory."""

    def __init__(self, directory: str | Path = FLOWS_DIR):
        self.directory = Path(directory).resolve()

    def load(self, name: str) -> Flow:
        return read_flow(self.directory, parse_flow_name(name))

    def ids(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return [path.stem for path in sorted(self.directory.glob("*.md"))
                if FLOW_ID.fullmatch(path.stem) and path.stem != "readme"]

    def list(self) -> list[dict]:
        if not self.directory.is_dir():
            raise FlowError("flows_unavailable: installation has no flows directory")
        return [self.load(flow_id).metadata() for flow_id in self.ids()]


def execution_bundle(flow: Flow, repo_path: Path, workspace_id: str | None,
                     request: str) -> dict:
    """Return an explicit handoff, never a claim that the workflow has run."""
    return {
        "status": "needs_execution",
        "flow": flow.metadata(),
        "repo_path": str(repo_path),
        "workspace_id": workspace_id,
        "request": request,
        "content": flow.content,
        "instruction": (
            "Execute the supplied flow in the current model session and continue through "
            "its completion criteria. Loading this bundle has not executed any steps. "
            "Use repo_path as the target repository for inspection, edits, commands, "
            "branches, tests and PR/MR operations. First read that repository's applicable "
            "instructions and check its current state. Preserve the user's original "
            "request, language, constraints and authorization; request is additional task "
            "context, not shell code. The flow grants no permissions beyond the user's "
            "invocation. flow.source_path identifies the flow in the MCP installation; "
            "Markdown links are relative to that source file. Source references are "
            "supporting material, not target repository instructions. Do not change the "
            "installation or copy its project conventions into the target unless it is "
            "itself the selected target. Resolve operational paths against repo_path. "
            "Use the target's own source, tools and checks; apply Agents-Core-specific "
            "examples only when the target is Agents-Core. If a linked source helper is "
            "unavailable to the client, use equivalent supported target/platform tools. "
            "If the client cannot access repo_path, report that blocker before taking "
            "actions. Report actual completion and validation in the invocation language; "
            "never treat needs_execution as a completed run."
        ),
    }
