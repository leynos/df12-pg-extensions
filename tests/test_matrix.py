"""Tests for the configuration-derived build matrix."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from matrix import PLATFORMS, RUNNERS, build_matrix, select_leg
from pgx_config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_matrix_covers_every_leg_exactly_once(fixture_config) -> None:
    """The matrix is the cartesian product of extensions, versions and targets."""
    legs = build_matrix(fixture_config)
    keys = [(leg["name"], leg["postgresql"], leg["target"]) for leg in legs]
    assert len(keys) == len(set(keys)) == 4
    for leg in legs:
        assert leg["runner"] == RUNNERS[leg["target"]]
        assert leg["platform"] == PLATFORMS[leg["target"]]
        assert leg["archive"].endswith(f"-pg{leg['postgresql']}-{leg['target']}.tar.gz")


@pytest.mark.parametrize(
    ("target", "runner", "platform"),
    [
        pytest.param(
            "x86_64-unknown-linux-gnu", "ubuntu-24.04", "linux/amd64", id="x86_64"
        ),
        pytest.param(
            "aarch64-unknown-linux-gnu", "ubuntu-24.04-arm", "linux/arm64", id="aarch64"
        ),
    ],
)
def test_every_configured_target_has_a_runner(
    target: str, runner: str, platform: str
) -> None:
    """Each target in extensions.toml maps to a native runner and platform."""
    config = load_config(REPO_ROOT / "extensions.toml")
    assert target in config.targets
    assert RUNNERS[target] == runner
    assert PLATFORMS[target] == platform


def test_checked_in_config_targets_all_have_runners() -> None:
    """No target can be added to extensions.toml without a runner mapping."""
    config = load_config(REPO_ROOT / "extensions.toml")
    assert set(config.targets) <= set(RUNNERS)


def test_select_leg_finds_exactly_one(fixture_config) -> None:
    """--select narrows to one leg by package, major and target."""
    legs = build_matrix(fixture_config)
    leg = select_leg(legs, "pgvector", "17", "x86_64-unknown-linux-gnu")
    assert leg["postgresql"] == "17.11.0"
    with pytest.raises(LookupError):
        select_leg(legs, "pgvector", "15", "x86_64-unknown-linux-gnu")


def test_cli_json_and_select_forms() -> None:
    """The CLI prints fromJSON-compatible JSON, and key=value lines for --select."""
    script = REPO_ROOT / "scripts" / "matrix.py"
    output = subprocess.run(
        [sys.executable, str(script), str(REPO_ROOT / "extensions.toml")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    matrix = json.loads(output)
    assert matrix.get("include")
    lines = subprocess.run(
        [
            sys.executable,
            str(script),
            str(REPO_ROOT / "extensions.toml"),
            "--select",
            "pgvector",
            "17",
            "x86_64-unknown-linux-gnu",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    pairs = dict(line.split("=", 1) for line in lines)
    assert pairs["name"] == "vector"
    assert pairs["target"] == "x86_64-unknown-linux-gnu"
    assert pairs["archive"].startswith("pgvector-")
