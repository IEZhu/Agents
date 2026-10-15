"""Read one pinned GitHub Actions run without returning every poll to a model.

Uses the authenticated gh CLI for GETs only. Prints one compact JSON result;
raw responses stay in the caller's evidence directory. This observes CI, not
review approval, artifact correctness or permission to publish.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


class ObservationError(ValueError):
    """An observation cannot support the requested identity or result."""


def write_json(path: Path, value) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".",
                                     suffix=".tmp", encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def gh_json(endpoint: str, timeout: float):
    try:
        result = subprocess.run(["gh", "api", "--method", "GET", endpoint],
                                capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ObservationError("GitHub read failed: " + type(error).__name__) from error
    if result.returncode:
        # gh's stderr can contain remote text or credentials. Keep it out of output.
        raise ObservationError(f"GitHub read failed with exit {result.returncode}; inspect gh authentication/network access")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ObservationError("GitHub read returned invalid JSON") from error


def require_identity(run: dict, repo: str, run_id: int, sha: str, attempt: int) -> None:
    if (run.get("id") != run_id or run.get("head_sha") != sha
            or run.get("run_attempt") != attempt
            or run.get("repository", {}).get("full_name", "").casefold() != repo.casefold()):
        raise ObservationError("Run repository, ID, SHA or attempt changed; do not reuse this observation")


def read_jobs(read, endpoint: str, run_id: int, sha: str) -> tuple[list, list]:
    jobs, pages, seen = [], [], set()
    expected = None
    while expected is None or len(jobs) < expected:
        page = read(endpoint + f"?per_page=100&page={len(pages) + 1}")
        count, batch = page.get("total_count"), page.get("jobs")
        if type(count) is not int or count < 0 or not isinstance(batch, list):
            raise ObservationError("Invalid job page")
        if expected is not None and expected != count:
            raise ObservationError("Job total changed while paging; obtain a new observation")
        expected = count
        pages.append(page)
        for job in batch:
            job_id = job.get("id")
            if (type(job_id) is not int or job_id in seen or job.get("run_id") != run_id
                    or job.get("head_sha") != sha):
                raise ObservationError("Duplicate job or mismatched run/SHA in job page")
            seen.add(job_id)
        jobs.extend(batch)
        if len(jobs) > expected or (len(jobs) < expected and not batch):
            raise ObservationError("Incomplete or inconsistent job pagination")
    if not jobs:
        raise ObservationError("Completed run has no observed jobs")
    return jobs, pages


def observe(repo: str, run_id: int, sha: str, attempt: int, output: Path, *,
            seconds: float = 55, interval: float = 10, expected_jobs: int | None = None,
            api=gh_json, clock=time.monotonic, sleep=time.sleep) -> dict:
    """Poll within one bounded host call; preserve failures and a pending timeout."""
    output.mkdir(parents=True, exist_ok=True)
    deadline = clock() + seconds
    endpoint = f"repos/{repo}/actions/runs/{run_id}"
    result = {"repository": repo, "run_id": run_id, "head_sha": sha, "attempt": attempt,
              "observed_at": datetime.now(timezone.utc).isoformat(), "outcome": "pending", "polls": 0,
              "evidence": str(output.resolve())}
    write_json(output / "result.json", result)

    def read(path):
        remaining = deadline - clock()
        if remaining <= 0:
            raise ObservationError("Observation deadline reached before verification finished")
        value = api(path, min(10, remaining))
        if not isinstance(value, dict):
            raise ObservationError("Expected a JSON object")
        return value

    try:
        while clock() < deadline:
            run = read(endpoint)
            result["polls"] += 1
            write_json(output / "run.json", run)
            require_identity(run, repo, run_id, sha, attempt)
            result.update(status=run.get("status"), conclusion=run.get("conclusion"), url=run.get("html_url"))
            if run.get("status") == "completed":
                jobs, pages = read_jobs(read, endpoint + f"/attempts/{attempt}/jobs", run_id, sha)
                write_json(output / "jobs.json", pages)
                current = read(endpoint)
                write_json(output / "run-after-jobs.json", current)
                require_identity(current, repo, run_id, sha, attempt)
                if (current.get("status"), current.get("conclusion")) != (run.get("status"), run.get("conclusion")):
                    raise ObservationError("Run state changed while reading jobs")
                result["jobs"] = len(jobs)
                result["job_conclusions"] = dict(Counter(job.get("conclusion") for job in jobs))
                result["attention"] = [{"id": job["id"], "name": job.get("name"), "status": job.get("status"),
                                        "conclusion": job.get("conclusion"), "url": job.get("html_url")}
                                       for job in jobs if job.get("status") != "completed" or job.get("conclusion") != "success"]
                if expected_jobs is not None and len(jobs) != expected_jobs:
                    raise ObservationError("Observed job count differs from the requested count")
                result["outcome"] = "success" if run.get("conclusion") == "success" and not result["attention"] else "attention"
                break
            sleep(min(interval, max(0, deadline - clock())))
    except (ObservationError, AttributeError, TypeError) as error:
        result.update(outcome="unverified", error=str(error))
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "result.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=55)
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--expected-jobs", type=int)
    args = parser.parse_args(argv)
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.sha)
            or args.run_id <= 0 or args.attempt <= 0
            or not 0 < args.seconds <= 55 or not 0 < args.interval <= 30
            or (args.expected_jobs is not None and args.expected_jobs <= 0)):
        parser.error("Use an explicit repository, positive run/attempt/job count, full lowercase SHA, seconds <=55 and interval <=30")
    result = observe(args.repo, args.run_id, args.sha, args.attempt, args.output,
                     seconds=args.seconds, interval=args.interval, expected_jobs=args.expected_jobs)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return {"success": 0, "pending": 3}.get(result["outcome"], 1)


if __name__ == "__main__":
    sys.exit(main())
