"""Tests for ``extensions.toml`` parsing and the checked-in configuration."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import FIXTURE_CONFIG
from pgx_config import Config, ConfigError, load_config, parse_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_checked_in_config_is_valid() -> None:
    """The real extensions.toml parses and pins every extension to a commit."""
    config = load_config(REPO_ROOT / "extensions.toml")
    assert config.extensions, "at least one extension must be configured"
    for extension in config.extensions:
        assert re.fullmatch(r"[0-9a-f]{40}", extension.commit), extension.name
    assert "@sha256:" in config.build_image, "build image must be digest-pinned"
    assert config.smoke.target in config.target_triples, "smoke leg names a target"


def test_targets_keep_file_order_and_metadata(fixture_config: Config) -> None:
    """Targets carry their runner and platform in the order written."""
    assert [t.triple for t in fixture_config.targets] == [
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
    ], "targets must keep file order"
    assert fixture_config.targets[1].runner == "ubuntu-24.04-arm", "arm runner"
    assert fixture_config.targets[1].platform == "linux/arm64", "arm platform"


def test_smoke_leg_is_parsed(fixture_config: Config) -> None:
    """The [smoke] table selects package, major and target."""
    assert (
        fixture_config.smoke.package,
        fixture_config.smoke.postgresql_major,
        fixture_config.smoke.target,
    ) == (
        "pgvector",
        17,
        "x86_64-unknown-linux-gnu",
    ), "smoke leg fields"


def test_archive_name_layout(fixture_config: Config) -> None:
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
                "@sha256:3a3fa7f043b142bc8008c8b308d39b47d2c84008addcd52f9f9a7a82d2a90474",
                "",
            ),
            "digest",
            id="unpinned_image",
        ),
        pytest.param(
            ('max_glibc = "2.34"', 'max_glibc = "2.34.1"'),
            "max_glibc",
            id="bad_max_glibc",
        ),
    ],
)
def test_malformed_config_is_rejected(mutation: tuple[str, str], message: str) -> None:
    """Each malformed field is rejected with a message naming the problem."""
    old, new = mutation
    assert old in FIXTURE_CONFIG, "mutation must change the fixture"
    with pytest.raises(ConfigError, match=message):
        parse_config(FIXTURE_CONFIG.replace(old, new))


def test_non_table_extension_entry_is_rejected() -> None:
    """An integer where an extension table belongs is a ConfigError, not a TypeError."""
    without_tables = FIXTURE_CONFIG[: FIXTURE_CONFIG.index("[[extensions]]")]
    text = without_tables.replace(
        "schema_version = 1\n", "schema_version = 1\nextensions = [1]\n", 1
    )
    with pytest.raises(ConfigError, match="must be a table"):
        parse_config(text)


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


def test_build_base_stays_at_or_below_the_theseus_glibc_floor() -> None:
    """The image is almalinux:9 (glibc 2.34) and the floor is Theseus's 2.34."""
    config = load_config(REPO_ROOT / "extensions.toml")
    assert config.build_image.startswith("docker.io/library/almalinux:9@sha256:"), (
        "the build base must be almalinux:9, whose glibc 2.34 equals the floor the "
        "Theseus postgres binary requires"
    )
    assert config.max_glibc == "2.34", "the floor is the Theseus binary's GLIBC_2.34"
