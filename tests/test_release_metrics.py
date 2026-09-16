"""The release-verification metric: closed label sets, and nothing else.

A metric whose labels are unbounded cannot be aggregated, and one whose
result and cause can disagree lets a dashboard show a failure with no
reason or a success with one. These tests hold both properties, because
both are easy to lose in a later edit that adds "just one more" category
taken straight from an error string.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from release_metrics import (
    ERROR_CATEGORIES,
    METRIC_NAME,
    OPERATIONS,
    RESULTS,
    MetricError,
    main,
    metric_line,
)


def test_a_passing_operation_reads_as_one_line_with_no_cause() -> None:
    """A pass carries the operation, the result and ``none``."""
    assert metric_line("audit", "pass", "none") == (
        "release_verification operation=audit result=pass error_category=none"
    )


def test_the_invisible_draft_has_a_category_of_its_own() -> None:
    """The fault that cost v1.0.0 is legible from the metric alone."""
    line = metric_line("smoke", "fail", "draft_not_visible")
    assert "error_category=draft_not_visible" in line
    assert line.startswith(f"{METRIC_NAME} operation=smoke")


@pytest.mark.parametrize(
    ("operation", "result", "category"),
    [
        pytest.param("publish", "pass", "none", id="an-operation-outside-the-set"),
        pytest.param("audit", "ok", "none", id="a-result-outside-the-set"),
        pytest.param("audit", "fail", "exploded", id="a-category-outside-the-set"),
    ],
)
def test_a_label_outside_its_closed_set_is_refused(
    operation: str, result: str, category: str
) -> None:
    """Labels come from the vocabulary, never from the thing that failed."""
    with pytest.raises(MetricError):
        metric_line(operation, result, category)


@pytest.mark.parametrize(
    ("result", "category"),
    [
        pytest.param("pass", "download_failed", id="a-pass-carrying-a-cause"),
        pytest.param("fail", "none", id="a-failure-carrying-no-cause"),
    ],
)
def test_a_result_and_a_cause_that_disagree_are_refused(
    result: str, category: str
) -> None:
    """A pass carries ``none`` and a failure carries a real category."""
    with pytest.raises(MetricError):
        metric_line("audit", result, category)


@given(
    operation=st.text(max_size=40),
    result=st.text(max_size=40),
    category=st.text(max_size=40),
)
def test_arbitrary_labels_never_reach_the_line(
    operation: str, result: str, category: str
) -> None:
    """Whatever arrives, either it is vocabulary or it is refused.

    This is the property that matters for boundedness: there is no input
    that produces a line carrying a label the sets do not contain, so an
    archive name, a tag or a raw error cannot become one.
    """
    try:
        line = metric_line(operation, result, category)
    except MetricError:
        return
    assert operation in OPERATIONS
    assert result in RESULTS
    assert category in ERROR_CATEGORIES
    assert line == (
        f"{METRIC_NAME} operation={operation} result={result} error_category={category}"
    )


def test_the_command_prints_the_line_and_succeeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The workflow calls this as a command, so its exit status is the contract."""
    code = main(
        ["--operation", "audit", "--result", "pass", "--error-category", "none"]
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == (
        "release_verification operation=audit result=pass error_category=none"
    )


def test_the_command_refuses_an_unbounded_label_without_printing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused metric writes nothing to stdout a collector could ingest."""
    code = main(
        [
            "--operation",
            "audit",
            "--result",
            "fail",
            "--error-category",
            "pgvector-0.8.6-pg17.11.0-x86_64-unknown-linux-gnu.tar.gz",
        ]
    )
    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must be one of" in captured.err
