"""Tests for manifest building and verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from build_manifest import (
    MANIFEST_NAME,
    ManifestError,
    build_manifest,
    collect_extensions,
    main,
    read_sidecar,
    sha256_of,
    verify_manifest,
    write_sidecar,
)
from conftest import FIXTURE_CONFIG, FIXTURE_FILES, first_leg, last_leg, write_archive
from pgx_config import Config

TAG = "v1.0.0"
REPOSITORY = "leynos/df12-pg-extensions"
GENERATED_AT = "2026-09-05T12:00:00+00:00"


def test_build_manifest_describes_every_leg(
    fixture_config: Config, full_dist: Path
) -> None:
    """One artifact per (version, target) with digest, size, URL and file list."""
    manifest = build_manifest(fixture_config, full_dist, TAG, REPOSITORY, GENERATED_AT)
    assert manifest["schema_version"] == 1, "schema version"
    assert manifest["release"] == TAG, "release tag"
    assert manifest["generated_at"] == GENERATED_AT, "caller-supplied timestamp"
    (extension,) = manifest["extensions"]
    assert extension["name"] == "vector"
    assert extension["source"] == {
        "repository": "https://github.com/pgvector/pgvector",
        "tag": "v0.8.6",
        "commit": "8ee86c96f0fd72390f890aa8a336fda6d3ab4c6c",
    }
    legs = {(a["postgresql"], a["target"]) for a in extension["artifacts"]}
    assert legs == {
        (v, t)
        for v in fixture_config.postgresql_versions
        for t in fixture_config.target_triples
    }
    for artifact in extension["artifacts"]:
        assert (
            artifact["url"]
            == f"https://github.com/{REPOSITORY}/releases/download/{TAG}/{artifact['file']}"
        )
        assert artifact["sha256"] == read_sidecar(full_dist / artifact["file"])
        assert artifact["size"] == (full_dist / artifact["file"]).stat().st_size
        assert artifact["files"] == sorted(FIXTURE_FILES)


def test_build_manifest_fails_closed_on_missing_leg(
    fixture_config, full_dist: Path
) -> None:
    """A release missing any expected archive is refused."""
    extension, pg_version, target = last_leg(fixture_config)
    missing = fixture_config.archive_name(extension, pg_version, target)
    (full_dist / missing).unlink()
    with pytest.raises(ManifestError, match=missing):
        build_manifest(fixture_config, full_dist, TAG, REPOSITORY, GENERATED_AT)


def test_build_manifest_rejects_sidecar_mismatch(
    fixture_config, full_dist: Path
) -> None:
    """A sidecar that does not match the archive bytes is refused."""
    extension, pg_version, target = first_leg(fixture_config)
    name = fixture_config.archive_name(extension, pg_version, target)
    sidecar = full_dist / (name + ".sha256")
    sidecar.write_text("0" * 64 + f"  {name}\n")
    with pytest.raises(ManifestError, match="does not match"):
        build_manifest(fixture_config, full_dist, TAG, REPOSITORY, GENERATED_AT)


def test_build_manifest_rejects_missing_sidecar(
    fixture_config, full_dist: Path
) -> None:
    """Every archive needs its sidecar."""
    extension, pg_version, target = first_leg(fixture_config)
    name = fixture_config.archive_name(extension, pg_version, target)
    (full_dist / (name + ".sha256")).unlink()
    with pytest.raises(ManifestError, match="missing checksum sidecar"):
        build_manifest(fixture_config, full_dist, TAG, REPOSITORY, GENERATED_AT)


def test_build_manifest_rejects_bad_layout(
    fixture_config: Config, full_dist: Path
) -> None:
    """An archive carrying a file outside lib/ or share/extension/ is refused."""
    extension, pg_version, target = first_leg(fixture_config)
    name = fixture_config.archive_name(extension, pg_version, target)
    entries = [
        *FIXTURE_FILES.items(),
        ("include/server/extension/vector/vector.h", b""),
    ]
    write_archive(full_dist / name, entries)
    with pytest.raises(ManifestError, match="outside"):
        build_manifest(fixture_config, full_dist, TAG, REPOSITORY, GENERATED_AT)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("abc  file.tar.gz\n", id="short_digest"),
        pytest.param("0" * 64 + "  other.tar.gz\n", id="wrong_name"),
        pytest.param("0" * 64 + "\n", id="no_name"),
        pytest.param("G" * 64 + "  file.tar.gz\n", id="non_hex"),
    ],
)
def test_read_sidecar_rejects_malformed(tmp_path: Path, line: str) -> None:
    """Sidecars must be sha256sum lines naming the archive."""
    archive = tmp_path / "file.tar.gz"
    archive.write_bytes(b"x")
    archive.with_name("file.tar.gz.sha256").write_text(line)
    with pytest.raises(ManifestError):
        read_sidecar(archive)


def test_cli_build_then_verify_round_trip(
    fixture_config, full_dist: Path, tmp_path: Path, capsys
) -> None:
    """The CLI writes manifest.json plus sidecar and verify accepts them."""
    config_path = tmp_path / "extensions.toml"
    config_path.write_text(FIXTURE_CONFIG)
    common = [
        "--config",
        str(config_path),
        "--dist",
        str(full_dist),
        "--tag",
        TAG,
        "--repository",
        REPOSITORY,
    ]
    assert main([*common, "build"]) == 0
    manifest_path = full_dist / MANIFEST_NAME
    assert manifest_path.is_file()
    assert read_sidecar(manifest_path)
    assert main([*common, "verify"]) == 0
    assert verify_manifest(fixture_config, full_dist, TAG, REPOSITORY) == 4
    out = capsys.readouterr().out
    assert "wrote" in out and "verified" in out


def test_verify_detects_tampered_manifest(
    fixture_config: Config, full_dist: Path
) -> None:
    """Editing a digest in manifest.json after publication is caught."""
    manifest = build_manifest(fixture_config, full_dist, TAG, REPOSITORY, GENERATED_AT)
    manifest["extensions"][0]["artifacts"][0]["sha256"] = "0" * 64
    path = full_dist / MANIFEST_NAME
    path.write_text(json.dumps(manifest))
    write_sidecar(path, sha256_of(path))
    with pytest.raises(ManifestError, match="do not match"):
        verify_manifest(fixture_config, full_dist, TAG, REPOSITORY)


def test_cli_check_archive(full_dist: Path, capsys) -> None:
    """check-archive validates individual archives without the full set."""
    archive = next(full_dist.glob("*.tar.gz"))
    assert (
        main(
            [
                "--config",
                str(Path(__file__).resolve().parents[1] / "extensions.toml"),
                "check-archive",
                str(archive),
            ]
        )
        == 0
    )
    assert archive.name in capsys.readouterr().out


def test_cli_requires_dist_for_build(capsys) -> None:
    """Build and verify need --dist, --tag and --repository."""
    assert (
        main(
            [
                "--config",
                str(Path(__file__).resolve().parents[1] / "extensions.toml"),
                "build",
            ]
        )
        == 1
    )
    assert "needs --dist" in capsys.readouterr().err


def test_unexpected_archive_is_rejected(
    fixture_config: Config, full_dist: Path
) -> None:
    """An archive the configuration does not expect fails both build and verify."""
    write_archive(
        full_dist / "pgcrypto-1.0.0-pg17.11.0-x86_64-unknown-linux-gnu.tar.gz",
        FIXTURE_FILES.items(),
    )
    with pytest.raises(ManifestError, match=r"does not expect.*pgcrypto"):
        collect_extensions(fixture_config, full_dist, TAG, REPOSITORY)


def test_collect_extensions_is_clock_free(
    fixture_config: Config, full_dist: Path
) -> None:
    """Two collections of the same directory are identical, with no timestamp inside."""
    first = collect_extensions(fixture_config, full_dist, TAG, REPOSITORY)
    second = collect_extensions(fixture_config, full_dist, TAG, REPOSITORY)
    assert first == second, "collection is deterministic"
    assert "generated_at" not in json.dumps(first), (
        "no timestamp in the extensions array"
    )


def test_verify_accepts_any_generated_at(
    fixture_config: Config, full_dist: Path
) -> None:
    """Verify compares the archives, not the timestamp the publisher recorded."""
    manifest = build_manifest(
        fixture_config, full_dist, TAG, REPOSITORY, "1999-01-01T00:00:00+00:00"
    )
    path = full_dist / MANIFEST_NAME
    path.write_text(json.dumps(manifest))
    write_sidecar(path, sha256_of(path))
    assert verify_manifest(fixture_config, full_dist, TAG, REPOSITORY) == 4, (
        "four artifacts"
    )


def test_verify_rejects_wrong_release_tag(
    fixture_config: Config, full_dist: Path
) -> None:
    """A manifest published under another tag is refused."""
    manifest = build_manifest(
        fixture_config, full_dist, "v0.9.0", REPOSITORY, GENERATED_AT
    )
    path = full_dist / MANIFEST_NAME
    path.write_text(json.dumps(manifest))
    write_sidecar(path, sha256_of(path))
    with pytest.raises(ManifestError, match=r"release is 'v0\.9\.0'"):
        verify_manifest(fixture_config, full_dist, TAG, REPOSITORY)
