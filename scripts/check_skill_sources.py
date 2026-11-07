#!/usr/bin/env python3
"""Check that every source a volatile skill declares still serves its document.

Why this is a script and not a test: it needs the network, and the test suite in
this repository deliberately runs without one. It is step 8 of
``flows/legal-reference-refresh.md``, run by hand before a pull request and
again when a refresh falls due.

What it checks, and why the obvious version of it does not work:

* **A status of 200 is not success.** One national legislative portal answers
  ``HTTP 200`` with an "Access Denied" body to some clients while serving the
  full statute to others — the same URL, the same moment. A status code cannot
  tell those two apart, so the body has to be looked at.
* **One failure is not a dead link.** A host that is fine on its own times out
  under parallel load. Every failure is retried once before it is reported.
* **Some APIs require a header.** The Spanish BOE consolidated-legislation API
  answers 400 to a bare request and returns the article XML once the ``Accept``
  header it documents is sent. That is an API contract, not a defect.
* **A short body can be the whole document.** A single-article XML payload runs
  to about a kilobyte, so only a few hundred bytes counts as a stub.

A host that refuses this client — 403, 406, or an error page under a 200 — is
reported and does **not** fail the run, because it is a limitation of where the
check runs rather than a defect in the file. No attempt is made to disguise the
client to get past such a block: the right response is to say in the pull
request that the source rests on what a browser read.

Exit status is 1 only for a URL that is positively wrong: a 404 or 410, or a
not-found page served under a 200. A refusal page under a 200 is a refusal, not
a wrong URL, and does not fail the run. ``--strict`` fails on anything not
served.

    python scripts/check_skill_sources.py
    python scripts/check_skill_sources.py --strict --skill skill-labor-cy
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
SKILLS_DIR = REPO_ROOT / "skills"

from src.utils.prompt_loader import split_frontmatter  # noqa: E402 - needs REPO_ROOT on the path

VOLATILE_PREFIXES = ("skill-jurisdiction-", "skill-labor-", "skill-investment-")
ACCEPT = "application/xml, application/json, text/html, */*"
STUB_BYTES = 500
# Two different things arrive under a status of 200, and they must not share a
# verdict. A refusal page means the host turned this client away, which is a
# limitation of where the check ran. A not-found page under a 200 — a soft 404 —
# means the URL is wrong, which is a defect in the file. Lumping them together
# made the run fail for valid URLs on a host that refuses curl, contradicting
# the policy stated above; review caught that.
REFUSAL_BODY = re.compile(r"Access Denied|could not be accessed|Something went wrong|Forbidden", re.I)
NOTFOUND_BODY = re.compile(r"Error 404|Page not found|No se encontr|\bnot found\b", re.I)

OK = "served"
STUB = "body too short to be the document"
REFUSED = "host refused this client"
REFUSED_200 = "host refused this client, under status 200"
MISSING = "not found at this URL"
MISSING_200 = "not found, under status 200"
UNREACHABLE = "host did not answer"

# Only a positively wrong URL fails the run.
FAILING = {MISSING, MISSING_200}


def declared_sources(path: Path) -> list[str]:
    """Read the frontmatter the way the loader and the tests do.

    A hand-rolled ``^---\\n`` match fails on a file saved with CRLF and returns
    an empty list, so the script would report a skill as having no sources and
    exit 0 — a check reporting silence as success. ``split_frontmatter`` is the
    one parser the rest of the repository uses.
    """
    with open(path, encoding="utf-8") as handle:
        front, _ = split_frontmatter(handle.read())
    data = (yaml.safe_load(front) or {}) if front else {}
    return [str(s) for s in (data.get("sources") or [])]


def probe(url: str, timeout: int, _retry: bool = True) -> tuple[str, int, int, str]:
    """Fetch one URL and judge the body, not only the status."""
    request = urllib.request.Request(url, headers={"Accept": ACCEPT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            code = response.getcode()
        head = body[:4000].decode("utf-8", "replace")
        if NOTFOUND_BODY.search(head):
            verdict = MISSING_200
        elif REFUSAL_BODY.search(head):
            verdict = REFUSED_200
        elif len(body) < STUB_BYTES:
            verdict = STUB
        else:
            verdict = OK
    except urllib.error.HTTPError as exc:
        code, body = exc.code, b""
        verdict = MISSING if exc.code in (404, 410) else REFUSED
    except Exception:  # noqa: BLE001 - every transport failure reads the same here
        code, body, verdict = 0, b"", UNREACHABLE

    if verdict != OK and _retry:
        again = probe(url, timeout, _retry=False)
        if again[3] == OK:
            return again[0], again[1], again[2], "served (first attempt failed)"
        return again
    return url, code, len(body), verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skill", action="append", help="limit to this skill, repeatable")
    parser.add_argument("--timeout", type=int, default=40, help="seconds per attempt")
    parser.add_argument("--jobs", type=int, default=6, help="concurrent fetches")
    parser.add_argument("--strict", action="store_true", help="fail on anything not served")
    args = parser.parse_args()

    paths = sorted(
        p
        for p in SKILLS_DIR.glob("*.mdc")
        if p.name.startswith(VOLATILE_PREFIXES) and (not args.skill or p.stem in args.skill)
    )
    if not paths:
        print("no volatile skills matched", file=sys.stderr)
        return 2

    rows: list[tuple[str, str, int, int, str]] = []
    with futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for path in paths:
            urls = declared_sources(path)
            if not urls:
                rows.append((path.name, "<no sources declared>", 0, 0, MISSING))
                continue
            for url, code, size, verdict in pool.map(
                lambda u: probe(u, args.timeout), urls
            ):
                rows.append((path.name, url, code, size, verdict))

    failures = [r for r in rows if r[4] in FAILING]
    unserved = [r for r in rows if not r[4].startswith("served")]

    current = None
    for name, url, code, size, verdict in rows:
        if name != current:
            print(f"\n## {name}")
            current = name
        flag = "!!" if verdict in FAILING else ("  " if verdict.startswith("served") else " ?")
        print(f"{flag} {code:>4} {size:>9}  {verdict:<34} {url}")

    print(
        f"\n{len(rows)} declared sources, {len(rows) - len(unserved)} served, "
        f"{len(unserved)} not served, {len(failures)} positively wrong"
    )
    if unserved:
        print("\nNot served, by host:")
        hosts: dict[str, list[str]] = {}
        for _, url, _, _, verdict in unserved:
            hosts.setdefault(re.sub(r"^https?://([^/]+).*", r"\1", url), []).append(verdict)
        for host, verdicts in sorted(hosts.items()):
            print(f"  {host:38} {len(verdicts):2} url(s): {sorted(set(verdicts))}")
        print(
            "\nA refused or unanswered host is a limitation of where this ran, not a defect in the "
            "file. Say so in the pull request instead of disguising the client."
        )
    if failures:
        print(f"\n{len(failures)} URL(s) are positively wrong and must be replaced or dropped:")
        for name, url, _, _, verdict in failures:
            print(f"  {name}: {url} - {verdict}")
        return 1
    return 1 if (args.strict and unserved) else 0


if __name__ == "__main__":
    sys.exit(main())
