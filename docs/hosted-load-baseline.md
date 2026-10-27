# Offline Hosted load baseline preparation

`scripts/dev/hosted_load_baseline.py` prepares a reproducible LOAD-01 workload
plan and exercises bounded generator accounting with a virtual clock. It has
only `validate` and `simulate` modes. Neither mode sends requests, loads models,
imports the engine, inspects processes, or reads environment values. It uses
Python 3.11 or newer and the standard library.

This component does **not** complete LOAD-01, measure real performance, verify
the pinned artifacts, or authorize Hosted publication. Every report has
`qualification.status: "unmeasured"` and `performance_pass: false`, including a
simulation that stays within every synthetic budget. CPU, memory, disk,
PostgreSQL, VDS, ChatGPT, durability and recovery evidence remain unmeasured.

## Freeze and run a plan

Create a private JSON manifest before a run. The example below contains
**deliberately fictitious hash pins, workload assumptions, and planning budgets**;
none is a measurement, recommendation, or existing capacity claim. Replace the
pins with the exact intended Core commit and image/model/configuration digests
before retaining a real qualification plan. Keep credentials and configuration
contents outside the manifest; only their nonsecret configuration digest belongs
here. Corpus sizes describe synthetic inputs; the simulator allocates no corpus
or request bodies.

```json
{
  "schema_version": 1,
  "pins": {
    "core_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "image_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "model_digest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "config_digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "corpus": {"documents": 100, "bytes": 10000, "tokens": 1000},
  "limits": {
    "duration_ms": 1000,
    "max_active": 1,
    "max_waiting": 2,
    "max_queued_bytes": 200,
    "max_generator_lag_ms": 20
  },
  "resources": {
    "planned_memory_bytes": 1000000000,
    "memory_reserve_bytes": 200000000,
    "memory_stop_used_bytes": 800000000,
    "planned_disk_bytes": 10000000000,
    "disk_reserve_bytes": 1000000000,
    "disk_stop_free_bytes": 1000000000,
    "cpu_stop_percent": 80,
    "pg_latency_increase_stop_percent": 10
  },
  "classes": [
    {
      "name": "warm_read",
      "cache_state": "hit",
      "arrivals": 4,
      "first_arrival_ms": 0,
      "interval_ms": 10,
      "request_bytes": 100,
      "tool_calls_per_action": 2,
      "deadline_ms": 500,
      "queue_ttl_ms": 200,
      "slo": {
        "p95_ms": 100,
        "p99_ms": 200,
        "max_queue_ms": 100,
        "max_rejection_ppm": 0,
        "max_noncompletion_ppm": 0
      },
      "synthetic": {
        "service_ms": 100,
        "generator_delay_ms": 0,
        "outcome": "completed",
        "admission": "admitted"
      }
    },
    {
      "name": "write",
      "cache_state": "not_applicable",
      "arrivals": 1,
      "first_arrival_ms": 40,
      "interval_ms": 0,
      "request_bytes": 100,
      "tool_calls_per_action": 1,
      "deadline_ms": 500,
      "queue_ttl_ms": 200,
      "slo": {
        "p95_ms": 100,
        "p99_ms": 200,
        "max_queue_ms": 100,
        "max_rejection_ppm": 0,
        "max_noncompletion_ppm": 0
      },
      "synthetic": {
        "service_ms": 20,
        "generator_delay_ms": 0,
        "outcome": "completed",
        "admission": "admitted"
      }
    }
  ]
}
```

Save the example as `plan.json` in a private working directory, then run from the
repository root, using the appropriate paths:

```sh
python3 -B scripts/dev/hosted_load_baseline.py validate --manifest plan.json --output validation.json
python3 -B scripts/dev/hosted_load_baseline.py simulate --manifest plan.json --output simulation.json
```

`validate` checks and freezes the plan without running the simulation. Exit code
0 means that validation or simulation completed; it never means a performance
pass. Invalid input or file I/O failure returns 2. A simulation exceeding its
synthetic budgets still returns 0 so callers must inspect the explicit budget
comparisons. There is no live mode, HTTP adapter flag, remote target, or hidden
default workload. Both commands require an output path and refuse to overwrite
the input manifest.

## Manifest version 1

All fields in the example are required. Unknown fields, duplicate JSON keys,
unknown enum values, non-finite numbers, floats, booleans in integer fields, and
out-of-range values are rejected. Errors describe schema locations without
echoing arbitrary input values. Manifest files are limited to 128 KiB of UTF-8
JSON. No payload, endpoint, credential, arbitrary class label, or environment
field is accepted.

| Field | Bound or interpretation |
| --- | --- |
| `schema_version` | Exactly integer 1. |
| `pins.core_sha` | Exact 40-character lowercase Git SHA-1 commit ID. |
| Other pins | `sha256:` followed by exactly 64 lowercase hexadecimal characters. Contents and existence are not verified. |
| `corpus.documents` | Integer 0 through 1 billion. |
| `corpus.bytes`, `corpus.tokens` | Integer 0 through 2^50; descriptive metadata only. |
| `limits.duration_ms` | Integer 1 through 3,600,000; exclusive end of the offered arrival window. |
| `limits.max_active` | 1 through 1,024 simulated work slots, including work retained after caller timeout. |
| `limits.max_waiting` | 0 through 10,000 waiting requests. |
| `limits.max_queued_bytes` | 0 through 64 MiB, counting declared bytes of waiting requests only. |
| `limits.max_generator_lag_ms` | 0 through 60,000; later simulated deliveries are dropped. |
| Resource byte fields | Positive integers through 2^50; planned capacities and frozen protective thresholds only. |
| `resources.cpu_stop_percent` | 1 through 100. |
| `resources.pg_latency_increase_stop_percent` | 0 through 1,000, relative to a future measured PostgreSQL baseline. |
| `classes` | 1 through 8 unique classes, at most 50,000 offered calls in total. |
| `classes[].name` | `cold_start`, `warm_read`, `routing`, `short_input`, `long_input`, `history_rebuild`, `write`, or `git_sync`. |
| `cache_state` | `cold`, `hit`, `miss`, or `not_applicable`; descriptive, without automatic latency effects. |
| `arrivals` | 1 through 50,000 calls per class; counts freeze the workload mix. |
| `first_arrival_ms`, `interval_ms` | 0 through 3,600,000. Every scheduled call must precede `duration_ms`. A zero interval is a simultaneous burst. |
| `request_bytes` | 1 byte through 1 MiB, used for waiting-byte accounting, without allocating a payload. |
| `tool_calls_per_action` | 1 through 100; `arrivals` must be divisible by it. Calls are scheduled independently, not chained by user-action completion. |
| `deadline_ms`, `queue_ttl_ms` | 1 through 3,600,000. Deadline starts at scheduled arrival; queue TTL starts at generator delivery. |
| SLO latency/queue fields | 0 through 3,600,000 ms, with `p95_ms <= p99_ms`. |
| SLO `*_ppm` fields | 0 through 1 million; parts per million of all offered calls, not just successes. |
| `synthetic.service_ms` | 0 through 3,600,000; fixed virtual service duration for that class. |
| `synthetic.generator_delay_ms` | 0 through 60,000; fixed virtual lateness for that class. |
| `synthetic.outcome` | `completed`, `rejected`, or `unknown`; an assumed synthetic response. |
| `synthetic.admission` | `admitted`, `not_admitted`, or `unknown`; synthetic assertion only. Completed work cannot be `not_admitted`; rejected work must be `not_admitted`. |

The memory stop-used threshold plus reserve cannot exceed planned memory.
The disk stop-free threshold must preserve the disk reserve and stay below
planned disk capacity. The simulator does not measure or enforce these resource
thresholds against any machine. Actual headroom and protective stop handling are
prerequisites for a future live run.

## Scheduling and accounting

The entire offered schedule is fixed before execution. Call `i` in each class
arrives at `first_arrival_ms + i * interval_ms`, regardless of responses, queue
occupancy or failures. Equal scheduled timestamps use manifest class order and
then call order. Generator delay changes delivery time, never the offered time.
There are no implicit retries, rescheduling, or discarded offered calls.

Delivered requests dispatch immediately when a slot is free; otherwise a FIFO
queue applies both count and byte bounds. Queue-full rejection takes precedence
when both bounds are exhausted. An item expires at the earlier of its queue TTL
or absolute caller deadline. An expired item never dispatches, including when a
slot becomes free at exactly the expiry time. Caller deadline wins over a
response at exactly the same time. Responses free capacity before new arrivals
at the same virtual timestamp.

An active caller timeout records `unknown` but **retains its work slot until the
synthetic service finishes**. A later response frees the slot without rewriting
the caller's unknown result or adding a second terminal result. The bounded
simulation drains outstanding work; it does not pretend that timeout cancels
native work. `peak_retained_after_deadline` exposes retained slots. No wall-clock
sleep or actual concurrency is involved.

Terminal counts obey:

```text
offered = completed + rejected + unknown + expired + dropped
dispatched = completed + rejected + unknown
```

`rejected` is a synthetic response after dispatch; `dropped` is a generator-side
failure, subdivided into queue count, queued bytes, and excessive generator lag.
`expired` means not dispatched before queue TTL/deadline. `unknown` means the
caller lacks a conclusive outcome, including a response profile marked unknown
or a caller deadline. `late`, `enqueued`, and `simulated_admitted` are overlapping
counters and must not be added to terminal counts. `known_admitted` is always
zero because no server admission is observed. `simulated_admitted` counts only
the manifest's explicit synthetic admission assertions at dispatch; a successful
response is never silently promoted to real admission evidence.

The report checks accounting and includes aggregate and per-class counts. The
offered rate is calls divided by the frozen offered window, **not** measured
throughput or a supported user count. The represented action count is only the
sum of each class's calls divided by its declared calls per action.

## Reports, quantiles, and privacy

Reports include the exact pins and frozen manifest. `manifest_sha256` hashes
UTF-8 JSON with sorted object keys, compact separators, and no trailing newline.
Whitespace in the input file is irrelevant; class list order is preserved.
`schedule.sha256` independently hashes each compact JSON
`[request_id, class_name, scheduled_ms]` followed by a newline, in offered order.
Changing service duration leaves this schedule hash unchanged.

All quantiles use nearest rank. End-to-end latency runs from scheduled arrival
to synthetic completion, including generator and queue delay; service latency
runs from dispatch to completion. Both populations contain completed calls only,
are labeled accordingly, and include their sample counts. Missing samples are
`null`, never zero. Generator delay includes every offered call. Queue delay
includes dispatches (zero for immediate dispatch) and items that expire after
enqueue, so expired waits remain visible. A call expiring before enqueue has no
queue sample.

Frozen SLO comparisons include p95/p99, maximum queue wait, generator lag,
`rejection_ppm = (rejected + dropped) / offered * 1,000,000`, and
`noncompletion_ppm = (offered - completed) / offered * 1,000,000`.
Their statuses are `within_synthetic_budget`, `exceeds_synthetic_budget`, or
`no_samples`. Good successful-call percentiles cannot hide the accompanying drop,
rejection, expiry, and unknown counts. There is deliberately no overall synthetic
performance pass. Resource comparisons retain the frozen thresholds with
`observed: null` and `status: "unmeasured"`.

Like `scripts/daemon_baseline.py`, output uses a fresh private temporary file,
flush/fsync, and atomic replacement. On POSIX the replacement file has mode 0600,
including replacement of an existing less-private file or a symlink; the symlink
target remains untouched. A failed replace preserves the old output and removes
the temporary file. This is a POSIX file-privacy contract, not a Windows ACL or
power-loss durability qualification. Use a private existing output directory;
existing directory permissions are not changed. Reports do not contain source
paths, command arguments, arbitrary payloads, credentials, environment values,
or personal data. The console prints only mode, manifest hash and qualification.

## Focused verification and future live work

Run only the offline suite when developing this component:

```sh
python3 -B -m unittest discover -s tests -p test_hosted_load_baseline.py -v
```

The dedicated [Hosted baseline workflow](../.github/workflows/hosted-load-baseline.yml)
runs this standard-library suite separately from model/runtime tests. It needs
no model download or live target. This harness is intentionally independent of
the sequential development benchmarks and daemon process snapshot.

Before real LOAD-01 qualification:

1. Verify the pinned private image, Core/model/configuration contents and target
   authorization. Establish representative synthetic corpora and per-action tool
   counts for cold start, reads/routing, input sizes, rebuilds, writes and Git sync,
   with cache and slow-dependency cases. These classes are names here, not real
   implementations of those operations.
2. Measure VDS CPU architecture/quota, available memory, disk I/O, OS and existing
   PostgreSQL baseline on an isolated test instance. Freeze safe headroom,
   PostgreSQL impact limits and stop handling before sending load. This manifest
   is preparation; do not retrofit SLOs after observing a qualification result.
3. Implement a **separate explicitly authorized live runner** at the conceptual
   `SyntheticObservation` boundary. Use an independent monotonic arrival clock,
   observed admission acknowledgements, bounded active/retained work and queue
   bytes, request deadlines, stable IDs and explicit unresolved outcomes. A
   timeout/disconnect must not free a native work slot until completion or proven
   cancellation. Do not relabel synthetic observations as real measurements.
4. Preserve offered-versus-dispatched times, measure generator lag and capacity,
   and use an external generator if co-location distorts the target. Record
   missed arrivals, rejection/expiry/unknown denominators, actual resource samples
   and stop events without storing request content or credentials. Add an
   independently bounded reconciliation mechanism for accepted writes.
5. Retain exact-image baseline evidence, then perform the separate LOAD-02 burst,
   recovery, soak and adverse-condition work against the final scheduler/storage
   contract. Freeze recovery-time and indexing/sync-lag budgets for those tests;
   version 1 of this offline manifest does not model those measurements.
6. Verify actual ChatGPT initialize/tool calls, OAuth, error/busy/retry behavior
   and durable write reconciliation for LOAD-03. Operational, authorization,
   privacy, publisher and OpenAI review gates remain separate. No public launch
   or capacity promise follows from this simulator.
