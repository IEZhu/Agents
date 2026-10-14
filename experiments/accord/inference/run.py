"""Run the frozen 2x2 pilot against a dedicated loopback llama.cpp process.

python3 -m experiments.accord.inference.run --pid PID --output OUTPUT_DIRECTORY
The server must already use the configuration documented in protocol.md.
Existing output directories are rejected; model calls are never retried.
"""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import subprocess
import time

from .analyze import summary
from .client import Client, encode, fingerprint, make_artifact, sha, verify_artifact, verify_cache
from .workload import WORKLOAD_PATH, load_workload, messages_for, score, self_test

MODEL_SHA = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
ENGINE_COMMIT = "d81235049384534c167caea52b85a694f6103d14"
CELLS = ("text_cold", "ids_cold", "text_warm", "ids_warm")
SEED = 20261014
SETTINGS = {"temperature": 0, "seed": 42, "n_predict": 128, "id_slot": 0,
            "top_k": 0, "top_p": 1, "min_p": 0, "repeat_penalty": 1,
            "presence_penalty": 0, "frequency_penalty": 0, "ignore_eos": False}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def hash_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def process_sample(pid):
    rss, cpu = subprocess.check_output(["ps", "-p", str(pid), "-o", "rss=,time="], text=True).split()
    seconds = 0.0
    for field in cpu.split(":"):
        seconds = seconds * 60 + float(field)
    return {"rss_bytes": int(rss) * 1024, "cpu_seconds": seconds}


def common_prefix(sequences):
    length = 0
    for column in zip(*sequences):
        if len(set(column)) != 1:
            break
        length += 1
    return sequences[0][:length]


def plan(cases):
    blocks = [(i, repeat) for repeat in range(3) for i in range(len(cases))]
    random.Random(SEED).shuffle(blocks)
    result = []
    for i, repeat in blocks:
        offset = (i + repeat) % len(CELLS)
        for cell in CELLS[offset:] + CELLS[:offset]:
            result.append({"case": cases[i]["name"], "repeat": repeat, "cell": cell})
    return result


def prepare(client, workload, props):
    root = Path(__file__).resolve().parents[3]
    for path, expected in workload["source_hashes"].items():
        if hash_file(root / path) != expected:
            raise ValueError("source changed: " + path)
    if props["build_info"] != "b11429-d81235049" or props["total_slots"] != 1:
        raise ValueError("unexpected engine build or slots")
    if props["default_generation_settings"]["n_ctx"] != 8192:
        raise ValueError("unexpected context size")
    if hash_file(props["model_path"]) != MODEL_SHA:
        raise ValueError("unexpected model weights/tokenizer")
    profile = {"engine_commit": ENGINE_COMMIT, "model_sha256": MODEL_SHA,
               "chat_template_sha256": sha(props["chat_template"]),
               "context_sha256": workload["context_sha256"], "context_size": 8192,
               "tokenization": {"add_special": True, "parse_special": True},
               "backend": "Metal", "kv_type": "f16", "flash_attention": True,
               "batch_size": 512, "ubatch_size": 512, "settings": SETTINGS}
    prepared = []
    for case in workload["cases"]:
        rendered, render_ms = client.json("/apply-template", {"messages": messages_for(case, workload)})
        prompt = rendered["prompt"]
        tokenized, tokenize_ms = client.json("/tokenize", {"content": prompt, **profile["tokenization"]})
        ids = tokenized["tokens"]
        # Both input representations use the engine's native tokenizer path. This
        # also checks that integer input is accepted unchanged, not stringified.
        echoed, _ = client.json("/tokenize", {"content": ids, **profile["tokenization"]})
        if echoed["tokens"] != ids or len(ids) + SETTINGS["n_predict"] > 8192:
            raise ValueError("input IDs mismatch or context overflow")
        start = time.perf_counter()
        artifact = make_artifact(profile, prompt, ids)
        artifact_create_ms = (time.perf_counter() - start) * 1000
        prepared.append({"case": case["name"], "prompt": prompt, "artifact": artifact,
                         "render_ms": render_ms, "tokenize_ms": tokenize_ms,
                         "artifact_create_ms": artifact_create_ms,
                         "input_tokens": len(ids), "native_integer_roundtrip_equal": True})
    return profile, prepared


def integrity_controls(profile, prepared, prefix):
    original = prepared[0]
    artifact = original["artifact"]
    results = []
    for field in ("model_sha256", "chat_template_sha256", "context_sha256"):
        wrong = copy.deepcopy(profile)
        wrong[field] = "0" * 64
        try:
            verify_artifact(artifact, wrong, original["prompt"])
        except ValueError:
            results.append({"control": "wrong_" + field, "rejected_before_dispatch": True})
        else:
            raise AssertionError("incompatible artifact accepted")
    changed = copy.deepcopy(artifact)
    changed["ids"][0] += 1
    try:
        verify_artifact(changed, profile, original["prompt"])
    except ValueError:
        results.append({"control": "altered_ids", "rejected_before_dispatch": True})
    else:
        raise AssertionError("altered IDs accepted")
    for changed_profile, changed_prefix in (({**profile, "model_sha256": "0" * 64}, prefix),
                                             (profile, prefix[:-1])):
        try:
            verify_cache({"profile": profile, "prefix_sha256": fingerprint(prefix)}, changed_profile, changed_prefix)
        except ValueError:
            results.append({"control": "cache_profile_or_prefix", "rejected_before_dispatch": True})
        else:
            raise AssertionError("incompatible cache accepted")
    return results


def trial(client, item, prepared, case, profile, prefix, cache, pid):
    warm, ids = item["cell"].endswith("warm"), item["cell"].startswith("ids")
    erase, erase_ms = client.json("/slots/0?action=erase", {})
    before = process_sample(pid)
    start = time.perf_counter()
    prompt = verify_artifact(prepared["artifact"], profile, prepared["prompt"]) if ids else prepared["prompt"]
    if warm:
        verify_cache(cache, profile, prefix)
    validation_ms = (time.perf_counter() - start) * 1000
    restore, restore_ms = None, 0
    if warm:
        restore, restore_ms = client.json("/slots/0?action=restore", {"filename": cache["filename"]})
        if restore["n_restored"] != len(prefix):
            raise ValueError("incomplete prefix restoration")
    response = client.completion(prompt, SETTINGS, cache=warm)
    after = process_sample(pid)
    final = response["final"]
    timing = final["timings"]
    # Return invariant errors as evidence, then abort after the caller persists it.
    errors = []
    if timing["cache_n"] != (len(prefix) if warm else 0):
        errors.append("unexpected reused token count")
    if timing["cache_n"] + timing["prompt_n"] != prepared["input_tokens"]:
        errors.append("evaluated plus reused tokens differ from full input")
    if final.get("truncated"):
        errors.append("context truncated")
    return {**item, **response, **timing, "input_tokens": prepared["input_tokens"],
            "score": score(response["content"], case["expected"]), "stop_type": final["stop_type"],
            "erase": erase, "erase_ms_excluded": erase_ms, "restore": restore,
            "restore_ms": restore_ms, "validation_ms": validation_ms,
            "operational_ttft_ms": response["client_ttft_ms"] + restore_ms + validation_ms,
            "operational_total_ms": response["client_total_ms"] + restore_ms + validation_ms,
            "server_cpu_seconds": after["cpu_seconds"] - before["cpu_seconds"],
            "rss_bytes": max(before["rss_bytes"], after["rss_bytes"]), "invariant_errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="http://127.0.0.1:18187")
    parser.add_argument("--slot-directory", type=Path, default=Path(".accord-local/slots"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    client = Client(args.base)
    workload = load_workload()
    props, _ = client.json("/props")
    profile, prepared = prepare(client, workload, props)
    prefix = common_prefix([p["artifact"]["ids"] for p in prepared])
    if not prefix:
        raise ValueError("no reusable prefix")
    order = plan(workload["cases"])
    setup = {"started_at": datetime.now(timezone.utc).isoformat(), "protocol_sha256": hash_file(Path(__file__).with_name("protocol.md")),
             "workload_sha256": hash_file(WORKLOAD_PATH), "profile": profile, "props": props,
             "prepared": prepared, "prefix_ids": prefix, "prefix_tokens": len(prefix),
             "seed": SEED, "order": order, "scorer_checks": self_test(),
             "integrity_controls": integrity_controls(profile, prepared, prefix),
             "harness_hashes": {p.name:hash_file(p) for p in Path(__file__).parent.glob("*.py")},
             "server_command": subprocess.check_output(["ps", "-p", str(args.pid), "-o", "command="], text=True).strip(),
             "hardware": {"chip": "Apple M4 Max", "memory_bytes": 38654705664, "os": "macOS 26.5.1", "backend": "Metal"}}
    write_json(args.output / "prepared.json", setup)
    print(json.dumps({"prepared": len(prepared), "input_tokens": [p["input_tokens"] for p in prepared], "prefix_tokens": len(prefix)}), flush=True)
    setup["warmups"] = []
    rendered, _ = client.json("/apply-template", {"messages": [{"role":"user", "content":"Return exactly {\"answer\":42}"}]})
    for _ in range(3):
        client.json("/slots/0?action=erase", {})
        setup["warmups"].append(client.completion(rendered["prompt"], {**SETTINGS, "n_predict": 16}, cache=False))
    client.json("/slots/0?action=erase", {})
    setup["prime"] = client.completion(prefix, {**SETTINGS, "n_predict": 1}, cache=False)
    filename = "accord-prefix-" + fingerprint(profile)[:16] + ".bin"
    saved, save_ms = client.json("/slots/0?action=save", {"filename": filename})
    if saved["n_saved"] != len(prefix):
        raise ValueError("unexpected saved prefix size")
    start = time.perf_counter()
    snapshot_sha = hash_file(args.slot_directory / filename)
    setup["snapshot_hash_ms"] = (time.perf_counter() - start) * 1000
    setup["save"], setup["save_ms"] = saved, save_ms
    setup["cache"] = {"filename": filename, "profile": profile, "prefix_sha256": fingerprint(prefix),
                      "snapshot_sha256": snapshot_sha, "bytes": saved["n_written"]}
    write_json(args.output / "setup.json", setup)
    prepared_by_name = {p["case"]: p for p in prepared}
    cases = {c["name"]: c for c in workload["cases"]}
    rows = []
    with (args.output / "trials.jsonl").open("x") as output:
        for index, item in enumerate(order):
            try:
                row = trial(client, item, prepared_by_name[item["case"]], cases[item["case"]],
                            profile, prefix, setup["cache"], args.pid)
            except Exception as error:
                write_json(args.output / "failure.json", {"trial_index": index, **item, "error": repr(error), "partial": getattr(error, "partial", None)})
                raise
            row["trial_index"] = index
            output.write(encode(row).decode() + "\n")
            output.flush()
            rows.append(row)
            if row["invariant_errors"]:
                raise RuntimeError(row["invariant_errors"])
            if (index+1) % 8 == 0:
                print(json.dumps({"completed": index+1, "of": len(order), "last_cell": row["cell"],
                                  "ttft_ms": round(row["client_ttft_ms"], 2), "reused_tokens": row["cache_n"]}), flush=True)
    result = summary(rows, setup)
    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(args.output / "summary.json", result)
    client.json("/slots/0?action=erase", {})
    print(json.dumps({"completed": len(rows), "summary": str(args.output / "summary.json")}), flush=True)


if __name__ == "__main__":
    main()
