"""Publish a calendar release tag for a merged, same-repository dev -> main PR."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid repository")
        self.prefix = f"https://api.github.com/repos/{repository}"
        self.token = token

    def request(self, method, path, data=None):
        request = Request(
            self.prefix + path,
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)


def release_pr(event, repository):
    pr = event.get("pull_request") or {}
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if not (
        event.get("action") == "closed" and pr.get("merged") is True
        and head.get("ref") == "dev" and base.get("ref") == "main"
        and (head.get("repo") or {}).get("full_name") == repository
        and (base.get("repo") or {}).get("full_name") == repository
        and (event.get("repository") or {}).get("full_name") == repository
    ):
        return None
    sha = pr.get("merge_commit_sha", "")
    number = pr.get("number")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Merged release PR has no valid merge commit SHA")
    if type(number) is not int or number <= 0:
        raise ValueError("Merged release PR has no valid number")
    return pr


def tag_target(api, tag):
    try:
        ref = api.request("GET", f"/git/ref/tags/{tag}")
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    return ref["object"]["type"], ref["object"]["sha"]


def publish(event, repository, api):
    pr = release_pr(event, repository)
    if pr is None:
        return {"status": "skipped"}
    sha = pr["merge_commit_sha"]
    commit = api.request("GET", f"/git/commits/{sha}")
    moment = datetime.fromisoformat(commit["committer"]["date"].replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("Merge commit timestamp has no timezone")
    version = moment.astimezone(timezone.utc).strftime("%y.%m.%d.%H%M")
    # A second release in the same minute has its own stable, per-PR tag.
    for tag in (f"v{version}", f"v{version}-pr{pr['number']}"):
        existing = tag_target(api, tag)
        if existing == ("commit", sha):
            return {"status": "existing", "version": version, "tag": tag, "sha": sha}
        if existing is not None:
            continue
        try:
            api.request("POST", "/git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
        except HTTPError as error:
            if error.code != 422:
                raise
            # The same or another merge may have created the name concurrently.
            existing = tag_target(api, tag)
            if existing == ("commit", sha):
                return {"status": "existing", "version": version, "tag": tag, "sha": sha}
            if existing is not None:
                continue
            raise
        if tag_target(api, tag) != ("commit", sha):
            raise RuntimeError(f"Created tag {tag} does not target the merge commit")
        return {"status": "created", "version": version, "tag": tag, "sha": sha}
    raise RuntimeError("Both release tag names already target different objects")


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    repository = os.environ["GITHUB_REPOSITORY"]
    result = publish(event, repository, GitHub(repository, os.environ["GH_TOKEN"]))
    print(json.dumps(result))
    if result["status"] != "skipped" and os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(f"Release version: **{result['version']}**\n\n"
                          f"Tag: `{result['tag']}` at `{result['sha']}` ({result['status']}).\n")


if __name__ == "__main__":
    main()
