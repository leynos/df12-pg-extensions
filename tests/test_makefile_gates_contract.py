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


def _commands(target: str, makefile: str) -> str:
    """Return the command lines of one target in `makefile`, comments dropped.

    A recipe line whose first non-whitespace character is `#` is handed to
    the shell, which treats it as a comment: nothing runs. To a contract
    reading the recipe as text such a line is indistinguishable from the
    gate it describes, so commenting a gate out would leave every
    assertion in this module passing while CI ran one command fewer. Both
    callers ask what the recipe runs, so the comments are dropped once,
    here.

    Make's own `#` comments before a recipe are not tab-prefixed and never
    match the recipe group at all.
    """
    found = re.search(
        rf"^{re.escape(target)}:.*\n((?:\t.*\n)+)", makefile, flags=re.MULTILINE
    )
    assert found, f"Makefile target {target} is missing"
    commands = [
        line for line in found.group(1).splitlines() if not line.strip().startswith("#")
    ]
    return "".join(f"{line}\n" for line in commands)


def _recipe(target: str) -> str:
    """Return the command lines of one target in this repository's Makefile."""
    return _commands(target, (REPO_ROOT / "Makefile").read_text(encoding="utf-8"))


def test_a_commented_out_recipe_line_is_not_a_command() -> None:
    """The reader is driven directly, not through the Makefile it guards.

    Parametrised over this repository's own correct recipes the filter
    would pass whether or not it existed, because nothing here is
    commented out. So the two shapes are fed in as text: a gate that is
    present and a gate that has been commented out, differing in nothing
    else. The second must disappear from what the reader reports, or a
    commented gate satisfies the descriptions above while running
    nothing.

    A trailing comment on a command line is a shell comment, not a Make
    one, and the command before it still runs; only a line that begins
    with `#` is wholly inert.
    """
    live = "gate:\n\truff check src\n\tshellcheck run.sh\n"
    commented = "gate:\n\truff check src\n\t# shellcheck run.sh\n"
    assert "shellcheck run.sh" in _commands("gate", live), (
        "a command line must be reported as a command"
    )
    assert "shellcheck run.sh" not in _commands("gate", commented), (
        "a commented-out gate must not be reported as a command"
    )
    assert "ruff check src" in _commands("gate", commented), (
        "dropping the comment must not drop the command beside it"
    )
    indented = "gate:\n\t    # shellcheck run.sh\n\truff check src\n"
    assert "shellcheck run.sh" not in _commands("gate", indented), (
        "a comment is a comment whatever whitespace precedes it"
    )
    trailing = "gate:\n\truff check src  # the lint gate\n"
    assert "ruff check src" in _commands("gate", trailing), (
        "a trailing shell comment does not stop the command before it"
    )


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
