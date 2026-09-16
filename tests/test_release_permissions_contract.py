"""Contracts on the release workflow's permissions and verification metrics.

Split from `test_workflow_contracts.py`, which these pushed past the
six-hundred-line ceiling, and because they answer a narrower question. That
module asks whether each job runs the command it should. This one asks who
each job is allowed to be while running it, and whether it says afterwards
what happened.

The rule both halves serve is one sentence: every job that runs a
`gh release` subcommand needs `contents: write`, including the two that
only read, because a draft release is invisible to a token holding only
`contents: read`.
"""

from __future__ import annotations

import re
import typing as typ
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


def _steps_of(workflow: dict[str, typ.Any], job: str) -> list[dict[str, typ.Any]]:
    """Return the steps of ``job``."""
    return workflow["jobs"][job]["steps"]


@pytest.fixture(scope="module")
def release() -> dict[str, typ.Any]:
    """Return the parsed release workflow."""
    return yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))


def test_release_write_permission_wherever_a_job_touches_the_draft(
    release: dict[str, typ.Any],
) -> None:
    """Every job that reaches the draft release at all gets contents: write.

    The earlier form of this contract asserted write "iff the job mutates the
    release", which read naturally and was wrong. A draft release is invisible
    to a token holding only contents: read: `gh release download` reports
    "release not found", so the audit and smoke jobs failed on every asset
    while their write-scoped siblings succeeded on the same command seconds
    earlier. Reading a draft, not writing to one, is what needs the scope, so
    the predicate is any `gh release` subcommand rather than the mutating
    three. `prepare` runs none and must stay read-only, which keeps the rule
    from collapsing into "write everywhere".
    """
    for job, spec in release["jobs"].items():
        touches_release = any(
            re.search(r"\bgh release\b", step.get("run", ""))
            for step in spec.get("steps", [])
        )
        assert (spec.get("permissions") == {"contents": "write"}) == touches_release, (
            f"{job}: contents: write iff the job runs a gh release subcommand"
        )


def test_release_smoke_reads_the_draft_with_write_scope(
    release: dict[str, typ.Any],
) -> None:
    """Smoke downloads its leg's asset from the draft, so it needs write scope."""
    smoke = release["jobs"]["smoke"]
    assert any(
        'gh release download "$TAG" --dir smoke-dist' in step.get("run", "")
        for step in smoke["steps"]
    ), "smoke must download its archive from the release"
    assert smoke["permissions"] == {"contents": "write"}, (
        "smoke downloads a draft release, which contents: read cannot see"
    )


def test_every_verification_job_records_a_bounded_outcome(
    release: dict[str, typ.Any],
) -> None:
    """Audit and smoke each emit one metric, on success and on failure alike.

    The jobs that stop a release are the ones whose outcomes are worth
    measuring, and a metric emitted only on the happy path measures
    nothing: it is the failures this exists to count. So the step is
    asserted to carry ``if: always()`` as well as to exist, and to name
    its own operation rather than the other job's.

    The labels are not checked here beyond the operation. They cannot be
    unbounded, because the script refuses anything outside its closed
    sets and ``tests/test_release_metrics.py`` holds that property over
    arbitrary input.
    """
    for job, operation in (("audit", "audit"), ("smoke", "smoke")):
        steps = _steps_of(release, job)
        recording = [
            step
            for step in steps
            if "scripts/release_metrics.py" in step.get("run", "")
        ]
        assert len(recording) == 1, (
            f"{job} must record exactly one verification outcome"
        )
        step = recording[0]
        assert step.get("if") == "always()", (
            f"{job}'s metric must be recorded on failure too, not only on success"
        )
        assert f"--operation {operation}" in step["run"], (
            f"{job}'s metric must name {operation}"
        )


def test_a_download_that_cannot_see_the_draft_is_categorised(
    release: dict[str, typ.Any],
) -> None:
    """Each verification job tells an invisible draft from a missing one.

    "release not found" is what the API says for both an unpublished
    release the token cannot see and a release that is genuinely absent.
    That ambiguity is what made the v1.0.0 failure unreadable, so the
    download step matches the message and reports `draft_not_visible`
    rather than letting it fall through as a generic download failure.
    """
    for job in ("audit", "smoke"):
        runs = [step.get("run", "") for step in _steps_of(release, job)]
        download = [run for run in runs if "gh release download" in run]
        assert len(download) == 1, f"{job} downloads in exactly one step"
        assert "release not found" in download[0], (
            f"{job} must recognise the invisible-draft message"
        )
        assert "category=draft_not_visible" in download[0], (
            f"{job} must report the invisible draft as its own category"
        )
