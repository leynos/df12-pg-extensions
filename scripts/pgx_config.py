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


class ConfigError(ValueError):
    """Raised when ``extensions.toml`` is malformed."""


@dataclasses.dataclass(frozen=True)
class Extension:
    """One extension pinned to an upstream tag and commit."""

    name: str
    package: str
    version: str
    repository: str
    tag: str
    commit: str
    smoke_sql: str


@dataclasses.dataclass(frozen=True)
class Config:
    """Validated contents of ``extensions.toml``."""

    postgresql_versions: tuple[str, ...]
    targets: tuple[str, ...]
    releases_url: str
    build_image: str
    extensions: tuple[Extension, ...]

    def archive_name(self, extension: Extension, pg_version: str, target: str) -> str:
        """Return the release asset name for one build leg.

        Example: ``pgvector-0.8.6-pg17.11.0-x86_64-unknown-linux-gnu.tar.gz``.
        """
        return f"{extension.package}-{extension.version}-pg{pg_version}-{target}.tar.gz"


def _require(table: dict, key: str, context: str) -> object:
    if key not in table:
        raise ConfigError(f"{context}: missing required key '{key}'")
    return table[key]


def _require_str(table: dict, key: str, context: str, pattern: re.Pattern) -> str:
    value = _require(table, key, context)
    if not isinstance(value, str) or not pattern.match(value):
        raise ConfigError(
            f"{context}: '{key}' must match {pattern.pattern}, got {value!r}"
        )
    return value


def _require_url(table: dict, key: str, context: str) -> str:
    value = _require(table, key, context)
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ConfigError(f"{context}: '{key}' must be an https:// URL, got {value!r}")
    return value.rstrip("/")


def _require_list(
    table: dict, key: str, context: str, pattern: re.Pattern
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


def _parse_extension(table: dict, index: int) -> Extension:
    context = f"extensions[{index}]"
    smoke_sql = _require(table, "smoke_sql", context)
    if not isinstance(smoke_sql, str) or not smoke_sql.strip():
        raise ConfigError(f"{context}: 'smoke_sql' must be a non-empty string")
    return Extension(
        name=_require_str(table, "name", context, _NAME_RE),
        package=_require_str(table, "package", context, _PACKAGE_RE),
        version=_require_str(table, "version", context, _VERSION_RE),
        repository=_require_url(table, "repository", context),
        tag=_require_str(table, "tag", context, re.compile(r"^\S+$")),
        commit=_require_str(table, "commit", context, _COMMIT_RE),
        smoke_sql=smoke_sql.strip(),
    )


def parse_config(text: str) -> Config:
    """Parse and validate the TOML text of ``extensions.toml``.

    Raises:
        ConfigError: when a required key is missing or malformed.
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
    postgresql = _require(raw, "postgresql", "top level")
    build = _require(raw, "build", "top level")
    extensions_raw = _require(raw, "extensions", "top level")
    if not isinstance(postgresql, dict) or not isinstance(build, dict):
        raise ConfigError("'postgresql' and 'build' must be tables")
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
    return Config(
        postgresql_versions=_require_list(
            postgresql, "versions", "postgresql", _VERSION_RE
        ),
        targets=_require_list(postgresql, "targets", "postgresql", _TARGET_RE),
        releases_url=_require_url(postgresql, "releases_url", "postgresql"),
        build_image=image,
        extensions=extensions,
    )


def load_config(path: Path) -> Config:
    """Read and validate ``extensions.toml`` from ``path``."""
    return parse_config(path.read_text(encoding="utf-8"))
