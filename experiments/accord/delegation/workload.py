"""Frozen inputs, multi-file artifact and independent code behavior checks."""

import ast
from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from ..ir import canonical, require, text_digest
from ..runtime import Validator
from ..evaluation.git_sink import git

BASE = "8ab433012fc16e68f95ca538dfbb4bf4d082a3d7"
HERE = Path(__file__).parent
ROOT = HERE.parents[2]
SOURCE = "src/engine/subagent_validation.py"
TEST = "tests/test_subagent_paths.py"
DOC = "docs/subagent-contract.md"
PATHS = (SOURCE, TEST, DOC)
REQUIREMENTS = "Report precise nested JSON paths, preserve accepted values, error codes and all existing bounds."
BEHAVIOR_RUNS = []


def source_snapshot():
    return {"base_source": git(ROOT, "show", f"{BASE}:{SOURCE}").stdout,
            "base_docs": git(ROOT, "show", f"{BASE}:{DOC}").stdout,
            "base_schema": git(ROOT, "show", f"{BASE}:src/schemas/subagent.py").stdout,
            "requirements": REQUIREMENTS}


def bundle(files):
    return canonical({"format": "accord.patch/0.1", "base": BASE, "files": files}).decode()


def candidate():
    return (HERE / "fixtures/candidate.json").read_text().rstrip("\n")


def parse_bundle(text):
    from .sdk_ir import pairs, reject_constant
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
    require(type(value) is dict and set(value) == {"format", "base", "files"}, "invalid patch envelope")
    require(value["format"] == "accord.patch/0.1" and value["base"] == BASE, "wrong patch base/version")
    files = value["files"]
    require(type(files) is dict and set(files) == set(PATHS), "patch path outside declared scope")
    require(all(type(v) is str for v in files.values()), "file contents must be text")
    require(len(text.encode()) <= 65536, "patch too large")
    return files


def bundle_scope(text, inputs):
    files = parse_bundle(text)
    for path in (SOURCE, TEST):
        ast.parse(files[path])
    return files[SOURCE] != inputs["base_source"] and bool(files[TEST].strip()) and bool(files[DOC].strip())


@lru_cache(maxsize=32)
def run_behavior(source, schema):
    with TemporaryDirectory(prefix="accord-behavior-") as directory:
        root = Path(directory)
        (root / "candidate.py").write_text(source)
        (root / "schema.py").write_text(schema)
        response = subprocess.run([sys.executable, str(HERE / "behavior_probe.py"), str(root)],
                                  capture_output=True, text=True, timeout=30)
    report = json.loads(response.stdout) if response.stdout else {"passed": False, "error": response.stderr[-2000:]}
    report.update(returncode=response.returncode, source_sha256=text_digest(source), schema_sha256=text_digest(schema))
    BEHAVIOR_RUNS.append(report)
    return report


def behavior(text, inputs):
    return run_behavior(parse_bundle(text)[SOURCE], inputs["base_schema"])["passed"] is True


def registry():
    return {"bundle_scope": Validator("1", bundle_scope), "behavior": Validator("1", behavior),
            "behavior_v2": Validator("2", behavior)}
