"""Contracts for the release and CI workflows and the scripts they invoke.

Each assertion matches the mechanism (the ``run:`` command, the ``uses:``
reference, the exact runner label) rather than a step name or a comment, so
deleting the protected line fails the contract even when its description
survives.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest
import yaml
from matrix import build_matrix
from pgx_config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPTS = REPO_ROOT / "scripts"
SHA_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")
SOURCE_BUILD_TOKENS = (
    re.compile(r"\bcargo install\b"),
    re.compile(r"\bpip3? install\b"),
    re.compile(r"\bapt(-get)? install\b"),
    re.compile(r"\bbrew install\b"),
    re.compile(r"\bnpm install\b"),
    re.compile(r"\bcurl\b[^\n]*\|\s*(ba)?sh\b"),
)


def load_workflow(name: str) -> dict:
    """Parse a workflow file into a dictionary."""
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def steps_of(workflow: dict, job: str) -> list[dict]:
    """Return the steps of ``job``."""
    return workflow["jobs"][job]["steps"]


def run_commands(workflow: dict) -> list[tuple[str, str]]:
    """Return every (job, run command) pair in the workflow."""
    return [
        (job, step["run"])
        for job, spec in workflow["jobs"].items()
        for step in spec.get("steps", [])
        if "run" in step
    ]


def step_running(workflow: dict, job: str, command: str) -> dict:
    """Return the single step in ``job`` whose ``run`` equals ``command``."""
    matches = [
        step
        for step in steps_of(workflow, job)
        if step.get("run", "").strip() == command
    ]
    assert len(matches) == 1, (
        f"{job}: expected one step running {command!r}, found {len(matches)}"
    )
    return matches[0]


@pytest.fixture(scope="module")
def release() -> dict:
    """The parsed release workflow."""
    return load_workflow("release.yml")


@pytest.fixture(scope="module")
def ci() -> dict:
    """The parsed CI workflow."""
    return load_workflow("ci.yml")


# --- shared rules -----------------------------------------------------------


@pytest.mark.parametrize("name", ["release.yml", "ci.yml"])
def test_every_action_is_pinned_by_commit_sha(name: str) -> None:
    """``uses:`` references must carry a 40-hex commit, never a tag or branch."""
    workflow = load_workflow(name)
    for job, spec in workflow["jobs"].items():
        for step in spec.get("steps", []):
            if "uses" in step:
                assert SHA_PIN.match(step["uses"]), (
                    f"{name}/{job}: unpinned {step['uses']}"
                )


@pytest.mark.parametrize("name", ["release.yml", "ci.yml"])
def test_checkout_never_persists_credentials(name: str) -> None:
    """Every checkout sets persist-credentials: false."""
    workflow = load_workflow(name)
    for job, spec in workflow["jobs"].items():
        for step in spec.get("steps", []):
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False, (
                    f"{name}/{job}"
                )


@pytest.mark.parametrize("name", ["release.yml", "ci.yml"])
def test_no_source_build_or_tool_install_on_the_runner(name: str) -> None:
    """Runner-level steps never compile or install tools; only the container does."""
    for job, command in run_commands(load_workflow(name)):
        for token in SOURCE_BUILD_TOKENS:
            assert not token.search(command), (
                f"{name}/{job}: {token.pattern} in {command!r}"
            )


@pytest.mark.parametrize("name", ["release.yml", "ci.yml"])
def test_top_level_permissions_are_read_only(name: str) -> None:
    """Workflows default to contents: read and widen per job only."""
    assert load_workflow(name)["permissions"] == {"contents": "read"}


# --- release.yml ------------------------------------------------------------


def test_release_triggers_on_version_tags_only(release: dict) -> None:
    """Releases come from tag pushes; there is no manual dispatch."""
    assert release["on"] == {"push": {"tags": ["v*"]}}


def test_release_matrix_is_derived_from_configuration(release: dict) -> None:
    """The prepare job computes the matrix and both fan-out jobs consume it."""
    step_running(
        release,
        "prepare",
        "set -euo pipefail\n"
        'matrix="$(python3 scripts/matrix.py extensions.toml)"\n'
        'echo "$matrix" | python3 -m json.tool >/dev/null\n'
        'echo "matrix=$matrix" >> "$GITHUB_OUTPUT"',
    )
    for job in ("build-assets", "smoke"):
        spec = release["jobs"][job]
        assert (
            spec["strategy"]["matrix"]
            == "${{ fromJSON(needs.prepare.outputs.matrix) }}"
        ), job
        assert spec["strategy"]["fail-fast"] is False, job
        assert spec["runs-on"] == "${{ matrix.runner }}", job
        assert "prepare" in spec["needs"], job


def test_release_build_leg_uses_container_script_with_matrix_inputs(
    release: dict,
) -> None:
    """The build step runs the wrapper with every matrix field it needs."""
    step = step_running(release, "build-assets", "bash scripts/build_extension.sh")
    expected = {
        "EXT_NAME": "name",
        "EXT_PACKAGE": "package",
        "EXT_VERSION": "version",
        "EXT_REPOSITORY": "repository",
        "EXT_TAG": "tag",
        "EXT_COMMIT": "commit",
        "PG_VERSION": "postgresql",
        "TARGET": "target",
        "PLATFORM": "platform",
        "ARCHIVE": "archive",
    }
    assert step["env"] == {
        key: f"${{{{ matrix.{field} }}}}" for key, field in expected.items()
    }


def test_release_build_leg_checks_layout_then_uploads_archive_and_sidecar(
    release: dict,
) -> None:
    """Each leg validates its archive and uploads both the archive and its sidecar."""
    steps = steps_of(release, "build-assets")
    runs = [step.get("run", "").strip() for step in steps]
    check = runs.index(
        'python3 scripts/build_manifest.py check-archive "dist/$ARCHIVE"'
    )
    upload = runs.index(
        'gh release upload "$TAG" "dist/$ARCHIVE" "dist/$ARCHIVE.sha256" --clobber'
    )
    assert check < upload
    assert release["jobs"]["build-assets"]["permissions"] == {"contents": "write"}


def test_release_manifest_is_built_from_published_archives(release: dict) -> None:
    """The manifest job re-downloads the archives, builds, and uploads manifest."""
    runs = [step.get("run", "") for step in steps_of(release, "manifest")]
    assert any(
        "gh release download \"$TAG\" --dir release-dist --pattern '*.tar.gz'" in run
        for run in runs
    )
    assert any(
        "gh release download \"$TAG\" --dir release-dist --pattern '*.tar.gz.sha256'"
        in run
        for run in runs
    )
    assert any(
        re.search(
            r"python3 scripts/build_manifest\.py build\s+--dist release-dist "
            r"--tag \"\$TAG\" --repository \"\$REPOSITORY\"",
            run,
        )
        for run in runs
    )
    assert any(
        re.search(
            r'gh release upload "\$TAG"\s+release-dist/manifest\.json '
            r"release-dist/manifest\.json\.sha256 --clobber",
            run,
        )
        for run in runs
    )
    assert release["jobs"]["manifest"]["needs"] == ["prepare", "build-assets"]


def test_release_audit_verifies_fresh_downloads(release: dict) -> None:
    """The audit job re-downloads every asset and runs verify against it."""
    runs = [step.get("run", "") for step in steps_of(release, "audit")]
    assert any('gh release download "$TAG" --dir audit-dist' in run for run in runs)
    assert any(
        re.search(
            r"python3 scripts/build_manifest\.py verify\s+--dist audit-dist "
            r"--tag \"\$TAG\" --repository \"\$REPOSITORY\"",
            run,
        )
        for run in runs
    )
    assert "permissions" not in release["jobs"]["audit"]


def test_release_smoke_verifies_sidecar_and_loads_extension(release: dict) -> None:
    """Every leg checks its sidecar, then loads the archive into PostgreSQL."""
    runs = [step.get("run", "") for step in steps_of(release, "smoke")]
    assert any(
        '(cd smoke-dist && sha256sum -c "$ARCHIVE.sha256")' in run for run in runs
    )
    step = step_running(release, "smoke", "bash scripts/smoke_test.sh")
    assert step["env"] == {
        "EXT_NAME": "${{ matrix.name }}",
        "PG_VERSION": "${{ matrix.postgresql }}",
        "TARGET": "${{ matrix.target }}",
        "ARCHIVE_PATH": "smoke-dist/${{ matrix.archive }}",
        "SMOKE_SQL": "${{ matrix.smoke_sql }}",
    }


def test_release_publishes_only_after_audit_and_smoke(release: dict) -> None:
    """Undrafting waits for the audit and every smoke leg."""
    publish = release["jobs"]["publish"]
    assert set(publish["needs"]) == {"prepare", "audit", "smoke"}
    step_running(release, "publish", 'gh release edit "$TAG" --draft=false')
    assert publish["permissions"] == {"contents": "write"}


def test_release_write_permission_only_where_gh_mutates(release: dict) -> None:
    """Only jobs that create, upload or edit the release get contents: write."""
    for job, spec in release["jobs"].items():
        mutates = any(
            re.search(r"\bgh release (create|upload|edit)\b", step.get("run", ""))
            for step in spec.get("steps", [])
        )
        assert (spec.get("permissions") == {"contents": "write"}) == mutates, job


# --- ci.yml -----------------------------------------------------------------


def test_ci_triggers(ci: dict) -> None:
    """CI runs on pull requests and can be dispatched for warm runs."""
    assert ci["on"]["pull_request"] == {"types": ["opened", "synchronize", "reopened"]}
    assert "workflow_dispatch" in ci["on"]


def test_ci_jobs_run_on_ubicloud_standard_2(ci: dict) -> None:
    """Developer-blocking Linux jobs run on the exact ubicloud-standard-2 label."""
    for job, spec in ci["jobs"].items():
        assert spec["runs-on"] == "ubicloud-standard-2", job


def test_ci_checks_job_runs_every_gate(ci: dict) -> None:
    """The checks job runs the same Make targets a contributor runs locally."""
    for command in ("make check-fmt", "make shellcheck", "make ruff", "make test"):
        step_running(ci, "checks", command)


def test_ci_smoke_build_exercises_the_full_pipeline_for_one_leg(ci: dict) -> None:
    """The PR smoke build selects one configured leg and runs build, check and smoke."""
    step_running(
        ci,
        "smoke-build",
        "python3 scripts/matrix.py extensions.toml "
        '--select pgvector 17 x86_64-unknown-linux-gnu >> "$GITHUB_OUTPUT"',
    )
    build = step_running(ci, "smoke-build", "bash scripts/build_extension.sh")
    assert build["env"]["ARCHIVE"] == "${{ steps.leg.outputs.archive }}"
    step_running(
        ci,
        "smoke-build",
        'python3 scripts/build_manifest.py check-archive "dist/$ARCHIVE"',
    )
    smoke = step_running(ci, "smoke-build", "bash scripts/smoke_test.sh")
    assert smoke["env"]["SMOKE_SQL"] == "${{ steps.leg.outputs.smoke_sql }}"


# --- scripts ----------------------------------------------------------------


@pytest.mark.parametrize(
    "script", ["build_extension.sh", "build_in_container.sh", "smoke_test.sh"]
)
def test_scripts_are_executable_and_strict(script: str) -> None:
    """Shell scripts carry the exec bit and fail fast."""
    path = SCRIPTS / script
    assert path.stat().st_mode & stat.S_IXUSR, f"{script} is not executable"
    assert "set -euo pipefail" in path.read_text(encoding="utf-8")


def test_container_build_uses_portable_flags_and_pgxs() -> None:
    """Both make invocations pass USE_PGXS=1, OPTFLAGS="" and with_llvm=no."""
    text = (SCRIPTS / "build_in_container.sh").read_text(encoding="utf-8")
    make_lines = re.findall(
        r"^make -C \"\$src\".*?(?=\n(?!\s))", text, flags=re.MULTILINE | re.DOTALL
    )
    assert len(make_lines) == 2, make_lines
    for line in make_lines:
        for token in (
            "USE_PGXS=1",
            'PG_CONFIG="$pg_config"',
            'OPTFLAGS=""',
            "with_llvm=no",
        ):
            assert token in line, f"{token} missing from {line!r}"
    assert 'DESTDIR="$stage"' in make_lines[1]


def test_container_build_verifies_theseus_archive_and_upstream_commit() -> None:
    """The Theseus tarball is checked against its sidecar and HEAD against the pin."""
    text = (SCRIPTS / "build_in_container.sh").read_text(encoding="utf-8")
    assert '(cd "$work" && sha256sum -c "$theseus_archive.sha256")' in text
    assert 'if [ "$head" != "$EXT_COMMIT" ]; then' in text
    assert "--strip-components=1" in text


def test_container_build_packages_reproducibly() -> None:
    """The tar is sorted, owner-normalised, mtime-fixed and gzipped without a name."""
    text = (SCRIPTS / "build_in_container.sh").read_text(encoding="utf-8")
    tar_block = re.search(
        r"^tar --sort=name .*?> \"\$out/\$ARCHIVE\"$",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert tar_block, "reproducible tar invocation missing"
    for token in (
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        '--mtime="@$SOURCE_DATE_EPOCH"',
        "gzip -n",
    ):
        assert token in tar_block.group(0), token
    assert '(cd "$out" && sha256sum "$ARCHIVE" > "$ARCHIVE.sha256")' in text


def test_wrapper_runs_the_pinned_image_for_the_matrix_platform() -> None:
    """The wrapper reads build.image from extensions.toml and passes --platform."""
    text = (SCRIPTS / "build_extension.sh").read_text(encoding="utf-8")
    assert (
        'BUILD_IMAGE="$(sed -n \'s/^image = "\\(.*\\)"$/\\1/p\' '
        '"$repo_root/extensions.toml")"' in text
    )
    assert '--platform "$PLATFORM"' in text
    assert "bash scripts/build_in_container.sh" in text
    assert '  "$BUILD_IMAGE" \\\n' in text


def test_smoke_script_verifies_and_exercises_the_extension() -> None:
    """The smoke script checks the sidecar, creates the extension and runs the SQL."""
    text = (SCRIPTS / "smoke_test.sh").read_text(encoding="utf-8")
    assert '(cd "$WORK_DIR" && sha256sum -c "$theseus_archive.sha256")' in text
    assert '-c "CREATE EXTENSION $EXT_NAME"' in text
    assert '-c "$SMOKE_SQL"' in text
    assert "-v ON_ERROR_STOP=1" in text


def test_configuration_and_matrix_agree() -> None:
    """The matrix has one leg per configured (extension, version, target)."""
    config = load_config(REPO_ROOT / "extensions.toml")
    legs = build_matrix(config)
    assert len(legs) == len(config.extensions) * len(config.postgresql_versions) * len(
        config.targets
    )
    assert {leg["archive"] for leg in legs} == {
        config.archive_name(ext, v, t)
        for ext in config.extensions
        for v in config.postgresql_versions
        for t in config.targets
    }
