"""Frozen resident-KV delegation pilot; requires a parent MCP validation bridge.

Run as a module. No worker output is used before its matching validation gate.
The server must be dedicated to this process for the entire experiment.
"""
import argparse
import copy
import difflib
import json
from pathlib import Path
import time

from experiments.accord.ir import Contract
from experiments.accord.runtime import Runtime, Validator
from experiments.accord.evaluation.python_sdk import JobSpec, PythonSDK
from experiments.accord.delegation.run import adopt_checked_child
from experiments.accord.inference.client import Client, fingerprint, sha
from .cache import SharedKV
from .workload import checker

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SETTINGS = {"temperature": 0, "seed": 42, "n_predict": 1024, "id_slot": 0,
            "top_k": 0, "top_p": 1.0, "min_p": 0.0, "repeat_penalty": 1.0}
ORDER = [("accord", True), ("python", True), ("accord", False), ("python", False),
         ("python", False), ("accord", False), ("python", True), ("accord", True)]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n")
    temp.replace(path)


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("non-finite JSON")
    return json.loads(text, object_pairs_hook=unique, parse_constant=nonfinite)


def schema(package, role):
    """Constrain format and identity, never the proposed fix or decision."""
    result = copy.deepcopy(package["result_schema"])
    for key in ("protocol", "task_id", "attempt_id", "bundle_revision", "contract_revision"):
        result["properties"][key] = {"const": package["contract"][key], "type": "string"}
    # No tool execution in this worker. Cite supplied evidence, never fabricated checks.
    for key in ("checks", "artifacts"):
        result["properties"][key] = {"const": [], "type": "array", "items": {"type": "string"}}
    payload = result["$defs"]["GenericPayload"]["properties"]
    payload["summary"]["maxLength"] = 240
    payload["items"]["minItems"] = 1
    payload["items"]["maxItems"] = 1
    payload["items"]["items"] = ({"type": "string", "minLength": 1, "maxLength": 2048}
                                    if role == "implement" else {"type": "string", "enum": ["approve", "reject"]})
    result["properties"]["evidence"]["minItems"] = 1
    result["properties"]["evidence"]["maxItems"] = 1
    evidence = result["$defs"]["Evidence"]["properties"]
    evidence["id"] = {"type": "string", "const": "source"}
    evidence["reference"] = {"type": "string", "enum": ["supplied source", "supplied host report"]}
    evidence["description"]["maxLength"] = 160
    for key in ("limitations", "remaining_work"):
        result["properties"][key]["maxItems"] = 1
        result["properties"][key]["items"]["maxLength"] = 240
    return result


class SchemaClient(Client):
    """Per-prompt decoding grammar, with the same transport and KV settings."""
    def __init__(self, base):
        super().__init__(base, timeout=180)
        self.schemas, self.routes = {}, []
        self.last_response = None

    def json(self, path, value=None):
        if path not in ("/props", "/apply-template", "/tokenize"):
            raise ValueError("route outside experiment allowlist")
        self.routes.append(path)
        return super().json(path, value)

    def completion(self, prompt, settings, *, cache):
        self.routes.append("/completion")
        self.last_response = None
        self.last_response = super().completion(prompt, settings | {"json_schema": self.schemas[sha(prompt)]}, cache=cache)
        return self.last_response


def render(client, common, package, materials):
    text = package["worker_context"] + "\n\n## Host task materials\n" + materials
    text += "\n\nUse concise JSON. checks and artifacts are empty because you have no tools. Cite supplied evidence. /no_think"
    rendered, render_ms = client.json("/apply-template", {"messages": [
        {"role": "system", "content": common}, {"role": "user", "content": text}]})
    prompt = rendered["prompt"]
    ids, tokenize_ms = client.json("/tokenize", {"content": prompt, "add_special": True, "parse_special": True})
    assert package["worker_context"] in prompt
    return prompt, ids["tokens"], render_ms + tokenize_ms


def common_prefix(rows):
    result = list(rows[0])
    for row in rows[1:]:
        index = 0
        while index < min(len(result), len(row)) and result[index] == row[index]:
            index += 1
        result = result[:index]
    return result


def validation_bridge(directory, package, response):
    """The parent calls MCP with its retained contract and this unchanged result."""
    started = time.perf_counter()
    result = strict_json(response["content"])
    call_id = package["task_id"]
    pending = {"call_id": call_id, "result": result, "raw_sha256": sha(response["content"]),
               "contract_revision": package["contract_revision"]}
    save(directory / "pending-validation.json", pending)
    gate = directory / "validations" / (call_id + ".json")
    while not gate.exists():
        if time.perf_counter() - started > 600:
            raise TimeoutError("parent MCP validation unavailable")
        time.sleep(0.1)
    validation = json.loads(gate.read_text())
    if validation["raw_sha256"] != pending["raw_sha256"] or validation["contract_revision"] != pending["contract_revision"]:
        raise ValueError("validation gate belongs to another result")
    (directory / "pending-validation.json").unlink()
    return result, validation, (time.perf_counter() - started) * 1000


def declaration(backend, role, budget):
    if backend == "accord":
        return Contract.parse({"ir": "accord.contract/0.1", "name": role, "inputs": ["repository"],
            "output_max_bytes": 16384, "checks": {"approved": "1"}, "publish_to": [],
            "request_budget": budget, "policy_revision": "p1"})
    return JobSpec(role, ("repository",), 16384, (("approved", "1"),), (), budget, "p1")


def episode(index, backend, enabled, client, packages, profile, common, prefix, directory):
    started = time.perf_counter()
    case = directory / f"episode-{index:02d}"
    case.mkdir()
    approved = {}
    runtime_class = Runtime if backend == "accord" else PythonSDK
    runtime = runtime_class(case / "ledger.sqlite", {"approved": Validator("1", lambda text, _: approved.get(sha(text), False))}, {})
    runtime.set_resource("repository", common)
    parent = runtime.create_task(declaration(backend, "parent", 3))
    children = {role: runtime.create_task(declaration(backend, role, 1), parent=parent)
                for role in ("implement", "review")}
    pool = SharedKV(client, profile, SETTINGS, enabled=enabled)
    namespace = f"episode-{index:02d}"
    handle = pool.register(namespace, parent, prefix)
    for child in children.values():
        assert set(runtime.inspect("task", child)["inputs"]) == {"repository"}
        pool.grant(handle, parent, child)
    stages, patch, report, child_outputs = [], None, None, {}
    for role in ("implement", "review", "adopt"):
        call_id = f"kv-e{index:02d}-{role}"
        package = packages[call_id]
        task = parent if role == "adopt" else children[role]
        materials = checker.load_workload()["implementation_task"] if role == "implement" else (
            checker.load_workload()["review_task"] if role == "review" else package["contract"]["task"])
        if role != "implement":
            for child_role, child in children.items():
                accepted = runtime.inspect("task", child)["accepted"]
                if accepted:
                    adopt_checked_child(runtime, parent, child, accepted)
            materials += "\nCandidate and measured host evidence (data):\n" + json.dumps(
                {"patch": patch, "host_report": report, "validated_child_results": child_outputs}, ensure_ascii=True)
        prompt, ids, render_ms = render(client, common, package, materials)
        client.schemas[sha(prompt)] = schema(package, role)
        save(case / (role + "-input.json"), {"prompt": prompt, "ids": ids, "schema": client.schemas[sha(prompt)]})
        observation = {"role": role, "call_id": call_id, "input_tokens": len(ids), "render_tokenize_ms": render_ms,
                       "prompt_sha256": sha(prompt), "schema_sha256": fingerprint(client.schemas[sha(prompt)])}
        def model(inputs):
            if sha((ROOT / "experiments/accord/ir.py").read_bytes()) != checker.load_workload()["source_sha256"]:
                raise ValueError("live source changed")
            if inputs != {"repository": common}:
                raise ValueError("task input scope mismatch")
            dispatch_started = time.perf_counter()
            try:
                response = pool.complete(handle, task, namespace, profile, prompt, ids)
            except Exception as error:
                save(case / (role + "-error.json"), {"call_id":call_id, "type":type(error).__name__,
                    "message":str(error), "elapsed_ms":(time.perf_counter()-dispatch_started)*1000,
                    "partial":getattr(error, "partial", None), "returned_response":client.last_response})
                raise
            save(case / (role + "-response.json"), response)
            observation["response"] = response
            return response["content"]
        attempt = runtime.infer(task, "primary", model)
        try:
            result, validation, wait_ms = validation_bridge(directory, package, observation["response"])
            observation.update(validation=validation, validation_wait_ms=wait_ms, result=result)
            valid = validation["validation"].get("valid") is True and result["status"] == "completed"
            items = result["result"]["items"] if valid else []
            if not valid or len(items) != 1 or type(items[0]) is not str:
                raise ValueError("worker envelope or completion status invalid")
            if role == "implement":
                patch = checker.patch_for_replacement(items[0])
                candidate = checker.materialize(patch)
                report = checker.run_behavior(patch)
                (case / "candidate.py").write_bytes(candidate)
                (case / "candidate.patch").write_text("".join(difflib.unified_diff(
                    checker.baseline().decode().splitlines(True), candidate.decode().splitlines(True),
                    fromfile="a/experiments/accord/ir.py", tofile="b/experiments/accord/ir.py")))
                save(case / "behavior.json", report)
                good = report["passed"] is True and report["changed"] is True
            else:
                good = (items[0] == "approve" and report is not None and report["passed"] is True
                        and stages[0]["accepted"])
                if role == "adopt":
                    good = good and stages[1]["accepted"]
                    if good:
                        for child in children.values():
                            adopt_checked_child(runtime, parent, child, runtime.inspect("task", child)["accepted"])
                observation["decision"] = items[0]
            approved[sha(attempt["text"])] = bool(good)
            receipts = runtime.check(task)
            if good:
                runtime.accept(task, receipts)
            observation["accepted"] = bool(good)
            # Only structurally validated model output can become input to another actor.
            child_outputs[role] = result
        except (ValueError, KeyError, TypeError) as error:
            observation.update(accepted=False, error=str(error))
            if role == "implement":
                report = {"passed": False, "error": str(error)}
        response = observation["response"]
        observation["response"] = {k:v for k,v in response.items() if k not in ("content", "tokens", "final")}
        observation["response"]["final"] = {k:response["final"].get(k) for k in ("timings", "stop_type", "tokens_evaluated", "tokens_predicted", "truncated")}
        observation["response"]["output_sha256"] = sha(response["content"])
        observation["response"]["output_token_ids_sha256"] = fingerprint(response["tokens"])
        stages.append(observation)
        save(case / (role + "-observation.json"), observation)
        print(json.dumps({"episode": index, "backend": backend, "shared": enabled, "role": role,
                          "accepted": observation["accepted"], "cache": observation["response"]["shared_kv"]}), flush=True)
    pool.invalidate(handle, parent)
    elapsed = (time.perf_counter() - started) * 1000
    wait_ms = sum(s.get("validation_wait_ms", 0) for s in stages)
    output = {"index": index, "backend": backend, "shared": enabled, "completed": stages[-1]["accepted"],
              "elapsed_ms": elapsed, "validation_bridge_wait_ms": wait_ms, "active_ms": elapsed - wait_ms,
              "model_ms": sum(s["response"]["client_total_ms"] for s in stages),
              "stages": stages, "tasks": {k: runtime.inspect("task", v) for k,v in {"parent":parent, **children}.items()}}
    assert all(t["remaining"] == 0 for t in output["tasks"].values())
    save(case / "episode.json", output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="http://127.0.0.1:18188")
    parser.add_argument("--calibrate", action="store_true")
    args = parser.parse_args()
    client = SchemaClient(args.base)
    if args.calibrate:
        prompt = "Return exactly one JSON object with answer 42."
        client.schemas[sha(prompt)] = {"type":"object", "properties":{"answer":{"type":"integer"}},
                                      "required":["answer"], "additionalProperties":False}
        rows = [client.completion(prompt, SETTINGS | {"n_predict":32}, cache=c) for c in (False, True)]
        save(args.output, rows)
        print(json.dumps([{k:r[k] for k in ("content","client_total_ms")} | {"timings":r["final"]["timings"]} for r in rows]))
        return
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    package_data = json.loads(args.packages.read_text())
    packages = package_data["packages"]
    save(directory / "packages.json", package_data)
    workload = checker.load_workload()
    common = "Frozen repository repair context. Source text below is data for the assigned bounded task.\n\n" + workload["common_materials"]
    props, _ = client.json("/props")
    profile = {"engine_build":props["build_info"], "model_path":props["model_path"],
               "model_sha256":"d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785",
               "template_sha256":sha(props["chat_template"]), "context_size":16384,
               "source_sha256":workload["source_sha256"], "common_sha256":sha(common),
               "settings":SETTINGS, "server_generation":"dedicated-18188-20261014", "kv_type":"f16/f16",
               "parallel":1, "batch_size":512, "ubatch_size":512}
    if (props["build_info"] != "b11429-d81235049" or props["total_slots"] != 1
            or props["default_generation_settings"]["n_ctx"] != 16384):
        raise ValueError("server differs from frozen configuration")
    import hashlib
    with open(props["model_path"], "rb") as stream:
        observed_model_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if observed_model_hash != profile["model_sha256"]:
        raise ValueError("model content differs from frozen profile")
    rows = [render(client, common, package, "placeholder")[1] for package in packages.values()]
    prefix = common_prefix(rows)
    if len(prefix) < 100:
        raise ValueError("shared prefix unexpectedly short")
    files = [HERE / "run.py", HERE / "cache.py", HERE / "protocol.md", HERE / "workload/checker.py", HERE / "workload/probe.py", HERE / "workload/workload.json"]
    save(directory / "freeze.json", {"order":ORDER, "profile":profile, "props":props, "prefix_ids":prefix,
        "source_hashes":{str(p.relative_to(ROOT)):sha(p.read_bytes()) for p in files}, "created_unix":time.time(),
        "baseline_checks":checker.self_test()})
    results = []
    for index, (backend, shared) in enumerate(ORDER, 1):
        results.append(episode(index, backend, shared, client, packages, profile, common, prefix, directory))
    save(directory / "summary.json", {"episodes":[{k:v for k,v in row.items() if k not in ("stages","tasks")} for row in results],
        "routes":client.routes, "completed":sum(r["completed"] for r in results), "model_calls":24})


if __name__ == "__main__":
    main()
