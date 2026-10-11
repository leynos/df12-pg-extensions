"""Contracts for the YAML lint gate in ``make lint`` and CI.

``make lint`` must run ``yamllint --strict`` over the Git-tracked YAML and
actionlint, and CI must install both through the pinned shared installer
before it runs the gate. The judgement functions are driven against fixtures
first, because a rule exercised only over this repository's own compliant
files would pass whether or not it detects anything, and then applied to the
real files. The Concordat ``yaml-lint-baseline`` rule audits the same wiring
with a full Makefile parse.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = "leynos/shared-actions/.github/actions/install-yaml-lint"
SHA_PIN = re.compile(r"^[0-9a-f]{40}$")
HAND_ROLLED = re.compile(
    r"uv tool install[^\n]*yamllint|download-actionlint"
    r"|releases/download[^\n]*actionlint"
)
PIPE = "git ls-files -z '*.yml' '*.yaml' | xargs -0 -r"


def parse_rules(makefile: str) -> list[tuple[list[str], list[str], list[str]]]:
    """Return ``(targets, prerequisites, recipe lines)`` for each rule."""
    rules: list[tuple[list[str], list[str], list[str]]] = []
    for line in makefile.splitlines():
        if line.startswith("\t"):
            if rules:
                rules[-1][2].append(line.strip())
        elif not line.startswith("#") and ":=" not in line and "?=" not in line:
            targets, separator, rest = line.partition(":")
            if separator:
                prerequisites = rest.split("##")[0].split()
                rules.append((targets.split(), prerequisites, []))
    return rules


def lint_closure(makefile: str) -> list[str]:
    """Return the recipe lines reachable from ``lint`` through the static edges."""
    rules = parse_rules(makefile)
    pending, seen, lines = ["lint"], set[str](), list[str]()
    while pending:
        target = pending.pop()
        if target in seen:
            continue
        seen.add(target)
        for targets, prerequisites, recipe in rules:
            if target in targets:
                pending.extend(prerequisites)
                pending.extend(
                    line.removeprefix("$(MAKE) ")
                    for line in recipe
                    if line.startswith("$(MAKE) ")
                )
                lines.extend(recipe)
    return lines


def gate_is_wired(makefile: str) -> bool:
    """Return whether the lint closure runs strict yamllint over Git's list."""
    lines = lint_closure(makefile)
    yamllint = any(
        line.startswith("git ls-files")
        and "'*.yml'" in line
        and "'*.yaml'" in line
        and "| xargs" in line
        and "$(YAMLLINT)" in line
        and "--strict" in line
        for line in lines
    )
    actionlint = any(line.startswith("$(ACTIONLINT)") for line in lines)
    return yamllint and actionlint


def ci_installs_before_lint(workflow: dict[str, object]) -> bool:
    """Return whether each job running ``make lint`` installs the tools first."""
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    ran = False
    for spec in jobs.values():
        installed = False
        for step in spec.get("steps", []):
            uses = step.get("uses", "")
            if uses.startswith(INSTALLER + "@"):
                installed = SHA_PIN.match(uses.partition("@")[2]) is not None
            if re.match(r"(/usr/bin/)?make( \S+)* lint( |$)", step.get("run", "")):
                ran = True
                if not installed:
                    return False
    return ran


GOOD = (
    "lint: yamllint actionlint\n"
    "yamllint:\n"
    f"\t{PIPE} $(YAMLLINT) --strict --\n"
    "actionlint:\n"
    "\t$(ACTIONLINT)\n"
)


@pytest.mark.parametrize(
    ("makefile", "wired"),
    [
        pytest.param(GOOD, True, id="wired"),
        pytest.param(GOOD.replace(" --strict", ""), False, id="not strict"),
        pytest.param(GOOD.replace(" '*.yaml'", ""), False, id="one pathspec"),
        pytest.param(
            GOOD.replace(f"{PIPE} $(YAMLLINT) --strict --", "$(YAMLLINT) --strict ."),
            False,
            id="whole tree",
        ),
        pytest.param(
            GOOD.replace(
                "lint: yamllint actionlint", "lint:\n\ttrue\nother: yamllint actionlint"
            ),
            False,
            id="outside the lint closure",
        ),
        pytest.param(
            GOOD.replace("\t$(ACTIONLINT)", "\ttrue"), False, id="no actionlint"
        ),
        pytest.param(
            "lint:\n\t$(MAKE) gate\ngate: yamllint actionlint\n"
            + GOOD.split("\n", 1)[1],
            True,
            id="delegated",
        ),
    ],
)
def test_the_gate_judgement_detects_each_defect(makefile: str, wired: bool) -> None:
    """Each defect fails the judgement, and the compliant shapes pass it."""
    assert gate_is_wired(makefile) is wired


def workflow_with(*steps: dict[str, str]) -> dict[str, object]:
    """Return a one-job workflow with ``steps``."""
    return {"jobs": {"checks": {"steps": list(steps)}}}


PINNED = {"uses": f"{INSTALLER}@{'0123456789abcdef' * 2 + '01234567'}"}


@pytest.mark.parametrize(
    ("workflow", "installs"),
    [
        pytest.param(workflow_with(PINNED, {"run": "make lint"}), True, id="pinned"),
        pytest.param(
            workflow_with(PINNED, {"run": "make lint MDLINT=true"}), True, id="options"
        ),
        pytest.param(
            workflow_with(PINNED, {"run": "/usr/bin/make lint"}), True, id="absolute"
        ),
        pytest.param(
            workflow_with({"uses": f"{INSTALLER}@v1"}, {"run": "make lint"}),
            False,
            id="unpinned",
        ),
        pytest.param(workflow_with({"run": "make lint"}), False, id="missing"),
        pytest.param(
            workflow_with({"run": "make lint"}, PINNED), False, id="installed after"
        ),
        pytest.param(workflow_with(PINNED, {"run": "make test"}), False, id="no lint"),
    ],
)
def test_the_ci_judgement_detects_each_defect(
    workflow: dict[str, object], installs: bool
) -> None:
    """Each defect fails the judgement, and the compliant shapes pass it."""
    assert ci_installs_before_lint(workflow) is installs


def test_make_lint_runs_the_yaml_lint_gate() -> None:
    """The real Makefile wires the gate into ``lint``."""
    assert gate_is_wired((REPO_ROOT / "Makefile").read_text(encoding="utf-8"))


def test_ci_installs_the_tools_before_it_runs_the_gate() -> None:
    """The real CI workflow installs the tools, pinned, and never by hand."""
    text = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert ci_installs_before_lint(yaml.safe_load(text))
    assert not HAND_ROLLED.search(text)


def test_the_local_yamllint_configuration_extends_the_estate_base() -> None:
    """The local file sets nothing but ``extends``."""
    config = yaml.safe_load((REPO_ROOT / ".yamllint.yml").read_text(encoding="utf-8"))
    assert config == {"extends": ".yamllint-estate.yml"}


def test_actionlint_accepts_the_estate_runner_labels() -> None:
    """The ubicloud runner labels this repository uses are declared."""
    config = yaml.safe_load(
        (REPO_ROOT / ".github/actionlint.yaml").read_text(encoding="utf-8")
    )
    assert "ubicloud-*" in config["self-hosted-runner"]["labels"]
