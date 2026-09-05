"""Tests for ``extensions.toml`` parsing and the checked-in configuration."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import FIXTURE_CONFIG
from pgx_config import ConfigError, load_config, parse_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_checked_in_config_is_valid() -> None:
    """The real extensions.toml parses and pins every extension to a commit."""
    config = load_config(REPO_ROOT / "extensions.toml")
    assert config.extensions, "at least one extension must be configured"
    for extension in config.extensions:
        assert re.fullmatch(r"[0-9a-f]{40}", extension.commit)
    assert "@sha256:" in config.build_image


def test_archive_name_layout(fixture_config) -> None:
    """Archive names carry package, version, PostgreSQL release and target."""
    extension = fixture_config.extensions[0]
    assert (
        fixture_config.archive_name(extension, "17.11.0", "x86_64-unknown-linux-gnu")
        == "pgvector-0.8.6-pg17.11.0-x86_64-unknown-linux-gnu.tar.gz"
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        pytest.param(
            ("schema_version = 1", "schema_version = 2"),
            "schema_version",
            id="schema_version",
        ),
        pytest.param(
            (
                'commit = "8ee86c96f0fd72390f890aa8a336fda6d3ab4c6c"',
                'commit = "8ee86c9"',
            ),
            "commit",
            id="short_commit",
        ),
        pytest.param(
            ('name = "vector"', 'name = "Vector"'), "name", id="uppercase_name"
        ),
        pytest.param(
            ('version = "0.8.6"', 'version = "0.8"'), "version", id="two_part_version"
        ),
        pytest.param(
            ('versions = ["17.11.0", "18.6.0"]', 'versions = ["17.11.0", "17.11.0"]'),
            "twice",
            id="duplicate_pg_version",
        ),
        pytest.param(
            ('versions = ["17.11.0", "18.6.0"]', "versions = []"),
            "non-empty",
            id="no_pg_versions",
        ),
        pytest.param(
            (
                'repository = "https://github.com/pgvector/pgvector"',
                'repository = "http://github.com/pgvector/pgvector"',
            ),
            "https",
            id="plain_http",
        ),
        pytest.param(
            (
                "smoke_sql = \"SELECT '[1,2,3]'::vector <-> '[4,5,6]'::vector\"",
                'smoke_sql = "  "',
            ),
            "smoke_sql",
            id="blank_smoke_sql",
        ),
        pytest.param(
            (
                "@sha256:6ebd97fa83deb272194a2cf015b3d26a4d538e9ad3a7a79d544c8af5b0a01443",
                "",
            ),
            "digest",
            id="unpinned_image",
        ),
    ],
)
def test_malformed_config_is_rejected(mutation: tuple[str, str], message: str) -> None:
    """Each malformed field is rejected with a message naming the problem."""
    old, new = mutation
    assert old in FIXTURE_CONFIG, "mutation must change the fixture"
    with pytest.raises(ConfigError, match=message):
        parse_config(FIXTURE_CONFIG.replace(old, new))


@pytest.mark.parametrize(
    "key", ["name", "package", "version", "repository", "tag", "commit", "smoke_sql"]
)
def test_missing_extension_key_is_rejected(key: str) -> None:
    """Every extension key is required."""
    text = re.sub(rf"^{key} = .*$", "", FIXTURE_CONFIG, count=1, flags=re.MULTILINE)
    with pytest.raises(ConfigError, match=key):
        parse_config(text)


def test_duplicate_extension_names_are_rejected() -> None:
    """Two extensions with the same CREATE EXTENSION name cannot coexist."""
    duplicated = (
        FIXTURE_CONFIG + FIXTURE_CONFIG[FIXTURE_CONFIG.index("[[extensions]]") :]
    )
    with pytest.raises(ConfigError, match="unique"):
        parse_config(duplicated)
