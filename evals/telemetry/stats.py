"""First-pass statistics over the tables extract.py writes.

    python evals/telemetry/stats.py DATA_DIR

Writes DATA_DIR/stats.json and prints each entry: call counts, routing statuses and
tiers, latencies, how often ROUTE_REQUIRED is followed by get_agent_context and how
many routed turns end with a log_interaction (upper bounds from one-to-one time
matching, since calls carry no shared id), agents, prompt sizes, footer and
language checks on the logged answers, how often each skill and implant is
retrieved, and those frequencies crossed with evals/ablation/RESULTS.md.
The logged-answer checks are only as good as the logging: see README.md.
"""
import collections
import csv
import json
import re
import statistics as st
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
def ts(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))
def pct(a, b): return round(100 * a / b, 1) if b else None
def q(vals, p):
    vals = sorted(vals); return vals[min(len(vals) - 1, int(p * len(vals)))] if vals else None
def match_one_to_one(starts, ends, window):
    """How many starts have a later end within the window, each end used at most once.

    Greedy in time order: each start takes the earliest unused end at or after it.
    Ends before the current start can never match a later start, so one pointer walks
    the sorted ends once.
    """
    ends = sorted(ends)
    j = matched = 0
    for start in sorted(starts):
        while j < len(ends) and ends[j] < start:
            j += 1
        if j < len(ends) and ends[j] - start <= window:
            matched += 1
            j += 1
    return matched


def main(D: Path) -> dict:
    def rows(n):
        # extract.py writes no file for a table with no rows (e.g. read_history never called).
        path = D / f"{n}.csv"
        return list(csv.DictReader(open(path))) if path.exists() else []
    route, gac, sk, im, hist, inter = (rows(n) for n in ("route", "gac", "skills_ret", "implants_ret", "history", "interactions"))
    S = {}
    stamps = [r["ts"] for r in route + gac + inter]
    S["period"] = [min(stamps)[:10], max(stamps)[:10]] if stamps else None
    S["counts"] = {"route_and_load": len(route), "get_agent_context": len(gac), "retrieve_skills": len(sk),
                   "retrieve_implants": len(im), "log_interaction": len(inter), "read_history": len(hist)}
    # routing
    S["route_status"] = collections.Counter(r["status"] for r in route).most_common()
    S["route_tier"] = collections.Counter(r["tier"] for r in route).most_common()
    S["route_protocol"] = collections.Counter(r["protocol"] for r in route).most_common()
    S["route_has_hash_pct"] = pct(sum(r["has_hash"] == "True" for r in route), len(route))
    S["route_q_lang"] = collections.Counter(r["q_lang"] for r in route).most_common()
    by_week = collections.defaultdict(collections.Counter)
    for r in route: by_week[ts(r["ts"]).strftime("%G-W%V")][r["status"]] += 1
    S["route_status_by_week"] = {w: dict(c) for w, c in sorted(by_week.items())}
    for name, rs in (("route_and_load", route), ("get_agent_context", gac)):
        lat = [float(r["latency"]) for r in rs if r["latency"] not in ("", "None")]
        S[f"latency_{name}_s"] = {"p50": q(lat, .5), "p90": q(lat, .9), "p99": q(lat, .99), "max": max(lat) if lat else None}
    # follow-through: ROUTE_REQUIRED -> get_agent_context within 180 s
    # Calls cannot be linked by id (see README), so pair each route with the first unused
    # later call in the window, one to one; the result is still an upper bound.
    rr = [ts(r["ts"]) for r in route if r["status"] == "ROUTE_REQUIRED"]
    fol = match_one_to_one(rr, [ts(g["ts"]) for g in gac], timedelta(seconds=180))
    S["route_required_followed_by_gac_180s_upper_bound"] = [fol, len(rr), pct(fol, len(rr))]
    # turns vs log_interaction: turns = route_and_load calls (protocol step 1) ; logs within 30 min after a route
    logged = match_one_to_one([ts(r["ts"]) for r in route], [ts(i["ts"]) for i in inter], timedelta(minutes=30))
    S["routed_turns_with_log_within_30m_upper_bound"] = [logged, len(route), pct(logged, len(route))]
    by_day_r = collections.Counter(r["ts"][:10] for r in route); by_day_i = collections.Counter(i["ts"][:10] for i in inter)
    S["log_to_route_ratio_by_day"] = {d: [by_day_i.get(d, 0), by_day_r[d]] for d in sorted(by_day_r)}
    # agents
    S["gac_agent"] = collections.Counter(g["agent"] or g["requested"] for g in gac).most_common()
    S["gac_status"] = collections.Counter(g["status"] for g in gac).most_common()
    S["gac_protocol"] = collections.Counter(g["protocol"] for g in gac).most_common()
    S["route_success_agent"] = collections.Counter(r["agent"] for r in route if r["agent"]).most_common()
    S["interaction_agent"] = collections.Counter(i["agent"] for i in inter).most_common()
    # prompt sizes
    pc = [int(g["prompt_chars"]) for g in gac if g["prompt_chars"] not in ("", "0")]
    S["gac_prompt_chars"] = {"n": len(pc), "p50": q(pc, .5), "p90": q(pc, .9), "max": max(pc) if pc else None}
    per_agent = collections.defaultdict(list)
    for g in gac:
        if g["prompt_chars"] not in ("", "0"): per_agent[g["agent"] or g["requested"]].append(int(g["prompt_chars"]))
    S["prompt_chars_median_by_agent"] = sorted(((a, int(st.median(v)), len(v)) for a, v in per_agent.items()), key=lambda x: -x[1])[:15]
    # compliance of logged answers
    n = len(inter)
    S["footer_present_pct"] = pct(sum(i["footer"] == "True" for i in inter), n)
    S["footer_full_pct"] = pct(sum(i["footer"] == "True" and i["footer_skills"] == "True" and i["footer_rules"] == "True" for i in inter), n)
    S["footer_agent_matches_meta_pct"] = pct(sum(i["footer_agent"] == i["agent"] for i in inter if i["footer"] == "True"), sum(i["footer"] == "True" for i in inter))
    lm = [(i["q_lang"], i["r_lang"]) for i in inter if i["q_lang"] in ("ru", "en") and i["r_lang"] in ("ru", "en")]
    S["language_pairs"] = collections.Counter(f"{a}->{b}" for a, b in lm).most_common()
    S["language_match_pct"] = pct(sum(a == b for a, b in lm), len(lm))
    rl = [int(i["r_len"]) for i in inter]
    S["response_chars"] = {"p50": q(rl, .5), "p90": q(rl, .9), "max": max(rl) if rl else None}
    S["persona_action"] = collections.Counter(i["persona_action"] for i in inter if i["persona_action"]).most_common()
    # enrichment frequencies
    skill_freq, skill_tier = collections.Counter(), collections.defaultdict(collections.Counter)
    nsk = []
    for s in sk:
        items = [x for x in s["returned"].split("|") if x]
        nsk.append(len(items))
        for x in items:
            name, tier, _d = (x.split(":") + ["", ""])[:3]; skill_freq[name] += 1; skill_tier[name][tier] += 1
    S["skills_per_call"] = collections.Counter(nsk).most_common()
    imp_freq = collections.Counter(); nim = []
    for s in im:
        items = [x for x in s["returned"].split("|") if x]; nim.append(len(items))
        for x in items: imp_freq[x.split(":")[0]] += 1
    S["implants_per_call"] = collections.Counter(nim).most_common()
    repo = ROOT
    all_skills = sorted(p.stem for p in repo.glob("skills/skill-*.mdc")); all_imps = sorted(p.stem for p in repo.glob("implants/implant-*.mdc"))
    S["skills_never_retrieved"] = [s for s in all_skills if skill_freq[s] == 0]
    S["implants_never_retrieved"] = [s for s in all_imps if imp_freq[s] == 0]
    S["top_skills"] = [(k, v, dict(skill_tier[k])) for k, v in skill_freq.most_common(20)]
    S["top_implants"] = imp_freq.most_common(20)
    # cross with ablation
    abl = {}
    for f in (ROOT / "evals/ablation/RESULTS.md",):
        sweep, retest = open(f).read().split("## Re-test")
        for part, key in ((sweep, "sweep"), (retest, "retest")):
            for m in re.finditer(r"\| ((?:rule|skill|implant)-\S+) \| (\d+) \| (\d+) \| (\d+) \| (\d+) \| ([+-]?\d+) \| (\d+) \| (\d+) \|", part):
                abl.setdefault(m[1], {})[key] = {"net": int(m[6]), "rw": int(m[7]), "rwo": int(m[8])}
    cross = []
    for comp, a in abl.items():
        freq = skill_freq.get(comp, 0) if comp.startswith("skill-") else imp_freq.get(comp, 0) if comp.startswith("implant-") else None
        if freq is None: continue
        best = a.get("retest", a.get("sweep"))
        cross.append((comp, freq, best["net"], best["rw"] - best["rwo"], "retest" if "retest" in a else "sweep"))
    calls = {"skill": len(sk), "implant": len(im)}
    S["frequent_but_not_helping"] = sorted([c for c in cross if c[3] <= 0 and c[1] >= 20], key=lambda c: -c[1])[:20]
    S["helpful_but_rare"] = sorted([c for c in cross if c[3] >= 2 and c[1] <= 5], key=lambda c: -c[3])[:15]
    S["history"] = {"n": len(hist), "modes": collections.Counter(h["mode"] for h in hist).most_common(), "with_query": sum(h["has_query"] == "True" for h in hist)}
    json.dump(S, open(D / "stats.json", "w"), ensure_ascii=False, indent=1, default=str)
    for k, v in S.items():
        s = json.dumps(v, ensure_ascii=False, default=str)
        print(f"{k}: {s[:600]}")
    return S


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else Path.cwd())
