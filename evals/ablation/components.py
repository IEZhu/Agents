"""List the prompt components the ablation sweep tests, with their owning agents.

    python evals/ablation/components.py --batch 3  # prints the component ids in batch 3
    python evals/ablation/components.py --write    # regenerates evals/ablation/components.json

components.json is the snapshot the 2026-09 sweep ran and RESULTS.md reports on;
--write refuses to replace it unless --force is given as well.

A component is one rule, skill or implant file. Owners are the agents that
declare a skill (core/preferred/capable) or an implant (preferred_implants);
the case generator picks its agent from them, or from all agents when a
component has no owner. Components already covered by the 2026-09-25 pilot
are skipped.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.utils.prompt_loader import get_agent_metadata, read_mdc  # noqa: E402

OUT = Path(__file__).with_name("components.json")
BATCH_SIZE = 10
DONE_IN_PILOT = {
    "rule-no-fabrication",
    "skill-content-structure",
    "implant-regression-first",
    "implant-iteration-budget",
}


def agent_names() -> list[str]:
    return sorted(p.parent.name for p in (ROOT / "agents").glob("*/system_prompt.mdc"))


def build() -> list[dict]:
    owners: dict[str, list[str]] = {}
    for agent in agent_names():
        meta = get_agent_metadata(agent)
        for key in ("core_skills", "preferred_skills", "capable_skills", "preferred_implants"):
            for name in meta.get(key) or []:
                owners.setdefault(name.removesuffix(".mdc"), []).append(agent)
    components = []
    for kind, folder in (("rule", "rules"), ("skill", "skills"), ("implant", "implants")):
        for path in sorted((ROOT / folder).glob(f"{kind}-*.mdc")):
            cid = path.stem
            if cid in DONE_IN_PILOT:
                continue
            fm, _ = read_mdc(str(path))
            components.append({
                "id": cid,
                "kind": kind,
                "file": f"{folder}/{path.name}",
                "short_name": fm.get("short_name"),
                "description": fm.get("description", ""),
                "owners": sorted(set(owners.get(cid, []))),
            })
    return components


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, help="print the ids in this 1-based batch")
    parser.add_argument("--write", action="store_true", help="regenerate components.json from the repository")
    parser.add_argument("--force", action="store_true", help="with --write: replace an existing snapshot")
    args = parser.parse_args()
    if args.batch is None and not args.write:
        parser.error("pass --batch N, or --write to regenerate the snapshot")
    if args.write:
        if OUT.exists() and not args.force:
            parser.error(f"{OUT.name} is the snapshot RESULTS.md reports on; add --force to replace it")
        components = build()
        OUT.write_text(json.dumps(components, ensure_ascii=False, indent=1) + "\n")
        batches = -(-len(components) // BATCH_SIZE)
        print(f"{len(components)} components, {batches} batches of {BATCH_SIZE} -> {OUT}")
        return
    components = json.loads(OUT.read_text())
    batches = -(-len(components) // BATCH_SIZE)
    if not 1 <= args.batch <= batches:
        parser.error(f"--batch must be between 1 and {batches}")
    start = (args.batch - 1) * BATCH_SIZE
    print(" ".join(c["id"] for c in components[start:start + BATCH_SIZE]))


if __name__ == "__main__":
    main()
