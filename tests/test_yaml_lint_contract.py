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
import shutil
import stat
import subprocess
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
    """Read the rules of a Makefile without expanding anything.

    Parameters
    ----------
    makefile : str
        The Makefile text.

    Returns
    -------
    list[tuple[list[str], list[str], list[str]]]
        One ``(targets, prerequisites, recipe lines)`` triple per rule.
    """
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
    """Collect the recipe lines reachable from ``lint``.

    Parameters
    ----------
    makefile : str
        The Makefile text.

    Returns
    -------
    list[str]
        Every recipe line reachable through prerequisites and literal
        ``$(MAKE) target`` calls.
    """
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


def is_bypassed(line: str) -> bool:
    """Say whether a recipe line leaves its tool's failure unable to fail ``make``.

    Parameters
    ----------
    line : str
        One recipe line.

    Returns
    -------
    bool
        ``True`` for a leading ``-`` or ``!``, ``|| true``, ``|| :``, or a
        command followed by ``;``.
    """
    return bool(
        line.startswith(("-", "!", "@-")) or re.search(r"\|\|\s*(true|:)\b|;", line)
    )


def gate_is_wired(makefile: str) -> bool:
    """Say whether the lint closure runs strict yamllint over Git's list and actionlint.

    Parameters
    ----------
    makefile : str
        The Makefile text.

    Returns
    -------
    bool
        ``True`` when both tools run in the lint closure and neither failure
        is bypassed.
    """
    lines = lint_closure(makefile)
    yamllint = any(
        line.startswith("git ls-files")
        and "'*.yml'" in line
        and "'*.yaml'" in line
        and "| xargs" in line
        and "$(YAMLLINT)" in line
        and "--strict" in line
        and not is_bypassed(line)
        for line in lines
    )
    actionlint = any(
        line.startswith("$(ACTIONLINT)") and not is_bypassed(line) for line in lines
    )
    return yamllint and actionlint


def ci_installs_before_lint(workflow: dict[str, object]) -> bool:
    """Say whether each job that runs ``make lint`` installs the tools first.

    Parameters
    ----------
    workflow : dict[str, object]
        The parsed workflow.

    Returns
    -------
    bool
        ``True`` when some job runs ``make lint``, every such job pins the
        installer by commit SHA before it, and no lint step ignores failure.
    """
    ran = False
    match workflow["jobs"]:
        case dict() as jobs:
            pass
        case other:
            message = f"workflow jobs must be a mapping, got {type(other).__name__}"
            raise AssertionError(message)
    for spec in jobs.values():
        installed = False
        for step in spec.get("steps", []):
            uses = step.get("uses", "")
            if uses.startswith(INSTALLER + "@"):
                installed = SHA_PIN.match(uses.partition("@")[2]) is not None
            command = step.get("run", "")
            if re.match(r"(/usr/bin/)?make( \S+)* lint( |$)", command):
                ran = True
                if (
                    not installed
                    or step.get("continue-on-error")
                    or is_bypassed(command)
                ):
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


def workflow_with(*steps: dict[str, object]) -> dict[str, object]:
    """Build a one-job workflow.

    Parameters
    ----------
    *steps : dict[str, object]
        The job's steps.

    Returns
    -------
    dict[str, object]
        A workflow whose only job holds ``steps``.
    """
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
        pytest.param(
            workflow_with(PINNED, {"run": "make lint || true"}), False, id="ignored"
        ),
        pytest.param(
            workflow_with(PINNED, {"run": "make lint", "continue-on-error": True}),
            False,
            id="continue on error",
        ),
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


# --- the boundary: run the targets in a controlled repository ---------------


def run_make(target: str, cwd: Path, **tools: str) -> subprocess.CompletedProcess[str]:
    """Run one Make target of this repository's Makefile inside ``cwd``.

    Parameters
    ----------
    target : str
        The target to build.
    cwd : Path
        The repository the recipe runs in.
    **tools : str
        Make variable overrides, such as ``YAMLLINT`` and ``ACTIONLINT``.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The finished process, output captured.
    """
    return subprocess.run(
        ["make", "-f", str(REPO_ROOT / "Makefile"), target]
        + [f"{name}={value}" for name, value in tools.items()],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def fake_tool(path: Path, exit_code: int) -> Path:
    """Write an executable stand-in that logs its arguments and exits.

    Parameters
    ----------
    path : Path
        Where to write the tool; its log is ``<path>.log``.
    exit_code : int
        The status the tool exits with.

    Returns
    -------
    Path
        ``path``, made executable.
    """
    path.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{path}.log"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def git_repo(root: Path, tracked: dict[str, str], untracked: dict[str, str]) -> Path:
    """Create a Git repository with tracked and untracked files.

    Parameters
    ----------
    root : Path
        The directory to initialise.
    tracked : dict[str, str]
        Files to create and stage, by path.
    untracked : dict[str, str]
        Files to create and leave untracked, by path.

    Returns
    -------
    Path
        ``root``.
    """
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for name, text in {**tracked, **untracked}.items():
        (root / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", *tracked], cwd=root, check=True)
    return root


def test_make_yamllint_lints_the_tracked_yaml_only_and_strictly(tmp_path: Path) -> None:
    """The recipe hands ``--strict`` and exactly the tracked YAML to the tool."""
    repo = git_repo(
        tmp_path / "repo", {"a.yml": "a: 1\n", "b.yaml": "b: 1\n"}, {"c.yml": "c: 1\n"}
    )
    tool = fake_tool(tmp_path / "yamllint", 0)

    result = run_make("yamllint", repo, YAMLLINT=str(tool))

    assert result.returncode == 0, result.stderr
    logged = Path(f"{tool}.log").read_text(encoding="utf-8").split()
    assert logged[0] == "--strict"
    assert sorted(logged[1:]) == ["--", "a.yml", "b.yaml"]


@pytest.mark.parametrize("target", ["yamllint", "actionlint"])
def test_a_failing_tool_fails_its_make_target(tmp_path: Path, target: str) -> None:
    """The tool's exit status reaches Make, so a finding stops the gate."""
    repo = git_repo(tmp_path / "repo", {"a.yml": "a: 1\n"}, {})
    tool = fake_tool(tmp_path / target, 1)
    variable = target.upper()

    result = run_make(target, repo, **{variable: str(tool)})

    assert result.returncode != 0


def test_make_lint_reaches_both_tools_before_anything_else_can_hide_them(
    tmp_path: Path,
) -> None:
    """A dry run of ``lint`` names both tools, so the closure includes them."""
    repo = git_repo(tmp_path / "repo", {"a.yml": "a: 1\n"}, {})

    result = subprocess.run(
        ["make", "-n", "-f", str(REPO_ROOT / "Makefile"), "lint"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert "yamllint --strict" in result.stdout
    assert "\nactionlint\n" in f"\n{result.stdout}"


@pytest.mark.skipif(
    shutil.which("yamllint") is None, reason="yamllint is not installed"
)
def test_the_real_yamllint_fails_strictly_on_a_tracked_warning_only(
    tmp_path: Path,
) -> None:
    """A tracked finding fails the gate and an untracked one does not."""
    repo = git_repo(
        tmp_path / "repo",
        {"good.yml": "key: value\n", "bad.yml": "key: value \n"},
        {"untracked-bad.yml": "key: value \n"},
    )
    for name in (".yamllint-estate.yml", ".yamllint.yml"):
        shutil.copy(REPO_ROOT / name, repo / name)
    subprocess.run(
        ["git", "add", ".yamllint-estate.yml", ".yamllint.yml"], cwd=repo, check=True
    )

    failing = run_make("yamllint", repo)
    subprocess.run(
        ["git", "rm", "-q", "--cached", "-f", "bad.yml"], cwd=repo, check=True
    )
    passing = run_make("yamllint", repo)

    assert failing.returncode != 0
    assert "bad.yml" in failing.stdout
    assert "untracked-bad.yml" not in failing.stdout
    assert passing.returncode == 0, passing.stdout


CANONICAL_ESTATE_BASE = {
    "extends": "default",
    "ignore": "**/charts/**/templates/\n",
    "rules": {
        "document-start": "disable",
        "comments": {"min-spaces-from-content": 1},
        "indentation": {
            "spaces": "consistent",
            "indent-sequences": "consistent",
            "check-multi-line-strings": False,
        },
        "braces": {"min-spaces-inside": 0, "max-spaces-inside": 1},
        "brackets": {"min-spaces-inside": 0, "max-spaces-inside": 1},
        "line-length": {
            "max": 120,
            "allow-non-breakable-words": True,
            "allow-non-breakable-inline-mappings": True,
            "ignore": ".github/workflows/\n.github/actions/\n",
        },
        "truthy": {"allowed-values": ["true", "false"], "check-keys": False},
    },
}


def test_the_estate_base_is_the_canonical_configuration() -> None:
    """Every rule setting the gate depends on is the estate's.

    A mutation that drops or changes any rule, heading or value fails here,
    including a value that survives under a sibling rule. Comments may differ;
    the Concordat
    ``yaml-lint-baseline`` rule audits the same file by digest.
    """
    text = (REPO_ROOT / ".yamllint-estate.yml").read_text(encoding="utf-8")
    assert yaml.safe_load(text) == CANONICAL_ESTATE_BASE
