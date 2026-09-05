"""Emit the release build matrix derived from ``extensions.toml``.

Printed as JSON suitable for ``strategy.matrix: ${{ fromJSON(...) }}`` so the
workflow cannot list a leg the configuration does not know about, or miss one
it does. With ``--select PACKAGE MAJOR TARGET`` it prints exactly one leg as
``key=value`` lines for ``$GITHUB_OUTPUT``, which the CI smoke build uses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pgx_config import Config, load_config

RUNNERS: dict[str, str] = {
    "x86_64-unknown-linux-gnu": "ubuntu-24.04",
    "aarch64-unknown-linux-gnu": "ubuntu-24.04-arm",
}
PLATFORMS: dict[str, str] = {
    "x86_64-unknown-linux-gnu": "linux/amd64",
    "aarch64-unknown-linux-gnu": "linux/arm64",
}


def build_matrix(config: Config) -> list[dict[str, str]]:
    """Return one entry per (extension, postgresql version, target)."""
    legs: list[dict[str, str]] = []
    for extension in config.extensions:
        for pg_version in config.postgresql_versions:
            for target in config.targets:
                if target not in RUNNERS:
                    raise SystemExit(f"no runner mapping for target {target}")
                legs.append(
                    {
                        "name": extension.name,
                        "package": extension.package,
                        "version": extension.version,
                        "repository": extension.repository,
                        "tag": extension.tag,
                        "commit": extension.commit,
                        "smoke_sql": extension.smoke_sql,
                        "postgresql": pg_version,
                        "target": target,
                        "runner": RUNNERS[target],
                        "platform": PLATFORMS[target],
                        "archive": config.archive_name(extension, pg_version, target),
                    }
                )
    return legs


def select_leg(
    legs: list[dict[str, str]], package: str, major: str, target: str
) -> dict[str, str]:
    """Return the single leg for ``package`` on PostgreSQL ``major`` and ``target``.

    Raises:
        LookupError: when zero or several legs match.
    """
    matches = [
        leg
        for leg in legs
        if leg["package"] == package
        and leg["postgresql"].split(".")[0] == major
        and leg["target"] == target
    ]
    if len(matches) != 1:
        raise LookupError(
            f"expected one leg for {package} pg{major} {target}, found {len(matches)}"
        )
    return matches[0]


def main(argv: list[str]) -> int:
    """Print the matrix JSON, or one leg as ``key=value`` lines with ``--select``."""
    usage = "usage: matrix.py extensions.toml [--select PACKAGE MAJOR TARGET]"
    if len(argv) not in (2, 6) or (len(argv) == 6 and argv[2] != "--select"):
        print(usage, file=sys.stderr)
        return 2
    legs = build_matrix(load_config(Path(argv[1])))
    if len(argv) == 2:
        print(json.dumps({"include": legs}, separators=(",", ":")))
        return 0
    try:
        leg = select_leg(legs, argv[3], argv[4], argv[5])
    except LookupError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    for key, value in leg.items():
        if "\n" in value:
            print(
                f"error: {key} must be a single line for GITHUB_OUTPUT", file=sys.stderr
            )
            return 1
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
