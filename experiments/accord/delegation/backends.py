"""Four arms: representation crossed with two lifecycle implementations."""

import json

from ..ir import Contract
from ..runtime import Runtime
from ..evaluation.python_sdk import JobSpec, PythonSDK
from .declarations import native_specs
from .sdk_ir import decode_set, export_contract, inspect_set
from .workload import HERE, registry

ARMS = ("accord_json", "accord_python", "sdk_python", "sdk_json")


def configurations(arm, variant=1, data=None):
    if arm.endswith("_python"):
        return native_specs(Contract if arm.startswith("accord") else JobSpec, variant)
    data = (HERE / f"contracts-{variant}.json").read_text() if data is None else data
    # Shared policy inspector, independent of the Accord parser/interpreter.
    inspect_set(data, {name: check.revision for name, check in registry().items()})
    if arm == "sdk_json":
        return decode_set(data)
    return {role: Contract.parse(json.loads(data)[role]) for role in ("parent", "child")}


def open_backend(arm, path, sink):
    host = Runtime if arm.startswith("accord") else PythonSDK
    return host(path, registry(), {"patches": sink})


def as_wire(specs):
    return {role: export_contract(spec) for role, spec in specs.items()}
