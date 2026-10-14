"""Trusted independent behavior probe. Run in the checker's isolated subprocess."""
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import sys

resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
# Restricted AST is the execution gate. The audit hook additionally forbids external calls.
def audit(event, args):
    if event.startswith(("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "pty.")):
        raise RuntimeError("external calls prohibited")
sys.addaudithook(audit)
path = Path(sys.argv[1]).resolve()
if path.name != "ir.py" or not path.parent.name.startswith("accord-shared-kv-run-"):
    raise ValueError("only a materialized ir.py is executable")
spec = importlib.util.spec_from_file_location("accord_ir_probe", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
Contract, AccordError = module.Contract, module.AccordError
base = {"ir":"accord.contract/0.1", "name":"document", "inputs":["z","spec"],
        "output_max_bytes":128, "checks":{"nonempty":"1", "mentions_spec":"2"},
        "publish_to":["report","other"], "request_budget":3, "policy_revision":"p1"}
text = json.dumps(base, separators=(",", ":"))
canonical_dict = base | {"inputs":["spec","z"], "publish_to":["other","report"]}
cases = []
def good(name, value):
    cases.append((name, value, "ok", None))
def bad(name, value, message):
    cases.append((name, value, "AccordError", message))
good("valid_dictionary", base)
good("valid_json_text", text)
good("valid_utf8_bytes", text.encode("utf-8"))
good("valid_utf16_bytes", text.encode("utf-16"))
good("valid_utf32_bytes", text.encode("utf-32"))
good("valid_json_ascii_escape", text.replace("document", "doc\\u0075ment"))
good("exact_65536_text_bytes", text + " " * (65536 - len(text.encode())))
good("exact_65536_raw_bytes", text.encode() + b" " * (65536 - len(text.encode())))
for name, value in (("raw_high_surrogate", "\ud800"), ("raw_low_surrogate", "\udfff"),
                    ("raw_surrogate_in_json", text.replace("document", "doc\ud800")),
                    ("raw_surrogate_pair", text.replace("document", "doc\ud83d\ude00"))):
    bad(name, value, "invalid contract JSON")
for name, value in (("oversize_ascii_text", text + " " * 65536),
                    ("oversize_raw_bytes", text.encode() + b" " * 65536),
                    ("oversize_multibyte_text", "é" * 32769)):
    bad(name, value, "contract too large")
for name, value in (("malformed_json", "{"), ("invalid_utf8", b"\xff"),
                    ("truncated_utf8", b"\xc3"), ("duplicate_key", '{"a":1,"a":2}'),
                    ("nonfinite_nan", text.replace('"request_budget":3', '"request_budget":NaN')),
                    ("nonfinite_infinity", text.replace('"request_budget":3', '"request_budget":Infinity')),
                    ("nonfinite_negative_infinity", text.replace('"request_budget":3', '"request_budget":-Infinity'))):
    bad(name, value, "invalid contract JSON")
for name, value in (("array_root", "[]"), ("null_root", "null"), ("integer_root", 3),
                    ("boolean_root", True), ("bytearray_root", bytearray(text.encode()))):
    bad(name, value, "contract must be an object")
for name, change, msg in (
    ("unknown_field", {"extra":1}, "unknown or missing contract fields"),
    ("wrong_version", {"ir":"accord.contract/2"}, "unsupported IR version"),
    ("checks_array", {"checks":[]}, "checks must be an object"),
    ("inputs_string", {"inputs":"spec"}, "resources must be arrays"),
    ("publish_string", {"publish_to":"report"}, "resources must be arrays"),
    ("bool_budget", {"request_budget":True}, "integer outside allowed bounds"),
    ("float_budget", {"request_budget":1.0}, "integer outside allowed bounds"),
    ("negative_budget", {"request_budget":-1}, "integer outside allowed bounds"),
    ("large_budget", {"request_budget":10001}, "integer outside allowed bounds"),
    ("zero_output_limit", {"output_max_bytes":0}, "integer outside allowed bounds"),
    ("large_output_limit", {"output_max_bytes":65537}, "integer outside allowed bounds"),
    ("duplicate_resource", {"inputs":["spec","spec"]}, "duplicate resource"),
    ("empty_checks", {"checks":{}}, "checks must be nonempty"),
    ("bad_identifier", {"name":"../escape"}, "invalid identifier"),
    ("dictionary_surrogate", {"name":"\ud800"}, "invalid identifier"),
    ("integer_check_revision", {"checks":{"check":1}}, "invalid identifier")):
    bad(name, base | change, msg)
bad("escaped_surrogate_json", json.dumps(base | {"name":"\ud800"}), "invalid identifier")
bad("nonascii_identifier_json", json.dumps(base | {"name":"é"}, ensure_ascii=False), "invalid identifier")
bad("missing_field", {k:v for k,v in base.items() if k != "name"}, "unknown or missing contract fields")
checks = []
for name, value, expected_type, expected_message in cases:
    try:
        result = Contract.parse(value)
        actual_type, actual_message = "ok", None
        correct_value = result.to_dict() == canonical_dict
        if correct_value:
            altered = Contract.parse(base | {"request_budget":2})
            correct_value = result.revision != altered.revision and result.revision == Contract.parse(base).revision
    except Exception as exc:
        actual_type, actual_message = type(exc).__name__, str(exc)
        correct_value = True
    passed = actual_type == expected_type and actual_message == expected_message and correct_value
    checks.append({"name":name, "passed":passed, "expected_type":expected_type,
                   "expected_message":expected_message, "actual_type":actual_type,
                   "actual_message":actual_message})
print(json.dumps({"passed":all(c["passed"] for c in checks), "count":len(checks),
                  "source_sha256":hashlib.sha256(path.read_bytes()).hexdigest(), "checks":checks},
                 ensure_ascii=True))
