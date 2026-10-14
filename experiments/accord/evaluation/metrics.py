"""Descriptive source accounting, not authoring-time or performance measurement."""

import ast
import json

from ..ir import text_digest
from .workload import HERE, ROOT


def measure(path):
    text = path.read_text()
    return {"path": str(path.relative_to(ROOT)), "physical_lines": len(text.splitlines()),
            "nonblank_lines": sum(bool(line.strip()) for line in text.splitlines()),
            "utf8_bytes": len(text.encode()), "sha256": text_digest(text)}


def main():
    groups = {
        "accord_runtime_and_ir": [HERE.parent / name for name in ("runtime.py", "ir.py")],
        "independent_sdk_including_job_spec": [HERE / "python_sdk.py"],
        "shared_storage": [HERE.parent / "store.py"],
        "shared_git_adapter": [HERE / "git_sink.py"],
        "shared_workload_and_predicates": [HERE / "workload.py"],
        "all_arm_wiring": [HERE / "clients.py"],
        "json_task_declaration": [HERE / "contract.json"],
        "paired_harness": [HERE / "run.py"],
    }
    clients = (HERE / "clients.py").read_text()
    declarations = []
    for node in ast.parse(clients).body:
        if isinstance(node, ast.FunctionDef) and node.name != "open_client":
            declarations.append({"function": node.name, "physical_lines": node.end_lineno - node.lineno + 1,
                                 "source": ast.get_source_segment(clients, node)})
    report = {"method": "Physical and nonblank lines include comments/docstrings. UTF-8 bytes include formatting.",
              "groups": {name: [measure(path) for path in paths] for name, paths in groups.items()},
              "declarations": declarations,
              "limits": "Runtime scopes differ: Accord includes child allocation and JSON parsing; SDK covers the tested single-task lifecycle. Neither counts as per-task authoring effort. No productivity or brevity win is inferred."}
    destination = HERE / "results/code-accounting.json"
    destination.write_text(json.dumps(report, indent=2) + "\n")
    for group, rows in report["groups"].items():
        print(group, sum(row["physical_lines"] for row in rows), "physical lines")
    for declaration in declarations:
        print(declaration["function"], declaration["physical_lines"], "physical lines")


if __name__ == "__main__":
    main()
