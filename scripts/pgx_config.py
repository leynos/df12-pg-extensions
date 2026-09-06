"""Load and validate ``extensions.toml``.

The configuration is the single source of truth for what the release
workflow builds. Every script and every workflow contract reads it through
this module so that a typo in the file fails fast with a clear message rather
than producing a release that is silently missing a leg.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
from pathlib import Path

SUPPORTED_SCHEMA_VERSION = 1
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_PACKAGE_RE = re.compile(r"^[a-z0-9_.-]+$")
_TARGET_RE = re.compile(r"^[a-z0-9_]+-[a-z0-9_-]+$")
_PLATFORM_RE = re.compile(r"^linux/[a-z0-9]+(/v\d+)?$")
_RUNNER_RE = re.compile(r"^[a-z0-9.-]+$")
_TAG_RE = re.compile(r"^\S+$")


class ConfigError(ValueError):
    """Raised when ``extensions.toml`` is malformed."""


@dataclasses.dataclass(frozen=True)
class Target:
    """A build target and where it is built.

    Attributes
    ----------
    triple : str
        Rust target triple, matching the Theseus asset names.
    runner : str
        GitHub-hosted runner label that builds this target natively.
    platform : str
        Container platform passed to ``docker run --platform``.
    """

    triple: str
    runner: str
    platform: str


@dataclasses.dataclass(frozen=True)
class SmokeLeg:
    """The one leg a pull request builds and smoke-tests.

    Attributes
    ----------
    package : str
        Upstream package name of the extension to build.
    postgresql_major : int
        PostgreSQL major whose configured release is used.
    target : str
        Target triple to build for.
    """

    package: str
    postgresql_major: int
    target: str


@dataclasses.dataclass(frozen=True)
class Extension:
    """One extension pinned to an upstream tag and commit.

    Attributes
    ----------
    name : str
        ``CREATE EXTENSION`` name.
    package : str
        Upstream package name, used in archive file names.
    version : str
        Extension version, for example ``0.8.6``.
    repository : str
        Upstream ``https://`` repository URL.
    tag : str
        Upstream tag to check out.
    commit : str
        Forty-hex commit the tag must resolve to.
    smoke_sql : str
        SQL that must succeed after ``CREATE EXTENSION``.
    """

    name: str
    package: str
    version: str
    repository: str
    tag: str
    commit: str
    smoke_sql: str


@dataclasses.dataclass(frozen=True)
class Config:
    """Validated contents of ``extensions.toml``.

    Attributes
    ----------
    postgresql_versions : tuple of str
        Theseus releases to build against.
    targets : tuple of Target
        Targets to build for, in file order.
    releases_url : str
        Base URL of the Theseus release downloads.
    build_image : str
        Digest-pinned container image the build runs in.
    max_glibc : str
        Highest ``GLIBC_x.y`` symbol version an archive may reference.
    smoke : SmokeLeg
        The leg a pull request exercises end to end.
    extensions : tuple of Extension
        Extensions to build.
    """

    postgresql_versions: tuple[str, ...]
    targets: tuple[Target, ...]
    releases_url: str
    build_image: str
    max_glibc: str
    smoke: SmokeLeg
    extensions: tuple[Extension, ...]

    @property
    def target_triples(self) -> tuple[str, ...]:
        """Return the configured target triples in file order."""
        return tuple(target.triple for target in self.targets)

    def archive_name(self, extension: Extension, pg_version: str, target: str) -> str:
        """Return the release asset name for one build leg.

        Parameters
        ----------
        extension : Extension
            The extension the archive holds.
        pg_version : str
            Theseus release the archive was built against.
        target : str
            Target triple the archive was built for.

        Returns
        -------
        str
            For example
            ``pgvector-0.8.6-pg17.11.0-x86_64-unknown-linux-gnu.tar.gz``.
        """
        return f"{extension.package}-{extension.version}-pg{pg_version}-{target}.tar.gz"


def _require(table: dict[str, object], key: str, context: str) -> object:
    if key not in table:
        raise ConfigError(f"{context}: missing required key '{key}'")
    return table[key]


def _require_table(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{context}: must be a table, got {type(value).__name__}")
    return value


def _require_str(
    table: dict[str, object], key: str, context: str, pattern: re.Pattern[str]
) -> str:
    value = _require(table, key, context)
    if not isinstance(value, str) or not pattern.match(value):
        raise ConfigError(
            f"{context}: '{key}' must match {pattern.pattern}, got {value!r}"
        )
    return value


def _require_url(table: dict[str, object], key: str, context: str) -> str:
    value = _require(table, key, context)
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ConfigError(f"{context}: '{key}' must be an https:// URL, got {value!r}")
    return value.rstrip("/")


def _require_list(
    table: dict[str, object], key: str, context: str, pattern: re.Pattern[str]
) -> tuple[str, ...]:
    value = _require(table, key, context)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{context}: '{key}' must be a non-empty list")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not pattern.match(item):
            raise ConfigError(
                f"{context}: '{key}' entry {item!r} must match {pattern.pattern}"
            )
        if item in items:
            raise ConfigError(f"{context}: '{key}' lists {item!r} twice")
        items.append(item)
    return tuple(items)


def _parse_extension(raw: object, index: int) -> Extension:
    context = f"extensions[{index}]"
    table = _require_table(raw, context)
    smoke_sql = _require(table, "smoke_sql", context)
    if not isinstance(smoke_sql, str) or not smoke_sql.strip():
        raise ConfigError(f"{context}: 'smoke_sql' must be a non-empty string")
    return Extension(
        name=_require_str(table, "name", context, _NAME_RE),
        package=_require_str(table, "package", context, _PACKAGE_RE),
        version=_require_str(table, "version", context, _VERSION_RE),
        repository=_require_url(table, "repository", context),
        tag=_require_str(table, "tag", context, _TAG_RE),
        commit=_require_str(table, "commit", context, _COMMIT_RE),
        smoke_sql=smoke_sql.strip(),
    )


def _parse_targets(raw: object) -> tuple[Target, ...]:
    table = _require_table(raw, "targets")
    if not table:
        raise ConfigError("targets: at least one target is required")
    targets: list[Target] = []
    for triple, spec in table.items():
        context = f"targets.{triple}"
        if not _TARGET_RE.match(triple):
            raise ConfigError(f"{context}: triple must match {_TARGET_RE.pattern}")
        spec_table = _require_table(spec, context)
        targets.append(
            Target(
                triple=triple,
                runner=_require_str(spec_table, "runner", context, _RUNNER_RE),
                platform=_require_str(spec_table, "platform", context, _PLATFORM_RE),
            )
        )
    return tuple(targets)


def _parse_smoke(
    raw: object,
    extensions: tuple[Extension, ...],
    config_versions: tuple[str, ...],
    targets: tuple[Target, ...],
) -> SmokeLeg:
    table = _require_table(raw, "smoke")
    package = _require_str(table, "package", "smoke", _PACKAGE_RE)
    if package not in {extension.package for extension in extensions}:
        raise ConfigError(f"smoke: package {package!r} is not a configured extension")
    major = _require(table, "postgresql_major", "smoke")
    if not isinstance(major, int) or isinstance(major, bool) or major <= 0:
        raise ConfigError("smoke: 'postgresql_major' must be a positive integer")
    if not any(version.split(".")[0] == str(major) for version in config_versions):
        raise ConfigError(f"smoke: no configured PostgreSQL version has major {major}")
    target = _require_str(table, "target", "smoke", _TARGET_RE)
    if target not in {candidate.triple for candidate in targets}:
        raise ConfigError(f"smoke: target {target!r} is not a configured target")
    return SmokeLeg(package=package, postgresql_major=major, target=target)


def parse_config(text: str) -> Config:
    """Parse and validate the TOML text of ``extensions.toml``.

    Parameters
    ----------
    text : str
        The file contents.

    Returns
    -------
    Config
        The validated configuration.

    Raises
    ------
    ConfigError
        When the TOML is invalid or a required key is missing or malformed.
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"invalid TOML: {err}") from err
    if raw.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ConfigError(
            f"schema_version must be {SUPPORTED_SCHEMA_VERSION}, "
            f"got {raw.get('schema_version')!r}"
        )
    postgresql = _require_table(_require(raw, "postgresql", "top level"), "postgresql")
    build = _require_table(_require(raw, "build", "top level"), "build")
    extensions_raw = _require(raw, "extensions", "top level")
    if not isinstance(extensions_raw, list) or not extensions_raw:
        raise ConfigError("'extensions' must be a non-empty array of tables")
    extensions = tuple(
        _parse_extension(table, i) for i, table in enumerate(extensions_raw)
    )
    names = [ext.name for ext in extensions]
    if len(set(names)) != len(names):
        raise ConfigError("extension names must be unique")
    image = _require(build, "image", "build")
    if not isinstance(image, str) or "@sha256:" not in image:
        raise ConfigError("build.image must be pinned by digest (…@sha256:…)")
    max_glibc = _require_str(build, "max_glibc", "build", re.compile(r"^\d+\.\d+$"))
    versions = _require_list(postgresql, "versions", "postgresql", _VERSION_RE)
    targets = _parse_targets(_require(raw, "targets", "top level"))
    return Config(
        postgresql_versions=versions,
        targets=targets,
        releases_url=_require_url(postgresql, "releases_url", "postgresql"),
        build_image=image,
        max_glibc=max_glibc,
        smoke=_parse_smoke(
            _require(raw, "smoke", "top level"), extensions, versions, targets
        ),
        extensions=extensions,
    )


def load_config(path: Path) -> Config:
    """Read and validate ``extensions.toml`` from ``path``.

    Parameters
    ----------
    path : Path
        Location of the configuration file.

    Returns
    -------
    Config
        The validated configuration.

    Raises
    ------
    ConfigError
        When the file cannot be read or is malformed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    return parse_config(text)
