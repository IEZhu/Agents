"""Text-free analysis tables from a Langfuse export.

    python evals/telemetry/extract.py DATA_DIR

Reads DATA_DIR/traces.jsonl and observations.jsonl (export_langfuse.py) and writes
route.csv, gac.csv, skills_ret.csv, implants_ret.csv, history.csv and
interactions.csv next to them. The tables keep lengths, statuses, agents, loaded
components and a heuristic language (share of Cyrillic letters outside code blocks
>= 0.3 means ru, after removing code, the English footer and URLs), never query or
answer text, so they can be shared. Logged answers are capped at 5000 characters by
log_interaction; r_truncated marks those.
"""
import csv
import json
import re
import sys
from pathlib import Path

CYR = re.compile(r"[А-Яа-яЁё]"); LAT = re.compile(r"[A-Za-z]")
FOOTER = re.compile(r"\*\*Agent\*\*:\s*([a-z0-9_]+)")
CODE = re.compile(r"```.*?```", re.S)
# The mandatory footer is English by design; inline code and URLs are language-neutral.
NOISE = re.compile(r"\*\*Agent\*\*:.*$|`[^`\n]*`|https?://\S+", re.M)
# log_interaction stores response_content[:5000] (src/server.py), so a 5000-char answer was cut.
LOG_CAP = 5000


def lang(text):
    if not isinstance(text, str) or not text.strip():
        return ""
    t = NOISE.sub(" ", CODE.sub(" ", text))
    c, l = len(CYR.findall(t)), len(LAT.findall(t))
    if c + l == 0:
        return "none"
    return "ru" if c / (c + l) >= 0.3 else "en"


def distance(d):
    """One format for retrieval distances: the legacy API returned 0, the v2 API 0.0."""
    try:
        return f"{float(d):.4f}"
    except (TypeError, ValueError):
        return ""


def parse(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except ValueError:
            return x
    return x


def write(D, name, rows):
    if not rows:
        # A stale table from an earlier export would be mixed into this one's stats.
        (D / f"{name}.csv").unlink(missing_ok=True)
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(D / f"{name}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print(name, len(rows))


def main(D: Path) -> None:
    route, gac, skills, implants, hist = [], [], [], [], []
    for line in open(D / "traces.jsonl"):
        t = json.loads(line)
        n, inp, out = t["name"], parse(t.get("input")), parse(t.get("output"))
        kw = (inp or {}).get("kwargs", {}) if isinstance(inp, dict) else {}
        base = {"ts": t["timestamp"], "latency": t.get("latency")}
        if n == "route_and_load":
            q = kw.get("query") or ""
            o = out if isinstance(out, dict) else {}
            route.append({**base, "q_len": len(q), "q_lang": lang(q), "protocol": kw.get("protocol_version"),
                          "has_hash": bool(kw.get("context_hash")), "has_history": bool(kw.get("chat_history")),
                          "status": o.get("status", "?" if out is None else "unparsed"), "tier": o.get("tier"),
                          "agent": o.get("agent"), "reasoning_len": len(o.get("reasoning") or ""),
                          "n_candidates": len(o.get("candidates") or []),
                          "n_skills": len(o.get("skills_loaded") or []), "n_implants": len(o.get("implants_loaded") or []),
                          "prompt_chars": len(o.get("system_prompt") or ""), "request_id": o.get("request_id")})
        elif n == "get_agent_context":
            q = kw.get("query") or ""
            o = out if isinstance(out, dict) else {}
            gac.append({**base, "q_len": len(q), "q_lang": lang(q), "requested": kw.get("agent_name"),
                        "protocol": kw.get("protocol_version"), "force_reload": kw.get("force_reload"),
                        "status": o.get("status", "?" if out is None else "unparsed"), "agent": o.get("agent"),
                        "skills": "|".join(o.get("skills_loaded") or []), "implants": "|".join(o.get("implants_loaded") or []),
                        "n_rules": len(o.get("rules_loaded") or []), "prompt_chars": len(o.get("system_prompt") or ""),
                        "request_id": o.get("request_id")})
        elif n == "retrieve_skills":
            o = out if isinstance(out, list) else []
            skills.append({**base, "n_results": kw.get("n_results"), "n_mandatory": len(kw.get("mandatory") or []),
                           "n_preferred": len(kw.get("preferred") or []), "n_capable": len(kw.get("capable") or []),
                           "returned": "|".join(f"{s.get('filename','?').removesuffix('.mdc')}:{s.get('tier','')}:{distance(s.get('distance'))}" for s in o if isinstance(s, dict))})
        elif n == "retrieve_implants":
            o = out if isinstance(out, list) else []
            implants.append({**base, "role": kw.get("role"), "n_results": kw.get("n_results"),
                             "n_preferred": len(kw.get("preferred_implants") or []),
                             "returned": "|".join(f"{s.get('filename','?').removesuffix('.mdc')}:{distance(s.get('distance'))}" for s in o if isinstance(s, dict))})
        elif n == "read_history":
            o = out if isinstance(out, dict) else {}
            hist.append({**base, "mode": o.get("mode"), "total": o.get("total"), "has_query": bool(kw.get("query"))})

    inter = []
    for line in open(D / "observations.jsonl"):
        o = json.loads(line)
        if o["type"] != "GENERATION":
            continue
        q, r, m = o.get("input"), o.get("output"), o.get("metadata") or {}
        q = q if isinstance(q, str) else json.dumps(q, ensure_ascii=False) if q else ""
        r = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) if r else ""
        f = FOOTER.search(r)
        inter.append({"ts": o["startTime"], "agent": m.get("agent"), "persona_action": m.get("persona_action"),
                      "q_len": len(q), "q_lang": lang(q), "r_len": len(r), "r_truncated": len(r) >= LOG_CAP, "r_lang": lang(r),
                      "footer": bool(f), "footer_agent": f.group(1) if f else "",
                      "footer_skills": "**Skills**" in r, "footer_rules": "**Rules**" in r})

    for name, rows in (("route", route), ("gac", gac), ("skills_ret", skills), ("implants_ret", implants),
                       ("history", hist), ("interactions", inter)):
        write(D, name, rows)


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else Path.cwd())
