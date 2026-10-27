#!/usr/bin/env python3
"""Validate a frozen LOAD-01 plan or simulate it offline; never qualify capacity.

Standard library only. No engine, network, model, process or environment reads.
See docs/hosted-load-baseline.md for the versioned manifest and accounting rules.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

MAX_MANIFEST_BYTES = 128 * 1024
MAX_ARRIVALS = 50_000
MAX_TIME_MS = 3_600_000
MAX_QUEUED_BYTES = 64 * 1024**2
MAX_REQUEST_BYTES = 1024**2
MAX_SIZE = 1024**5
CLASS_NAMES = frozenset({
    "cold_start", "warm_read", "routing", "short_input", "long_input",
    "history_rebuild", "write", "git_sync",
})
COUNT_KEYS = (
    "offered", "late", "enqueued", "dispatched", "known_admitted",
    "simulated_admitted", "completed", "rejected", "unknown", "expired", "dropped",
    "dropped_queue_full", "dropped_byte_budget", "dropped_generator_late",
    "expired_queue_ttl", "expired_deadline", "unknown_deadline", "unknown_response",
)


class ManifestError(ValueError):
    """A redacted, schema-only diagnostic, never an echoed input value."""


def _object(value, fields: set[str], path: str) -> dict:
    if type(value) is not dict or value.keys() != fields:
        raise ManifestError(f"{path}: fields do not match schema")
    return value


def _integer(value, minimum: int, maximum: int, path: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ManifestError(f"{path}: expected integer in [{minimum}, {maximum}]")
    return value


def _enum(value, allowed: set[str] | frozenset[str], path: str) -> None:
    if type(value) is not str or value not in allowed:
        raise ManifestError(f"{path}: unsupported value")


def _validate(data) -> dict:
    _object(data, {"schema_version", "pins", "corpus", "limits", "resources", "classes"}, "manifest")
    _integer(data["schema_version"], 1, 1, "schema_version")
    pins = _object(data["pins"], {"core_sha", "image_digest", "model_digest", "config_digest"}, "pins")
    for key, value in pins.items():
        pattern = r"[0-9a-f]{40}" if key == "core_sha" else r"sha256:[0-9a-f]{64}"
        if type(value) is not str or re.fullmatch(pattern, value) is None:
            raise ManifestError(f"pins.{key}: expected exact lowercase digest")
    corpus = _object(data["corpus"], {"documents", "bytes", "tokens"}, "corpus")
    for key, value in corpus.items():
        _integer(value, 0, 1_000_000_000 if key == "documents" else MAX_SIZE, f"corpus.{key}")
    limits = _object(data["limits"], {
        "duration_ms", "max_active", "max_waiting", "max_queued_bytes", "max_generator_lag_ms",
    }, "limits")
    for key, lower, upper in (
        ("duration_ms", 1, MAX_TIME_MS), ("max_active", 1, 1024),
        ("max_waiting", 0, 10_000), ("max_queued_bytes", 0, MAX_QUEUED_BYTES),
        ("max_generator_lag_ms", 0, 60_000),
    ):
        _integer(limits[key], lower, upper, f"limits.{key}")
    resources = _object(data["resources"], {
        "planned_memory_bytes", "memory_reserve_bytes", "memory_stop_used_bytes",
        "planned_disk_bytes", "disk_reserve_bytes", "disk_stop_free_bytes",
        "cpu_stop_percent", "pg_latency_increase_stop_percent",
    }, "resources")
    for key, value in resources.items():
        lower, upper = (1, 100) if key == "cpu_stop_percent" else (
            (0, 1000) if key == "pg_latency_increase_stop_percent" else (1, MAX_SIZE))
        _integer(value, lower, upper, f"resources.{key}")
    if resources["memory_reserve_bytes"] + resources["memory_stop_used_bytes"] > resources["planned_memory_bytes"]:
        raise ManifestError("resources: memory stop threshold must preserve the reserve")
    if not resources["disk_reserve_bytes"] <= resources["disk_stop_free_bytes"] < resources["planned_disk_bytes"]:
        raise ManifestError("resources: disk stop threshold must preserve the reserve below planned capacity")
    classes = data["classes"]
    if type(classes) is not list or not 1 <= len(classes) <= len(CLASS_NAMES):
        raise ManifestError("classes: expected between 1 and 8 classes")
    names, total = set(), 0
    for index, item in enumerate(classes):
        path = f"classes[{index}]"
        _object(item, {
            "name", "cache_state", "arrivals", "first_arrival_ms", "interval_ms", "request_bytes",
            "tool_calls_per_action", "deadline_ms", "queue_ttl_ms", "slo", "synthetic",
        }, path)
        _enum(item["name"], CLASS_NAMES, f"{path}.name")
        if item["name"] in names:
            raise ManifestError("classes: duplicate class")
        names.add(item["name"])
        _enum(item["cache_state"], {"cold", "hit", "miss", "not_applicable"}, f"{path}.cache_state")
        for key, lower, upper in (
            ("arrivals", 1, MAX_ARRIVALS), ("first_arrival_ms", 0, MAX_TIME_MS),
            ("interval_ms", 0, MAX_TIME_MS), ("request_bytes", 1, MAX_REQUEST_BYTES),
            ("tool_calls_per_action", 1, 100), ("deadline_ms", 1, MAX_TIME_MS),
            ("queue_ttl_ms", 1, MAX_TIME_MS),
        ):
            _integer(item[key], lower, upper, f"{path}.{key}")
        if item["first_arrival_ms"] + (item["arrivals"] - 1) * item["interval_ms"] >= limits["duration_ms"]:
            raise ManifestError(f"{path}: arrivals must fall within the offered window")
        if item["arrivals"] % item["tool_calls_per_action"]:
            raise ManifestError(f"{path}: arrivals must represent whole user actions")
        total += item["arrivals"]
        slo = _object(item["slo"], {
            "p95_ms", "p99_ms", "max_queue_ms", "max_rejection_ppm", "max_noncompletion_ppm",
        }, f"{path}.slo")
        for key, value in slo.items():
            _integer(value, 0, 1_000_000 if key.endswith("ppm") else MAX_TIME_MS, f"{path}.slo.{key}")
        if slo["p95_ms"] > slo["p99_ms"]:
            raise ManifestError(f"{path}.slo: p95 budget must not exceed p99 budget")
        synthetic = _object(item["synthetic"], {
            "service_ms", "generator_delay_ms", "outcome", "admission",
        }, f"{path}.synthetic")
        _integer(synthetic["service_ms"], 0, MAX_TIME_MS, f"{path}.synthetic.service_ms")
        _integer(synthetic["generator_delay_ms"], 0, 60_000, f"{path}.synthetic.generator_delay_ms")
        _enum(synthetic["outcome"], {"completed", "rejected", "unknown"}, f"{path}.synthetic.outcome")
        _enum(synthetic["admission"], {"admitted", "not_admitted", "unknown"}, f"{path}.synthetic.admission")
        if (synthetic["outcome"] == "completed" and synthetic["admission"] == "not_admitted") or (
            synthetic["outcome"] == "rejected" and synthetic["admission"] != "not_admitted"
        ):
            raise ManifestError(f"{path}.synthetic: inconsistent outcome and admission")
    if total > MAX_ARRIVALS:
        raise ManifestError("classes: total arrivals exceed 50000")
    return data


@dataclass(frozen=True)
class FrozenManifest:
    canonical_json: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def freeze_manifest(data: dict) -> FrozenManifest:
    """Validate and copy all inputs into an immutable, canonical JSON snapshot."""
    _validate(data)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(canonical.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest: exceeds 128 KiB")
    return FrozenManifest(canonical)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError("manifest: duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ManifestError("manifest: non-finite JSON number")


def _decode(text: str) -> dict:
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise ManifestError("manifest: invalid JSON") from error


def load_manifest(path: Path) -> FrozenManifest:
    with path.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest: exceeds 128 KiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise ManifestError("manifest: expected UTF-8 JSON") from error
    return freeze_manifest(_decode(text))


def _snapshot_data(manifest: FrozenManifest) -> dict:
    # The public Python API also validates snapshots; direct construction cannot
    # bypass bounds enforced by the file loader.
    if type(manifest) is not FrozenManifest or type(manifest.canonical_json) is not str:
        raise ManifestError("manifest: expected frozen manifest")
    if len(manifest.canonical_json) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest: exceeds 128 KiB")
    return _validate(_decode(manifest.canonical_json))


@dataclass(frozen=True)
class Arrival:
    request_id: int
    class_name: str
    scheduled_ms: int


def _schedule(data: dict) -> tuple[Arrival, ...]:
    entries = sorted(
        (item["first_arrival_ms"] + sequence * item["interval_ms"], index, sequence)
        for index, item in enumerate(data["classes"])
        for sequence in range(item["arrivals"])
    )
    return tuple(Arrival(number, data["classes"][index]["name"], time)
                 for number, (time, index, _sequence) in enumerate(entries))


def offered_schedule(manifest: FrozenManifest) -> tuple[Arrival, ...]:
    """Precompute open arrivals, independent of service outcomes or completions."""
    return _schedule(_snapshot_data(manifest))


@dataclass(frozen=True)
class SyntheticObservation:
    """Future adapter seam: synthetic predictions are never server evidence.

    A live implementation needs a separate runner and observed admission/response
    records, including unresolved requests. It must not relabel this object.
    """
    end_ms: int
    caller_deadline_ms: int
    outcome: str
    admission: str
    deadline_elapsed: bool


def _synthetic_observation(arrival: Arrival, item: dict, dispatched_ms: int) -> SyntheticObservation:
    profile = item["synthetic"]
    deadline = arrival.scheduled_ms + item["deadline_ms"]
    response_at = dispatched_ms + profile["service_ms"]
    timed_out = response_at >= deadline
    return SyntheticObservation(
        response_at, deadline, "unknown" if timed_out else profile["outcome"],
        profile["admission"], timed_out,
    )


@dataclass
class _Stats:
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNT_KEYS, 0))
    latency: list[int] = field(default_factory=list)
    generator: list[int] = field(default_factory=list)
    queue: list[int] = field(default_factory=list)
    service: list[int] = field(default_factory=list)


def _quantiles(values: list[int], population: str) -> dict:
    values = sorted(values)
    return {
        "population": population, "samples": len(values), "method": "nearest_rank",
        "p95": values[math.ceil(len(values) * 95 / 100) - 1] if values else None,
        "p99": values[math.ceil(len(values) * 99 / 100) - 1] if values else None,
        "max": values[-1] if values else None,
    }


def _summary(stats: _Stats) -> dict:
    return {
        "counts": stats.counts,
        "latency_ms": _quantiles(stats.latency, "synthetic_completed_only_from_scheduled_arrival"),
        "generator_delay_ms": _quantiles(stats.generator, "all_offered"),
        "queue_delay_ms": _quantiles(stats.queue, "all_dispatched_and_expired_after_enqueue"),
        "service_ms": _quantiles(stats.service, "synthetic_completed_only_from_dispatch"),
    }


def _comparison(value: int | float | None, limit: int) -> dict:
    return {
        "value": value, "limit": limit,
        "status": "no_samples" if value is None else (
            "within_synthetic_budget" if value <= limit else "exceeds_synthetic_budget"),
    }


def _base_report(manifest: FrozenManifest, data: dict, mode: str) -> dict:
    return {
        "report_schema_version": 1, "mode": mode, "evidence_kind": "synthetic_plan",
        "manifest_sha256": manifest.sha256, "manifest_hash_encoding": "canonical_json_utf8",
        "pins": data["pins"], "frozen_manifest": data,
        "qualification": {
            "status": "unmeasured", "performance_pass": False,
            "reason": "Offline synthetic preparation cannot qualify LOAD-01 or Hosted capacity.",
            "evidence": dict.fromkeys((
                "pin_artifact_verification", "real_server_admission", "latency", "resources",
                "postgresql", "vds", "chatgpt", "durability", "recovery",
            ), "unmeasured"),
        },
        "resource_comparisons": {
            "status": "unmeasured", "frozen_thresholds": data["resources"],
            "observed": None, "stop_thresholds_enforced_against_real_resources": False,
        },
    }


def validation_report(manifest: FrozenManifest) -> dict:
    data = _snapshot_data(manifest)
    manifest = freeze_manifest(data)
    report = _base_report(manifest, data, "validate")
    report["validation"] = "valid_plan_only"
    return report


def simulate(manifest: FrozenManifest) -> dict:
    """Run a bounded FIFO virtual clock, draining every offered arrival exactly once."""
    data = _snapshot_data(manifest)
    manifest = freeze_manifest(data)
    limits = data["limits"]
    classes = {item["name"]: item for item in data["classes"]}
    stats = {name: _Stats() for name in classes}
    schedule = _schedule(data)
    events = []
    digest = hashlib.sha256()
    for arrival in schedule:
        ready = arrival.scheduled_ms + classes[arrival.class_name]["synthetic"]["generator_delay_ms"]
        heapq.heappush(events, (ready, 2, arrival.request_id, "arrival"))
        digest.update(json.dumps([arrival.request_id, arrival.class_name, arrival.scheduled_ms],
                                 separators=(",", ":")).encode("utf-8") + b"\n")
    waiting: OrderedDict[int, int] = OrderedDict()  # request ID -> expiry time
    active: dict[int, tuple[int, SyntheticObservation]] = {}
    terminal: set[int] = set()
    queued_bytes = 0
    peak_active = peak_waiting = peak_bytes = peak_retained = now = 0
    retained: set[int] = set()

    def finish(arrival: Arrival, outcome: str, reason: str | None = None) -> None:
        if arrival.request_id in terminal:
            raise RuntimeError("duplicate terminal outcome")
        terminal.add(arrival.request_id)
        record = stats[arrival.class_name]
        record.counts[outcome] += 1
        if reason:
            record.counts[reason] += 1
        if outcome == "completed":
            record.latency.append(now - arrival.scheduled_ms)

    def ready_at(arrival: Arrival) -> int:
        return arrival.scheduled_ms + classes[arrival.class_name]["synthetic"]["generator_delay_ms"]

    def expire(arrival: Arrival) -> None:
        stats[arrival.class_name].queue.append(now - ready_at(arrival))
        reason = "expired_deadline" if now >= arrival.scheduled_ms + classes[arrival.class_name]["deadline_ms"] else "expired_queue_ttl"
        finish(arrival, "expired", reason)

    def dispatch(arrival: Arrival) -> None:
        nonlocal peak_active
        record = stats[arrival.class_name]
        record.counts["dispatched"] += 1
        record.queue.append(now - ready_at(arrival))
        observation = _synthetic_observation(arrival, classes[arrival.class_name], now)
        record.counts["simulated_admitted"] += int(observation.admission == "admitted")
        active[arrival.request_id] = (now, observation)
        peak_active = max(peak_active, len(active))
        if observation.deadline_elapsed:
            heapq.heappush(events, (observation.caller_deadline_ms, -1, arrival.request_id, "deadline"))
        heapq.heappush(events, (observation.end_ms, 0, arrival.request_id, "response"))

    def drain() -> None:
        nonlocal queued_bytes
        while waiting and len(active) < limits["max_active"]:
            request_id, expiry = waiting.popitem(last=False)
            arrival = schedule[request_id]
            queued_bytes -= classes[arrival.class_name]["request_bytes"]
            if now >= expiry:
                expire(arrival)
            else:
                dispatch(arrival)

    while events and (len(terminal) < len(schedule) or active or waiting):
        now, _priority, request_id, event = heapq.heappop(events)
        arrival = schedule[request_id]
        item, record = classes[arrival.class_name], stats[arrival.class_name]
        if event == "deadline":
            # Caller uncertainty does not cancel native work or release its slot.
            finish(arrival, "unknown", "unknown_deadline")
            retained.add(request_id)
            peak_retained = max(peak_retained, len(retained))
        elif event == "response":
            dispatched_at, observation = active.pop(request_id)
            retained.discard(request_id)
            if request_id not in terminal:
                reason = "unknown_response" if observation.outcome == "unknown" else None
                finish(arrival, observation.outcome, reason)
                if observation.outcome == "completed":
                    record.service.append(now - dispatched_at)
            drain()
        elif event == "expiry":
            if request_id in waiting:
                del waiting[request_id]
                queued_bytes -= item["request_bytes"]
                expire(arrival)
        else:
            record.counts["offered"] += 1
            delay = now - arrival.scheduled_ms
            record.generator.append(delay)
            record.counts["late"] += int(delay > 0)
            if delay > limits["max_generator_lag_ms"]:
                finish(arrival, "dropped", "dropped_generator_late")
            elif now >= arrival.scheduled_ms + item["deadline_ms"]:
                finish(arrival, "expired", "expired_deadline")
            elif len(active) < limits["max_active"]:
                dispatch(arrival)
            elif len(waiting) >= limits["max_waiting"]:
                finish(arrival, "dropped", "dropped_queue_full")
            elif queued_bytes + item["request_bytes"] > limits["max_queued_bytes"]:
                finish(arrival, "dropped", "dropped_byte_budget")
            else:
                expiry = min(now + item["queue_ttl_ms"], arrival.scheduled_ms + item["deadline_ms"])
                waiting[request_id] = expiry
                queued_bytes += item["request_bytes"]
                record.counts["enqueued"] += 1
                peak_waiting = max(peak_waiting, len(waiting))
                peak_bytes = max(peak_bytes, queued_bytes)
                heapq.heappush(events, (expiry, 1, request_id, "expiry"))
    if len(terminal) != len(schedule) or active or waiting or queued_bytes:
        raise RuntimeError("incomplete offered-work accounting")
    aggregate = _Stats()
    for record in stats.values():
        counts = record.counts
        if counts["offered"] != sum(counts[k] for k in ("completed", "rejected", "unknown", "expired", "dropped")):
            raise RuntimeError("terminal accounting mismatch")
        if counts["dispatched"] != counts["completed"] + counts["rejected"] + counts["unknown"]:
            raise RuntimeError("dispatch accounting mismatch")
        for key in COUNT_KEYS:
            aggregate.counts[key] += counts[key]
        for key in ("latency", "generator", "queue", "service"):
            getattr(aggregate, key).extend(getattr(record, key))
    report = _base_report(manifest, data, "simulate")
    report["evidence_kind"] = "virtual_clock_synthetic"
    report["schedule"] = {
        "sha256": digest.hexdigest(), "hash_encoding": "json_id_class_scheduled_ms_lines_utf8",
        "offered_window_ms": limits["duration_ms"], "offered_calls": len(schedule),
        "offered_calls_per_second": len(schedule) * 1000 / limits["duration_ms"],
        "first_arrival_ms": schedule[0].scheduled_ms, "last_arrival_ms": schedule[-1].scheduled_ms,
        "virtual_end_ms": now, "real_elapsed_ms": None,
        "represented_user_actions": sum(item["arrivals"] // item["tool_calls_per_action"] for item in classes.values()),
    }
    report["occupancy"] = {"scope": "synthetic_local_generator", "peak_active": peak_active,
                           "peak_waiting": peak_waiting, "peak_queued_bytes": peak_bytes,
                           "peak_retained_after_deadline": peak_retained,
                           "final_active": 0, "final_waiting": 0, "final_queued_bytes": 0}
    report["aggregate"] = _summary(aggregate)
    report["per_class"] = {name: _summary(record) for name, record in stats.items()}
    report["budget_comparisons"] = {}
    for name, summary in report["per_class"].items():
        slo, counts = classes[name]["slo"], summary["counts"]
        rejection_ppm = (counts["rejected"] + counts["dropped"]) * 1_000_000 / counts["offered"]
        noncompletion_ppm = (counts["offered"] - counts["completed"]) * 1_000_000 / counts["offered"]
        report["budget_comparisons"][name] = {
            "p95_ms": _comparison(summary["latency_ms"]["p95"], slo["p95_ms"]),
            "p99_ms": _comparison(summary["latency_ms"]["p99"], slo["p99_ms"]),
            "max_queue_ms": _comparison(summary["queue_delay_ms"]["max"], slo["max_queue_ms"]),
            "generator_lag_ms": _comparison(summary["generator_delay_ms"]["max"], limits["max_generator_lag_ms"]),
            "rejection_ppm": _comparison(rejection_ppm, slo["max_rejection_ppm"]),
            "noncompletion_ppm": _comparison(noncompletion_ppm, slo["max_noncompletion_ppm"]),
        }
    return report


def write_report(output: Path, report: dict) -> None:
    """Atomically replace a private report, including an existing output symlink."""
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + output.name, dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mode", choices=("validate", "simulate"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.manifest.resolve() == args.output.resolve():
            raise ManifestError("output must not replace the manifest")
        manifest = load_manifest(args.manifest)
        report = validation_report(manifest) if args.mode == "validate" else simulate(manifest)
        write_report(args.output, report)
    except ManifestError as error:
        print(f"Invalid plan: {error}", file=sys.stderr)
        return 2
    except OSError:
        print("Unable to read the manifest or atomically write the report.", file=sys.stderr)
        return 2
    print(json.dumps({"mode": args.mode, "manifest_sha256": report["manifest_sha256"],
                      "qualification": "unmeasured", "performance_pass": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
