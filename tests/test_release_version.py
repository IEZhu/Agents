"""Release event, immutable tag, retry and calendar-version regressions."""
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError
import importlib.util

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_version", ROOT / "scripts/release_version.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
REPO = "IEZhu/Agents"
SHA = "a" * 40
OTHER = "b" * 40


def promotion():
    repo = {"full_name": REPO}
    return {"action": "closed", "repository": repo, "pull_request": {
        "number": 381, "merged": True, "merge_commit_sha": SHA,
        "head": {"ref": "dev", "repo": repo}, "base": {"ref": "main", "repo": repo},
    }}


def http_error(code):
    return HTTPError("https://api.github.com", code, "synthetic error", {}, None)


class API:
    def __init__(self, date="2026-10-14T14:00:59+02:00", tags=None, failure=None, race=None,
                 tag_objects=None):
        self.date, self.tags = date, dict(tags or {})
        self.failure, self.race, self.calls = failure, race, []
        self.tag_objects = dict(tag_objects or {})

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if path == f"/git/commits/{SHA}":
            return {"committer": {"date": self.date}}
        if method == "GET" and path.startswith("/git/tags/"):
            return {"object": self.tag_objects[path.removeprefix("/git/tags/")]}
        if method == "GET":
            tag = path.removeprefix("/git/ref/tags/")
            if tag not in self.tags:
                raise http_error(404)
            target = self.tags[tag]
            return {"object": target if isinstance(target, dict) else
                    {"type": "commit", "sha": target}}
        if self.race is not None:
            self.tags[data["ref"].removeprefix("refs/tags/")] = self.race
            self.race = None
            raise http_error(422)
        if self.failure:
            raise self.failure
        self.tags[data["ref"].removeprefix("refs/tags/")] = data["sha"]
        return {}


def test_publishes_exact_merge_calendar_version_and_not_current_main():
    api = API()
    result = release.publish(promotion(), REPO, api)
    assert result == {"status": "created", "version": "26.10.14.1200",
                      "tag": "v26.10.14.1200", "sha": SHA}
    assert api.tags == {"v26.10.14.1200": SHA}
    assert api.calls[0] == ("GET", f"/git/commits/{SHA}", None)


@pytest.mark.parametrize("mutation", [
    lambda e: e.update(action="opened"),
    lambda e: e["pull_request"].update(merged=False),
    lambda e: e["pull_request"]["head"].update(ref="test"),
    lambda e: e["pull_request"]["base"].update(ref="dev"),
    lambda e: e["pull_request"]["head"].update(repo={"full_name": "fork/Agents"}),
    lambda e: e["pull_request"]["base"].update(repo={"full_name": "fork/Agents"}),
    lambda e: e["repository"].update(full_name="fork/Agents"),
    lambda e: e.pop("pull_request"),
])
def test_non_release_events_make_no_requests(mutation):
    event, api = deepcopy(promotion()), API()
    mutation(event)
    assert release.publish(event, REPO, api) == {"status": "skipped"}
    assert not api.calls


@pytest.mark.parametrize("field,value", [
    ("merge_commit_sha", None), ("merge_commit_sha", "HEAD"), ("merge_commit_sha", "b" * 39),
    ("number", True), ("number", 0), ("number", "381"),
])
def test_invalid_merge_identity_fails_before_requests(field, value):
    event, api = promotion(), API()
    event["pull_request"][field] = value
    with pytest.raises(ValueError):
        release.publish(event, REPO, api)
    assert not api.calls


def test_rerun_is_noop_and_does_not_use_rerun_time():
    api = API()
    first = release.publish(promotion(), REPO, api)
    second = release.publish(promotion(), REPO, api)
    assert second == {**first, "status": "existing"}
    assert sum(method == "POST" for method, _, _ in api.calls) == 1


@pytest.mark.parametrize("tag", ["v26.10.14.1200", "v26.10.14.1200-pr381"])
@pytest.mark.parametrize("nested", [False, True])
def test_matching_annotated_release_tag_is_reused_without_writes(tag, nested):
    outer, inner = "c" * 40, "d" * 40
    tags = {tag: {"type": "tag", "sha": outer}}
    if tag.endswith("-pr381"):
        tags["v26.10.14.1200"] = OTHER
    objects = {outer: {"type": "commit", "sha": SHA}}
    if nested:
        objects[outer] = {"type": "tag", "sha": inner}
        objects[inner] = {"type": "commit", "sha": SHA}
    api = API(tags=tags, tag_objects=objects)
    assert release.publish(promotion(), REPO, api) == {
        "status": "existing", "version": "26.10.14.1200", "tag": tag, "sha": SHA,
    }
    assert api.tags == tags
    assert all(method == "GET" for method, _, _ in api.calls)


def test_annotated_tag_for_another_commit_is_preserved_and_uses_fallback():
    obj = "c" * 40
    target = {"type": "tag", "sha": obj}
    api = API(tags={"v26.10.14.1200": target},
              tag_objects={obj: {"type": "commit", "sha": OTHER}})
    assert release.publish(promotion(), REPO, api)["tag"] == "v26.10.14.1200-pr381"
    assert api.tags == {"v26.10.14.1200": target, "v26.10.14.1200-pr381": SHA}


def test_concurrent_matching_annotated_tag_is_reused_without_fallback():
    obj = "c" * 40
    api = API(race={"type": "tag", "sha": obj},
              tag_objects={obj: {"type": "commit", "sha": SHA}})
    assert release.publish(promotion(), REPO, api)["status"] == "existing"
    assert api.tags == {"v26.10.14.1200": {"type": "tag", "sha": obj}}
    assert sum(method == "POST" for method, _, _ in api.calls) == 1


@pytest.mark.parametrize("code", [403, 404])
def test_annotated_tag_object_read_errors_fail_before_publication(code):
    class UnreadableTag(API):
        def request(self, method, path, data=None):
            if path.startswith("/git/tags/"):
                raise http_error(code)
            return super().request(method, path, data)
    api = UnreadableTag(tags={"v26.10.14.1200": {"type": "tag", "sha": "c" * 40}})
    with pytest.raises(HTTPError) as error:
        release.publish(promotion(), REPO, api)
    assert error.value.code == code
    assert all(method == "GET" for method, _, _ in api.calls)


def test_same_minute_releases_get_distinct_stable_tags():
    api = API(tags={"v26.10.14.1200": OTHER})
    result = release.publish(promotion(), REPO, api)
    assert result["tag"] == "v26.10.14.1200-pr381"
    assert api.tags["v26.10.14.1200"] == OTHER
    assert release.publish(promotion(), REPO, api)["status"] == "existing"


@pytest.mark.parametrize("race,tag", [(SHA, "v26.10.14.1200"), (OTHER, "v26.10.14.1200-pr381")])
def test_concurrent_create_converges_without_updating_refs(race, tag):
    api = API(race=race)
    assert release.publish(promotion(), REPO, api)["tag"] == tag
    assert {method for method, _, _ in api.calls} <= {"GET", "POST"}


def test_both_names_conflict_fails_and_preserves_tags():
    tags = {"v26.10.14.1200": OTHER, "v26.10.14.1200-pr381": OTHER}
    api = API(tags=tags)
    with pytest.raises(RuntimeError, match="different objects"):
        release.publish(promotion(), REPO, api)
    assert api.tags == tags
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("failure", [http_error(401), http_error(403), http_error(422), OSError("offline")])
def test_publication_errors_propagate(failure):
    with pytest.raises(type(failure)):
        release.publish(promotion(), REPO, API(failure=failure))


def test_tag_read_authentication_failure_propagates():
    class Unauthorized(API):
        def request(self, method, path, data=None):
            if "/git/ref/" in path:
                raise http_error(403)
            return super().request(method, path, data)
    with pytest.raises(HTTPError):
        release.publish(promotion(), REPO, Unauthorized())


@pytest.mark.parametrize("date", ["invalid", "2026-10-14T12:00:00"])
def test_invalid_or_naive_commit_time_fails(date):
    api = API(date=date)
    with pytest.raises(ValueError):
        release.publish(promotion(), REPO, api)
    assert not api.tags


def test_workflow_runs_only_after_main_merge_with_job_write_permission():
    workflow = yaml.load((ROOT / ".github/workflows/release-version.yml").read_text(),
                         Loader=yaml.BaseLoader)
    assert workflow["on"] == {"pull_request": {"branches": ["dev", "main"],
        "types": ["opened", "synchronize", "reopened", "closed"]}}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["publish"]
    assert job["permissions"] == {"contents": "write"}
    assert "merged == true" in job["if"]
    assert "base.ref == 'main'" in job["if"]
    assert "head.repo.full_name == github.repository" in job["if"]
    assert "base.repo.full_name == github.repository" in job["if"]
    assert workflow["jobs"]["check"]["if"] == "github.event.action != 'closed'"
    assert "head.ref == 'dev'" in job["if"]
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.merge_commit_sha }}"
    assert checkout["with"]["persist-credentials"] == "false"
