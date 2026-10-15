"""Physical checkout authorization and aliases, using isolated local Git repos."""
from pathlib import Path
import subprocess

import pytest

from src.cooperation.identity import IdentityError, normalize_resource, resolve_identity
from src.daemon.workspaces import ClientContext, WorkspaceError, WorkspaceRegistry


def git(root, *arguments):
    return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkouts(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "-c", "user.name=Test", "-c", "user.email=test@example.test",
        "commit", "--allow-empty", "-m", "base", "-q")
    external = tmp_path / "outside" / "linked"
    git(main, "worktree", "add", "--detach", str(external))
    authority = tmp_path / "service"
    authority.mkdir()
    return main, external, authority


def context(root, *physical):
    return ClientContext("test", "http", "workspace", root,
                         cooperation_roots=tuple(physical or (root,)))


def test_linked_checkout_requires_explicit_grant_and_preserves_memory(checkouts):
    main, external, authority = checkouts
    client = context(main)
    with pytest.raises(IdentityError, match="outside"):
        resolve_identity(client, str(external), authority_path=authority)
    granted = context(main, external)
    identity = resolve_identity(granted, str(external), authority_path=authority)
    original = resolve_identity(client, str(main), authority_path=authority)
    assert identity.repository_id == original.repository_id
    assert identity.checkout_id != original.checkout_id
    assert identity.checkout_path == str(external.resolve())
    assert identity.base_commit == git(main, "rev-parse", "HEAD")
    assert identity.branch is None
    with pytest.raises(WorkspaceError):
        granted.workspace_target(str(external))
    with pytest.raises(IdentityError):
        resolve_identity(granted, str(main), authority_path=authority)


def test_aliases_and_installations_are_distinct_domains(checkouts):
    main, external, authority = checkouts
    alias = authority.parent / "alias"
    alias.symlink_to(main, target_is_directory=True)
    original = resolve_identity(context(main), str(main), authority_path=authority)
    aliased = resolve_identity(context(main), str(alias), authority_path=authority)
    assert aliased.repository_id == original.repository_id
    assert aliased.checkout_id == original.checkout_id
    second = authority.parent / "second-service"
    second.mkdir()
    assert resolve_identity(context(main), str(main), authority_path=second).repository_id != original.repository_id


def test_cross_repo_and_git_environment_do_not_grant_access(checkouts, monkeypatch):
    main, external, authority = checkouts
    other = authority.parent / "other"
    other.mkdir()
    git(other, "init", "-q")
    first = resolve_identity(context(main), authority_path=authority)
    second = resolve_identity(context(other), authority_path=authority)
    with pytest.raises(IdentityError) as error:
        resolve_identity(context(main), authority_path=authority, repository_id=second.repository_id)
    assert error.value.code == "unauthorized_actor"
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert resolve_identity(context(main), authority_path=authority).repository_id == first.repository_id


def test_resource_normalization_aliases_escape_and_nested_repository(checkouts):
    main, external, authority = checkouts
    (main / "src").mkdir()
    (main / "src" / "file.py").write_text("value = 1\n")
    (main / "alias").symlink_to(main / "src", target_is_directory=True)
    (main / "escape").symlink_to(external, target_is_directory=True)
    identity = resolve_identity(context(main), authority_path=authority)
    resource = {"resource_type": "physical_file", "resource_key": "src/file.py", "access": "write"}
    canonical = normalize_resource(resource, identity)
    assert canonical.resource_key == identity.checkout_id + ":src/file.py"
    assert normalize_resource({**resource, "resource_key": "alias/file.py"}, identity) == canonical
    assert normalize_resource(canonical, identity) == canonical
    assert normalize_resource({**resource, "resource_key": "checkout_helper.py"}, identity).resource_key.endswith(":checkout_helper.py")
    for key in ("../outside", "src/../file.py", "/absolute", "escape/file.py", "checkout_other:file.py"):
        with pytest.raises(IdentityError):
            normalize_resource({**resource, "resource_key": key}, identity)
    nested = main / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    with pytest.raises(IdentityError, match="nested"):
        normalize_resource({**resource, "resource_key": "nested/new.py"}, identity)
    assert resolve_identity(context(main), str(nested), authority_path=authority).repository_id != identity.repository_id


def test_case_aliases_collide_without_collapsing_distinct_files(checkouts):
    main, _, authority = checkouts
    (main / "Mixed.txt").write_text("first")
    identity = resolve_identity(context(main), authority_path=authority)
    resource = {"resource_type": "physical_file", "resource_key": "Mixed.txt", "access": "write"}
    first = normalize_resource(resource, identity)
    if not (main / "mixed.txt").exists():
        (main / "mixed.txt").write_text("second")
        assert normalize_resource({**resource, "resource_key": "mixed.txt"}, identity) != first
    else:
        assert normalize_resource({**resource, "resource_key": "mixed.txt"}, identity) == first


def test_new_file_case_aliases_remain_stable_after_creation(checkouts):
    main, _, authority = checkouts
    identity = resolve_identity(context(main), authority_path=authority)
    resource = {"resource_type": "physical_file", "resource_key": "Future.txt", "access": "write"}
    before = normalize_resource(resource, identity)
    other_case = normalize_resource({**resource, "resource_key": "future.txt"}, identity)
    (main / "Future.txt").write_text("new")
    assert normalize_resource(resource, identity) == before
    if (main / "future.txt").exists():
        assert other_case == before
    else:
        assert other_case != before


@pytest.mark.parametrize("sensitive", [True, False])
def test_new_names_obey_verified_filesystem_sensitivity(checkouts, monkeypatch, sensitive):
    from src.cooperation import identity as identities
    main, _, authority = checkouts
    identity = resolve_identity(context(main), authority_path=authority)
    monkeypatch.setattr(identities, "_case_sensitive", lambda directory: sensitive)
    resource = {"resource_type": "physical_file", "resource_key": "Future.py", "access": "write"}
    first = normalize_resource(resource, identity)
    second = normalize_resource({**resource, "resource_key": "future.py"}, identity)
    assert (first == second) is not sensitive


def test_registry_retains_physical_grants_separately(checkouts):
    main, external, authority = checkouts
    registry = WorkspaceRegistry(authority)
    identity = registry.register(main, physical_root=external)
    assert registry.resolve(identity) == main
    assert registry.cooperation_roots(identity) == (external,)
    assert registry.register(main, physical_root=external) == identity
    assert registry.cooperation_roots(identity) == (external,)
    # A subsequently owner-registered grant is explicit; nothing derives all
    # worktrees merely from the shared Git common directory.
    registry.register(main)
    assert set(registry.cooperation_roots(identity)) == {main, external}


def test_removed_worktree_does_not_revoke_remaining_grants(checkouts):
    main, external, authority = checkouts
    registry = WorkspaceRegistry(authority)
    identity = registry.register(main)
    registry.register(main, physical_root=external)
    git(main, "worktree", "remove", str(external))
    assert registry.cooperation_roots(identity) == (main,)
    registry.register(main)
    from src.daemon.state import read_json
    assert read_json(registry.checkout_path)[identity] == [str(main)]


def test_removed_grants_do_not_exhaust_registration_limit(checkouts):
    main, _, authority = checkouts
    registry = WorkspaceRegistry(authority)
    identity = registry.register(main)
    old = []
    for number in range(49):
        checkout = authority / str(number)
        checkout.mkdir()
        registry.register(main, physical_root=checkout)
        old.append(checkout)
    for checkout in old:
        checkout.rmdir()
    new = authority / "new"
    new.mkdir()
    assert registry.register(main, physical_root=new) == identity
    assert set(registry.cooperation_roots(identity)) == {main, new}


def test_retargeted_grant_still_fails_closed(checkouts):
    main, external, authority = checkouts
    registry = WorkspaceRegistry(authority)
    identity = registry.register(main)
    registry.register(main, physical_root=external)
    git(main, "worktree", "remove", str(external))
    external.symlink_to(main, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="workspace_invalid"):
        registry.cooperation_roots(identity)
    with pytest.raises(WorkspaceError, match="workspace_invalid"):
        registry.register(main)
