"""Tests for the configuration-derived build matrix."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FIXTURE_CONFIG
from matrix import build_matrix, main, smoke_leg
from pgx_config import Config, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_matrix_covers_every_leg_exactly_once(fixture_config: Config) -> None:
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


def test_smoke_leg_matches_configuration(fixture_config: Config) -> None:
    """The smoke leg is the configured package on the configured major and target."""
    leg = smoke_leg(fixture_config)
    assert leg["package"] == fixture_config.smoke.package, "smoke package"
    assert leg["postgresql"].startswith(f"{fixture_config.smoke.postgresql_major}."), (
        "smoke major"
    )
    assert leg["target"] == fixture_config.smoke.target, "smoke target"


def test_smoke_leg_requires_exactly_one_match(fixture_config: Config) -> None:
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


def run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
    """Run ``matrix.py`` with ``argv`` and capture both streams."""
    script = REPO_ROOT / "scripts" / "matrix.py"
    return subprocess.run(
        [sys.executable, str(script), *argv],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param((), id="no_arguments"),
        pytest.param(("a.toml", "b.toml"), id="two_paths"),
        pytest.param(("extensions.toml", "--json"), id="unknown_flag"),
        pytest.param(("extensions.toml", "--smoke-leg", "extra"), id="too_many"),
    ],
)
def test_cli_rejects_bad_usage(argv: tuple[str, ...]) -> None:
    """Wrong argument counts or an unknown flag exit 2 with the usage line."""
    result = run_cli(*argv)
    assert result.returncode == 2, result.stderr
    assert result.stderr.startswith("usage: matrix.py"), result.stderr
    assert result.stdout == "", "nothing is printed on usage errors"


@pytest.mark.parametrize("mode", [(), ("--smoke-leg",)], ids=["matrix", "smoke_leg"])
def test_cli_reports_a_missing_configuration(
    tmp_path: Path, mode: tuple[str, ...]
) -> None:
    """A configuration path that does not exist fails closed in both modes."""
    result = run_cli(str(tmp_path / "absent.toml"), *mode)
    assert result.returncode == 1, result.stderr
    assert result.stderr.startswith("error: cannot read"), result.stderr
    assert result.stdout == "", "nothing is printed when the configuration fails"


@pytest.mark.parametrize("mode", [(), ("--smoke-leg",)], ids=["matrix", "smoke_leg"])
def test_cli_reports_a_malformed_configuration(
    tmp_path: Path, mode: tuple[str, ...]
) -> None:
    """Invalid TOML is reported as a configuration error, not a traceback."""
    broken = tmp_path / "broken.toml"
    broken.write_text("[postgresql\nversions = [\n", encoding="utf-8")
    result = run_cli(str(broken), *mode)
    assert result.returncode == 1, result.stderr
    assert result.stderr.startswith("error: "), result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def test_cli_matrix_lists_every_extension(tmp_path: Path) -> None:
    """A second extension multiplies the legs; the JSON carries both."""
    second = (
        FIXTURE_CONFIG.replace("[[extensions]]", "[[extensions]]", 1)
        + """
[[extensions]]
name = "hstore_plus"
package = "hstore-plus"
version = "1.0.0"
repository = "https://example.invalid/hstore-plus"
tag = "v1.0.0"
commit = "0000000000000000000000000000000000000001"
smoke_sql = "SELECT 1"
"""
    )
    config_path = tmp_path / "two.toml"
    config_path.write_text(second, encoding="utf-8")
    result = run_cli(str(config_path))
    assert result.returncode == 0, result.stderr
    legs = json.loads(result.stdout)["include"]
    assert len(legs) == 8, "two extensions x two versions x two targets"
    assert {leg["package"] for leg in legs} == {"pgvector", "hstore-plus"}, "packages"


def test_smoke_leg_refuses_a_multi_line_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A smoke_sql spanning lines cannot be written to GITHUB_OUTPUT and fails."""
    config_path = tmp_path / "multiline.toml"
    config_path.write_text(
        FIXTURE_CONFIG.replace(
            "smoke_sql = \"SELECT '[1,2,3]'::vector <-> '[4,5,6]'::vector\"",
            'smoke_sql = """SELECT 1;\nSELECT 2"""',
        ),
        encoding="utf-8",
    )
    status = main(["matrix.py", str(config_path), "--smoke-leg"])
    captured = capsys.readouterr()
    assert status == 1, captured.err
    assert "smoke_sql must be a single line" in captured.err, captured.err
    assert captured.out == "", "no key is emitted before the check fails"
