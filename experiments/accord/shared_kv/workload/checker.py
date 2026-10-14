"""Non-executing bounded patch decoder; only run_behavior executes gated ir.py.

Import API: load_workload(), parse_patch(raw), materialize(raw), run_behavior(raw=None),
implementation_materials(), review_materials(raw, report), self_test().
The implementation child returns exactly one replacement string in result.items.
Extract it only after Agents-Core validates the complete envelope, then pass it to
patch_for_replacement() or materialize_replacement(). Metadata is host-owned.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap

HERE = Path(__file__).resolve().parent
MAX_PATCH_BYTES = 8192
MAX_SNIPPET_BYTES = 2048
MAX_AST_NODES = 200
class InvalidPatch(ValueError):
    pass

def require(test, message):
    if not test:
        raise InvalidPatch(message)
def sha(raw):
    return hashlib.sha256(raw).hexdigest()
def pairs(items):
    value = {}
    for key, item in items:
        require(key not in value, "duplicate JSON key")
        value[key] = item
    return value
def reject_constant(value):
    raise InvalidPatch("nonfinite JSON")
def load_workload():
    return json.loads((HERE / "workload.json").read_text())
def baseline():
    w = load_workload()
    raw = (HERE / "baseline.py.txt").read_bytes()
    require(sha(raw) == w["source_sha256"], "baseline fingerprint mismatch")
    return raw

def validate_snippet(snippet):
    require(type(snippet) is str, "replacement must be text")
    try:
        encoded = snippet.encode("utf-8")
    except UnicodeError as exc:
        raise InvalidPatch("invalid replacement Unicode") from exc
    require(0 < len(encoded) <= MAX_SNIPPET_BYTES, "replacement size")
    require(snippet.endswith("\n") and "\r" not in snippet and "\t" not in snippet,
            "replacement must use spaces and a final LF")
    require(len(snippet.splitlines()) <= 36, "too many lines")
    require(all(not line.strip() or line.startswith("        ") for line in snippet.splitlines()),
            "replacement must retain eight-space base indentation")
    try:
        tree = ast.parse(textwrap.dedent(snippet))
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise InvalidPatch("invalid Python snippet") from exc
    nodes = list(ast.walk(tree))
    require(len(nodes) <= MAX_AST_NODES, "AST size")
    require(len(tree.body) == 1 and type(tree.body[0]) is ast.If, "single input-type gate required")
    outer = tree.body[0]
    expected_gate = ast.parse("isinstance(data, (str, bytes))", mode="eval").body
    require(ast.dump(outer.test) == ast.dump(expected_gate) and not outer.orelse, "input-type gate changed")
    allowed = (ast.Module, ast.If, ast.Expr, ast.Assign, ast.Try, ast.ExceptHandler,
               ast.Raise, ast.Call, ast.Name, ast.Load, ast.Store, ast.Constant,
               ast.Tuple, ast.IfExp, ast.Compare, ast.LtE, ast.Gt, ast.Attribute,
               ast.keyword, ast.Pass)
    require(all(type(n) in allowed for n in nodes), "syntax outside allowlist")
    protected = {"str", "bytes", "isinstance", "len", "require", "json", "_unique_object",
                 "_reject_constant", "ValueError", "UnicodeError", "UnicodeEncodeError", "AccordError"}
    locals_ = {"data"}
    for n in nodes:
        if isinstance(n, ast.Assign):
            require(len(n.targets) == 1 and type(n.targets[0]) is ast.Name, "simple local assignments only")
            locals_.add(n.targets[0].id)
        if isinstance(n, ast.ExceptHandler) and n.name:
            locals_.add(n.name)
    require(len(locals_) <= 7 and not locals_ & protected, "protected name assignment")
    require(all(re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) and "__" not in name for name in locals_),
            "invalid local name")
    allowed_names = protected | locals_
    exception_names = {"ValueError", "UnicodeError", "UnicodeEncodeError", "AccordError"}
    require(sum(isinstance(n, ast.Call) for n in nodes) <= 14, "too many calls")
    for n in nodes:
        if isinstance(n, ast.Name):
            require(n.id in allowed_names, "unknown name")
        if isinstance(n, ast.Constant):
            require((type(n.value) is str and n.value in {"utf-8", "contract too large", "invalid contract JSON"})
                    or (type(n.value) is int and n.value == 65536), "constant outside allowlist")
        if isinstance(n, ast.Attribute):
            require(type(n.value) is ast.Name and (n.value.id, n.attr) in {("json", "loads"), ("data", "encode")},
                    "attribute outside allowlist")
        if isinstance(n, ast.Call):
            if type(n.func) is ast.Name:
                require(n.func.id in {"isinstance", "len", "require", "AccordError"}, "call outside allowlist")
                require(not n.keywords, "keywords outside allowlist")
                require(len(n.args) == {"isinstance":2, "len":1, "require":2, "AccordError":1}[n.func.id],
                        "wrong argument count")
            elif type(n.func) is ast.Attribute:
                require(type(n.func.value) is ast.Name, "indirect attribute call prohibited")
                key = (n.func.value.id, n.func.attr)
                if key == ("data", "encode"):
                    require(not n.keywords and len(n.args) == 1 and type(n.args[0]) is ast.Constant
                            and n.args[0].value == "utf-8", "only strict UTF-8 encoding allowed")
                else:
                    require(len(n.args) == 1 and type(n.args[0]) is ast.Name and n.args[0].id == "data",
                            "json.loads must read data")
                    require(len(n.keywords) == 2 and
                            {k.arg: k.value.id for k in n.keywords if type(k.value) is ast.Name} ==
                            {"object_pairs_hook":"_unique_object", "parse_constant":"_reject_constant"},
                            "JSON hooks changed")
            else:
                raise InvalidPatch("indirect call prohibited")
        if isinstance(n, ast.ExceptHandler):
            types = n.type.elts if type(n.type) is ast.Tuple else [n.type]
            require(types and all(type(t) is ast.Name and t.id in exception_names for t in types),
                    "exception type outside allowlist")
        if isinstance(n, ast.Raise):
            require(n.exc is None or (type(n.exc) is ast.Call and type(n.exc.func) is ast.Name and n.exc.func.id == "AccordError"),
                    "only AccordError or a bare reraising statement is allowed")
            require(n.cause is None or (type(n.cause) is ast.Name and n.cause.id in locals_), "invalid cause")
    return snippet

def parse_patch(raw):
    require(type(raw) is str, "patch must be JSON text")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError as exc:
        raise InvalidPatch("invalid patch Unicode") from exc
    require(0 < len(encoded) <= MAX_PATCH_BYTES, "patch size")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        raise InvalidPatch(str(exc)) from exc
    require(type(value) is dict and set(value) == {"format", "base_sha256", "span", "replacement"}, "patch shape")
    w = load_workload()
    require(value["format"] == "accord.parse-replacement/1", "patch format")
    require(value["base_sha256"] == w["source_sha256"], "wrong source fingerprint")
    require(value["span"] == "Contract.parse.json-input", "wrong replacement span")
    validate_snippet(value["replacement"])
    return value

def materialize(raw):
    value = parse_patch(raw)
    original, w = baseline(), load_workload()
    start, end = w["span"]["start_byte"], w["span"]["end_byte_exclusive"]
    require(sha(original[start:end]) == w["span"]["sha256"], "span fingerprint mismatch")
    candidate = original[:start] + value["replacement"].encode() + original[end:]
    try:
        ast.parse(candidate.decode("utf-8"))
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise InvalidPatch("materialized module invalid") from exc
    return candidate

def patch_for_replacement(replacement):
    w = load_workload()
    validate_snippet(replacement)
    return json.dumps({"format":"accord.parse-replacement/1", "base_sha256":w["source_sha256"],
                       "span":"Contract.parse.json-input", "replacement":replacement}, ensure_ascii=True)

def materialize_replacement(source, replacement):
    require(type(source) in (str, bytes), "source must be text or bytes")
    try:
        raw_source = source.encode("utf-8") if type(source) is str else source
    except UnicodeError as exc:
        raise InvalidPatch("invalid source Unicode") from exc
    require(len(raw_source) <= 16384 and raw_source == baseline(), "source differs from frozen baseline")
    return materialize(patch_for_replacement(replacement))

def run_behavior(raw=None):
    # The caller cannot provide an executable path or subprocess command.
    source = baseline() if raw is None else materialize(raw)
    with tempfile.TemporaryDirectory(prefix="accord-shared-kv-run-", dir="/private/tmp") as temp:
        root = Path(temp)
        (root / "ir.py").write_bytes(source)
        process = subprocess.run([sys.executable, "-I", "-B", str(HERE / "probe.py"), str(root / "ir.py")],
                                 cwd=root, env={"PATH":"/usr/bin:/bin"}, capture_output=True, text=True, timeout=10)
    require(process.returncode == 0, "probe failed: " + process.stderr[-2000:])
    require(len(process.stdout) <= 65536, "probe output bound")
    report = json.loads(process.stdout)
    require(report["source_sha256"] == sha(source), "probe source mismatch")
    report.update(returncode=process.returncode, changed=source != baseline(),
                  checker_sha256=sha(Path(__file__).read_bytes()), probe_sha256=sha((HERE / "probe.py").read_bytes()))
    return report

def implementation_materials():
    w = load_workload()
    return w["implementation_task"] + "\n\n" + w["common_materials"]
def review_materials(raw, report):
    w = load_workload()
    parsed = parse_patch(raw)
    require(report["source_sha256"] == sha(materialize(raw)), "review evidence belongs to another candidate")
    return (w["review_task"] + "\n\n" + w["common_materials"] + "\n\nCandidate patch:\n" +
            json.dumps(parsed, ensure_ascii=True) + "\n\nIndependent host evidence:\n" + json.dumps(report, ensure_ascii=True))

def self_test():
    w = load_workload()
    start, end = w["span"]["start_byte"], w["span"]["end_byte_exclusive"]
    original_snippet = baseline()[start:end].decode()
    value = {"format":"accord.parse-replacement/1", "base_sha256":w["source_sha256"],
             "span":"Contract.parse.json-input", "replacement":original_snippet}
    raw = json.dumps(value)
    require(materialize(raw) == baseline(), "baseline roundtrip")
    cases = [("extra key", json.dumps(value | {"extra":1})),
             ("wrong hash", json.dumps(value | {"base_sha256":"0"*64})),
             ("wrong span", json.dumps(value | {"span":"other"})),
             ("wrong format", json.dumps(value | {"format":"other"})),
             ("duplicate key", raw[:-1] + ',"span":"Contract.parse.json-input"}'),
             ("nonfinite JSON", raw[:-1] + ',"extra":NaN}'),
             ("array root", "[]"), ("fenced JSON", "```json\n" + raw + "\n```"),
             ("oversize JSON", " "*8193), ("surrogate JSON", "\ud800"),
             ("escaped surrogate snippet", json.dumps(value | {"replacement":"\ud800"})),
             ("oversize snippet", json.dumps(value | {"replacement":" "*2049})),
             ("non-text snippet", json.dumps(value | {"replacement":3})),
             ("not JSON", "{"), ("concatenated JSON", raw+raw), ("nontext input", value)]
    unsafe = ["import os", "open('x')", "__import__('os')", "while data: pass", "return data",
              "data = data.__class__", "data = 65536 ** 65536", "del data", "global data",
              "data = data.encode.__call__(data)", "data = [x for x in data]", "data = lambda: data", "data.encode('utf-8', errors='ignore')",
              "json.loads(data)", "len = data", "raise ValueError('invalid contract JSON')",
              "try:\n                pass\n            except:\n                pass"]
    for statement in unsafe:
        snippet = "        if isinstance(data, (str, bytes)):\n            " + statement + "\n"
        cases.append((statement, json.dumps(value | {"replacement":snippet})))
    for name, case in cases:
        try:
            materialize(case)
        except InvalidPatch:
            continue
        raise AssertionError("unsafe patch accepted: " + name)
    report = run_behavior()
    failed = [c["name"] for c in report["checks"] if not c["passed"]]
    require(failed == ["raw_high_surrogate", "raw_low_surrogate", "raw_surrogate_in_json", "raw_surrogate_pair"],
            "baseline behavior differed from frozen defect")
    return {"passed":True, "patch_rejection_cases":len(cases), "baseline_roundtrip":True,
            "baseline_behavior_count":report["count"], "baseline_expected_failures":failed,
            "baseline_report":report}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--patch", type=Path)
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps(self_test(), indent=2))
    elif args.baseline:
        print(json.dumps(run_behavior(), indent=2))
    elif args.patch:
        with args.patch.open() as stream:
            raw = stream.read(MAX_PATCH_BYTES + 1)
        print(json.dumps(run_behavior(raw), indent=2))
if __name__ == "__main__":
    main()
