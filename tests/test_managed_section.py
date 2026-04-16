"""Tests for the managed-section editor used by describe_repo and init_repo.sh."""

import os

import pytest

from src.memory.managed_section import (
    DuplicateMarkerError,
    InvertedMarkersError,
    PartialMarkerError,
    read_section,
    remove_section,
    upsert_section,
)

BEGIN = "# >>> TEST SECTION >>>"
END = "# <<< TEST SECTION <<<"


@pytest.fixture
def md_path(tmp_path):
    return str(tmp_path / "CLAUDE.md")


class TestUpsert:
    def test_creates_file_when_missing(self, md_path):
        action = upsert_section(md_path, BEGIN, END, "hello world")
        assert action == "created"
        with open(md_path) as f:
            content = f.read()
        assert BEGIN in content
        assert END in content
        assert "hello world" in content

    def test_appends_when_no_markers(self, md_path):
        with open(md_path, "w") as f:
            f.write("# Existing content\n\nLine two\n")
        action = upsert_section(md_path, BEGIN, END, "added")
        assert action == "appended"
        with open(md_path) as f:
            content = f.read()
        assert content.startswith("# Existing content")
        assert "added" in content
        # Existing content preserved verbatim
        assert "Line two" in content

    def test_replaces_existing_section(self, md_path):
        upsert_section(md_path, BEGIN, END, "first version")
        action = upsert_section(md_path, BEGIN, END, "second version")
        assert action == "replaced"
        body = read_section(md_path, BEGIN, END)
        assert body == "second version"
        # Exactly one begin and one end marker after replace
        with open(md_path) as f:
            content = f.read()
        assert content.count(BEGIN) == 1
        assert content.count(END) == 1

    def test_replace_preserves_outside_content(self, md_path):
        with open(md_path, "w") as f:
            f.write("# Routing protocol\n\nRouting body\n\n")
        upsert_section(md_path, BEGIN, END, "memory body v1")
        upsert_section(md_path, BEGIN, END, "memory body v2")
        with open(md_path) as f:
            content = f.read()
        assert "Routing body" in content
        assert "memory body v2" in content
        assert "memory body v1" not in content

    def test_partial_marker_raises(self, md_path):
        with open(md_path, "w") as f:
            f.write(f"some text\n{BEGIN}\nbody without end marker\n")
        with pytest.raises(PartialMarkerError):
            upsert_section(md_path, BEGIN, END, "new body")

    def test_duplicate_marker_raises(self, md_path):
        with open(md_path, "w") as f:
            f.write(f"{BEGIN}\nbody\n{END}\n{BEGIN}\nbody2\n{END}\n")
        with pytest.raises(DuplicateMarkerError):
            upsert_section(md_path, BEGIN, END, "new body")

    def test_inverted_markers_raise(self, md_path):
        with open(md_path, "w") as f:
            f.write(f"{END}\nbody\n{BEGIN}\n")
        with pytest.raises(InvertedMarkersError):
            upsert_section(md_path, BEGIN, END, "new body")

    def test_replace_does_not_grow_blank_lines(self, md_path):
        upsert_section(md_path, BEGIN, END, "body")
        for _ in range(5):
            upsert_section(md_path, BEGIN, END, "body")
        with open(md_path) as f:
            content = f.read()
        # No runs of 3+ consecutive newlines accumulated
        assert "\n\n\n\n" not in content

    def test_atomic_write_no_temp_left_behind(self, md_path):
        upsert_section(md_path, BEGIN, END, "body")
        files_in_dir = os.listdir(os.path.dirname(md_path))
        assert not any(f.startswith(".managed_section.") for f in files_in_dir)


class TestRead:
    def test_returns_none_when_missing_file(self, tmp_path):
        result = read_section(str(tmp_path / "nope.md"), BEGIN, END)
        assert result is None

    def test_returns_none_when_no_section(self, md_path):
        with open(md_path, "w") as f:
            f.write("just text\n")
        assert read_section(md_path, BEGIN, END) is None

    def test_roundtrip_preserves_body(self, md_path):
        body = "## Section\n\nSome **markdown** with *emphasis*."
        upsert_section(md_path, BEGIN, END, body)
        assert read_section(md_path, BEGIN, END) == body


class TestRemove:
    def test_removes_existing_section(self, md_path):
        with open(md_path, "w") as f:
            f.write("prefix line\n")
        upsert_section(md_path, BEGIN, END, "body")
        removed = remove_section(md_path, BEGIN, END)
        assert removed is True
        with open(md_path) as f:
            content = f.read()
        assert BEGIN not in content
        assert END not in content
        assert "prefix line" in content

    def test_returns_false_when_no_section(self, md_path):
        with open(md_path, "w") as f:
            f.write("plain content\n")
        assert remove_section(md_path, BEGIN, END) is False

    def test_returns_false_when_file_missing(self, tmp_path):
        assert remove_section(str(tmp_path / "nope.md"), BEGIN, END) is False
