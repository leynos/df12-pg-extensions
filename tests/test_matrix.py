"""Tests for the configuration-derived build matrix."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from matrix import build_matrix, smoke_leg
from pgx_config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_matrix_covers_every_leg_exactly_once(fixture_config) -> None:
    """The matrix is the cartesian product of extensions, versions and targets."""
    legs = build_matrix(fixture_config)
    keys = [(leg["name"], leg["postgresql"], leg["target"]) for leg in legs]
    assert len(keys) == len(set(keys)) == 4, "one leg per combination"
    by_triple = {target.triple: target for target in fixture_config.targets}
    for leg in legs:
        assert leg["runner"] == by_triple[leg["target"]].runner, leg["target"]
        assert leg["platform"] == by_triple[leg["target"]].platform, leg["target"]
        assert leg["releases_url"] == fixture_config.releases_url, "releases URL"
        assert leg["archive"].endswith(
            f"-pg{leg['postgresql']}-{leg['target']}.tar.gz"
        ), "archive name"


def test_checked_in_config_produces_a_matrix() -> None:
    """Every configured target and version appears in the real matrix."""
    config = load_config(REPO_ROOT / "extensions.toml")
    legs = build_matrix(config)
    assert {leg["target"] for leg in legs} == set(config.target_triples), "targets"
    assert {leg["postgresql"] for leg in legs} == set(config.postgresql_versions), (
        "versions"
    )


def test_smoke_leg_matches_configuration(fixture_config) -> None:
    """The smoke leg is the configured package on the configured major and target."""
    leg = smoke_leg(fixture_config)
    assert leg["package"] == fixture_config.smoke.package, "smoke package"
    assert leg["postgresql"].startswith(f"{fixture_config.smoke.postgresql_major}."), (
        "smoke major"
    )
    assert leg["target"] == fixture_config.smoke.target, "smoke target"


def test_smoke_leg_requires_exactly_one_match(fixture_config) -> None:
    """Two configured releases of the same major make the smoke leg ambiguous."""
    ambiguous = fixture_config.__class__(
        **{**fixture_config.__dict__, "postgresql_versions": ("17.11.0", "17.12.0")}
    )
    with pytest.raises(LookupError, match="found 2"):
        smoke_leg(ambiguous)


def test_cli_json_and_smoke_leg_forms() -> None:
    """The CLI prints fromJSON-compatible JSON, and key=value lines for --smoke-leg."""
    script = REPO_ROOT / "scripts" / "matrix.py"
    config_path = REPO_ROOT / "extensions.toml"
    config = load_config(config_path)
    output = subprocess.run(
        [sys.executable, str(script), str(config_path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    matrix = json.loads(output)
    assert matrix["include"], "matrix has legs"
    lines = subprocess.run(
        [sys.executable, str(script), str(config_path), "--smoke-leg"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    pairs = dict(line.split("=", 1) for line in lines)
    assert pairs["package"] == config.smoke.package, "smoke package"
    assert pairs["target"] == config.smoke.target, "smoke target"
    assert pairs["releases_url"] == config.releases_url, "releases URL"
    assert set(pairs) == set(matrix["include"][0]), "same keys as the matrix"
