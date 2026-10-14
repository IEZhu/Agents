"""Independent executable acceptance checks for the production patch.

Run in a disposable directory containing candidate.py and the frozen schema.py.
Candidate Python is trusted task code; a subprocess is not a security sandbox.
"""

import importlib.util
import json
from pathlib import Path
import sys
import types


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def probe(directory):
    root = Path(directory)
    # The candidate depends only on this schema; do not import mutable repo modules.
    for name in ("src", "src.schemas"):
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
    load("src.schemas.subagent", root / "schema.py")
    m = load("candidate", root / "candidate.py")
    rows = []
    def rejects(name, value, code, path=None, **options):
        try:
            m.bounded_object(value, max_chars=options.pop("max_chars", 128000), **options)
            rows.append({"name": name, "passed": False, "observed": "accepted"})
        except m.ContractError as exc:
            ok = exc.code == code and (exc.path == path if path is not None else True)
            rows.append({"name": name, "passed": ok, "code": exc.code, "path": exc.path})
    rejects("nested_nonfinite", {"evidence": [{"score": float("inf")}]}, "invalid_json_value", "$.evidence[0].score")
    rejects("special_key", {"a.b": [{"x": {1}}]}, "invalid_json_value", '$["a.b"][0].x')
    rejects("array_bound", {"outer": {"items": list(range(101))}}, "array_too_large", "$.outer.items")
    rejects("object_bound", {"outer": {str(i): 0 for i in range(101)}}, "object_too_large", "$.outer")
    rejects("non_string_key", {"outer": {1: "value"}}, "invalid_key", "$.outer")
    rejects("oversized_key", {"outer": {"x" * 129: "value"}}, "invalid_key", "$.outer")
    rejects("unsupported_value", {"outer": [object()]}, "invalid_json_value", "$.outer[0]")
    rejects("duplicate_keys", '{"a":1,"a":2}', "duplicate_key", allow_json=True)
    rejects("raw_nonfinite", '{"a":[NaN]}', "non_finite", allow_json=True)
    rejects("trailing_json", '{} {}', "invalid_json", allow_json=True)
    rejects("root_type", [1], "object_required", "$")
    rejects("payload_bound", {"outer": "x" * 200}, "payload_too_large", max_chars=100)
    deep = 0
    for _ in range(14):
        deep = {"a": deep}
    rejects("depth_bound", deep, "structure_too_large")
    rejects("node_bound", {"a": [[0] * 100 for _ in range(100)]}, "structure_too_large")
    rejects("invalid_unicode", {"a": "\ud800"}, "invalid_json_value")
    rejects("unicode_before_structural_error", {"a": "\ud800", "b": [0] * 101}, "array_too_large")
    rejects("huge_int_before_structural_error", {"a": 10**5000, "b": [0] * 101}, "array_too_large")
    rejects("unicode_before_size_error", {"a": "\ud800"}, "payload_too_large", max_chars=7)
    for name, value in (("finite_values", {"a": [None, True, 3, 1.5, "hello"]}),
                        ("array_at_bound", {"a": list(range(100))}),
                        ("object_at_bound", {str(i): 0 for i in range(100)})):
        rows.append({"name": name, "passed": m.bounded_object(value, max_chars=128000) == value})
    try:
        m.bounded_object({"outer": [object()]}, max_chars=1000)
    except m.ContractError as exc:
        rows.append({"name": "result_prefix", "passed": m.diagnostics(exc, "$.result")[0].path == "$.result.outer[0]"})
    return {"passed": all(row["passed"] for row in rows), "checks": rows}


if __name__ == "__main__":
    result = probe(sys.argv[1])
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
