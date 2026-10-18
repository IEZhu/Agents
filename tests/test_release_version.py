"""Release event, immutable tag, retry and calendar-version regressions."""
from copy import deepcopy
import base64
from pathlib import Path
from urllib.error import HTTPError
import importlib.util
import json

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_version", ROOT / "scripts/release_version.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
REPO = "IEZhu/Agents"
SHA = "a" * 40
OTHER = "b" * 40
MAIN, DEV = "e" * 40, "f" * 40  # a release merge commit's parents: main's previous HEAD, the dev HEAD


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
                 tag_objects=None, parents=None, files=None):
        self.date, self.tags = date, dict(tags or {})
        self.parents = dict(parents or {})  # parent SHA -> committer date; empty: a squash merge
        self.failure, self.race, self.calls = failure, race, []
        self.tag_objects = dict(tag_objects or {})
        self.files = dict(files or {})

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if method == "GET" and path.startswith("/contents/"):
            name, ref = path.removeprefix("/contents/").split("?ref=")
            assert ref == SHA  # never read the moving main branch during publication
            if name not in self.files:
                raise http_error(404)
            return {"type": "file", "encoding": "base64",
                    "content": base64.b64encode(self.files[name].encode()).decode()}
        if path == f"/git/commits/{SHA}":
            return {"committer": {"date": self.date}, "parents": [{"sha": sha} for sha in self.parents]}
        if path.startswith("/git/commits/"):
            return {"committer": {"date": self.parents[path.removeprefix("/git/commits/")]}}
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
                      "tag": "v26.10.14.1200", "sha": SHA, "source": SHA}
    assert api.tags == {"v26.10.14.1200": SHA}
    assert api.calls[0] == ("GET", f"/git/commits/{SHA}", None)


def test_merge_commit_is_versioned_by_the_dev_head_it_merged():
    # #415: dev showed 26.10.15.1814-dev; its merge into main at 19:05 was tagged v26.10.15.1905.
    api = API(date="2026-10-15T21:05:20+02:00",
              parents={MAIN: "2026-10-14T20:07:38+02:00", DEV: "2026-10-15T20:14:29+02:00"})
    result = release.publish(promotion(), REPO, api)
    assert result == {"status": "created", "version": "26.10.15.1814",
                      "tag": "v26.10.15.1814", "sha": SHA, "source": DEV}
    assert api.tags == {"v26.10.15.1814": SHA}  # the tag still marks the merge commit on main
    assert [call for call in api.calls if call[1].startswith("/git/commits/")] == [
        ("GET", f"/git/commits/{sha}", None) for sha in (SHA, MAIN, DEV)]


def test_merged_revision_older_than_main_keeps_the_merge_time():
    # A dev HEAD older than main's previous HEAD (an unmerged-back hotfix) must not move the version back.
    api = API(parents={MAIN: "2026-10-14T11:00:00+00:00", DEV: "2026-10-14T10:00:00+00:00"})
    result = release.publish(promotion(), REPO, api)
    assert (result["version"], result["source"]) == ("26.10.14.1200", SHA)


@pytest.mark.parametrize("parents", [
    {MAIN: "2026-10-14T11:00:00", DEV: "2026-10-14T11:30:00+00:00"},
    {MAIN: "2026-10-14T11:00:00+00:00", DEV: "invalid"},
])
def test_invalid_or_naive_parent_time_fails(parents):
    api = API(parents=parents)
    with pytest.raises(ValueError):
        release.publish(promotion(), REPO, api)
    assert not api.tags


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
        "status": "existing", "version": "26.10.14.1200", "tag": tag, "sha": SHA, "source": SHA,
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


class ReleaseAPI(API):
    """Fake published-release storage, separate from Git ref storage."""

    def __init__(self, existing=None, release_failure=None, release_race=None, **kwargs):
        super().__init__(**kwargs)
        self.release = existing
        self.release_failure = release_failure
        self.release_race = release_race

    def request(self, method, path, data=None):
        if not path.startswith("/releases"):
            return super().request(method, path, data)
        self.calls.append((method, path, data))
        if method == "GET":
            if self.release is None:
                raise http_error(404)
            return self.release
        assert method == "POST" and path == "/releases"
        if self.release_race is not None:
            self.release = self.release_race
            raise http_error(422)
        if self.release_failure is not None:
            raise self.release_failure
        self.release = {**data, "published_at": "2026-10-14T12:01:00Z"}
        return self.release


def published_release(**overrides):
    """Return the public fields required to reuse a published stable release."""
    return {"tag_name": "v26.10.14.1200", "draft": False, "prerelease": False,
            "published_at": "2026-10-14T12:01:00Z", **overrides}


def test_promotion_publishes_a_github_release_after_verifying_its_tag():
    api = ReleaseAPI()
    result = release.publish_release(promotion(), REPO, api)
    assert result == {"status": "created", "version": "26.10.14.1200",
                      "tag": "v26.10.14.1200", "sha": SHA, "source": SHA, "github_release": "created",
                      "release_url": "https://github.com/IEZhu/Agents/releases/tag/v26.10.14.1200"}
    write = next(call for call in api.calls if call[:2] == ("POST", "/releases"))
    assert write[2] == {
        "tag_name": result["tag"], "target_commitish": SHA, "name": result["tag"],
        "draft": False, "prerelease": False, "make_latest": "legacy",
        "generate_release_notes": True,
        "body": f"Promotion: https://github.com/{REPO}/pull/381\n\n"
                f"Merge commit: `{SHA}`.\n\nCalendar version: `26.10.14.1200`, the commit time of `{SHA}`.",
    }
    assert api.tags[result["tag"]] == SHA
    assert api.calls[-2:] == [("GET", f"/releases/tags/{result['tag']}", None),
                             ("GET", f"/git/ref/tags/{result['tag']}", None)]


def test_release_notes_name_the_dev_head_the_version_comes_from():
    api = ReleaseAPI(date="2026-10-15T21:05:20+02:00",
                     parents={MAIN: "2026-10-14T20:07:38+02:00", DEV: "2026-10-15T20:14:29+02:00"})
    result = release.publish_release(promotion(), REPO, api)
    assert result["tag"] == api.release["tag_name"] == "v26.10.15.1814"
    assert api.release["body"].endswith(f"Calendar version: `26.10.15.1814`, the commit time of `{DEV}`.")
    assert api.release["target_commitish"] == SHA


def test_published_release_rerun_preserves_existing_notes_and_makes_no_writes():
    existing = published_release(body="Maintainer notes")
    api = ReleaseAPI(existing=existing, tags={"v26.10.14.1200": SHA})
    result = release.publish_release(promotion(), REPO, api)
    assert result["status"] == result["github_release"] == "existing"
    assert api.release == existing
    assert all(method == "GET" for method, _, _ in api.calls)


def test_failed_release_creation_can_resume_after_the_tag_was_created():
    api = ReleaseAPI(release_failure=http_error(403))
    with pytest.raises(HTTPError):
        release.publish_release(promotion(), REPO, api)
    assert api.tags == {"v26.10.14.1200": SHA}
    api.release_failure = None
    result = release.publish_release(promotion(), REPO, api)
    assert result["status"] == "existing" and result["github_release"] == "created"
    assert sum(path == "/git/refs" for _, path, _ in api.calls) == 1


def test_same_minute_fallback_is_used_for_the_github_release_too():
    api = ReleaseAPI(tags={"v26.10.14.1200": OTHER})
    result = release.publish_release(promotion(), REPO, api)
    assert api.release["tag_name"] == result["tag"] == "v26.10.14.1200-pr381"
    assert api.tags["v26.10.14.1200"] == OTHER


def test_concurrent_release_creation_is_reused_without_editing_it():
    api = ReleaseAPI(release_race=published_release(body="Other publisher"))
    result = release.publish_release(promotion(), REPO, api)
    assert result["github_release"] == "existing"
    assert api.release["body"] == "Other publisher"
    assert all(method in {"GET", "POST"} for method, _, _ in api.calls)


@pytest.mark.parametrize("overrides", [
    {"draft": True}, {"prerelease": True}, {"published_at": None},
    {"tag_name": "vwrong"}, {"draft": None},
])
@pytest.mark.parametrize("racing", [False, True])
def test_non_public_or_mismatched_release_is_not_changed_or_accepted(overrides, racing):
    existing = published_release(**overrides)
    api = ReleaseAPI(**({"release_race": existing} if racing else {"existing": existing}))
    with pytest.raises(RuntimeError, match="published stable release"):
        release.publish_release(promotion(), REPO, api)
    assert api.release == existing
    assert all(method in {"GET", "POST"} for method, _, _ in api.calls)


@pytest.mark.parametrize("failure", [http_error(401), http_error(403), http_error(422), OSError("offline")])
def test_github_release_errors_are_visible_without_deleting_the_tag(failure):
    api = ReleaseAPI(release_failure=failure)
    with pytest.raises(type(failure)):
        release.publish_release(promotion(), REPO, api)
    assert api.tags == {"v26.10.14.1200": SHA}


def test_release_lookup_denied_is_not_treated_as_missing():
    class Denied(ReleaseAPI):
        def request(self, method, path, data=None):
            if path.startswith("/releases"):
                raise http_error(403)
            return super().request(method, path, data)
    api = Denied()
    with pytest.raises(HTTPError):
        release.publish_release(promotion(), REPO, api)
    assert api.release is None


def test_release_tag_moved_by_another_writer_is_reported():
    class Moved(ReleaseAPI):
        def request(self, method, path, data=None):
            result = super().request(method, path, data)
            if method == "POST" and path == "/releases":
                self.tags[data["tag_name"]] = OTHER
            return result
    with pytest.raises(RuntimeError, match="no longer targets"):
        release.publish_release(promotion(), REPO, Moved())


def test_non_release_event_does_not_publish_a_github_release():
    api = ReleaseAPI()
    event = promotion()
    event["pull_request"]["base"]["ref"] = "dev"
    assert release.publish_release(event, REPO, api) == {"status": "skipped"}
    assert not api.calls


def test_actions_entrypoint_publishes_and_reports_both_results(monkeypatch, tmp_path, capsys):
    event_file, summary_file = tmp_path / "event.json", tmp_path / "summary.md"
    event_file.write_text(json.dumps(promotion()), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GH_TOKEN", "fake-test-token")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    api = ReleaseAPI()
    monkeypatch.setattr(release, "GitHub", lambda repository, token: api)
    release.main()
    output = json.loads(capsys.readouterr().out)
    assert output["github_release"] == "created"
    assert output["sha"] == SHA
    summary = summary_file.read_text(encoding="utf-8")
    assert output["release_url"] in summary and SHA in summary
    assert "Glama Auto-Release builds asynchronously" in summary


def test_created_release_must_be_visible_before_success():
    class Invisible(ReleaseAPI):
        def request(self, method, path, data=None):
            if method == "GET" and path.startswith("/releases"):
                raise http_error(404)
            return super().request(method, path, data)
    with pytest.raises(RuntimeError, match="published stable release"):
        release.publish_release(promotion(), REPO, Invisible())


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


def test_promotion_checks_require_one_new_calendar_version():
    """A promotion must advance VERSION and synchronize the plugin cache version."""
    workflow = yaml.load((ROOT / ".github/workflows/release-version.yml").read_text(),
                         Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["check"]["steps"]
    version_check = next((step for step in steps
                          if "bump_plugin_version.py --check" in step.get("run", "")), None)
    assert version_check is not None, "Promotion must verify the unified calendar version"
    assert "base.ref == 'main'" in version_check["if"]
    assert "head.ref == 'dev'" in version_check["if"]
    assert version_check["env"]["PLUGIN_BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert '--base "$PLUGIN_BASE_SHA"' in version_check["run"]
    assert steps[0]["with"]["fetch-depth"] == "0"


def canonical_files(version="26.10.18.0805", plugin_version=None):
    return {"VERSION": version + "\n",
            "plugins/agents-core/.claude-plugin/plugin.json": json.dumps({
                "name": "agents-core", "version": plugin_version or version})}


def test_canonical_number_is_shared_by_server_plugin_and_exact_merge_tag(tmp_path, monkeypatch):
    from src import version as runtime
    files = canonical_files()
    (tmp_path / "VERSION").write_text(files["VERSION"], encoding="utf-8")
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    runtime.agents_core_version.cache_clear()
    try:
        api = ReleaseAPI(files=files, date="2026-10-20T09:00:00Z")
        result = release.publish_release(promotion(), REPO, api)
        assert result["version"] == runtime.agents_core_version() == "26.10.18.0805"
        assert result["tag"] == api.release["tag_name"] == "v26.10.18.0805"
        assert api.tags == {result["tag"]: SHA}
        assert result["version_file"] == "VERSION"
        assert f"from VERSION at `{SHA}`" in api.release["body"]
        writes = sum(method == "POST" for method, _, _ in api.calls)
        assert release.publish_release(promotion(), REPO, api)["status"] == "existing"
        assert sum(method == "POST" for method, _, _ in api.calls) == writes
    finally:
        runtime.agents_core_version.cache_clear()


@pytest.mark.parametrize("version,plugin", [("0.1.3", None), ("26.02.30.0805", None),
                                          ("26.10.18.0805", "0.1.3")])
def test_invalid_or_mismatched_canonical_version_fails_before_publication(version, plugin):
    api = API(files=canonical_files(version, plugin))
    with pytest.raises(ValueError):
        release.publish(promotion(), REPO, api)
    assert all(method == "GET" for method, _, _ in api.calls)


def test_canonical_version_requires_a_plugin_manifest():
    api = API(files={"VERSION": "26.10.18.0805\n"})
    with pytest.raises(ValueError, match="no plugin manifest"):
        release.publish(promotion(), REPO, api)
    assert not api.tags


@pytest.mark.parametrize("code", [401, 403, 500])
def test_canonical_file_read_failure_never_falls_back_to_commit_time(code):
    class Unreadable(API):
        def request(self, method, path, data=None):
            if path.startswith("/contents/"):
                raise http_error(code)
            return super().request(method, path, data)
    api = Unreadable()
    with pytest.raises(HTTPError):
        release.publish(promotion(), REPO, api)
    assert not api.tags


@pytest.mark.parametrize("racing", [False, True])
def test_canonical_tag_conflict_preserves_refs_and_never_adds_another_number(racing):
    api = API(files=canonical_files(), **({"race": OTHER} if racing else
                                        {"tags": {"v26.10.18.0805": OTHER}}))
    with pytest.raises(RuntimeError, match="Canonical release tag"):
        release.publish(promotion(), REPO, api)
    assert api.tags == {"v26.10.18.0805": OTHER}


def test_canonical_concurrent_publication_reuses_matching_tag():
    api = API(files=canonical_files(), race=SHA)
    assert release.publish(promotion(), REPO, api)["status"] == "existing"
    assert api.tags == {"v26.10.18.0805": SHA}
