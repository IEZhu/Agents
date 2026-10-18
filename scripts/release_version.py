"""Publish a calendar tag and GitHub Release for a same-repository dev -> main PR."""
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import calendar_version


class GitHub:
    """Repository-scoped JSON client for GitHub's Git database and Releases APIs."""

    def __init__(self, repository, token):
        """Bind credentials to a syntactically valid owner/repository name."""
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid repository")
        self.prefix = f"https://api.github.com/repos/{repository}"
        self.token = token

    def request(self, method, path, data=None):
        """Return decoded JSON, propagating HTTP and transport failures."""
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
    """Return a validated merged promotion PR, or None for unrelated events."""
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
    """Resolve a tag to its final object; only a missing ref returns None."""
    try:
        ref = api.request("GET", f"/git/ref/tags/{tag}")
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    target = ref["object"]
    while target["type"] == "tag":
        target = api.request("GET", f"/git/tags/{target['sha']}")["object"]
    return target["type"], target["sha"]


def commit_time(commit):
    """A Git database commit's committer time; a time without a timezone is invalid."""
    moment = datetime.fromisoformat(commit["committer"]["date"].replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("Commit timestamp has no timezone")
    return moment


def release_moment(api, commit):
    """Legacy version time and source for revisions without a VERSION file.

    A merge commit takes the time of the revision it merged, the promoted dev HEAD, so the
    release keeps the version dev showed for it. A merged revision older than main's previous
    HEAD, and a squash or rebase merge, keep the merge commit's own time. src/version.py
    applies the same rule to a main checkout.
    """
    parents = [parent["sha"] for parent in commit.get("parents") or []]
    if len(parents) == 2:
        previous, merged = (api.request("GET", f"/git/commits/{sha}") for sha in parents)
        if commit_time(merged) >= commit_time(previous):
            return commit_time(merged), parents[1]
    return commit_time(commit), commit["sha"]


def repository_text(api, sha, path):
    """Read a UTF-8 file at an immutable commit; only a 404 means absent."""
    try:
        blob = api.request("GET", f"/contents/{path}?ref={sha}")
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    if blob.get("type") != "file" or blob.get("encoding") != "base64":
        raise ValueError(f"Expected a base64 file at {path}")
    return base64.b64decode("".join(blob["content"].split()), validate=True).decode("utf-8")


def canonical_version(api, sha):
    """Verify the single release number against the plugin at the exact merge SHA."""
    text = repository_text(api, sha, "VERSION")
    if text is None:
        return None
    version = text.strip()
    calendar_version.parse(version)
    manifest = repository_text(api, sha, "plugins/agents-core/.claude-plugin/plugin.json")
    if manifest is None:
        raise ValueError("Release has VERSION but no plugin manifest")
    plugin = json.loads(manifest)
    if not isinstance(plugin, dict) or plugin.get("name") != "agents-core" or plugin.get("version") != version:
        raise ValueError("Plugin version must match the canonical VERSION file")
    return version


def publish(event, repository, api):
    """Create or reuse the release's UTC calendar tag at the merge commit without moving refs."""
    pr = release_pr(event, repository)
    if pr is None:
        return {"status": "skipped"}
    sha = pr["merge_commit_sha"]
    commit = api.request("GET", f"/git/commits/{sha}")
    version = canonical_version(api, sha)
    metadata = {"version_file": "VERSION"} if version is not None else {}
    source = sha
    if version is None:
        moment, source = release_moment(api, {**commit, "sha": sha})
        version = moment.astimezone(timezone.utc).strftime(calendar_version.FORMAT)
    # Preserve historical retry names. New releases have exactly one canonical tag.
    tags = (f"v{version}",) if metadata else (f"v{version}", f"v{version}-pr{pr['number']}")
    for tag in tags:
        existing = tag_target(api, tag)
        if existing == ("commit", sha):
            return {"status": "existing", "version": version, "tag": tag, "sha": sha, "source": source, **metadata}
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
                return {"status": "existing", "version": version, "tag": tag, "sha": sha, "source": source, **metadata}
            if existing is not None:
                continue
            raise
        if tag_target(api, tag) != ("commit", sha):
            raise RuntimeError(f"Created tag {tag} does not target the merge commit")
        return {"status": "created", "version": version, "tag": tag, "sha": sha, "source": source, **metadata}
    if metadata:
        raise RuntimeError("Canonical release tag already targets another object; prepare a new VERSION through dev")
    raise RuntimeError("Both release tag names already target different objects")


def published_release(api, tag):
    """Read a release by tag, distinguishing a missing release from API failures."""
    try:
        return api.request("GET", f"/releases/tags/{tag}")
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


def publish_release(event, repository, api):
    """Publish once after tagging; reruns preserve existing release content."""
    result = publish(event, repository, api)
    if result["status"] == "skipped":
        return result
    tag, sha = result["tag"], result["sha"]
    existing = published_release(api, tag)
    status = "existing"
    if existing is None:
        origin = (f"from VERSION at `{sha}`" if result.get("version_file")
                  else f"the commit time of `{result['source']}`")
        try:
            api.request("POST", "/releases", {
                "tag_name": tag, "target_commitish": sha, "name": tag,
                "draft": False, "prerelease": False, "make_latest": "legacy",
                "generate_release_notes": True,
                "body": f"Promotion: https://github.com/{repository}/pull/{event['pull_request']['number']}\n\n"
                        f"Merge commit: `{sha}`.\n\nCalendar version: `{result['version']}`, "
                        f"{origin}.",
            })
            status = "created"
        except HTTPError as error:
            if error.code != 422:
                raise
            # Another publisher may have won. Reuse only an actual public release.
            existing = published_release(api, tag)
            if existing is None:
                raise
        else:
            existing = published_release(api, tag)
    if not (existing and existing.get("tag_name") == tag
            and existing.get("draft") is False and existing.get("prerelease") is False
            and existing.get("published_at")):
        raise RuntimeError(f"Tag {tag} has no matching published stable release; inspect it manually")
    if tag_target(api, tag) != ("commit", sha):
        raise RuntimeError(f"Release tag {tag} no longer targets the merge commit")
    return {**result, "github_release": status,
            "release_url": f"https://github.com/{repository}/releases/tag/{tag}"}


def main():
    """Read the Actions event and credentials, then print and summarize publication."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    repository = os.environ["GITHUB_REPOSITORY"]
    result = publish_release(event, repository, GitHub(repository, os.environ["GH_TOKEN"]))
    print(json.dumps(result))
    if result["status"] != "skipped" and os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(f"Release version: **{result['version']}**\n\n"
                          f"Tag: `{result['tag']}` at `{result['sha']}` ({result['status']}).\n\n"
                          f"GitHub Release: {result['release_url']} ({result['github_release']}).\n\n"
                          "Glama Auto-Release builds asynchronously; verify its build and published version separately.\n")


if __name__ == "__main__":
    main()
