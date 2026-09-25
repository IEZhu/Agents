from src.daemon.state import atomic_private


def test_atomic_private_replaces_content_without_leftovers(tmp_path):
    # Windows used to raise PermissionError here: it cannot open the parent
    # directory to fsync it, which broke every stdio server start.
    target = tmp_path / "state" / "marker"
    atomic_private(target, "first")
    atomic_private(target, b"second")
    assert target.read_bytes() == b"second"
    assert [path.name for path in target.parent.iterdir()] == ["marker"]
