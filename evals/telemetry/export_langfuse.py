"""Export this project's Langfuse observations to JSONL.

    python evals/telemetry/export_langfuse.py ~/evals-runs/langfuse-2026-10-01
    python evals/telemetry/export_langfuse.py OUT_DIR --since 2026-09-01

Reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_HOST from the
environment, falling back to the repository's .env. Uses the v2 observations API
(cursor pagination, 1000 per page): the legacy /api/public/traces and
/api/public/observations endpoints stop working on Langfuse Cloud on 2026-11-16.

Writes observations.jsonl (every observation, input and output parsed from JSON when
they are JSON) and traces.jsonl (one record per trace, built from its root
observation: name, timestamp, latency, input, output, metadata), the two files
extract.py reads.

The export holds the user's queries and answers as Agents-Core logged them (queries
capped at 2000 characters, answers at 5000, by log_interaction in src/server.py) and
the tool inputs in full: write it outside the repository and never commit it.
extract.py turns it into text-free tables.
"""
import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")
# latency arrives with core as well, but the documentation places it under metrics.
FIELDS = "core,basic,io,trace_context,metadata,metrics"


def env_value(raw: str) -> str:
    """A .env value: the text inside quotes, or up to an inline ' #' comment."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    return raw.split(" #", 1)[0].split("\t#", 1)[0].strip()


def settings() -> dict[str, str]:
    found = {k: os.environ[k] for k in KEYS if os.environ.get(k)}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, _, value = line.strip().removeprefix("export ").partition("=")
            key = key.strip()
            if key in KEYS and key not in found:
                found[key] = env_value(value)
    if missing := [k for k in KEYS if k not in found]:
        raise SystemExit(f"missing {missing}: set them in the environment or in {env_file}")
    return found


def parsed(value):
    """v2 returns input and output as raw strings; decode the JSON ones."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def as_trace(root: dict) -> dict:
    """The legacy trace record extract.py reads, from a trace's root observation."""
    return {"id": root["traceId"], "name": root.get("traceName") or root.get("name"),
            "timestamp": root["startTime"], "latency": root.get("latency"),
            "input": root.get("input"), "output": root.get("output"), "metadata": root.get("metadata"),
            "sessionId": root.get("sessionId") or None, "userId": root.get("userId") or None,
            "release": root.get("release") or None, "tags": root.get("tags") or [],
            "environment": root.get("environment")}


def trace_roots(observations: list[dict]) -> list[dict]:
    """One observation per trace: the one without a parent; failing that, the earliest
    one that has children in the trace; failing that, the earliest.

    Some traces (the agent_interaction span of log_interaction, with its response
    generation as a child) come without their root observation in the v2 export.
    """
    parents = {o.get("parentObservationId") for o in observations}

    def rank(o):
        return (o.get("parentObservationId") is not None, o["id"] not in parents, o["startTime"])

    roots = {}
    for o in observations:
        current = roots.get(o["traceId"])
        if current is None or rank(o) < rank(current):
            roots[o["traceId"]] = o
    return list(roots.values())


def main(out: Path, since: str) -> None:
    cfg = settings()
    host = cfg["LANGFUSE_HOST"].rstrip("/")
    auth = base64.b64encode(f"{cfg['LANGFUSE_PUBLIC_KEY']}:{cfg['LANGFUSE_SECRET_KEY']}".encode()).decode()

    def get(**query) -> dict:
        url = f"{host}/api/public/v2/observations?{urllib.parse.urlencode(query)}"
        for attempt in range(6):
            try:
                request = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    raise
                time.sleep(5 * (attempt + 1))
        raise SystemExit("gave up on /api/public/v2/observations after 6 attempts")

    until = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    observations, cursor = [], None
    while True:
        query = {"limit": 1000, "fields": FIELDS, "fromStartTime": f"{since}T00:00:00Z", "toStartTime": until}
        if cursor:
            query["cursor"] = cursor
        page = get(**query)
        observations += [{**o, "input": parsed(o.get("input")), "output": parsed(o.get("output"))} for o in page["data"]]
        next_cursor = page.get("meta", {}).get("cursor")
        if not next_cursor:
            break  # the API omits the cursor after the last page
        if next_cursor == cursor or not page["data"]:
            # Asking again would loop on the same page; stopping would write a partial export.
            raise SystemExit(f"pagination made no progress after {len(observations)} observations; nothing written")
        cursor = next_cursor
        time.sleep(0.3)

    out.mkdir(parents=True, exist_ok=True)
    traces = [as_trace(o) for o in trace_roots(observations)]
    for name, rows in (("observations", observations), ("traces", traces)):
        with open(out / f"{name}.jsonl", "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{name}: {len(rows)} -> {out / f'{name}.jsonl'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir")
    parser.add_argument("--since", default="2025-01-01", help="first day to export, YYYY-MM-DD")
    args = parser.parse_args()
    main(Path(args.out_dir).expanduser().resolve(), args.since)
