"""Independent data consumer for JobSpec; never imports Contract or Runtime."""

from dataclasses import asdict
import json
import sys

from ..evaluation.python_sdk import JobSpec
from ..ir import canonical, digest, require

FIELDS = {"ir", "name", "inputs", "output_max_bytes", "checks", "publish_to", "request_budget", "policy_revision"}


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate JSON key: " + key)
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("nonfinite JSON: " + value)


def decode_contract(value):
    require(type(value) is dict and set(value) == FIELDS, "unknown or missing contract field")
    require(value["ir"] == "accord.contract/0.1", "unsupported contract version")
    require(type(value["inputs"]) is list and type(value["publish_to"]) is list, "resources must be arrays")
    require(type(value["checks"]) is dict, "checks must be an object")
    require(len(canonical(value)) <= 65536, "contract too large")
    return JobSpec(value["name"], tuple(value["inputs"]), value["output_max_bytes"],
                   tuple(value["checks"].items()), tuple(value["publish_to"]),
                   value["request_budget"], value["policy_revision"])


def decode_set(data):
    require(type(data) is str and len(data.encode()) <= 131072, "invalid contract set size")
    value = json.loads(data, object_pairs_hook=pairs, parse_constant=reject_constant)
    require(type(value) is dict and set(value) == {"ir", "parent", "child"}, "unknown contract set fields")
    require(value["ir"] == "accord.contract-set/0.1", "unsupported contract set version")
    return {role: decode_contract(value[role]) for role in ("parent", "child")}


def export_contract(spec):
    value = asdict(spec)
    value.update(ir="accord.contract/0.1", inputs=list(spec.inputs), publish_to=list(spec.publish_to),
                 checks=dict(spec.checks))
    return value


def export_set(specs):
    return canonical({"ir": "accord.contract-set/0.1", **{role: export_contract(spec) for role, spec in specs.items()}}).decode()


def inspect_set(data, registry):
    specs = decode_set(data)
    parent, child = specs["parent"], specs["child"]
    for spec in specs.values():
        require(spec.policy_revision == parent.policy_revision, "policy mismatch")
        for name, revision in spec.checks:
            require(registry.get(name) == revision, "unavailable validator: " + name)
    require(set(child.inputs) <= set(parent.inputs), "child expands input scope")
    require(set(child.publish_to) <= set(parent.publish_to), "child expands effect scope")
    require(child.output_max_bytes <= parent.output_max_bytes, "child expands output limit")
    require(child.request_budget <= parent.request_budget, "child allocation exceeds parent budget")
    normalized = export_set(specs)
    return {"digest": digest("accord.contract-set/0.1", json.loads(normalized)),
            "parent": export_contract(parent), "child": export_contract(child),
            "root_credits_after_allocation": parent.request_budget - child.request_budget}


def main():
    from pathlib import Path
    data = Path(sys.argv[1]).read_text()
    print(json.dumps(inspect_set(data, {"bundle_scope": "1", "behavior": "1", "behavior_v2": "2"}), sort_keys=True))
    assert "experiments.accord.runtime" not in sys.modules
    assert "experiments.accord.delegation.declarations" not in sys.modules


if __name__ == "__main__":
    main()
