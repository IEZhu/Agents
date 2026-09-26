"""Export every Langfuse trace and observation of this project to JSONL.

    python evals/telemetry/export_langfuse.py ~/evals-runs/langfuse-2026-10-01

Reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_HOST from the
environment, falling back to the repository's .env. Writes traces.jsonl and
observations.jsonl (100 per page, retrying on 429/5xx).

The export holds the user's queries and answers verbatim: write it outside the
repository and never commit it. extract.py turns it into text-free tables.
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")


def settings() -> dict[str, str]:
    found = {k: os.environ[k] for k in KEYS if os.environ.get(k)}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, _, value = line.strip().partition("=")
            if key in KEYS and key not in found:
                found[key] = value.strip().strip('"').strip("'")
    if missing := [k for k in KEYS if k not in found]:
        raise SystemExit(f"missing {missing}: set them in the environment or in {env_file}")
    return found


def main(out: Path) -> None:
    cfg = settings()
    host = cfg["LANGFUSE_HOST"].rstrip("/")
    auth = base64.b64encode(f"{cfg['LANGFUSE_PUBLIC_KEY']}:{cfg['LANGFUSE_SECRET_KEY']}".encode()).decode()

    def get(path: str, **query) -> dict:
        url = f"{host}{path}?{urllib.parse.urlencode(query)}"
        for attempt in range(6):
            try:
                request = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    raise
                time.sleep(5 * (attempt + 1))
        raise SystemExit(f"gave up on {path} after 6 attempts")

    out.mkdir(parents=True, exist_ok=True)
    for kind, path in (("traces", "/api/public/traces"), ("observations", "/api/public/observations")):
        rows, page = [], 1
        while True:
            data = get(path, limit=100, page=page)
            rows += data["data"]
            if page >= data["meta"]["totalPages"]:
                break
            page += 1
            time.sleep(0.3)
        with open(out / f"{kind}.jsonl", "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{kind}: {len(rows)} -> {out / f'{kind}.jsonl'}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]).expanduser().resolve())
