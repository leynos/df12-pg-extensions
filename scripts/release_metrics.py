#!/usr/bin/env python3
"""Emit one bounded metric per release-verification operation.

The verification jobs are where a release stops. `audit` re-downloads every
asset and checks it against the manifest; `smoke` loads an archive into a
PostgreSQL and queries it. When either fails, the run stops before `publish`
and the release stays a draft, so their outcomes are the reliability signal
this workflow actually has.

Every label here is drawn from a closed set. Archive names, tags, release
URLs, request identifiers and raw error text are deliberately absent: they
are unbounded, and a metric carrying them stops being aggregatable. The
detail stays in the job log, which is where an operator reading a failure
goes next; the metric says only what happened and which kind of thing went
wrong.

`draft_not_visible` is a category of its own rather than a download failure
because it is a specific, recurring, and entirely silent fault: a draft
release is invisible to a token holding only `contents: read`, and the API
reports it as absent rather than refusing access. It cost this repository
the v1.0.0 release, and it looked exactly like a missing release. Naming it
means the next occurrence is legible from the metric alone.
"""

from __future__ import annotations

import argparse
import sys
import typing as typ

#: The verification operations that report an outcome.
OPERATIONS: typ.Final[frozenset[str]] = frozenset({"audit", "smoke"})

#: Whether the operation completed.
RESULTS: typ.Final[frozenset[str]] = frozenset({"pass", "fail"})

#: Why it did not, or ``none`` when it did.
ERROR_CATEGORIES: typ.Final[frozenset[str]] = frozenset(
    {
        "none",
        "draft_not_visible",
        "download_failed",
        "verification_failed",
        "smoke_failed",
    }
)

#: The measurement name every line carries.
METRIC_NAME: typ.Final[str] = "release_verification"


class MetricError(ValueError):
    """A label outside its closed set, or a result and category that disagree."""


def _require(value: str, allowed: frozenset[str], label: str) -> str:
    """Return ``value`` when it is in ``allowed``, else refuse it by name."""
    if value not in allowed:
        permitted = ", ".join(sorted(allowed))
        message = f"{label} must be one of {permitted}; got {value!r}"
        raise MetricError(message)
    return value


def metric_line(operation: str, result: str, error_category: str) -> str:
    """Build the metric line for one verification outcome.

    A passing operation carries ``none``, and a failing one carries a real
    category. Allowing either to disagree would let a dashboard count a
    failure with no cause, or a success with one.

    >>> metric_line("audit", "pass", "none")
    'release_verification operation=audit result=pass error_category=none'
    >>> metric_line("smoke", "fail", "draft_not_visible")
    'release_verification operation=smoke result=fail error_category=draft_not_visible'
    """
    operation = _require(operation, OPERATIONS, "operation")
    result = _require(result, RESULTS, "result")
    error_category = _require(error_category, ERROR_CATEGORIES, "error_category")
    if (result == "pass") != (error_category == "none"):
        message = (
            f"result {result!r} and error_category {error_category!r} disagree: "
            f"a passing operation carries none and a failing one carries a cause"
        )
        raise MetricError(message)
    return (
        f"{METRIC_NAME} operation={operation} "
        f"result={result} error_category={error_category}"
    )


def main(argv: list[str] | None = None) -> int:
    """Print one metric line, refusing any label outside its closed set."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--error-category", required=True)
    args = parser.parse_args(argv)
    try:
        line = metric_line(args.operation, args.result, args.error_category)
    except MetricError as error:
        print(f"release metric refused: {error}", file=sys.stderr)
        return 2
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
