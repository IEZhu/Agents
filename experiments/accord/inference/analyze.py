"""Descriptive paired measurements; repetitions are not independent quality tasks."""

from collections import defaultdict
import statistics


def summary(rows, setup):
    groups = defaultdict(list)
    for row in rows:
        groups[row["cell"]].append(row)
    fields = ("client_ttft_ms", "client_total_ms", "operational_ttft_ms", "operational_total_ms",
              "prompt_ms", "predicted_ms", "prompt_n", "cache_n", "predicted_n", "restore_ms", "validation_ms",
              "request_body_bytes", "server_cpu_seconds")
    cells = {}
    for cell, data in groups.items():
        cells[cell] = {"runs": len(data), "correct": sum(row["score"]["correct"] for row in data),
                       "valid_json": sum(row["score"]["valid_json"] for row in data),
                       "limit_stops": sum(row["stop_type"] == "limit" for row in data),
                       "max_sampled_rss_bytes": max(row["rss_bytes"] for row in data)}
        for field in fields:
            values = [row[field] for row in data]
            cells[cell][field] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
    indexed = {(r["case"], r["repeat"], r["cell"]): r for r in rows}
    keys = sorted(set((r["case"], r["repeat"]) for r in rows))
    contrasts = {}
    for left, right in (("text_cold", "ids_cold"), ("text_warm", "ids_warm"),
                        ("text_cold", "text_warm"), ("ids_cold", "ids_warm")):
        pairs = [(indexed[c, rep, left], indexed[c, rep, right]) for c, rep in keys]
        contrasts[left + "_vs_" + right] = {
            "pairs": len(pairs), "exact_text_matches": sum(a["content"] == b["content"] for a, b in pairs),
            "exact_token_matches": sum(a["tokens"] == b["tokens"] for a, b in pairs),
            "correct_to_wrong": [f'{a["case"]}:{a["repeat"]}' for a,b in pairs if a["score"]["correct"] and not b["score"]["correct"]],
            "wrong_to_correct": [f'{a["case"]}:{a["repeat"]}' for a,b in pairs if not a["score"]["correct"] and b["score"]["correct"]],
            "paired_request_ttft_ratio_median": statistics.median(a["client_ttft_ms"]/b["client_ttft_ms"] for a,b in pairs),
            "paired_operational_total_ratio_median": statistics.median(a["operational_total_ms"]/b["operational_total_ms"] for a,b in pairs),
            "paired_operational_total_saved_ms_median": statistics.median(a["operational_total_ms"]-b["operational_total_ms"] for a,b in pairs),
        }
    costs = {}
    for representation in ("text", "ids"):
        cold = groups[representation + "_cold"]
        warm = groups[representation + "_warm"]
        n = len(cold)
        cold_ms = sum(r["operational_total_ms"] for r in cold)
        warm_ms = sum(r["operational_total_ms"] for r in warm)
        cache_setup = setup["prime"]["client_total_ms"] + setup["save_ms"] + setup["snapshot_hash_ms"]
        token_preparation = sum(x["tokenize_ms"] + x["artifact_create_ms"] for x in setup["prepared"])
        cold_token_setup = token_preparation if representation == "ids" else 0
        warm_token_setup = token_preparation  # LCP derivation also prepares IDs for the text-cache arm.
        rendering = sum(x["render_ms"] for x in setup["prepared"])
        per_request_saving = (cold_ms-warm_ms)/n
        costs[representation] = {"requests": n, "cold_total_ms": cold_ms+cold_token_setup+rendering,
                                 "warm_total_including_cache_setup_ms": warm_ms+cache_setup+warm_token_setup+rendering,
                                 "cache_setup_ms": cache_setup, "cold_token_preparation_ms": cold_token_setup, "warm_token_preparation_ms": warm_token_setup, "rendering_ms": rendering,
                                 "estimated_cache_break_even_requests": (cache_setup+warm_token_setup-cold_token_setup)/per_request_saving if per_request_saving > 0 else None,
                                 "logical_input_tokens": sum(r["input_tokens"] for r in cold),
                                 "cold_evaluated_prompt_tokens": sum(r["prompt_n"] for r in cold),
                                 "warm_evaluated_prompt_tokens": sum(r["prompt_n"] for r in warm),
                                 "warm_reused_prompt_tokens": sum(r["cache_n"] for r in warm),
                                 "priming_evaluated_prompt_tokens": setup["prime"]["final"]["timings"]["prompt_n"]}
    return {"cells": cells, "contrasts": contrasts, "costs": costs,
            "distinct_tasks": len(set(r["case"] for r in rows)), "runs": len(rows),
            "failures": [{k:r[k] for k in ("case","repeat","cell","content","score","stop_type")}
                         for r in rows if not r["score"]["correct"]],
            "api_charges_usd": 0, "electricity_cost": None, "energy_joules": None, "hardware_amortization": None,
            "limits": ["One quantized model and one local device; descriptive repeated measurements, not a general performance guarantee.",
                       "Request timing excludes one-time rendering/tokenization; operational timing adds artifact validation and cache restore.",
                       "Cold cache erase is test setup and excluded; warm disk restore is included in operational timing.",
                       "CPU seconds and sampled RSS do not measure GPU energy or per-request peak device memory.",
                       "Cost aggregation is measured service time plus observed setup, not a provider invoice or production-load forecast.",
                       "Twelve distinct deterministic repository tasks; repeats are not independent quality samples."]}
