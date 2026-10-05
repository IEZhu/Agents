"""The web UI's library sync routes (#170): ``/ui/api/sync…`` behind the editor's session.

``FakeSyncer`` of ``test_daemon_user_sync`` stands in for the engine's git work. GitHub is the real
``GitHubAccount`` against the fake GitHub of ``test_user_sync_github`` with an in-memory token store;
scopes and conflicts use the real engine on a temporary library. Nothing reaches the network, an OS
secret store or the real daemon.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import socket

import httpx
import pytest

from src import user_library
from src.daemon.sync_ui import ROUTES
from src.flows import FlowCatalog
from src.user_flows import FlowLibrary
from src.user_sync import keys
from src.user_sync.engine import Syncer, SyncError
from src.user_sync.github import GitHubAccount
from tests.test_daemon_user_sync import TOKEN, FakeSyncer, make_app, running, settled, until
from tests.test_user_sync_github import (  # noqa: F401  (fake and no_real_secret_store are fixtures)
    DEVICE_CODE, GRANTED, PENDING, MemoryStore, assert_secret_free, fake, key_material, no_real_secret_store,
    public_key)
from tests.test_user_sync_github import TOKEN as GITHUB_TOKEN

UI = {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:8765"}
PRIVATE = ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAAPrivateKeyMaterialForTestsOnly\n"
           "-----END OPENSSH PRIVATE KEY-----\n")
REPOSITORY = "octocat/agents-library"
THIS_KEY = public_key(3, "agents-core-sync:mac-0001")
OTHER_KEY = public_key(4, "agents-core-sync:linux-9f9f")
KEYS_PATH = f"/api/v3/repos/{REPOSITORY}/keys"


class UISyncer(FakeSyncer):
    """What the web UI calls on the engine. Git work is faked; scopes and conflicts are the real
    engine's on the same library; GitHub is a real account against the fake GitHub."""

    def __init__(self, tmp_path, account, **options):
        super().__init__(tmp_path / "library", **options)
        self.state_dir = tmp_path / "service" / "user-sync"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / keys.KEY_NAME).write_text(PRIVATE)  # served by no route
        (self.state_dir / f"{keys.KEY_NAME}.pub").write_text(THIS_KEY + "\n")
        self.allow_file_remote = False
        self.account = account
        vars(self.config).update(remote=f"git@github.com:{REPOSITORY}.git", name="Me", email="me@example.com",
                                 label="mac-0001", branch="main", private_confirmed=False,
                                 ask_new_repositories=False)
        self.engine = Syncer(self.library, tmp_path / "engine-state")

    def github_account(self):
        return self.account

    def github_client(self):
        if not self.account.status()["connected"]:
            raise SyncError("not_signed_in", "no GitHub account is connected here")
        return self.account.client()

    def conflicts(self):
        return self.engine.conflicts()

    def scopes(self):
        return self.engine.scopes()

    def change_scopes(self, **arguments):
        self._record("change_scopes", **arguments)
        return self.engine.change_scopes(**arguments)

    def configure(self, **arguments):
        self._record("configure", **arguments)
        return self.status()

    def resolve(self, conflict_id, action):
        self._record("resolve", id=conflict_id, action=action)
        return {"status": "resolved", "id": conflict_id, "action": action}

    def regenerate_key(self):
        self._record("regenerate_key")
        return {"status": "replaced", "deploy_key": "manual", "public_key": THIS_KEY}

    def add_deploy_key(self):
        self._record("add_deploy_key")
        return {"status": "present", "message": "already there"}

    def setup_github(self, repository, **arguments):
        self._record("setup_github", repository=repository, **arguments)
        return {"status": "waiting_for_access", "repository": repository, "label": "mac-0001"}

    def privacy(self):
        return "private"


@pytest.fixture
def account(fake, tmp_path):
    return GitHubAccount(tmp_path / "service" / "user-sync", "agents-core-sync-test", host="github.com",
                         web_url=fake.url, api_url=fake.api, client_id="Iv1.test", store=MemoryStore())


def signed_in(fake, account):
    fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
    account.complete_sign_in(GITHUB_TOKEN)
    return account


async def sign_in(http):
    code = (await http.post("/admin/ui/code", headers={"Authorization": "Bearer " + TOKEN})).json()["code"]
    assert (await http.post("/ui/api/session", json={"code": code}, headers=UI)).status_code == 200


class Recorder:
    """Every answer's text, to show that none holds a secret."""

    def __init__(self, http):
        self.http, self.texts = http, []

    async def __call__(self, method, path, body=None, headers=UI):
        response = await self.http.request(method, path, headers=headers,
                                           content=None if body is None else json.dumps(body))
        self.texts.append(response.text)
        return response


@asynccontextmanager
async def ui(tmp_path, syncer, **options):
    app, service = make_app(tmp_path, syncer, **options)
    async with running(app) as http:
        await until(lambda: service.user_sync.armed)
        await sign_in(http)
        yield Recorder(http), service


def no_secrets(texts):
    assert_secret_free(*texts)  # the GitHub token and the device code, or any piece of them
    for text in texts:
        assert "PRIVATE KEY" not in text and "PrivateKeyMaterial" not in text


# --- access -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_sync_route_needs_the_session_the_header_and_the_pages_origin(tmp_path, account):
    syncer = UISyncer(tmp_path, account, started=False)
    app, service = make_app(tmp_path, syncer)
    routes = [(method, path) for path, methods in ROUTES.items() for method in methods]
    assert len(routes) == 21
    async with running(app) as http:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as stranger:
            for method, path in routes:
                assert (await stranger.request(method, path, headers=UI)).status_code == 401, path
                bearer = await stranger.request(method, path, headers={**UI, "Authorization": "Bearer " + TOKEN})
                assert bearer.status_code == 401, path  # the service token is not a session
        await sign_in(http)
        for method, path in routes:
            for headers in ({"Origin": "http://127.0.0.1:8765"},  # no X-Agents-UI: a form or a link
                            {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:9999"},  # another loopback port
                            {"X-Agents-UI": "1", "Origin": "https://attacker.example"}):
                response = await http.request(method, path, headers=headers)
                assert response.status_code == 403 and response.json() == {"error": "origin_not_allowed"}, \
                    (method, path, headers)
            assert (await http.request(method, path, headers={**UI, "Host": "attacker.example"})).status_code == 403
        assert syncer.calls == [] and syncer.runs == []
        # The page's own GET carries the header and, as browsers do on same-origin GETs, no Origin.
        status = await http.get("/ui/api/sync", headers={"X-Agents-UI": "1"})
        assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
        assert "set-cookie" in status.headers  # the session slides like the editor's


@pytest.mark.asyncio
async def test_bad_requests_get_json_answers_and_change_nothing(tmp_path, account):
    syncer = UISyncer(tmp_path, account, started=False)
    async with ui(tmp_path, syncer) as (call, service):
        assert (await call("GET", "/ui/api/sync/nothing")).status_code == 404
        wrong = await call("GET", "/ui/api/sync/run")
        assert wrong.status_code == 405 and wrong.headers["allow"] == "POST"
        assert (await call("DELETE", "/ui/api/sync/scopes")).headers["allow"] == "GET, PUT"
        broken = await call.http.post("/ui/api/sync/check", content=b"{not json", headers=UI)
        assert broken.status_code == 400 and broken.json()["error"] == "invalid_request"
        for path, body in [("/ui/api/sync/check", {"force": True}),
                           ("/ui/api/sync/setup", {"name": "Me", "email": "me@example.com"}),
                           ("/ui/api/sync/setup", {"github": REPOSITORY, "remote": "git@x:y/z.git", "name": "Me",
                                                   "email": "me@example.com"}),
                           ("/ui/api/sync/setup", {"github": REPOSITORY, "email": "me@example.com"}),
                           ("/ui/api/sync/setup", {"github": REPOSITORY, "name": "Me", "email": "me@example.com",
                                                   "token": "x"}),
                           ("/ui/api/sync/start", {}), ("/ui/api/sync/start", {"confirm": 5}),
                           ("/ui/api/sync/conflicts/resolve", {"id": "x", "action": "merge"}),
                           ("/ui/api/sync/machines/remove", {"id": "12"}),
                           ("/ui/api/sync/machines/remove", {"id": True})]:
            response = await call("POST", path, body)
            assert response.status_code == 400 and response.json()["error"] == "invalid_request", (path, body)
        for body in ({}, {"fetch_minutes": "5"}, {"paused": "yes"}, {"unknown": 1}, {"name": ["Me"]}):
            assert (await call("PUT", "/ui/api/sync/settings", body)).status_code == 400, body
        for body in ({}, {"exclude": "history"}, {"exclude": [5]}):
            assert (await call("PUT", "/ui/api/sync/scopes", body)).status_code == 400, body
        refused = await call("PUT", "/ui/api/sync/scopes", {"exclude": ["nope/x"]})
        assert refused.status_code == 409 and refused.json()["reason"] == "invalid"
        assert [name for name, _ in syncer.calls] == ["change_scopes"]


# --- status -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_status_says_what_the_page_needs(tmp_path, account):
    syncer = UISyncer(tmp_path, account)
    syncer.result = {"status": "synced", "sent": [], "received": ["common/a.md"], "received_from": ["laptop"],
                     "conflicts": [], "pushed": False}
    record = {"path": "personas/common/doc.json", "flow": "user:doc", "kind": "both_changed", "kept": "remote",
              "machine": "laptop", "time": "2026-10-05T12:00:00+00:00", "local_content": "{\"persona\": \"mine\"}"}
    conflicts = syncer.library / ".agents-sync" / "conflicts"
    conflicts.mkdir(parents=True)
    (conflicts / "20261005T120000000000Z-aaaaaaaaaa.json").write_text(json.dumps(record))
    async with ui(tmp_path, syncer) as (call, service):
        await settled(service, syncer, 1)
        response = await call("GET", "/ui/api/sync")
        status = response.json()
        assert status["state"] == "synced" and status["started"] and status["loop"]["state"] == "running"
        assert (status["repository"], status["github_repository"], status["ssh"]) == (REPOSITORY, REPOSITORY, True)
        assert status["github"] is None  # no account connected
        assert status["loop"]["last_cycle"]["received_from"] == ["laptop"]  # who sent what the cycle received
        assert status["conflict_list"] == [{"id": "20261005T120000000000Z-aaaaaaaaaa", "path": record["path"],
                                            "flow": "user:doc", "repo_key": None, "kind": "both_changed",
                                            "kept": "remote", "machine": "laptop", "time": record["time"],
                                            "deleted_on": None, "mine": "content"}]
        assert "local_content" not in response.text  # the kept text stays out of the status
        no_secrets(call.texts)


@pytest.mark.asyncio
async def test_a_fresh_installation_gets_a_neutral_label_to_suggest(tmp_path, account):
    syncer = UISyncer(tmp_path, account)
    syncer.set_up = False
    async with ui(tmp_path, syncer) as (call, _):
        status = (await call("GET", "/ui/api/sync")).json()
        assert status["state"] == "off" and status["started"] is None
        label = status["suggested_label"]
        assert label.split("-")[0] in ("mac", "linux", "windows") and socket.gethostname().lower() not in label


# --- GitHub sign-in ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_in_keeps_the_device_code_and_the_token_in_the_daemon(fake, tmp_path, account):
    syncer = UISyncer(tmp_path, account, started=False)
    fake.reply("POST", "/login/device/code", 200, {"device_code": DEVICE_CODE, "user_code": "WDJB-MJHT",
                                                   "verification_uri": "https://github.com/login/device",
                                                   "expires_in": 900, "interval": 5})
    async with ui(tmp_path, syncer) as (call, service):
        now = [1000.0]
        service.flows_ui.sync.clock = lambda: now[0]
        started = (await call("POST", "/ui/api/sync/github/device", {})).json()
        assert set(started) == {"attempt", "user_code", "verification_uri", "interval", "expires_in"}
        assert (started["user_code"], started["interval"]) == ("WDJB-MJHT", 5)
        polls = lambda: [request for request in fake.requests if request.path == "/login/oauth/access_token"]  # noqa: E731
        poll = f"/ui/api/sync/github/device?attempt={started['attempt']}"
        assert (await call("GET", "/ui/api/sync/github/device?attempt=guess")).status_code == 404
        early = await call("GET", poll)
        assert early.json() == {"state": "pending", "interval": 5} and polls() == []  # before the interval: no request
        fake.reply("POST", "/login/oauth/access_token", 200, PENDING)
        now[0] += 5
        assert (await call("GET", poll)).json() == {"state": "pending", "interval": 5} and len(polls()) == 1
        fake.reply("POST", "/login/oauth/access_token", 200, GRANTED)
        fake.reply("GET", "/api/v3/user", 200, {"login": "octocat"})
        now[0] += 5
        first, second = await asyncio.gather(call("GET", poll), call("GET", poll))  # one poll at a time
        answers = sorted([first, second], key=lambda response: response.status_code)
        assert answers[0].status_code == 200 and answers[0].json()["state"] == "connected"
        assert answers[0].json()["github"]["login"] == "octocat" and answers[1].status_code == 404
        assert len(polls()) == 2  # the second poll waited, and found the sign-in finished
        assert account._store.token == GITHUB_TOKEN  # kept in the store, never sent to the browser
        status = (await call("GET", "/ui/api/sync")).json()
        assert status["github"]["connected"] and status["github"]["login"] == "octocat"
        forgotten = await call("POST", "/ui/api/sync/github/forget", {})
        assert forgotten.status_code == 200 and forgotten.json()["revoke_url"].endswith("/settings/applications")
        assert account._store.token is None and (await call("GET", "/ui/api/sync")).json()["github"] is None
        no_secrets(call.texts)


@pytest.mark.asyncio
async def test_an_expired_or_refused_sign_in_ends_with_its_reason(fake, tmp_path, account):
    syncer = UISyncer(tmp_path, account, started=False)
    code = {"device_code": DEVICE_CODE, "user_code": "WDJB-MJHT", "verification_uri": "https://github.com/login/device",
            "expires_in": 900, "interval": 5}
    async with ui(tmp_path, syncer) as (call, service):
        now = [1000.0]
        service.flows_ui.sync.clock = lambda: now[0]
        fake.reply("POST", "/login/device/code", 200, code)
        attempt = (await call("POST", "/ui/api/sync/github/device", {})).json()["attempt"]
        fake.reply("POST", "/login/oauth/access_token", 200, {"error": "access_denied"})
        now[0] += 5
        denied = await call("GET", f"/ui/api/sync/github/device?attempt={attempt}")
        assert denied.status_code == 409 and denied.json()["reason"] == "access_denied"
        assert (await call("GET", f"/ui/api/sync/github/device?attempt={attempt}")).status_code == 404
        fake.reply("POST", "/login/device/code", 200, code)
        attempt = (await call("POST", "/ui/api/sync/github/device", {})).json()["attempt"]
        now[0] += 901
        expired = await call("GET", f"/ui/api/sync/github/device?attempt={attempt}")
        assert expired.status_code == 409 and expired.json()["reason"] == "expired_token"
        no_secrets(call.texts)


# --- the sync task decides when git runs ------------------------------------------------------


@pytest.mark.asyncio
async def test_operations_that_run_git_wait_for_the_cycle_and_settings_do_not(tmp_path, account):
    syncer = UISyncer(tmp_path, account)
    async with ui(tmp_path, syncer) as (call, service):
        await settled(service, syncer, 1)
        syncer.release.clear()
        user_library.notify(syncer.library, ["common/a.md"])
        await until(syncer.running.is_set)  # a cycle runs
        checking = asyncio.create_task(call("POST", "/ui/api/sync/check", {}))
        paused = await call("PUT", "/ui/api/sync/settings", {"paused": True})
        assert paused.status_code == 200 and ("pause", {}) in syncer.calls  # at once, during the cycle
        await asyncio.sleep(.3)
        assert not checking.done() and ("check", {}) not in syncer.calls
        assert service.inflight == 0  # a waiting sync request does not hold a drain
        syncer.release.set()
        assert (await checking).status_code == 200 and ("check", {}) in syncer.calls
        assert service.user_sync.active is False  # the pause holds


@pytest.mark.asyncio
async def test_each_route_uses_the_queue_only_for_git_and_the_network(tmp_path, account):
    syncer = UISyncer(tmp_path, account, started=False)
    async with ui(tmp_path, syncer) as (call, service):
        decided = []
        perform = service.user_sync.perform

        async def recording(command, operation, *, queued):
            decided.append((command, queued))
            return await perform(command, operation, queued=queued)
        service.user_sync.perform = recording
        setup = {"github": REPOSITORY, "name": "Me", "email": "me@example.com", "label": "mac-0001"}
        for method, path, body in [
                ("POST", "/ui/api/sync/setup", setup),
                ("POST", "/ui/api/sync/setup", {"remote": "git@git.example.com:me/lib.git", "name": "Me",
                                                "email": "me@example.com", "trust_host_key": "SHA256:x"}),
                ("POST", "/ui/api/sync/check", {}), ("GET", "/ui/api/sync/preview", None),
                ("POST", "/ui/api/sync/start", {"confirm": "abc123", "confirm_private": True}),
                ("POST", "/ui/api/sync/run", {"confirm": "abc123"}), ("POST", "/ui/api/sync/key/regenerate", {}),
                ("PUT", "/ui/api/sync/settings", {"fetch_minutes": 10, "name": "New Me", "label": "desk"}),
                ("PUT", "/ui/api/sync/settings", {"paused": False}),
                ("PUT", "/ui/api/sync/scopes", {"exclude": ["history"]}),
                ("POST", "/ui/api/sync/conflicts/resolve", {"id": "20261005T120000000000Z-aaaaaaaaaa", "action": "keep"}),
                ("POST", "/ui/api/sync/disconnect", {})]:
            response = await call(method, path, body)
            assert response.status_code == 200, (path, response.text)
        assert decided == [("setup", True), ("setup", True), ("check", True), ("preview", True), ("start", True),
                           ("run", True), ("regenerate_key", True), ("configure", False), ("resume", False),
                           ("scopes", False), ("resolve", False), ("disconnect", True)]
        names = [name for name, _ in syncer.calls]
        assert names == ["setup_github", "setup", "check", "preview", "setup", "start", "regenerate_key", "configure",
                         "resume", "change_scopes", "resolve", "disconnect"]
        assert syncer.calls[0][1] == {"repository": REPOSITORY, "name": "Me", "email": "me@example.com",
                                      "label": "mac-0001"}
        assert syncer.calls[1][1]["trust_host_key"] == "SHA256:x"
        # Start with the owner's privacy confirmation records it with setup on the same remote, then starts.
        assert syncer.calls[4][1]["confirm_private"] is True and syncer.calls[5][1] == {"confirm": "abc123"}
        assert [run.confirm for run in syncer.runs if run.force] == ["abc123"]  # Sync now, with a confirmation
        assert syncer.calls[7][1] == {"fetch_minutes": 10, "name": "New Me", "label": "desk"}


# --- scopes, conflicts, machines --------------------------------------------------------------


@pytest.mark.asyncio
async def test_including_a_scope_again_needs_the_hash_of_its_upload_list(tmp_path, account):
    syncer = UISyncer(tmp_path, account, started=False)
    flows = FlowLibrary(FlowCatalog(), user_dir=syncer.library)
    flows.save("user:a", "# A\n")
    flows.save("user:a", "# A\n\nagain\n", expected_revision=flows.get("user:a")["flow"]["revision"])
    async with ui(tmp_path, syncer) as (call, _):
        groups = (await call("GET", "/ui/api/sync/scopes")).json()
        assert {group["group"]: group["syncs"] for group in groups["groups"]} == {
            "common": True, "personas": True, "history": True, "components": True}
        assert groups["ask_new_repositories"] is False
        excluded = (await call("PUT", "/ui/api/sync/scopes", {"exclude": ["history"]})).json()
        assert excluded["status"] == "saved" and {"group": "history", "syncs": False} in excluded["groups"]
        asked = (await call("PUT", "/ui/api/sync/scopes", {"include": ["history"]})).json()
        assert asked["status"] == "confirmation_needed" and len(asked["hash"]) == 64
        assert len(asked["upload"]) == 1 and asked["upload"][0].startswith(".history/common/a/")
        wrong = (await call("PUT", "/ui/api/sync/scopes", {"include": ["history"], "confirm": "0" * 64})).json()
        assert wrong == asked  # nothing changed
        assert "history" in json.loads((syncer.library / ".agents-sync" / "scopes.json").read_text())["exclude"]
        done = (await call("PUT", "/ui/api/sync/scopes", {"include": ["history"], "confirm": asked["hash"]})).json()
        assert done["status"] == "saved" and {"group": "history", "syncs": True} in done["groups"]
        assert json.loads((syncer.library / ".agents-sync" / "scopes.json").read_text())["exclude"] == []


@pytest.mark.asyncio
async def test_a_conflict_shows_its_texts_from_inside_the_library_only(tmp_path, account):
    syncer = UISyncer(tmp_path, account, started=False)
    library = syncer.library
    (library / "common").mkdir(parents=True)
    (library / "common" / "doc.md").write_text("# Current\n")
    version = ".history/common/doc/20261005T120000000000Z-aaaaaaaaaaaa.md"
    (library / version).parent.mkdir(parents=True)
    (library / version).write_text("# Mine\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("not the library's")
    (library / "common" / "link.md").symlink_to(outside)
    records = {"20261005T120000000000Z-aaaaaaaaaa": {"path": "common/doc.md", "flow": "user:doc", "kind": "both_changed",
                                                     "kept": "remote", "local_version": version},
               "20261005T120100000000Z-bbbbbbbbbb": {"path": "../outside.txt", "kind": "both_changed",
                                                     "local_content": "kept text"},
               "20261005T120200000000Z-cccccccccc": {"path": "common/link.md", "kind": "both_changed",
                                                     "local_version": "../outside.txt"}}
    (library / ".agents-sync" / "conflicts").mkdir(parents=True)
    for record_id, record in records.items():
        (library / ".agents-sync" / "conflicts" / f"{record_id}.json").write_text(json.dumps(record))
    async with ui(tmp_path, syncer) as (call, _):
        async def detail(record_id):
            return (await call("GET", f"/ui/api/sync/conflict?id={record_id}")).json()
        flow = await detail("20261005T120000000000Z-aaaaaaaaaa")
        assert (flow["current"], flow["mine"], flow["binary"]) == ("# Current\n", "# Mine\n", False)
        outside_path = await detail("20261005T120100000000Z-bbbbbbbbbb")
        assert (outside_path["current"], outside_path["mine"]) == (None, "kept text")
        linked = await detail("20261005T120200000000Z-cccccccccc")
        assert (linked["current"], linked["mine"]) == (None, None)
        unknown = await call("GET", "/ui/api/sync/conflict?id=20261005T120300000000Z-dddddddddd")
        assert unknown.status_code == 409 and unknown.json()["reason"] == "invalid"
        assert all("not the library's" not in text for text in call.texts)


@pytest.mark.asyncio
async def test_machines_are_the_deploy_keys_and_this_one_cannot_be_removed(fake, tmp_path, account):
    syncer = UISyncer(tmp_path, signed_in(fake, account))
    listed = [{"id": 11, "title": "Agents-Core mac-0001", "key": key_material(THIS_KEY), "read_only": False},
              {"id": 12, "title": "Agents-Core linux-9f9f", "key": key_material(OTHER_KEY), "read_only": False,
               "created_at": "2026-10-01T08:00:00Z"}]
    fake.reply("GET", KEYS_PATH, 200, listed, repeat=True)
    async with ui(tmp_path, syncer) as (call, _):
        machines = (await call("GET", "/ui/api/sync/machines")).json()
        assert machines["available"] and machines["repository"] == REPOSITORY and machines["label"] == "mac-0001"
        assert [(m["id"], m["label"], m["this"]) for m in machines["machines"]] == [(11, "mac-0001", True),
                                                                                (12, "linux-9f9f", False)]
        own = await call("POST", "/ui/api/sync/machines/remove", {"id": 11})
        assert own.status_code == 409 and own.json()["reason"] == "this_machine"
        missing = await call("POST", "/ui/api/sync/machines/remove", {"id": 99})
        assert missing.status_code == 409 and missing.json()["reason"] == "invalid"
        fake.reply("DELETE", f"{KEYS_PATH}/12", 204, None)
        removed = await call("POST", "/ui/api/sync/machines/remove", {"id": 12})
        assert removed.json() == {"status": "removed", "id": 12, "title": "Agents-Core linux-9f9f"}
        assert [r.path for r in fake.requests if r.method == "DELETE"] == [f"{KEYS_PATH}/12"]
        assert "authorization" in fake.requests[-1].headers  # the daemon called GitHub, with the token
        no_secrets(call.texts)


@pytest.mark.asyncio
async def test_machines_need_a_github_repository_and_an_account(tmp_path, account):
    syncer = UISyncer(tmp_path, account, started=False)
    async with ui(tmp_path, syncer) as (call, _):
        signed_out = (await call("GET", "/ui/api/sync/machines")).json()
        assert signed_out["available"] is False and "Sign in to GitHub" in signed_out["message"]
        syncer.config.remote = "git@git.example.com:me/lib.git"
        elsewhere = (await call("GET", "/ui/api/sync/machines")).json()
        assert elsewhere == {"available": False, "label": "mac-0001", "machines": [],
                             "message": "The machines are listed for a repository on GitHub."}
        assert (await call("POST", "/ui/api/sync/machines/remove", {"id": 12})).json()["reason"] == "not_signed_in"


@pytest.mark.asyncio
async def test_disconnect_removes_this_machines_deploy_key_only_after_it_disconnected(fake, tmp_path, account):
    syncer = UISyncer(tmp_path, signed_in(fake, account))
    fake.reply("GET", KEYS_PATH, 200, [{"id": 11, "title": "Agents-Core mac-0001", "key": key_material(THIS_KEY),
                                       "read_only": False}], repeat=True)
    async with ui(tmp_path, syncer) as (call, service):
        await settled(service, syncer, 1)
        syncer.errors["disconnect"] = SyncError("lock_held", "another sync of this library is running", state="busy")
        refused = await call("POST", "/ui/api/sync/disconnect", {})
        assert refused.status_code == 409 and refused.json()["reason"] == "lock_held"
        assert not [r for r in fake.requests if r.method == "DELETE"]  # GitHub stays as it was
        del syncer.errors["disconnect"]
        fake.reply("DELETE", f"{KEYS_PATH}/11", 204, None)
        done = (await call("POST", "/ui/api/sync/disconnect", {})).json()
        assert done["status"] == "off" and done["deploy_key"] == "removed"
        assert "Agents-Core mac-0001" in done["deploy_key_message"]
        assert [r.path for r in fake.requests if r.method == "DELETE"] == [f"{KEYS_PATH}/11"]
        no_secrets(call.texts)


@pytest.mark.asyncio
async def test_disconnect_reports_a_deploy_key_it_could_not_remove(fake, tmp_path, account):
    syncer = UISyncer(tmp_path, signed_in(fake, account))
    fake.reply("GET", KEYS_PATH, 502, None)
    async with ui(tmp_path, syncer) as (call, _):
        done = (await call("POST", "/ui/api/sync/disconnect", {})).json()
        assert done["status"] == "off" and done["deploy_key"] == "failed"
        assert "remove it on GitHub" in done["deploy_key_message"] and ("disconnect", {}) in syncer.calls
