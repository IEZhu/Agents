"""Build system prompts with ONE revision's own code and content.

`prompt_ab` runs this file with cwd set to a worktree of the revision under test,
so every import below resolves against that revision; the file itself only uses
interfaces that exist in older revisions too. Flags such as IMPLANT_NEED_GATE
come from the environment and are read by that revision's config.

    python _prompt_builder.py --dataset D.jsonl --out prompts.json
        [--agents agents.json] [--implants production|none|Name,Name]
    python _prompt_builder.py --catalog-out catalog.json

`--implants` replaces the implant layer for every case: `production` keeps the
revision's own selection, `none` loads no implant, and a list loads exactly those
implants (short names or file stems) in the same format production uses. The
per-query prompt cache is cleared before every case, so no build can reuse a
prompt enriched under another implant set.
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.getcwd())


def load_cases(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(json.loads(line))
    return rows


def implant_records(retriever, names):
    """Records in the shape of the retriever's preferred-implant fast path."""
    metas = retriever.store.get_all_metadatas()
    by_name = {}
    for meta in metas:
        filename = meta.get("filename", "")
        by_name[filename.removesuffix(".mdc")] = filename
        if meta.get("short_name"):
            by_name[meta["short_name"]] = filename
    missing = [n for n in names if n not in by_name]
    if missing:
        raise SystemExit(f"unknown implants: {missing}; known: {sorted(by_name)}")
    ids = [by_name[n] for n in names]
    found = retriever.store.get(ids=ids)
    lookup = {cid: (found.metadatas[i] or {}, found.documents[i]) for i, cid in enumerate(found.ids)}
    return [{"filename": cid, "content": lookup[cid][0].get("body", lookup[cid][1]),
             "metadata": lookup[cid][0], "distance": 0.0} for cid in ids]


async def build(args):
    from evals.runners.run_mcp_vs_vanilla import _strip_platform_instructions, build_mcp_system_prompt
    from src import server
    from src.engine import enrichment

    spec = args.implants
    if spec != "production":
        records = [] if spec == "none" else implant_records(enrichment.implant_retriever, spec.split(","))
        enrichment.implant_retriever.retrieve = lambda *a, **k: list(records)

    agents = json.load(open(args.agents)) if args.agents else {}
    out = {}
    for case in load_cases(args.dataset):
        server.SESSION_CACHE.clear()
        if case["id"] in agents:
            agent = agents[case["id"]]
            prompt, _hash, skills, implants, rules, tier = await server._load_and_enrich(agent, case["query"], [])
            prompt = _strip_platform_instructions(prompt)
            meta = {"agent": agent, "tier": tier, "routing_path": "fixed", "skills_loaded": list(skills),
                    "implants_loaded": list(implants), "rules_loaded": list(rules)}
        else:
            prompt, meta = await build_mcp_system_prompt(case["query"], pick_agent=None)
        out[case["id"]] = {"system_prompt": prompt, "meta": meta}
    write_atomic(args.out, {"root": os.getcwd(), "implants": spec,
                            "need_gate": os.environ.get("IMPLANT_NEED_GATE", "off"),
                            "embedding_model": os.environ.get("EMBEDDING_MODEL"), "prompts": out})


def write_atomic(path, data):
    """Write to a temporary file and rename, so a killed build leaves no partial file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def catalog(path):
    from evals.runners.run_mcp_vs_vanilla import _get_router
    write_atomic(path, _get_router().get_agent_catalog())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset")
    ap.add_argument("--out")
    ap.add_argument("--agents")
    ap.add_argument("--implants", default="production")
    ap.add_argument("--catalog-out")
    args = ap.parse_args()
    if args.catalog_out:
        catalog(args.catalog_out)
    else:
        asyncio.run(build(args))


if __name__ == "__main__":
    main()
