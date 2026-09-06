"""Emit the release build matrix derived from ``extensions.toml``.

Printed as JSON suitable for ``strategy.matrix: ${{ fromJSON(...) }}`` so the
workflow cannot list a leg the configuration does not know about, or miss one
it does. With ``--smoke-leg`` it prints the configured smoke leg as
``key=value`` lines for ``$GITHUB_OUTPUT``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pgx_config import Config, ConfigError, load_config


def build_matrix(config: Config) -> list[dict[str, str]]:
    """Return one matrix entry per (extension, PostgreSQL version, target).

    Parameters
    ----------
    config : Config
        The validated configuration.

    Returns
    -------
    list of dict
        Entries whose keys are the environment the build scripts need:
        ``name``, ``package``, ``version``, ``repository``, ``tag``,
        ``commit``, ``smoke_sql``, ``postgresql``, ``releases_url``, ``max_glibc``,
        ``target``, ``runner``, ``platform`` and ``archive``.
    """
    return [
        {
            "name": extension.name,
            "package": extension.package,
            "version": extension.version,
            "repository": extension.repository,
            "tag": extension.tag,
            "commit": extension.commit,
            "smoke_sql": extension.smoke_sql,
            "postgresql": pg_version,
            "releases_url": config.releases_url,
            "max_glibc": config.max_glibc,
            "target": target.triple,
            "runner": target.runner,
            "platform": target.platform,
            "archive": config.archive_name(extension, pg_version, target.triple),
        }
        for extension in config.extensions
        for pg_version in config.postgresql_versions
        for target in config.targets
    ]


def smoke_leg(config: Config) -> dict[str, str]:
    """Return the single matrix entry selected by ``[smoke]``.

    Parameters
    ----------
    config : Config
        The validated configuration.

    Returns
    -------
    dict
        The matching entry from :func:`build_matrix`.

    Raises
    ------
    LookupError
        When zero or several entries match the smoke selection.
    """
    smoke = config.smoke
    matches = [
        leg
        for leg in build_matrix(config)
        if leg["package"] == smoke.package
        and leg["postgresql"].split(".")[0] == str(smoke.postgresql_major)
        and leg["target"] == smoke.target
    ]
    if len(matches) != 1:
        raise LookupError(
            f"expected one leg for {smoke.package} pg{smoke.postgresql_major} "
            f"{smoke.target}, found {len(matches)}"
        )
    return matches[0]


def main(argv: list[str]) -> int:
    """Print the matrix JSON, or the smoke leg as ``key=value`` lines.

    Parameters
    ----------
    argv : list of str
        ``[config_path]`` or ``[config_path, "--smoke-leg"]``.

    Returns
    -------
    int
        Process exit status: ``0`` on success, ``1`` when the configuration
        cannot be loaded or the smoke leg cannot be emitted, ``2`` on usage
        errors.
    """
    usage = "usage: matrix.py extensions.toml [--smoke-leg]"
    if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "--smoke-leg"):
        print(usage, file=sys.stderr)
        return 2
    try:
        config = load_config(Path(argv[1]))
    except ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if len(argv) == 2:
        print(json.dumps({"include": build_matrix(config)}, separators=(",", ":")))
        return 0
    try:
        leg = smoke_leg(config)
    except LookupError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    multi_line = [key for key, value in leg.items() if "\n" in value]
    if multi_line:
        print(
            f"error: {', '.join(multi_line)} must be a single line for GITHUB_OUTPUT",
            file=sys.stderr,
        )
        return 1
    for key, value in leg.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
