"""Local authority integration: real P-256 keys, temporary state, no models."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
import stat

from cryptography.hazmat.primitives.asymmetric import ec
import pytest

from src.instruction_signing import Status, build_seal, fingerprint, public_key_pem, verify_seal
from src.instruction_signing.authority import AuthorityError, AuthorityStore
from src.instruction_signing import storage
from src.instruction_signing.__main__ import main


@pytest.fixture
def enrolled(tmp_path):
    base = tmp_path.resolve()
    installation, library = base / "installation", base / "library"
    installation.mkdir()
    library.mkdir()
    store = AuthorityStore(base / "state", installation)
    initial = store.initialize(label="owner-selected label", library_root=library)
    return store, installation, library, initial


def _seal(key, *, kind="library", files=None):
    return build_seal(files or {"common/flow.md": b"instructions"}, key, kind=kind,
                      root_name="display name", origin="cli" if kind == "library" else "release",
                      signer="untrusted display label", signed_at="2026-10-18T00:00:00Z")


def _add(store, kind="release"):
    key = ec.generate_private_key(ec.SECP256R1())
    store.add_key(public_key_pem(key.public_key()), kind=kind, label="owner label",
                  expected_token=store.snapshot().token)
    return key


def _inventory(extra=True):
    paths = ["integrity/protected-files.json", "src/runtime.py"]
    if extra:
        paths.append("src/extra.py")
    return {"schema": "agents-core/protected-files/1", "protected_roots": ["src"],
            "files": [{"path": p, "class": "configuration" if p.startswith("integrity/") else "runtime",
                       "type": "regular", "executable": False} for p in sorted(paths)],
            "exclusions": [], "dependencies": {"environment": {"boundary": "external", "identity": "a" * 64},
                "artifacts": [{"path": "runtime.whl", "sha256": "b" * 64}]}}


def _policy(store, key, *, extra=True, additional_paths=("local/extra",), mode="enforce", expected_token=None):
    inventory = json.dumps(_inventory(extra)).encode()
    files = {f["path"]: b"runtime" for f in _inventory(extra)["files"]}
    files["integrity/protected-files.json"] = inventory
    return store.set_policy(inventory_bytes=inventory, release_seal=_seal(key, kind="release", files=files),
                            additional_paths=additional_paths, mode=mode,
                            expected_token=expected_token or store.snapshot().token)


def test_explicit_enrollment_is_private_idempotent_only_by_read_and_never_uses_hostname(enrolled, monkeypatch):
    store, _, library, snapshot = enrolled
    assert snapshot.valid and snapshot.revision == 0 and snapshot.authority_id
    assert snapshot.entries[0].kind == "library"
    assert snapshot.entries[0].label == "owner-selected label"
    assert snapshot.entries[0].roots == (os.path.normcase(str(library)),)
    original = (store.directory / "machine.key").read_bytes()
    assert isinstance(store.machine_key(expected_token=snapshot.token).curve, ec.SECP256R1)
    assert AuthorityStore(store.directory.parent, store.installation).snapshot() == snapshot
    with pytest.raises(AuthorityError, match="refusing reset"):
        store.initialize(label="replacement", library_root=library)
    assert (store.directory / "machine.key").read_bytes() == original
    if os.name != "nt":
        for path in store.directory.rglob("*"):
            assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700


def test_kind_and_exact_root_are_authority_not_signer_metadata(enrolled, tmp_path):
    store, installation, library, snapshot = enrolled
    machine = store.machine_key(expected_token=snapshot.token)
    release = _add(store)
    for key, kind, root in ((machine, "library", library), (release, "release", installation)):
        result = store.verify(_seal(key, kind=kind), kind=kind, root=root)
        assert result.status == Status.VALID and result.authorization_checked
    other = tmp_path / "copy"
    other.mkdir()
    assert store.verify(_seal(machine), kind="library", root=other).status == Status.UNTRUSTED
    assert store.verify(_seal(release, kind="release"), kind="release", root=library).status == Status.UNTRUSTED
    assert store.verify(_seal(release), kind="library", root=library).status == Status.UNTRUSTED
    assert store.verify(_seal(machine), kind="library", root=installation).status == Status.UNTRUSTED
    # Valid cryptography cannot elevate a library signer into release authority.
    for path in ("src/runtime.py", "integrity/protected-files.json", "policy.json"):
        seal = _seal(machine, kind="release", files={path: b"candidate"})
        raw = verify_seal(seal, {fingerprint(machine.public_key()): public_key_pem(machine.public_key())}, expected_kind="release")
        assert raw.status == Status.VALID and raw.signature_verified
        denied = store.verify(seal, kind="release", root=installation, expected_path=path)
        assert denied.status == Status.UNTRUSTED and not denied.authorization_checked
    with pytest.raises(AuthorityError, match="cannot acquire release"):
        store.add_key(public_key_pem(machine.public_key()), kind="release", label="release", expected_token=store.snapshot().token)


def test_revocation_changes_token_across_store_instances_and_blocks_stale_handoff(enrolled):
    store, installation, library, snapshot = enrolled
    key = store.machine_key(expected_token=snapshot.token)
    sealed = _seal(key)
    other = AuthorityStore(store.directory.parent, installation)
    current = store.remove_key(snapshot.entries[0].fingerprint, expected_token=snapshot.token)
    assert current.revision == snapshot.revision + 1 and current.token != snapshot.token
    assert other.verify(sealed, kind="library", root=library).status == Status.UNTRUSTED
    assert other.verify(sealed, kind="library", root=library, expected_token=snapshot.token).reason == "authority_revision_changed"
    with pytest.raises(AuthorityError, match="changed"):
        other.machine_key(expected_token=snapshot.token)
    with pytest.raises(AuthorityError, match="revoked"):
        other.machine_key(expected_token=current.token)
    with pytest.raises(AuthorityError, match="changed"):
        _add_with_token(other, snapshot.token)


def _add_with_token(store, token):
    key = ec.generate_private_key(ec.SECP256R1())
    return store.add_key(public_key_pem(key.public_key()), kind="release", label="key", expected_token=token)


@pytest.mark.parametrize("target", ["accepted.json", "authority.key", "revisions/00000000.json"])
@pytest.mark.parametrize("damage", ["invalid", "missing"])
def test_corrupt_or_missing_state_grants_no_trust_and_never_resets(enrolled, target, damage):
    store, installation, library, snapshot = enrolled
    key = store.machine_key(expected_token=snapshot.token)
    private = (store.directory / "machine.key").read_bytes()
    path = store.directory / target
    if damage == "invalid":
        path.write_bytes(b"{}")
    else:
        path.unlink()
    broken = store.snapshot()
    assert not broken.valid and broken.entries == () and broken.token is None and broken.diagnostic
    assert store.verify(_seal(key), kind="library", root=library).status == Status.UNTRUSTED
    with pytest.raises((AuthorityError, OSError, KeyError)):
        _add(store)
    with pytest.raises(AuthorityError, match="refusing reset"):
        store.initialize(label="new label", library_root=library)
    assert (store.directory / "machine.key").read_bytes() == private


@pytest.mark.parametrize("field", ["fingerprint", "kind", "roots", "source"])
def test_edited_metadata_and_forged_policy_fail_authentication(enrolled, field):
    store, _, _, _ = enrolled
    path = store.directory / "revisions/00000000.json"
    value = json.loads(path.read_bytes())
    value["state"]["entries"][0][field] = [store.installation] if field == "roots" else "release"
    path.write_text(json.dumps(value))
    assert not store.snapshot().valid


def test_head_rollback_missing_middle_and_previous_digest_edits_are_refused(enrolled):
    store, _, _, initial = enrolled
    old_head = (store.directory / "accepted.json").read_bytes()
    _add(store)
    current = (store.directory / "accepted.json").read_bytes()
    (store.directory / "accepted.json").write_bytes(old_head)
    assert not store.snapshot().valid
    (store.directory / "accepted.json").write_bytes(current)
    assert store.snapshot().valid
    record = store.directory / "revisions/00000001.json"
    value = json.loads(record.read_bytes())
    value["previous"] = "0" * 64
    record.write_text(json.dumps(value))
    assert not store.snapshot().valid
    record.unlink()
    assert not store.snapshot().valid


def test_policy_minimum_requires_current_release_authority_and_explicit_transition(enrolled):
    store, _, _, initial = enrolled
    machine = store.machine_key(expected_token=initial.token)
    with pytest.raises(AuthorityError, match="pinned release"):
        _policy(store, machine)
    release = _add(store)
    accepted = _policy(store, release)
    policy = store.policy(expected_token=accepted.token)
    assert policy["additional_paths"] == ["local/extra"]
    assert {f["path"] for f in policy["inventory"]["files"]} >= {"src/runtime.py", "src/extra.py"}
    assert accepted.policy_digest == hashlib.sha256(accepted.policy_bytes).hexdigest()
    # Caller cannot remove mandatory paths through the optional selection list.
    for mode in ("warn", "enforce"):
        changed = _policy(store, release, additional_paths=(), mode=mode)
        assert changed.token != accepted.token
        assert "src/extra.py" in {f["path"] for f in store.policy()["inventory"]["files"]}
        assert {e.kind for e in changed.entries} == {"release", "library"}
        assert store.verify(_seal(machine, kind="release"), kind="release", root=store.installation).status == Status.UNTRUSTED
    # A newly signed inventory plus an owner transition can change the minimum.
    with pytest.raises(AuthorityError, match="Only enforce"):
        _policy(store, release, mode="off")
    reduced = _policy(store, release, extra=False, additional_paths=())
    assert "src/extra.py" not in {f["path"] for f in store.policy()["inventory"]["files"]}
    with pytest.raises(AuthorityError, match="changed"):
        _policy(store, release, expected_token=accepted.token)
    store.remove_key(fingerprint(release.public_key()), expected_token=reduced.token)
    with pytest.raises(AuthorityError, match="revoked"):
        store.policy()


def test_unsigned_inventory_edits_and_candidate_configuration_cannot_weaken_policy(enrolled):
    store, installation, _, _ = enrolled
    release = _add(store)
    before = _policy(store, release)
    (installation / "integrity.json").write_text('{"mode":"off","entries":[],"policy":{}}')
    assert store.snapshot() == before and store.policy()["mode"] == "enforce"
    raw = json.dumps(_inventory()).encode()
    seal = _seal(release, kind="release", files={"integrity/protected-files.json": raw})
    for candidate in (json.dumps(_inventory(False)).encode(), raw):
        with pytest.raises(AuthorityError):
            store.set_policy(inventory_bytes=candidate, release_seal=seal, additional_paths=(), mode="off", expected_token=before.token)
    assert store.snapshot() == before


def test_atomic_failure_before_or_after_record_never_authorizes_partial_transition(enrolled, monkeypatch):
    store, _, _, initial = enrolled
    write = storage.write

    def fail_head(path, data):
        if path.name == "accepted.json":
            raise OSError("injected head failure")
        write(path, data)

    monkeypatch.setattr(storage, "write", fail_head)
    with pytest.raises(OSError, match="injected"):
        _add(store)
    assert not store.snapshot().valid
    assert (store.directory / "revisions/00000000.json").exists()
    assert (store.directory / "revisions/00000001.json").exists()


def test_concurrent_owners_cannot_both_commit_the_same_prior_token(enrolled):
    store, _, _, snapshot = enrolled

    def mutate(_):
        try:
            _add_with_token(store, snapshot.token)
            return True
        except (AuthorityError, BlockingIOError):
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(mutate, range(2)))
    assert sorted(results) == [False, True]
    assert store.snapshot().revision == snapshot.revision + 1


def test_no_cross_process_lock_fallback_is_accepted(enrolled, monkeypatch):
    store, _, _, _ = enrolled

    @contextmanager
    def fallback(*args, **kwargs):
        yield None

    monkeypatch.setattr(storage, "file_lock", fallback)
    assert not store.snapshot().valid


@pytest.mark.parametrize("damage", ["corrupt", "missing", "other-key", "public-mismatch"])
def test_machine_key_failure_never_generates_or_enrolls_replacement(enrolled, damage):
    store, _, _, snapshot = enrolled
    private = store.directory / "machine.key"
    if damage == "corrupt":
        private.write_bytes(b"not a key")
    elif damage == "missing":
        private.unlink()
    elif damage == "other-key":
        from cryptography.hazmat.primitives import serialization
        key = ec.generate_private_key(ec.SECP256R1())
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    else:
        (store.directory / "machine.pub").write_bytes(b"other public key")
    before = private.read_bytes() if private.exists() else None
    with pytest.raises((AuthorityError, ValueError, OSError)):
        store.machine_key(expected_token=snapshot.token)
    assert (private.read_bytes() if private.exists() else None) == before
    assert store.snapshot() == snapshot


def test_policy_preview_authenticates_inputs_and_records_exact_prior_and_new_digest(enrolled):
    store, _, _, _ = enrolled
    key = _add(store)
    snapshot = store.snapshot()
    raw = json.dumps(_inventory()).encode()
    files = {f["path"]: b"runtime" for f in _inventory()["files"]}
    files["integrity/protected-files.json"] = raw
    args = dict(inventory_bytes=raw, release_seal=_seal(key, kind="release", files=files),
                additional_paths=("local/extra",), mode="enforce", expected_token=snapshot.token)
    preview = store.preview_policy(**args)
    assert preview["previous_policy"] is None and store.snapshot() == snapshot
    accepted = store.set_policy(**args)
    assert accepted.policy_digest == preview["digest"]
    record = json.loads((store.directory / "revisions/00000002.json").read_bytes())
    assert record["previous"] == snapshot.digest
    assert record["policy_digest"] == preview["digest"]


def test_history_capacity_refusal_changes_no_accepted_state(enrolled, monkeypatch):
    from src.instruction_signing import authority
    store, _, _, snapshot = enrolled
    monkeypatch.setattr(authority, "_MAX_REVISIONS", 1)
    with pytest.raises(AuthorityError, match="capacity"):
        _add(store)
    assert store.snapshot() == snapshot


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode contract")
@pytest.mark.parametrize("target", ["machine.key", "authority.key", "accepted.json", "revisions"])
def test_loose_permissions_are_refused_without_silent_repair(enrolled, target):
    store, _, _, snapshot = enrolled
    path = store.directory / target
    path.chmod(0o755 if path.is_dir() else 0o644)
    if target == "machine.key":
        with pytest.raises(AuthorityError):
            store.machine_key(expected_token=snapshot.token)
    else:
        assert not store.snapshot().valid
    assert stat.S_IMODE(path.stat().st_mode) in (0o644, 0o755)


@pytest.mark.skipif(os.name == "nt", reason="Native link privileges differ; Windows ACL cases run separately")
@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_authority_file_links_are_refused(enrolled, tmp_path, link):
    store, _, _, _ = enrolled
    path = store.directory / "accepted.json"
    outside = tmp_path / "outside"
    outside.write_bytes(path.read_bytes())
    outside.chmod(0o600)
    path.unlink()
    if link == "symlink":
        path.symlink_to(outside)
    else:
        os.link(outside, path)
    assert not store.snapshot().valid


@pytest.mark.skipif(os.name != "nt", reason="Requires real Windows DACLs")
def test_native_windows_user_only_acl_on_every_authority_file(enrolled):
    from src.daemon import acl
    store, _, _, _ = enrolled
    _add(store)
    for path in [store.directory, *store.directory.rglob("*")]:
        assert acl.is_private(path, inherited_below=path.is_dir())
        assert acl.owned_by_user_or_admins(path)


@pytest.mark.skipif(os.name != "nt", reason="Requires real Windows DACLs")
@pytest.mark.parametrize("unrestricted", ["everyone", "null-dacl"])
def test_native_windows_other_user_or_null_acl_is_refused(enrolled, unrestricted):
    import ctypes
    from ctypes import wintypes
    from src.daemon import acl
    store, _, _, _ = enrolled
    path = store.directory / "accepted.json"
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    try:
        if unrestricted == "everyone":
            acl._check(acl._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                f"D:P(A;;FA;;;{acl.user_sid()})(A;;FR;;;WD)", 1, ctypes.byref(descriptor), None))
            present, defaulted = wintypes.BOOL(), wintypes.BOOL()
            acl._check(acl._advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                                                            ctypes.byref(dacl), ctypes.byref(defaulted)))
        error = acl._advapi32.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
        assert not acl.is_private(path)
        assert not store.snapshot().valid
    finally:
        acl.restrict(path)
        if descriptor.value:
            acl._kernel32.LocalFree(descriptor)


def test_cli_add_prints_canonical_fingerprint_and_scope_decline_does_not_mutate(enrolled, tmp_path, monkeypatch, capsys):
    store, installation, library, snapshot = enrolled
    key = ec.generate_private_key(ec.SECP256R1())
    pem = tmp_path / "candidate.pub"
    pem.write_bytes(public_key_pem(key.public_key()))
    argv = ["--state-dir", str(store.directory.parent), "--installation", str(installation)]
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert main([*argv, "trust", "add", str(pem), "--release", "--label", "release label"]) == 1
    shown = capsys.readouterr().out
    assert f"Fingerprint: {fingerprint(key.public_key())}\n" in shown
    assert f"Root: {snapshot.installation_root}\n" in shown
    assert "Kind: release\n" in shown
    assert store.snapshot() == snapshot
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert main([*argv, "trust", "add", str(pem), "--release", "--label", "release label"]) == 0
    assert main([*argv, "trust", "list"]) == 0
    assert "release label" in capsys.readouterr().out
    assert main([*argv, "trust", "remove", fingerprint(key.public_key())]) == 0
    assert len(store.snapshot().entries) == 1


def test_cli_eof_and_corrupt_store_fail_without_reset(enrolled, tmp_path, monkeypatch, capsys):
    store, installation, _, snapshot = enrolled
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "key.pem"
    path.write_bytes(public_key_pem(key.public_key()))
    argv = ["--state-dir", str(store.directory.parent), "--installation", str(installation)]

    def eof(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert main([*argv, "trust", "add", str(path), "--label", "label"]) == 1
    assert store.snapshot() == snapshot
    (store.directory / "accepted.json").write_text("{}")
    assert main([*argv, "trust", "list"]) == 2
    assert "Authority unavailable" in capsys.readouterr().err


def test_state_cannot_be_inside_candidate_or_library(tmp_path):
    installation = tmp_path.resolve() / "installation"
    installation.mkdir()
    with pytest.raises(AuthorityError):
        AuthorityStore(installation / "state", installation)
    store = AuthorityStore(tmp_path.resolve() / "state", installation)
    with pytest.raises(AuthorityError):
        store.initialize(label="label", library_root=tmp_path.resolve())
