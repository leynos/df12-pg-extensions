"""Run the release workflow's own download blocks under each token scope.

The contract tests next door read `release.yml` and assert what it says.
These execute what it says: the `run:` block is lifted out of the workflow
verbatim and run under `bash`, against a stand-in for `gh` that behaves the
way the GitHub releases API behaves for a draft. That is the same idiom the
container-invocation test uses with a stand-in for `docker`.

What this proves and what it does not is worth stating, because it would be
easy to read more into it. It does **not** establish that a draft release is
invisible to a `contents: read` token: that is GitHub's behaviour, and no
test written here could establish it. A stand-in cannot supply evidence
about the thing it stands in for.

That evidence exists, and it is not a test. Release run 34995521161 is the
`v1.0.0` run in which `audit` and all six `smoke` legs failed against a
draft that demonstrably existed with fourteen assets, under the read scope
they then had, while the write-scoped `manifest` job had downloaded from
the same release eleven seconds earlier. The run of the same tag after the
retag, under the write scope this change grants, is the other half. Those
two runs are the end-to-end evidence for the GitHub fact; what follows is
the evidence for ours.

What it proves is everything on our side of that boundary: that the block
the workflow actually runs succeeds under the write scope, fails under the
read scope, and separates the shared "release not found" response from a
generic download failure, reporting it as the category the metric step then
reads. It does not tell an invisible draft from a genuinely missing one, and
nothing could: the API says the same sentence for both, which is why both
arrive as `draft_not_visible`. The distinction the category draws is between
that answer and every other way a download can fail. A later edit that swaps
the download for something else, drops the token from the step's environment,
or lets the not-found answer fall through as a generic failure is caught here
rather than on the next release.
"""

from __future__ import annotations

import os
import subprocess
import typing as typ
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

#: What the API says when a draft is hidden from the token, and when the
#: release is simply not there. They are the same sentence, which is the
#: ambiguity the download step exists to resolve.
RELEASE_NOT_FOUND = "release not found"


#: Workflow expressions this harness knows how to stand in for. Anything
#: else is refused rather than guessed: a step environment the test cannot
#: resolve is one the test would otherwise run with a blank value, which is
#: how a missing variable comes to look like a passing case.
_EXPRESSIONS: typ.Final[dict[str, str]] = {
    "${{ secrets.GITHUB_TOKEN }}": "TOKEN",
    "${{ github.repository }}": "leynos/df12-pg-extensions",
    "${{ needs.prepare.outputs.tag }}": "v1.0.0",
    "${{ matrix.archive }}": (
        "pgvector-0.8.6-pg17.11.0-x86_64-unknown-linux-gnu.tar.gz"
    ),
}


# The environment comes from the step rather than from this file, so a step
# that stops declaring `GH_TOKEN` is a step the harness runs without one.
# Supplying it here instead would make the test blind to exactly the edit most
# likely to break the download.
def _download_step(job: str) -> tuple[str, dict[str, str]]:
    """Return the download step's `run:` script and its declared environment."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    steps = [
        step
        for step in workflow["jobs"][job]["steps"]
        if "gh release download" in step.get("run", "")
    ]
    assert len(steps) == 1, f"{job} must download in exactly one step"
    step = steps[0]
    declared: dict[str, str] = {}
    for name, value in step.get("env", {}).items():
        text = str(value)
        if text.startswith("${{"):
            assert text in _EXPRESSIONS, f"{job}: unresolved step expression {text}"
            text = _EXPRESSIONS[text]
        declared[name] = text
    return step["run"], declared


# The scope is read from the token the step passes, not baked in, so a step
# that stops passing one is a step that can no longer see the draft. That is
# how the real thing behaves, and a stand-in that ignored the token would let a
# dropped `GH_TOKEN` pass unnoticed.
#
# A draft is served only to a token carrying `contents: write`. To any other
# token the API reports the release as absent rather than refusing access, and
# it says the same thing when the release really is absent, so both cases print
# the one message.
def _fake_gh(tmp_path: Path, *, release_exists: bool = True) -> Path:
    """Write a stand-in for `gh` that answers as the releases API does."""
    fake = tmp_path / "fake-gh"
    body = [
        "#!/usr/bin/env bash",
        "set -uo pipefail",
        'dir=""',
        'pattern=""',
        "while [ $# -gt 0 ]; do",
        '  case "$1" in',
        '    --dir) dir="$2"; shift 2 ;;',
        '    --pattern) pattern="$2"; shift 2 ;;',
        "    *) shift ;;",
        "  esac",
        "done",
        f'release_exists="{int(release_exists)}"',
        'case "${GH_TOKEN:-}" in',
        "  *contents-write*) scope=write ;;",
        "  *) scope=other ;;",
        "esac",
        'if [ "$scope" != "write" ] || [ "$release_exists" != "1" ]; then',
        f'  echo "{RELEASE_NOT_FOUND}" >&2',
        "  exit 1",
        "fi",
    ]
    # A sidecar has to hold the real digest of the archive beside it: the
    # smoke block verifies it, so a stand-in writing arbitrary bytes would
    # fail the step for the wrong reason.
    body += [
        'mkdir -p "$dir"',
        'name="${pattern:-manifest.json}"',
        'case "$name" in',
        "  *.sha256)",
        '    archive="${name%.sha256}"',
        '    (cd "$dir" && sha256sum "$archive" > "$name") ;;',
        "  *)",
        '    printf "asset" > "$dir/$name" ;;',
        "esac",
        "exit 0",
    ]
    fake.write_text("\n".join(body) + "\n", encoding="utf-8")
    fake.chmod(0o755)
    return fake


# Returns the exit status, the standard error, and the step outputs the block
# wrote. The token the step declares is given the scope under test; the rest of
# the environment is the step's own.
def _run_download(
    job: str, tmp_path: Path, *, scope: str, release_exists: bool = True
) -> tuple[int, str, dict[str, str]]:
    """Run the job's download step with the stand-in on PATH."""
    block, declared = _download_step(job)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    if not gh.exists():
        gh.symlink_to(_fake_gh(tmp_path, release_exists=release_exists))
    output_file = tmp_path / "github-output"
    output_file.write_text("", encoding="utf-8")
    work = tmp_path / f"work-{scope}-{release_exists}"
    work.mkdir(exist_ok=True)
    resolved = {
        name: (f"token-with-contents-{scope}" if value == "TOKEN" else value)
        for name, value in declared.items()
    }
    completed = subprocess.run(
        ["bash", "-c", block],
        cwd=work,
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "HOME", "LANG", "TMPDIR"}
            },
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            **resolved,
            "GITHUB_OUTPUT": str(output_file),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    outputs: dict[str, str] = {}
    for line in output_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    return completed.returncode, completed.stderr, outputs


JOBS: typ.Final[tuple[str, ...]] = ("audit", "smoke")


@pytest.mark.parametrize("job", JOBS)
def test_the_write_scope_reaches_the_draft(job: str, tmp_path: Path) -> None:
    """Under `contents: write` the download succeeds and reports no fault."""
    status, _, outputs = _run_download(job, tmp_path, scope="write")
    assert status == 0, f"{job} must download the draft under the write scope"
    assert outputs["category"] == "none"


@pytest.mark.parametrize("job", JOBS)
def test_the_read_scope_cannot_see_the_draft(job: str, tmp_path: Path) -> None:
    """Under `contents: read` the draft reads as absent, and is named as such.

    This is the failure that stopped `v1.0.0`, and the assertion that
    matters is the category rather than the exit status: a job that merely
    failed here would have told an operator nothing the logs did not
    already say, which is exactly what happened at the time.
    """
    status, stderr, outputs = _run_download(job, tmp_path, scope="read")
    assert status != 0, f"{job} must fail when it cannot see the draft"
    assert outputs["category"] == "draft_not_visible"
    assert RELEASE_NOT_FOUND in stderr, "the real message must still reach the log"


@pytest.mark.parametrize("job", JOBS)
def test_a_missing_release_is_reported_the_same_way(job: str, tmp_path: Path) -> None:
    """A genuinely absent release is indistinguishable, and honestly so.

    The API gives one sentence for both, so the step cannot separate them
    and does not pretend to. Recording that here keeps a later reader from
    assuming the category means the scope is wrong, when it means only that
    the release could not be seen.
    """
    status, _, outputs = _run_download(
        job, tmp_path, scope="write", release_exists=False
    )
    assert status != 0
    assert outputs["category"] == "draft_not_visible"
