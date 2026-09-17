"""Contracts on the Make targets that stand between a change and CI.

Split from `test_workflow_contracts.py`, which asks what each workflow job
runs. This one asks what the recipes those jobs invoke actually contain, so
that `make all` locally and the checks job in CI are the same gate.

The recipes are read as commands rather than as text. A gate named in a
recipe but never reached, or two gates collapsed into one command line that
still matches both descriptions, is the failure this module exists to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _recipe(target: str) -> str:
    """Return the recipe lines of one Make target."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    found = re.search(
        rf"^{re.escape(target)}:.*\n((?:\t.*\n)+)", makefile, flags=re.MULTILINE
    )
    assert found, f"Makefile target {target} is missing"
    return found.group(1)


def test_makefile_declares_every_gate_ci_runs() -> None:
    """The Make targets CI invokes exist and do what the workflow expects."""
    for target, needle in (
        ("check-fmt", "ruff@$(RUFF_VERSION) format --check"),
        ("shellcheck", "shellcheck --shell=bash $(SHELL_SOURCES)"),
        ("ruff", "ruff@$(RUFF_VERSION) check $(PY_SOURCES)"),
        ("smoke-leg", "scripts/matrix.py extensions.toml --smoke-leg"),
    ):
        assert needle in _recipe(target), f"Makefile target {target} must run {needle}"


def test_make_test_runs_the_suite_and_the_doctests_separately() -> None:
    """`make test` runs two pytest commands, and only one restricts collection.

    Asserted per command line rather than as two substrings of the whole
    recipe. A single line reading `python -m pytest --doctest-modules
    scripts` satisfies both substrings at once, and it collects nothing
    but `scripts/`: the unit tests, the property tests and every contract
    in this directory would stop running while the contract that exists to
    keep them running went on passing. So the lines are counted, one
    required to carry the flag and one required not to.
    """
    commands = [
        line.strip()
        for line in _recipe("test").splitlines()
        if "python -m pytest" in line
    ]
    doctest_passes = [line for line in commands if "--doctest-modules scripts" in line]
    suite_passes = [line for line in commands if "--doctest-modules" not in line]
    assert len(suite_passes) == 1, (
        f"make test must run the suite in a pytest command of its own; "
        f"found {suite_passes}"
    )
    assert len(doctest_passes) == 1, (
        f"make test must run the docstring examples in scripts/ in a pytest "
        f"command of its own; found {doctest_passes}"
    )
