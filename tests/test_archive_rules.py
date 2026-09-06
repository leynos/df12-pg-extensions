"""Tests for the archive layout rules shared with the consumer hook."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from archive_rules import (
    ALLOWED_PREFIXES,
    ArchiveError,
    classify_path,
    validate_archive,
    validate_members,
)
from conftest import FIXTURE_FILES, write_archive
from hypothesis import given
from hypothesis import strategies as st


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param("lib/vector.so", "lib/vector.so", id="lib_file"),
        pytest.param("./lib/vector.so", "lib/vector.so", id="dot_slash_prefix"),
        pytest.param(
            "share/extension/vector.control",
            "share/extension/vector.control",
            id="control",
        ),
        pytest.param(
            "share/extension/vector--0.8.6.sql",
            "share/extension/vector--0.8.6.sql",
            id="sql",
        ),
        pytest.param("lib/", None, id="bare_prefix"),
        pytest.param("lib/bitcode/vector.index.bc", None, id="nested_under_lib"),
        pytest.param("share/vector.control", None, id="share_root"),
        pytest.param("include/server/extension/vector/vector.h", None, id="headers"),
        pytest.param("bin/psql", None, id="bin"),
        pytest.param("lib/../bin/psql", None, id="parent_component"),
        pytest.param("/lib/vector.so", None, id="absolute"),
        pytest.param("lib//vector.so", None, id="empty_component"),
        pytest.param("lib\\vector.so", None, id="backslash"),
        pytest.param("libx/vector.so", None, id="prefix_lookalike"),
        pytest.param("", None, id="empty"),
    ],
)
def test_classify_path(path: str, expected: str | None) -> None:
    """Only plain files directly under lib/ or under share/extension/ are accepted."""
    assert classify_path(path) == expected


@given(st.text(min_size=0, max_size=40))
def test_classify_path_accepts_only_canonical_allowed_paths(path: str) -> None:
    """Whatever the input, an accepted path is canonical and under an allowed prefix."""
    accepted = classify_path(path)
    if accepted is None:
        return
    assert accepted.startswith(ALLOWED_PREFIXES)
    parts = accepted.split("/")
    assert all(part not in ("", ".", "..") for part in parts)
    assert "\\" not in accepted and not accepted.startswith("/")
    if accepted.startswith("lib/"):
        assert accepted.count("/") == 1


@given(
    st.lists(
        st.sampled_from(["lib", "share", "extension", "..", ".", "", "bin", "x"]),
        min_size=1,
        max_size=6,
    )
)
def test_classify_path_never_escapes(parts: list[str]) -> None:
    """A path containing .. or an empty component is never accepted."""
    path = "/".join(parts)
    if any(part in ("..", "") for part in parts):
        assert classify_path(path) is None


def _member(name: str, kind: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    return info


def test_validate_members_returns_sorted_files() -> None:
    """Directories are tolerated and the regular files come back sorted."""
    members = [
        _member("share", tarfile.DIRTYPE),
        _member("share/extension", tarfile.DIRTYPE),
        _member("share/extension/vector.control"),
        _member("lib", tarfile.DIRTYPE),
        _member("lib/vector.so"),
    ]
    assert validate_members(members).files == (
        "lib/vector.so",
        "share/extension/vector.control",
    )


@pytest.mark.parametrize(
    ("members", "message"),
    [
        pytest.param(
            [
                _member("lib/vector.so", tarfile.SYMTYPE),
                _member("share/extension/v.control"),
            ],
            "regular files",
            id="symlink",
        ),
        pytest.param(
            [
                _member("lib/vector.so", tarfile.LNKTYPE),
                _member("share/extension/v.control"),
            ],
            "regular files",
            id="hardlink",
        ),
        pytest.param(
            [
                _member("lib/vector.so", tarfile.CHRTYPE),
                _member("share/extension/v.control"),
            ],
            "regular files",
            id="device",
        ),
        pytest.param(
            [_member("bin/psql"), _member("share/extension/v.control")],
            "outside",
            id="foreign_prefix",
        ),
        pytest.param(
            [_member("lib/../lib/vector.so"), _member("share/extension/v.control")],
            "outside",
            id="escape",
        ),
        pytest.param(
            [_member("bin", tarfile.DIRTYPE), _member("share/extension/v.control")],
            "directory outside",
            id="foreign_directory",
        ),
        pytest.param(
            [
                _member("lib/vector.so"),
                _member("./lib/vector.so"),
                _member("share/extension/v.control"),
            ],
            "duplicate",
            id="duplicate",
        ),
        pytest.param([_member("lib/vector.so")], "control", id="no_control_file"),
        pytest.param([], "no files", id="empty"),
    ],
)
def test_validate_members_rejects(members: list[tarfile.TarInfo], message: str) -> None:
    """Each forbidden member type or path fails with a descriptive error."""
    with pytest.raises(ArchiveError, match=message):
        validate_members(members)


def test_validate_archive_reads_gzip_tar(tmp_path: Path) -> None:
    """A real gzip tar with the fixture layout validates and lists its files."""
    archive = tmp_path / "fixture.tar.gz"
    write_archive(archive, FIXTURE_FILES.items(), sidecar=False)
    assert validate_archive(archive).files == tuple(sorted(FIXTURE_FILES))


def test_validate_archive_rejects_non_tar(tmp_path: Path) -> None:
    """Random bytes are reported as unreadable rather than crashing."""
    archive = tmp_path / "junk.tar.gz"
    archive.write_bytes(b"not a tar")
    with pytest.raises(ArchiveError, match="not a readable gzip tar"):
        validate_archive(archive)
