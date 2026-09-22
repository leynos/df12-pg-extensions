"""Run the workflow's own outcome-recording blocks under each failure.

The metric a verification job emits is the only aggregatable record of
why a release stopped, and it is assembled in shell from the outcomes of
the steps before it. That assembly can be wrong while every step is
right, and it fails in a way that leaves no trace: `metric_line` refuses
a failure carrying `none` as its cause, so a block reporting that
combination exits non-zero and records nothing at all. A release then
fails with no metric, which is indistinguishable from a release that
never ran.

So the blocks are lifted out of `release.yml` and run, as the download
blocks next door are, rather than read. Each case supplies the step
outcomes GitHub would supply and asserts the line that comes out.

The case this module was written for is the smoke job's checksum. It
used to run in the tail of the download step, after that step had
already written `category=none`, so a checksum mismatch produced exactly
the refused combination. The remedy was to give it its own step, which
is what the `audit` job had always done.
"""

from __future__ import annotations

import os
import subprocess
import typing as typ
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The metric every line carries, as `release_metrics.py` spells it.
METRIC_NAME: typ.Final[str] = "release_verification"


def _record_step(job: str) -> str:
    """Return the outcome-recording step's `run:` script for one job."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    steps = [
        step
        for step in workflow["jobs"][job]["steps"]
        if "release_metrics.py" in step.get("run", "")
    ]
    assert len(steps) == 1, f"{job} must record its outcome in exactly one step"
    return steps[0]["run"]


# `outcomes` is the environment the block reads, such as `DOWNLOAD_OUTCOME`;
# `tmp_path` holds the step summary the block appends to.
def _run_record(job: str, tmp_path: Path, **outcomes: str) -> Emitted:
    """Run the job's recording block with the given step outcomes."""
    summary = tmp_path / "summary"
    summary.write_text("", encoding="utf-8")
    completed = subprocess.run(
        ["bash", "-c", _record_step(job)],
        cwd=REPO_ROOT,
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "HOME", "LANG", "TMPDIR"}
            },
            **outcomes,
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return Emitted(
        completed.returncode, completed.stdout, summary.read_text(encoding="utf-8")
    )


class Emitted(typ.NamedTuple):
    """What a recording block did.

    Attributes
    ----------
    status : int
        Its exit status.
    stdout : str
        What it printed.
    summary : str
        What it appended to the step summary.
    """

    status: int
    stdout: str
    summary: str


# The result is derived from the category rather than passed separately: they
# are not independent, and a case free to state a passing result beside a cause
# would be asserting a combination `metric_line` refuses.
def _assert_metric(emitted: Emitted, operation: str, expected_category: str) -> None:
    """Assert the block emitted exactly the metric the outcome implies."""
    result = "pass" if expected_category == "none" else "fail"
    expected = (
        f"{METRIC_NAME} operation={operation} result={result} "
        f"error_category={expected_category}"
    )
    assert emitted.status == 0, (
        f"the recording block exited {emitted.status}; a block that cannot "
        f"emit its metric leaves the failure with no aggregatable record at "
        f"all. Output: {emitted.stdout!r}"
    )
    assert emitted.stdout.strip() == expected, (
        f"expected {expected!r}, got {emitted.stdout.strip()!r}"
    )
    # The step summary is the copy an operator reads on the run page; the
    # log line alone would pass with the `tee` into it deleted.
    assert emitted.summary.strip() == expected, (
        f"the step summary must carry exactly {expected!r}, got {emitted.summary!r}"
    )


@pytest.mark.parametrize(
    ("job", "outcomes", "category"),
    [
        pytest.param(
            "audit",
            {"DOWNLOAD_OUTCOME": "success", "VERIFY_OUTCOME": "success"},
            "none",
            id="audit-passes",
        ),
        pytest.param(
            "audit",
            {
                "DOWNLOAD_OUTCOME": "failure",
                "DOWNLOAD_CATEGORY": "draft_not_visible",
                "VERIFY_OUTCOME": "skipped",
            },
            "draft_not_visible",
            id="audit-cannot-see-the-draft",
        ),
        pytest.param(
            "audit",
            {"DOWNLOAD_OUTCOME": "success", "VERIFY_OUTCOME": "failure"},
            "verification_failed",
            id="audit-fails-verification",
        ),
    ],
)
def test_the_audit_block_classifies_each_outcome(
    job: str, outcomes: dict[str, str], category: str, tmp_path: Path
) -> None:
    """Every way the audit job can end names its own cause."""
    _assert_metric(_run_record(job, tmp_path, **outcomes), "audit", category)


@pytest.mark.parametrize(
    ("outcomes", "category"),
    [
        pytest.param(
            {
                "DOWNLOAD_OUTCOME": "success",
                "VERIFY_OUTCOME": "success",
                "LOAD_OUTCOME": "success",
            },
            "none",
            id="smoke-passes",
        ),
        pytest.param(
            {
                "DOWNLOAD_OUTCOME": "failure",
                "DOWNLOAD_CATEGORY": "draft_not_visible",
                "VERIFY_OUTCOME": "skipped",
                "LOAD_OUTCOME": "skipped",
            },
            "draft_not_visible",
            id="smoke-cannot-see-the-draft",
        ),
        pytest.param(
            {
                "DOWNLOAD_OUTCOME": "success",
                "VERIFY_OUTCOME": "failure",
                "LOAD_OUTCOME": "skipped",
            },
            "verification_failed",
            id="smoke-fails-its-checksum",
        ),
        pytest.param(
            {
                "DOWNLOAD_OUTCOME": "success",
                "VERIFY_OUTCOME": "success",
                "LOAD_OUTCOME": "failure",
            },
            "smoke_failed",
            id="smoke-fails-to-load",
        ),
    ],
)
def test_the_smoke_block_classifies_each_outcome(
    outcomes: dict[str, str], category: str, tmp_path: Path
) -> None:
    """Every way the smoke job can end names its own cause.

    The checksum case is the one this was written for. Before the
    verification moved into its own step there was no `VERIFY_OUTCOME`
    for the block to read, so a mismatch arrived as a failed download
    carrying `none`, and the metric was refused rather than recorded.
    """
    _assert_metric(_run_record("smoke", tmp_path, **outcomes), "smoke", category)


def test_a_failure_carrying_no_cause_records_nothing(tmp_path: Path) -> None:
    """The combination the old shape produced, and what it cost.

    This is not a rule about how the workflow should be written. It is
    the measurement that makes the rule worth having: when a step
    reports failure while its category still says `none`, the metric is
    refused and the run ends with no record of why. The old smoke job
    reached this state whenever a checksum failed, because the category
    was written before the check ran.

    It is asserted rather than described so that a later change making
    the refusal lenient, which would look like a kindness, shows up as
    this case passing something through.
    """
    emitted = _run_record(
        "smoke",
        tmp_path,
        DOWNLOAD_OUTCOME="failure",
        DOWNLOAD_CATEGORY="none",
        VERIFY_OUTCOME="skipped",
        LOAD_OUTCOME="skipped",
    )

    assert emitted.status != 0, (
        "a failure carrying no cause must be refused; recording it would let "
        "a dashboard count a failure with nothing wrong"
    )
    assert METRIC_NAME not in emitted.stdout, (
        f"no metric line may be emitted for a refused combination, but the "
        f"block printed {emitted.stdout!r}"
    )
    assert METRIC_NAME not in emitted.summary, (
        f"a refused combination must not reach the step summary either, but "
        f"it holds {emitted.summary!r}"
    )


def test_the_smoke_job_verifies_outside_its_download() -> None:
    """The checksum runs in a step of its own, so it can be classified.

    Asserted on the command rather than on a step named "Verify", since
    a step keeps its name when its `run:` moves elsewhere. The download
    step is asserted not to verify, because that is where it was and
    where it would return to.
    """
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["smoke"]["steps"]
    verifying = [step for step in steps if "sha256sum -c" in step.get("run", "")]

    assert len(verifying) == 1, (
        f"exactly one smoke step must verify the sidecar; {len(verifying)} do"
    )
    assert verifying[0].get("id") == "verify", (
        "the verifying step must carry the id the recording step reads, or "
        "its outcome cannot be classified"
    )
    downloading = [
        step for step in steps if "gh release download" in step.get("run", "")
    ]
    assert not any("sha256sum" in step.get("run", "") for step in downloading), (
        "the download step must not verify: its category is written before "
        "the check would run, so a mismatch reports a failure with no cause "
        "and the metric is refused"
    )
