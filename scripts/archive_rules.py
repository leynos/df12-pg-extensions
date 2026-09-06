"""Layout rules for extension archives.

An archive is a gzip tar whose entries are regular files under exactly two
prefixes relative to the PostgreSQL install root: ``lib/`` for shared objects
and ``share/extension/`` for control and SQL files. The consumer hook in
pg-embed-setup-unpriv enforces the same rules before it writes anything, so
the publisher must never ship an archive the hook would refuse.
"""

from __future__ import annotations

import dataclasses
import posixpath
import tarfile
from collections.abc import Iterable
from pathlib import Path

ALLOWED_PREFIXES: tuple[str, ...] = ("lib/", "share/extension/")


class ArchiveError(ValueError):
    """Raised when an archive violates the layout rules."""


@dataclasses.dataclass(frozen=True)
class ArchiveSummary:
    """Regular files found in a valid archive.

    Attributes
    ----------
    files : tuple of str
        Canonical paths relative to the install root, sorted.
    """

    files: tuple[str, ...]


def _normalised(path: str) -> str | None:
    """Return the canonical form of ``path`` if it is a plain relative path.

    The canonical form is a ``/``-separated path with no leading ``./``,
    empty components, ``.``, ``..`` or backslashes. ``None`` means the path
    cannot be accepted whatever prefix it claims.
    """
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        return None
    parts = path.split("/")
    if parts and parts[0] == ".":
        parts = parts[1:]
    if any(part in ("", ".", "..") for part in parts):
        return None
    return "/".join(parts)


def classify_path(path: str) -> str | None:
    """Return the canonical path when ``path`` lies under an allowed prefix.

    Parameters
    ----------
    path : str
        A tar entry name.

    Returns
    -------
    str or None
        The canonical relative path, or ``None`` when the entry is not a
        regular-file path the hook accepts.

    Examples
    --------
    >>> classify_path("./lib/vector.so")
    'lib/vector.so'
    >>> classify_path("lib/../bin/psql") is None
    True
    >>> classify_path("include/server/vector.h") is None
    True
    """
    canonical = _normalised(path)
    if canonical is None:
        return None
    for prefix in ALLOWED_PREFIXES:
        if canonical.startswith(prefix) and len(canonical) > len(prefix):
            # Files must sit directly under lib/ or anywhere under
            # share/extension/; a nested lib/bitcode tree is refused.
            if prefix == "lib/" and "/" in canonical[len(prefix) :]:
                return None
            return canonical
    return None


def _validate_directory(member: tarfile.TarInfo) -> None:
    canonical = _normalised(member.name)
    if canonical is None:
        raise ArchiveError(f"directory entry has an unacceptable path: {member.name!r}")
    inside = any(
        (canonical + "/").startswith(prefix) or prefix.startswith(canonical + "/")
        for prefix in ALLOWED_PREFIXES
    )
    if not inside:
        raise ArchiveError(f"directory outside the allowed prefixes: {member.name!r}")


def _validate_file(member: tarfile.TarInfo, seen: list[str]) -> str:
    if not member.isreg():
        raise ArchiveError(
            f"only regular files and directories are allowed, got {member.name!r} "
            f"(type {member.type!r})"
        )
    accepted = classify_path(member.name)
    if accepted is None:
        raise ArchiveError(f"file outside lib/ or share/extension/: {member.name!r}")
    if accepted in seen:
        raise ArchiveError(f"duplicate entry: {accepted!r}")
    return accepted


def _require_control_file(files: list[str]) -> None:
    if not files:
        raise ArchiveError("archive contains no files")
    has_control = any(
        name.startswith("share/extension/") and name.endswith(".control")
        for name in files
    )
    if not has_control:
        raise ArchiveError("archive has no share/extension/*.control file")


def validate_members(members: Iterable[tarfile.TarInfo]) -> ArchiveSummary:
    """Validate tar members and return the regular files they contain.

    Parameters
    ----------
    members : iterable of tarfile.TarInfo
        The archive's members in archive order.

    Returns
    -------
    ArchiveSummary
        The accepted regular files, sorted.

    Raises
    ------
    ArchiveError
        On the first member that breaks a rule, or when no control file is
        present.
    """
    files: list[str] = []
    for member in members:
        if member.isdir():
            _validate_directory(member)
            continue
        files.append(_validate_file(member, files))
    _require_control_file(files)
    return ArchiveSummary(files=tuple(sorted(files)))


def validate_archive(path: Path) -> ArchiveSummary:
    """Open a gzip tar at ``path`` and validate its layout.

    Parameters
    ----------
    path : Path
        The archive to inspect.

    Returns
    -------
    ArchiveSummary
        The accepted regular files, sorted.

    Raises
    ------
    ArchiveError
        When the file cannot be opened, is not a readable gzip tar, or breaks a
        layout rule.
    """
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            return validate_members(archive.getmembers())
    except tarfile.TarError as err:
        raise ArchiveError(f"{path.name}: not a readable gzip tar: {err}") from err
    except OSError as err:
        raise ArchiveError(f"{path.name}: cannot be opened: {err}") from err


def basename_of(path: str) -> str:
    """Return the final component of a ``/``-separated path.

    Parameters
    ----------
    path : str
        A ``/``-separated path.

    Returns
    -------
    str
        The last component.
    """
    return posixpath.basename(path)
