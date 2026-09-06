"""Contracts for the release and CI workflows and the scripts they invoke.

Each assertion matches the mechanism (the ``run:`` command, the ``uses:``
reference, the exact runner label) rather than a step name or a comment, so
deleting the protected line fails the contract even when its description
survives.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

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
BUILD_ENV_FROM_MATRIX = {
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
    "THESEUS_RELEASES_URL": "releases_url",
    "MAX_GLIBC": "max_glibc",
}


def load_workflow(name: str) -> dict[str, Any]:
    """Parse a workflow file into a dictionary."""
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def steps_of(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    """Return the steps of ``job``."""
    return workflow["jobs"][job]["steps"]


def run_commands(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    """Return every (job, run command) pair in the workflow."""
    return [
        (job, step["run"])
        for job, spec in workflow["jobs"].items()
        for step in spec.get("steps", [])
        if "run" in step
    ]


def step_running(workflow: dict[str, Any], job: str, command: str) -> dict[str, Any]:
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


def step_using(workflow: dict[str, Any], job: str, action: str) -> dict[str, Any]:
    """Return the single step in ``job`` whose ``uses`` starts with ``action@``."""
    matches = [
        step
        for step in steps_of(workflow, job)
        if step.get("uses", "").startswith(action + "@")
    ]
    assert len(matches) == 1, (
        f"{job}: expected one step using {action}, found {len(matches)}"
    )
    return matches[0]


@pytest.fixture(scope="module")
def release() -> dict[str, Any]:
    """Return the parsed release workflow."""
    return load_workflow("release.yml")


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    """Return the parsed CI workflow."""
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
                    f"{name}/{job}: checkout must set persist-credentials: false"
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
    assert load_workflow(name)["permissions"] == {"contents": "read"}, (
        f"{name}: top-level permissions must be contents: read"
    )


# --- release.yml ------------------------------------------------------------


def test_release_triggers_on_version_tags_only(release: dict[str, Any]) -> None:
    """Releases come from tag pushes; there is no manual dispatch."""
    assert release["on"] == {"push": {"tags": ["v*"]}}, (
        "release triggers only on v* tags"
    )


def test_release_runs_serialise_per_ref(release: dict[str, Any]) -> None:
    """One release run per tag at a time, never cancelling an in-flight audit."""
    assert release["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": False,
    }, "release concurrency group must serialise per ref without cancelling"


def _tag_regex(release: dict[str, Any]) -> re.Pattern[str]:
    steps = steps_of(release, "prepare")
    resolve = next(step for step in steps if step.get("id") == "resolve")
    match = re.search(r'\[\[ "\$REF_NAME" =~ (\S+) \]\]', resolve["run"])
    assert match, "prepare/resolve must validate REF_NAME with a bash regex"
    return re.compile(match.group(1))


@pytest.mark.parametrize("tag", ["v1.0.0", "v0.0.1", "v12.34.56"])
def test_release_tag_validation_accepts_semver_tags(
    release: dict[str, Any], tag: str
) -> None:
    """The tag regex in the resolve step accepts vMAJOR.MINOR.PATCH."""
    assert _tag_regex(release).search(tag), f"{tag} must be accepted"


@pytest.mark.parametrize(
    "tag", ["1.0.0", "v1.0", "v1.0.0-rc1", "v1.0.0.0", "release-1", "v01.0.0x"]
)
def test_release_tag_validation_rejects_other_tags(
    release: dict[str, Any], tag: str
) -> None:
    """Anything that is not vMAJOR.MINOR.PATCH stops the release."""
    assert not _tag_regex(release).search(tag), f"{tag} must be rejected"
    resolve = next(
        step for step in steps_of(release, "prepare") if step.get("id") == "resolve"
    )
    assert "exit 1" in resolve["run"], "an invalid tag must exit non-zero"


def test_release_refuses_to_resume_a_published_release(release: dict[str, Any]) -> None:
    """A published release is immutable; only a draft of the same tag may be resumed."""
    create = next(step for step in steps_of(release, "create-release") if "run" in step)
    text = create["run"]
    assert 'gh release view "$TAG" --json isDraft --jq .isDraft' in text, (
        "create-release must inspect the draft flag"
    )
    assert 'if [ "$is_draft" != "true" ]; then' in text, (
        "a published release is refused"
    )
    refusal = text.split('"$is_draft" != "true"')[1]
    assert "exit 1" in refusal.split("fi")[0], "refusal must fail the job"


def test_container_build_enforces_the_glibc_floor() -> None:
    """Every shared object is checked against MAX_GLIBC and the build fails above it."""
    text = (SCRIPTS / "build_in_container.sh").read_text(encoding="utf-8")
    assert "THESEUS_RELEASES_URL DIST_DIR MAX_GLIBC; do" in text, (
        "MAX_GLIBC is required"
    )
    assert "objdump -T \"$so\" | { grep -o 'GLIBC_[0-9.]*' || true; }" in text, (
        "symbol versions read, tolerating a library with no GLIBC imports"
    )
    assert 'above the permitted floor GLIBC_$MAX_GLIBC" >&2' in text, (
        "violation reported"
    )
    assert "    exit 1\n  fi\ndone" in text, "violation fails the build"


def test_release_matrix_is_derived_from_configuration(release: dict[str, Any]) -> None:
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
        ), f"{job}: matrix must come from the prepare job"
        assert spec["strategy"]["fail-fast"] is False, (
            f"{job}: legs must not cancel each other"
        )
        assert spec["runs-on"] == "${{ matrix.runner }}", (
            f"{job}: runner comes from the matrix"
        )
        assert "prepare" in spec["needs"], f"{job}: must depend on prepare"


def test_release_build_leg_uses_container_script_with_matrix_inputs(
    release: dict[str, Any],
) -> None:
    """The build step runs the wrapper with every matrix field it needs."""
    step = step_running(release, "build-assets", "bash scripts/build_extension.sh")
    assert step["env"] == {
        key: f"${{{{ matrix.{field} }}}}"
        for key, field in BUILD_ENV_FROM_MATRIX.items()
    }, "build step env must map every required variable from the matrix"


def test_release_build_leg_checks_layout_then_uploads_archive_and_sidecar(
    release: dict[str, Any],
) -> None:
    """Each leg validates its archive and uploads both the archive and its sidecar."""
    runs = [step.get("run", "").strip() for step in steps_of(release, "build-assets")]
    check = runs.index(
        'python3 scripts/build_manifest.py check-archive "dist/$ARCHIVE"'
    )
    upload = runs.index(
        'gh release upload "$TAG" "dist/$ARCHIVE" "dist/$ARCHIVE.sha256" --clobber'
    )
    assert check < upload, "the layout check must run before the upload"
    assert release["jobs"]["build-assets"]["permissions"] == {"contents": "write"}, (
        "build-assets uploads, so it needs contents: write"
    )


def test_release_manifest_is_built_from_published_archives(
    release: dict[str, Any],
) -> None:
    """The manifest job re-downloads the archives, builds, and uploads manifest."""
    runs = [step.get("run", "") for step in steps_of(release, "manifest")]
    assert any(
        "gh release download \"$TAG\" --dir release-dist --pattern '*.tar.gz'" in run
        for run in runs
    ), "manifest job must download the archives"
    assert any(
        "gh release download \"$TAG\" --dir release-dist --pattern '*.tar.gz.sha256'"
        in run
        for run in runs
    ), "manifest job must download the sidecars"
    assert any(
        re.search(
            r"python3 scripts/build_manifest\.py build\s+--dist release-dist "
            r"--tag \"\$TAG\" --repository \"\$REPOSITORY\"",
            run,
        )
        for run in runs
    ), "manifest job must run build_manifest.py build on the downloads"
    assert any(
        re.search(
            r'gh release upload "\$TAG"\s+release-dist/manifest\.json '
            r"release-dist/manifest\.json\.sha256 --clobber",
            run,
        )
        for run in runs
    ), "manifest job must upload manifest.json and its sidecar"
    assert release["jobs"]["manifest"]["needs"] == ["prepare", "build-assets"], (
        "manifest job waits for every build leg"
    )


def test_release_audit_verifies_fresh_downloads(release: dict[str, Any]) -> None:
    """The audit job re-downloads every asset and runs verify against it."""
    runs = [step.get("run", "") for step in steps_of(release, "audit")]
    assert any('gh release download "$TAG" --dir audit-dist' in run for run in runs), (
        "audit must download every asset afresh"
    )
    assert any(
        re.search(
            r"python3 scripts/build_manifest\.py verify\s+--dist audit-dist "
            r"--tag \"\$TAG\" --repository \"\$REPOSITORY\"",
            run,
        )
        for run in runs
    ), "audit must run build_manifest.py verify"
    assert "permissions" not in release["jobs"]["audit"], "audit is read-only"


def test_release_smoke_verifies_sidecar_and_loads_extension(
    release: dict[str, Any],
) -> None:
    """Every leg checks its sidecar, then loads the archive into PostgreSQL."""
    runs = [step.get("run", "") for step in steps_of(release, "smoke")]
    assert any(
        '(cd smoke-dist && sha256sum -c "$ARCHIVE.sha256")' in run for run in runs
    ), "smoke must verify the downloaded archive against its sidecar"
    step = step_running(release, "smoke", "bash scripts/smoke_test.sh")
    assert step["env"] == {
        "EXT_NAME": "${{ matrix.name }}",
        "PG_VERSION": "${{ matrix.postgresql }}",
        "TARGET": "${{ matrix.target }}",
        "ARCHIVE_PATH": "smoke-dist/${{ matrix.archive }}",
        "SMOKE_SQL": "${{ matrix.smoke_sql }}",
        "THESEUS_RELEASES_URL": "${{ matrix.releases_url }}",
    }, "smoke step env must come from the matrix"


def test_release_publishes_only_after_audit_and_smoke(release: dict[str, Any]) -> None:
    """Undrafting waits for the audit and every smoke leg."""
    publish = release["jobs"]["publish"]
    assert set(publish["needs"]) == {"prepare", "audit", "smoke"}, (
        "publish waits for audit and smoke"
    )
    step_running(release, "publish", 'gh release edit "$TAG" --draft=false')
    assert publish["permissions"] == {"contents": "write"}, "publish edits the release"


def test_release_write_permission_only_where_gh_mutates(
    release: dict[str, Any],
) -> None:
    """Only jobs that create, upload or edit the release get contents: write."""
    for job, spec in release["jobs"].items():
        mutates = any(
            re.search(r"\bgh release (create|upload|edit)\b", step.get("run", ""))
            for step in spec.get("steps", [])
        )
        assert (spec.get("permissions") == {"contents": "write"}) == mutates, (
            f"{job}: contents: write iff the job mutates the release"
        )


# --- ci.yml -----------------------------------------------------------------


def test_ci_triggers(ci: dict[str, Any]) -> None:
    """CI runs on pull requests and can be dispatched for warm runs."""
    assert ci["on"]["pull_request"] == {
        "types": ["opened", "synchronize", "reopened"]
    }, "CI runs on pull request open, synchronize and reopen"
    assert "workflow_dispatch" in ci["on"], "CI must be dispatchable"


def test_ci_jobs_run_on_ubicloud_standard_2(ci: dict[str, Any]) -> None:
    """Developer-blocking Linux jobs run on the exact ubicloud-standard-2 label."""
    for job, spec in ci["jobs"].items():
        assert spec["runs-on"] == "ubicloud-standard-2", (
            f"{job}: must run on ubicloud-standard-2"
        )


def test_ci_checks_job_checks_out_and_lints_markdown(ci: dict[str, Any]) -> None:
    """The checks job checks out the tree and runs the pinned markdownlint action."""
    step_using(ci, "checks", "actions/checkout")
    lint = step_using(ci, "checks", "DavidAnson/markdownlint-cli2-action")
    assert "**/*.md" in lint["with"]["globs"], (
        "markdownlint must cover every Markdown file"
    )


def test_ci_checks_job_runs_every_gate(ci: dict[str, Any]) -> None:
    """The checks job runs the same Make targets a contributor runs locally."""
    for command in ("make check-fmt", "make shellcheck", "make ruff", "make test"):
        step_running(ci, "checks", command)


def test_ci_smoke_build_exercises_the_full_pipeline_for_the_configured_leg(
    ci: dict[str, Any],
) -> None:
    """The PR smoke build resolves the configured leg, then builds and smokes it."""
    step_running(
        ci,
        "smoke-build",
        'python3 scripts/matrix.py extensions.toml --smoke-leg >> "$GITHUB_OUTPUT"',
    )
    build = step_running(ci, "smoke-build", "bash scripts/build_extension.sh")
    assert build["env"] == {
        key: f"${{{{ steps.leg.outputs.{field} }}}}"
        for key, field in BUILD_ENV_FROM_MATRIX.items()
    }, "smoke build env must come from the resolved leg"
    step_running(
        ci,
        "smoke-build",
        'python3 scripts/build_manifest.py check-archive "dist/$ARCHIVE"',
    )
    smoke = step_running(ci, "smoke-build", "bash scripts/smoke_test.sh")
    assert smoke["env"]["SMOKE_SQL"] == "${{ steps.leg.outputs.smoke_sql }}", (
        "smoke SQL from the leg"
    )
    assert (
        smoke["env"]["THESEUS_RELEASES_URL"] == "${{ steps.leg.outputs.releases_url }}"
    ), "releases URL from the leg"


# --- Makefile ---------------------------------------------------------------


def test_makefile_declares_every_gate_ci_runs() -> None:
    """The Make targets CI invokes exist and do what the workflow expects."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    for target, needle in (
        ("check-fmt", "ruff@$(RUFF_VERSION) format --check"),
        ("shellcheck", "shellcheck --shell=bash $(SHELL_SOURCES)"),
        ("ruff", "ruff@$(RUFF_VERSION) check $(PY_SOURCES)"),
        ("test", "python -m pytest"),
        ("smoke-leg", "scripts/matrix.py extensions.toml --smoke-leg"),
    ):
        recipe = re.search(
            rf"^{re.escape(target)}:.*\n((?:\t.*\n)+)", makefile, flags=re.MULTILINE
        )
        assert recipe, f"Makefile target {target} is missing"
        assert needle in recipe.group(1), f"Makefile target {target} must run {needle}"


# --- scripts ----------------------------------------------------------------


@pytest.mark.parametrize(
    "script", ["build_extension.sh", "build_in_container.sh", "smoke_test.sh"]
)
def test_scripts_are_executable_and_strict(script: str) -> None:
    """Shell scripts carry the exec bit and fail fast."""
    path = SCRIPTS / script
    assert path.stat().st_mode & stat.S_IXUSR, f"{script} is not executable"
    assert "set -euo pipefail" in path.read_text(encoding="utf-8"), (
        f"{script} must fail fast"
    )


def test_container_build_uses_portable_flags_and_pgxs() -> None:
    """Both make invocations pass USE_PGXS=1, OPTFLAGS="" and with_llvm=no."""
    text = (SCRIPTS / "build_in_container.sh").read_text(encoding="utf-8")
    make_lines = re.findall(
        r"^make -C \"\$src\".*?(?=\n(?!\s))", text, flags=re.MULTILINE | re.DOTALL
    )
    assert len(make_lines) == 2, (
        f"expected a build and an install make line, got {make_lines}"
    )
    for line in make_lines:
        for token in (
            "USE_PGXS=1",
            'PG_CONFIG="$pg_config"',
            'OPTFLAGS=""',
            "with_llvm=no",
        ):
            assert token in line, f"{token} missing from {line!r}"
    assert 'DESTDIR="$stage"' in make_lines[1], "install must go to the staging root"


def test_container_build_verifies_theseus_archive_and_upstream_commit() -> None:
    """The Theseus tarball is checked against its sidecar and HEAD against the pin."""
    text = (SCRIPTS / "build_in_container.sh").read_text(encoding="utf-8")
    assert '(cd "$work" && sha256sum -c "$theseus_archive.sha256")' in text, (
        "sidecar check"
    )
    assert 'if [ "$head" != "$EXT_COMMIT" ]; then' in text, "pinned commit check"
    assert "--strip-components=1" in text, "Theseus archive has a top-level directory"


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
        assert token in tar_block.group(0), f"{token} missing from the tar invocation"
    assert '(cd "$out" && sha256sum "$ARCHIVE" > "$ARCHIVE.sha256")' in text, (
        "sidecar written"
    )


def test_wrapper_requires_releases_url_and_pins_the_image() -> None:
    """The wrapper refuses to run without THESEUS_RELEASES_URL and reads build.image."""
    text = (SCRIPTS / "build_extension.sh").read_text(encoding="utf-8")
    assert (
        "PG_VERSION TARGET PLATFORM ARCHIVE THESEUS_RELEASES_URL MAX_GLIBC; do" in text
    ), "THESEUS_RELEASES_URL and MAX_GLIBC must be required, not defaulted"
    assert "THESEUS_RELEASES_URL:-" not in text, "no hard-coded releases URL fallback"
    assert (
        'BUILD_IMAGE="$(sed -n \'s/^image = "\\(.*\\)"$/\\1/p\' '
        '"$repo_root/extensions.toml")"'
    ) in text, "image comes from extensions.toml"
    assert '--platform "$PLATFORM"' in text, (
        "platform is passed to the container runtime"
    )


def _fake_docker(tmp_path: Path) -> Path:
    """Write a stand-in for docker that records its arguments and fakes the build."""
    fake = tmp_path / "fake-docker"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf "%s\\n" "$@" > "$FAKE_DOCKER_ARGS"\n'
        'mkdir -p "$FAKE_DOCKER_DIST"\n'
        'printf "fake" > "$FAKE_DOCKER_DIST/$ARCHIVE"\n'
        '(cd "$FAKE_DOCKER_DIST" && sha256sum "$ARCHIVE" > "$ARCHIVE.sha256")\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def test_wrapper_runs_the_pinned_image_for_the_leg(tmp_path: Path) -> None:
    """Running the wrapper with a fake docker proves the exact container invocation."""
    config = load_config(REPO_ROOT / "extensions.toml")
    leg = build_matrix(config)[0]
    dist_dir = f"dist-contract-{hashlib.sha256(str(tmp_path).encode()).hexdigest()[:8]}"
    dist = REPO_ROOT / dist_dir
    env = {
        **os.environ,
        "EXT_NAME": leg["name"],
        "EXT_PACKAGE": leg["package"],
        "EXT_VERSION": leg["version"],
        "EXT_REPOSITORY": leg["repository"],
        "EXT_TAG": leg["tag"],
        "EXT_COMMIT": leg["commit"],
        "PG_VERSION": leg["postgresql"],
        "TARGET": leg["target"],
        "PLATFORM": leg["platform"],
        "ARCHIVE": leg["archive"],
        "THESEUS_RELEASES_URL": leg["releases_url"],
        "MAX_GLIBC": leg["max_glibc"],
        "DIST_DIR": dist_dir,
        "DOCKER": str(_fake_docker(tmp_path)),
        "FAKE_DOCKER_ARGS": str(tmp_path / "args"),
        "FAKE_DOCKER_DIST": str(dist),
    }
    try:
        result = subprocess.run(
            ["bash", str(SCRIPTS / "build_extension.sh")],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        args = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    finally:
        for path in dist.glob("*"):
            path.unlink()
        if dist.exists():
            dist.rmdir()
    assert args[:2] == ["run", "--rm"], "container is run once and removed"
    assert args[args.index("--platform") + 1] == leg["platform"], (
        "platform from the leg"
    )
    assert args[-3:] == [config.build_image, "bash", "scripts/build_in_container.sh"], (
        "the pinned image runs the container build script"
    )
    forwarded = {args[i + 1] for i, token in enumerate(args) if token == "--env"}
    for variable in BUILD_ENV_FROM_MATRIX:
        if variable != "PLATFORM":
            assert variable in forwarded, (
                f"{variable} must be forwarded into the container"
            )
    assert "DIST_DIR" in forwarded, "DIST_DIR must be forwarded into the container"


def test_wrapper_refuses_to_run_without_releases_url(tmp_path: Path) -> None:
    """Dropping THESEUS_RELEASES_URL is a hard error, not a silent default."""
    leg = build_matrix(load_config(REPO_ROOT / "extensions.toml"))[0]
    env = {
        key: leg[field]
        for key, field in BUILD_ENV_FROM_MATRIX.items()
        if key != "THESEUS_RELEASES_URL"
    }
    result = subprocess.run(
        ["bash", str(SCRIPTS / "build_extension.sh")],
        env={**os.environ, **env, "DOCKER": str(_fake_docker(tmp_path))},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, "missing THESEUS_RELEASES_URL must exit 2"
    assert "THESEUS_RELEASES_URL is required" in result.stderr, (
        "the error names the variable"
    )


def test_smoke_script_verifies_exercises_and_reports() -> None:
    """The smoke script checks the sidecar, runs the SQL, and dumps logs on failure."""
    text = (SCRIPTS / "smoke_test.sh").read_text(encoding="utf-8")
    assert '(cd "$WORK_DIR" && sha256sum -c "$theseus_archive.sha256")' in text, (
        "sidecar check"
    )
    assert '-c "CREATE EXTENSION $EXT_NAME"' in text, "creates the extension"
    assert '-c "$SMOKE_SQL"' in text, "runs the configured SQL"
    assert "-v ON_ERROR_STOP=1" in text, "psql stops on the first error"
    assert "trap report_failure ERR" in text, "failures are reported by the ERR trap"
    assert 'cat "$WORK_DIR/$log" >&2' in text, "the trap prints the server logs"
    assert (
        "for var in EXT_NAME PG_VERSION TARGET ARCHIVE_PATH SMOKE_SQL "
        "THESEUS_RELEASES_URL; do" in text
    ), "THESEUS_RELEASES_URL is required"


def test_configuration_and_matrix_agree() -> None:
    """The matrix has one leg per configured (extension, version, target)."""
    config = load_config(REPO_ROOT / "extensions.toml")
    legs = build_matrix(config)
    assert len(legs) == len(config.extensions) * len(config.postgresql_versions) * len(
        config.targets
    ), "one leg per combination"
    assert {leg["archive"] for leg in legs} == {
        config.archive_name(ext, v, t)
        for ext in config.extensions
        for v in config.postgresql_versions
        for t in config.target_triples
    }, "archive names follow the configuration"
