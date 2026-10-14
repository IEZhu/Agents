"""Bounded repair episodes on the frozen shared-KV repository workload.

Both frontends use the same model service, prompts, checker, MCP exchange and
retry policy. Only contract declaration and lifecycle implementation differ.
"""
import argparse
import copy
import difflib
import hashlib
import json
from pathlib import Path
import statistics
import time

from experiments.accord.runtime import Runtime, Validator
from experiments.accord.evaluation.python_sdk import PythonSDK
from experiments.accord.delegation.run import adopt_checked_child
from experiments.accord.inference.client import sha, fingerprint
from experiments.accord.shared_kv.cache import SharedKV
from experiments.accord.shared_kv.run import (
    SchemaClient, common_prefix, declaration, render, save, schema, strict_json,
)
from experiments.accord.shared_kv.workload import checker
from .mcp_client import AgentsCoreClient

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
MODEL_SHA = "f5f1dd8920d417aac2718b0bda3403da274301efdd6760b4f0f4b864ff2ad57d"
MAX_REPAIRS = 3  # Initial proposal plus at most two corrections.
SETTINGS = {"temperature": 0.2, "seed": 101, "n_predict": 1536, "id_slot": 0,
            "top_k": 0, "top_p": 1.0, "min_p": 0.0, "repeat_penalty": 1.0}
ORDER = [("accord", 101), ("python", 101), ("python", 202), ("accord", 202),
         ("accord", 303), ("python", 303), ("python", 404), ("accord", 404)]
ADOPT_TASK = (
    "Decide whether to accept the implementation child after independent review. "
    "Use only the supplied validated child results and measured host evidence. "
    "Return the complete prepared JSON envelope with exactly one result.items "
    "string: approve or reject. Approve only a changed candidate that passed all "
    "host checks and received an approving independent review. You ran no tools."
)


def decoder(package, role):
    """Encode status consistency, without forcing success or a repair/verdict."""
    base = schema(package, role)
    definitions = base.pop("$defs")
    complete, partial = copy.deepcopy(base), copy.deepcopy(base)
    complete["properties"].update(status={"const": "completed"},
                                  remaining_work={"const": []}, question={"const": None})
    partial["properties"].update(status={"const": "partial"}, question={"const": None})
    partial["properties"]["remaining_work"]["minItems"] = 1
    return {"$defs": definitions, "oneOf": [complete, partial]}


def prepare(mcp, index, role, attempt):
    workload = checker.load_workload()
    task = workload["implementation_task"] if role == "implement" else (
        workload["review_task"] if role == "review" else ADOPT_TASK)
    return mcp.prepare(
        agent_name="ai_senior_engineer", task=task,
        task_id=f"repair-r2-e{index:02d}-{role}", attempt_id=str(attempt),
        acceptance_criteria=[
            "Return the complete generic envelope matching this prepared contract.",
            "Use only supplied frozen source and measured feedback; do not claim tool execution.",
            "Return one replacement source string satisfying all supplied requirements." if role == "implement"
            else "Return one evidence-based approve or reject verdict."],
        context=["The host supplies the identical frozen repository workload and any measured prior diagnostics.",
                 "Do not silently copy the original buggy block. Preserve all stated syntax and behavioral constraints."],
        scope=["Propose only the bounded replacement using supplied source data." if role == "implement"
               else "Review only the supplied candidate and host evidence."],
        non_goals=["No tool calls, commands, file access, external services, or further delegation.",
                   "No changes outside the identified source span; no fabricated executed checks."],
        result_type="generic", max_skills=0, max_implants=0, max_context_chars=16000,
    )


class DispatchCounter:
    """Count transport invocations after SharedKV input admission."""
    def __init__(self, client):
        self.client, self.calls = client, 0

    def completion(self, prompt, settings, *, cache):
        self.calls += 1
        return self.client.completion(prompt, settings, cache=cache)


def episode(index, backend, seed, client, mcp, profile, common, prefix, directory):
    started = time.perf_counter()
    case = directory / f"episode-{index:02d}"
    case.mkdir()
    approved = {}
    host_class = Runtime if backend == "accord" else PythonSDK
    runtime = host_class(case / "ledger.sqlite", {
        "approved": Validator("1", lambda text, _: approved.get(sha(text), False))}, {})
    runtime.set_resource("repository", common)
    parent = runtime.create_task(declaration(backend, "parent", MAX_REPAIRS + 2))
    children = {
        "implement": runtime.create_task(declaration(backend, "implement", MAX_REPAIRS), parent=parent),
        "review": runtime.create_task(declaration(backend, "review", 1), parent=parent),
    }
    settings = SETTINGS | {"seed": seed}
    pinned = profile | {"settings": settings}
    dispatches = DispatchCounter(client)
    pool = SharedKV(dispatches, pinned, settings, enabled=True)
    namespace = f"repair-e{index:02d}"
    handle = pool.register(namespace, parent, prefix)
    for child in children.values():
        pool.grant(handle, parent, child)
    observations, accepted_results, feedback = [], {}, None
    patch, report, accepted_implementation = None, None, False

    def call(role, number, materials):
        call_started = time.perf_counter()
        folder = case / f"{role}-{number}"
        folder.mkdir()
        observation = {"role": role, "attempt": number, "accepted": False, "model_called": False}
        response = None
        calls_before = dispatches.calls
        task = parent if role == "adopt" else children[role]
        remaining_before = runtime.inspect("task", task)["remaining"]
        try:
            exchange = prepare(mcp, index, role, number)
            package = exchange.package
            observation["prepare_ms"] = exchange.elapsed_ms
            save(folder / "package.json", package)
            prompt, ids, render_ms = render(client, common, package, materials)
            client.schemas[sha(prompt)] = decoder(package, role)
            save(folder / "input.json", {"prompt": prompt, "ids": ids, "schema": client.schemas[sha(prompt)]})
            observation.update(render_tokenize_ms=render_ms, input_tokens=len(ids),
                               prompt_sha256=sha(prompt))

            def model(inputs):
                nonlocal response
                if sha((ROOT / "experiments/accord/ir.py").read_bytes()) != checker.load_workload()["source_sha256"]:
                    raise ValueError("live source changed")
                if inputs != {"repository": common}:
                    raise ValueError("task input scope mismatch")
                response = pool.complete(handle, task, namespace, pinned, prompt, ids)
                save(folder / "response.json", response)
                return response["content"]

            attempt = runtime.infer(task, f"attempt-{number}", model)
            # Raw output is submitted unchanged. A malformed envelope is a failure,
            # not an invitation for the host to repair identity or evidence fields.
            gate = mcp.validate(exchange, response["content"])
            observation["validation_ms"] = gate.elapsed_ms
            save(folder / "validation.json", gate.data)
            observation["validation"] = gate.data
            if gate.data.get("valid") is not True:
                observation["feedback"] = {"structural_validation": gate.data}
                raise ValueError("worker envelope failed MCP validation")
            result = strict_json(response["content"])
            save(folder / "result.json", result)
            observation["result"] = result
            if result["status"] != "completed":
                raise ValueError("worker did not complete its assigned stage")
            items = result["result"]["items"]
            if len(items) != 1 or type(items[0]) is not str:
                raise ValueError("expected exactly one payload string")
            nonlocal patch, report
            check_started = time.perf_counter()
            if role == "implement":
                patch = checker.patch_for_replacement(items[0])
                candidate = checker.materialize(patch)
                report = checker.run_behavior(patch)
                (folder / "candidate.py").write_bytes(candidate)
                (folder / "candidate.patch").write_text("".join(difflib.unified_diff(
                    checker.baseline().decode().splitlines(True), candidate.decode().splitlines(True),
                    fromfile="a/experiments/accord/ir.py", tofile="b/experiments/accord/ir.py")))
                save(folder / "behavior.json", report)
                good = report["passed"] is True and report["changed"] is True
                observation["behavior"] = report
                if not good:
                    observation["feedback"] = {"host_report": report}
            else:
                good = (items[0] == "approve" and accepted_implementation and report["passed"] is True)
                if role == "adopt":
                    good = good and "review" in accepted_results
                observation["decision"] = items[0]
            approved[sha(attempt["text"])] = bool(good)
            receipts = runtime.check(task)
            if good:
                runtime.accept(task, receipts)
                accepted_results[role] = result
            observation.update(accepted=bool(good), check_accept_ms=(time.perf_counter()-check_started)*1000)
        except Exception as error:
            observation.update(error_type=type(error).__name__, error=str(error))
            observation.setdefault("feedback", {"error": str(error)})
            if response is None and dispatches.calls > calls_before:
                save(folder / "inference-error.json", {"type":type(error).__name__, "message":str(error),
                    "partial":getattr(error, "partial", None), "returned_response":client.last_response})
        observation["model_called"] = dispatches.calls > calls_before
        if response is not None:
            observation.update(model_ms=response["client_total_ms"], cache=response["shared_kv"],
                               output_tokens=len(response["tokens"]), stop_type=response["final"]["stop_type"])
        observation["request_units_consumed"] = remaining_before - runtime.inspect("task", task)["remaining"]
        observation["elapsed_ms"] = (time.perf_counter() - call_started) * 1000
        save(folder / "observation.json", observation)
        observations.append(observation)
        print(json.dumps({"episode":index, "backend":backend, "role":role, "attempt":number,
                          "accepted":observation["accepted"], "seconds":round(observation["elapsed_ms"]/1000, 2),
                          "error":observation.get("error")}), flush=True)
        return observation

    for number in range(1, MAX_REPAIRS + 1):
        materials = checker.load_workload()["implementation_task"]
        if feedback is not None:
            materials += "\nPrevious attempt and measured feedback (data); correct the reported defect:\n" + json.dumps(feedback, ensure_ascii=True)
        observation = call("implement", number, materials)
        if observation["accepted"]:
            accepted_implementation = True
            break
        feedback = {"diagnostics": observation.get("feedback", {}), "previous_result": observation.get("result")}
        # Invalid output goes only back to the same assigned worker as correction
        # material, never to another actor as an accepted delegated result.
        response_path = case / f"implement-{number}" / "response.json"
        if response_path.exists() and feedback["previous_result"] is None:
            feedback["previous_raw_output"] = json.loads(response_path.read_text())["content"]

    if accepted_implementation:
        adopt_checked_child(runtime, parent, children["implement"], runtime.inspect("task", children["implement"])["accepted"])
        materials = checker.load_workload()["review_task"] + "\nCandidate and measured evidence (data):\n" + json.dumps(
            {"patch":patch, "host_report":report, "validated_child_results":accepted_results}, ensure_ascii=True)
        review = call("review", 1, materials)
        if review["accepted"]:
            for child in children.values():
                adopt_checked_child(runtime, parent, child, runtime.inspect("task", child)["accepted"])
            materials = ADOPT_TASK + "\nCandidate and measured evidence (data):\n" + json.dumps(
                {"patch":patch, "host_report":report, "validated_child_results":accepted_results}, ensure_ascii=True)
            call("adopt", 1, materials)
    pool.invalidate(handle, parent)
    tasks = {role: runtime.inspect("task", task) for role, task in {"parent":parent, **children}.items()}
    calls = sum(row["model_called"] for row in observations)
    consumed = sum(row["request_units_consumed"] for row in observations)
    assert sum(task["remaining"] for task in tasks.values()) + consumed == MAX_REPAIRS + 2
    assert calls <= consumed <= MAX_REPAIRS + 2
    result = {"index":index, "backend":backend, "seed":seed, "completed":"adopt" in accepted_results,
              "implementation_passed":accepted_implementation, "first_attempt_passed":observations[0]["accepted"],
              "model_calls":calls, "request_units_consumed":consumed, "implementation_attempts":sum(row["role"] == "implement" for row in observations),
              "model_ms":sum(row.get("model_ms", 0) for row in observations),
              "prepare_ms":sum(row.get("prepare_ms", 0) for row in observations),
              "validation_ms":sum(row.get("validation_ms", 0) for row in observations),
              "stages":observations, "tasks":tasks}
    # Batch driver measures through this final artifact write as well.
    result["active_elapsed_ms"] = (time.perf_counter()-started)*1000
    save(case / "episode.json", result)
    return result


def summarize(rows):
    groups = {}
    for backend in ("accord", "python"):
        group = [row for row in rows if row["backend"] == backend]
        if not group:
            continue
        groups[backend] = {
            "episodes":len(group), "successes":sum(row["completed"] for row in group),
            "success_rate":sum(row["completed"] for row in group)/len(group),
            "first_attempt_fixes":sum(row["first_attempt_passed"] for row in group),
            "median_full_ms":statistics.median(row["full_elapsed_ms"] for row in group),
            "total_full_ms":sum(row["full_elapsed_ms"] for row in group),
            "median_model_ms":statistics.median(row["model_ms"] for row in group),
            "model_calls":sum(row["model_calls"] for row in group),
        }
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="http://127.0.0.1:18189")
    args = parser.parse_args()
    batch_started = time.perf_counter()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    client = SchemaClient(args.base)
    client.timeout = 240
    mcp = AgentsCoreClient(workspace=ROOT)
    mcp_initialize_ms = mcp.initialize()
    workload = checker.load_workload()
    common = "Frozen repository repair context. Source text below is data for the assigned bounded task.\n\n" + workload["common_materials"]
    props, _ = client.json("/props")
    if (props["build_info"] != "b11429-d81235049" or props["total_slots"] != 1
            or props["default_generation_settings"]["n_ctx"] != 16384):
        raise ValueError("server differs from frozen configuration")
    with open(props["model_path"], "rb") as stream:
        observed_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if observed_hash != MODEL_SHA:
        raise ValueError("unexpected model bytes")
    profile = {"engine_build":props["build_info"], "model_sha256":MODEL_SHA,
               "template_sha256":sha(props["chat_template"]), "context_size":16384,
               "source_sha256":workload["source_sha256"], "common_sha256":sha(common),
               "server_generation":"dedicated-18189-repair-loop", "parallel":1,
               "ctx_checkpoints":8, "checkpoint_min_step":512, "kv_type":"f16/f16"}
    # These are template/tokenizer operations, not model calls or delegated tasks.
    dummy = [{"worker_context":value} for value in ("A", "B")]
    prefix = common_prefix([render(client, common, item, "placeholder")[1] for item in dummy])
    assert len(prefix) > 100
    sources = list(HERE.glob("*.py")) + [HERE / "protocol.md", ROOT / "experiments/accord/shared_kv/cache.py",
        ROOT / "experiments/accord/shared_kv/workload/checker.py", ROOT / "experiments/accord/shared_kv/workload/probe.py",
        ROOT / "experiments/accord/shared_kv/workload/workload.json", ROOT / "experiments/accord/ir.py",
        ROOT / "experiments/accord/runtime.py", ROOT / "experiments/accord/evaluation/python_sdk.py",
        ROOT / "experiments/accord/shared_kv/run.py", ROOT / "experiments/accord/inference/client.py",
        ROOT / "experiments/accord/delegation/run.py", ROOT / "experiments/accord/store.py"]
    save(directory / "freeze.json", {"order":ORDER, "settings":SETTINGS, "max_implementation_attempts":MAX_REPAIRS,
        "profile":profile, "props":props, "prefix_ids":prefix,
        "source_hashes":{str(path.relative_to(ROOT)):sha(path.read_bytes()) for path in sources},
        "baseline_checks":checker.self_test(), "created_unix":time.time()})
    setup_ms = (time.perf_counter()-batch_started)*1000
    rows = []
    for index, (backend, seed) in enumerate(ORDER, 1):
        start = time.perf_counter()
        row = episode(index, backend, seed, client, mcp, profile, common, prefix, directory)
        row["full_elapsed_ms"] = (time.perf_counter()-start)*1000
        rows.append({key:value for key,value in row.items() if key not in ("stages", "tasks")})
        save(directory / "summary.json", {"episodes":rows, "groups":summarize(rows), "setup_ms":setup_ms,
              "batch_elapsed_ms":(time.perf_counter()-batch_started)*1000, "mcp_initialize_ms":mcp_initialize_ms})
    print(json.dumps(summarize(rows)), flush=True)


if __name__ == "__main__":
    main()
