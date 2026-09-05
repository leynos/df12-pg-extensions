"""Shared fixtures: fixture archives and a configuration matching them."""

from __future__ import annotations

import hashlib
import io
import tarfile
from collections.abc import Iterable
from pathlib import Path

import pytest
from pgx_config import Config, Extension, parse_config

FIXTURE_IMAGE = (
    "docker.io/library/almalinux:9"
    "@sha256:3a3fa7f043b142bc8008c8b308d39b47d2c84008addcd52f9f9a7a82d2a90474"
)

FIXTURE_CONFIG = """
schema_version = 1

[postgresql]
versions = ["17.11.0", "18.6.0"]
releases_url = "https://github.com/theseus-rs/postgresql-binaries/releases/download"

[targets."x86_64-unknown-linux-gnu"]
runner = "ubuntu-24.04"
platform = "linux/amd64"

[targets."aarch64-unknown-linux-gnu"]
runner = "ubuntu-24.04-arm"
platform = "linux/arm64"

[build]
image = "FIXTURE_IMAGE"
max_glibc = "2.34"

[smoke]
package = "pgvector"
postgresql_major = 17
target = "x86_64-unknown-linux-gnu"

[[extensions]]
name = "vector"
package = "pgvector"
version = "0.8.6"
repository = "https://github.com/pgvector/pgvector"
tag = "v0.8.6"
commit = "8ee86c96f0fd72390f890aa8a336fda6d3ab4c6c"
smoke_sql = "SELECT '[1,2,3]'::vector <-> '[4,5,6]'::vector"
""".replace("FIXTURE_IMAGE", FIXTURE_IMAGE)

FIXTURE_FILES: dict[str, bytes] = {
    "lib/vector.so": b"\x7fELF-not-really",
    "share/extension/vector.control": (
        b"default_version = '0.8.6'\nmodule_pathname = '$libdir/vector'\n"
    ),
    "share/extension/vector--0.8.6.sql": b"CREATE TYPE vector;\n",
}


def make_tar_bytes(entries: Iterable[tuple[str, bytes | None]]) -> bytes:
    """Build a gzip tar in memory; a ``None`` payload creates a directory entry."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            if payload is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            else:
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def write_archive(
    path: Path, entries: Iterable[tuple[str, bytes | None]], *, sidecar: bool = True
) -> str:
    """Write a fixture archive (and by default its sidecar); return its digest."""
    payload = make_tar_bytes(entries)
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    if sidecar:
        path.with_name(path.name + ".sha256").write_text(f"{digest}  {path.name}\n")
    return digest


def first_leg(config: Config) -> tuple[Extension, str, str]:
    """Return the first configured (extension, PostgreSQL version, target)."""
    return (
        config.extensions[0],
        config.postgresql_versions[0],
        config.target_triples[0],
    )


def last_leg(config: Config) -> tuple[Extension, str, str]:
    """Return the last configured (extension, PostgreSQL version, target)."""
    return (
        config.extensions[-1],
        config.postgresql_versions[-1],
        config.target_triples[-1],
    )


@pytest.fixture
def fixture_config() -> Config:
    """Return a small validated configuration with two versions and two targets."""
    return parse_config(FIXTURE_CONFIG)


@pytest.fixture
def full_dist(tmp_path: Path, fixture_config: Config) -> Path:
    """Return a dist directory holding every archive the fixture expects."""
    dist = tmp_path / "dist"
    dist.mkdir()
    extension = fixture_config.extensions[0]
    for pg_version in fixture_config.postgresql_versions:
        for target in fixture_config.target_triples:
            name = fixture_config.archive_name(extension, pg_version, target)
            write_archive(dist / name, FIXTURE_FILES.items())
    return dist
