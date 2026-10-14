"""Frozen real repository task, shared predicates and recorded candidate."""

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Callable

from ..ir import require, text_digest

BASE = "3d95c42b79693d6a73d017d602f1b4262f3a300e"
ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).parent
TARGET = "tests/README.md"
ANCHOR = "### Checks outside pytest\n"
HEADING = "### Focused subagent contract checks\n"
PATHS = {"test_guide": TARGET, "core_tests": "tests/test_subagent.py",
         "mcp_tests": "tests/test_subagent_mcp.py", "api_guide": "docs/subagent-contract.md",
         "project_config": "pyproject.toml"}
COMMAND = "LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_subagent.py tests/test_subagent_mcp.py -q"


@dataclass(frozen=True)
class Check:
    revision: str
    run: Callable[[str, dict[str, str]], bool]


def source_snapshot(repo=ROOT):
    inputs = {name: subprocess.check_output(["git", "-C", str(repo), "show", f"{BASE}:{path}"], text=True)
              for name, path in PATHS.items()}
    inputs["test_evidence"] = (HERE / "fixtures/test-evidence.json").read_text()
    return inputs


def candidate(inputs):
    require(inputs["test_guide"].count(ANCHOR) == 1, "insertion anchor changed")
    require(HEADING not in inputs["test_guide"], "task already applied")
    return inputs["test_guide"].replace(ANCHOR, (HERE / "fixtures/subagent-testing.md").read_text() + "\n" + ANCHOR, 1)


def inserted_section(text, inputs):
    before, after = inputs["test_guide"].split(ANCHOR, 1)
    suffix = ANCHOR + after
    if not text.startswith(before) or not text.endswith(suffix):
        return None
    return text[len(before):len(text) - len(suffix)]


def bounded_edit(text, inputs):
    section = inserted_section(text, inputs)
    return section is not None and section.startswith(HEADING) and 300 <= len(section) <= 2500


def runnable_command(text, inputs):
    section = inserted_section(text, inputs) or ""
    blocks = re.findall(r"(?ms)^```bash\n(.*?)^```[ \t]*$", section)
    return len(blocks) == 1 and blocks[0].strip() == COMMAND and "../docs/subagent-contract.md" in section


def source_alignment(text, inputs):
    section = inserted_section(text, inputs) or ""
    core = {n.name for n in ast.walk(ast.parse(inputs["core_tests"])) if isinstance(n, ast.FunctionDef)}
    mcp = {n.name for n in ast.walk(ast.parse(inputs["mcp_tests"])) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    facts = ("test_replayed_or_wrong_identity" in core and "test_budget_never_crops_mandatory_or_optional_instructions" in core
             and "test_registered_input_and_output_schemas" in mcp and "test_mcp_does_not_coerce_invalid_inputs" in mcp)
    return (facts and "structural_only" in inputs["api_guide"] and "structural_only" in section
            and "does not prove" in section and "authorize" in section)


def focused_tests(text, inputs):
    evidence = json.loads(inputs["test_evidence"])
    expected = {k: text_digest(v) for k, v in inputs.items() if k != "test_evidence"}
    return (evidence["base_revision"] == BASE and evidence["returncode"] == 0
            and evidence["passed"] > 0 and evidence["input_sha256"] == expected)


def checks():
    return {name: Check("2" if name == "runnable_command" else "1", function) for name, function in
            [("bounded_edit", bounded_edit), ("runnable_command", runnable_command),
             ("source_alignment", source_alignment), ("focused_tests", focused_tests)]}
