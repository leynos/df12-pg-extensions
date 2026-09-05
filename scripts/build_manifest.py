"""Build and verify the release manifest from published archives.

``build`` reads every archive the configuration expects from a directory,
verifies each ``.sha256`` sidecar, validates the archive layout, and writes
``manifest.json`` plus ``manifest.json.sha256``. It refuses to run when any
expected archive is missing or when the directory holds an archive the
configuration does not expect, so a release can never be published with a
leg silently absent or an asset nobody audited.

``verify`` re-reads a directory holding the manifest and every archive and
checks that the manifest describes exactly what is there.

``check-archive`` validates the sidecar and layout of individual archives
without needing the full set; CI uses it on a single smoke-built leg.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

from archive_rules import ArchiveError, validate_archive
from pgx_config import Config, ConfigError, load_config

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"


class ManifestError(ValueError):
    """Raised when the archives or manifest are inconsistent."""


def sha256_of(path: Path) -> str:
    """Return the SHA-256 of a file.

    Parameters
    ----------
    path : Path
        File to hash.

    Returns
    -------
    str
        Lower-case hex digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_sidecar(path: Path) -> str:
    """Return the digest recorded in a ``sha256sum``-format sidecar.

    Parameters
    ----------
    path : Path
        The archive whose ``<name>.sha256`` sidecar is read.

    Returns
    -------
    str
        Lower-case hex digest from the sidecar.

    Raises
    ------
    ManifestError
        When the sidecar is missing, names another file, or holds a
        malformed digest.
    """
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        raise ManifestError(f"missing checksum sidecar: {sidecar.name}")
    line = sidecar.read_text(encoding="utf-8").strip()
    parts = line.split()
    if len(parts) != 2 or parts[1].lstrip("*") != path.name:
        raise ManifestError(
            f"{sidecar.name}: expected '<sha256>  {path.name}', got {line!r}"
        )
    digest = parts[0].lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ManifestError(f"{sidecar.name}: malformed digest {parts[0]!r}")
    return digest


def write_sidecar(path: Path, digest: str) -> None:
    """Write a ``sha256sum``-format sidecar with a Unix newline.

    Parameters
    ----------
    path : Path
        The file the sidecar describes.
    digest : str
        Its lower-case hex SHA-256.
    """
    path.with_name(path.name + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )


def describe_archive(path: Path) -> tuple[str, int, tuple[str, ...]]:
    """Check an archive's sidecar and layout and describe it.

    Parameters
    ----------
    path : Path
        The archive.

    Returns
    -------
    tuple
        ``(digest, size, files)`` where ``files`` is the sorted file list.

    Raises
    ------
    ManifestError
        When the sidecar disagrees with the bytes or the layout is invalid.
    """
    digest = sha256_of(path)
    recorded = read_sidecar(path)
    if recorded != digest:
        raise ManifestError(
            f"{path.name}: sidecar digest {recorded} does not match {digest}"
        )
    try:
        summary = validate_archive(path)
    except ArchiveError as err:
        raise ManifestError(f"{path.name}: {err}") from err
    return digest, path.stat().st_size, summary.files


def _reject_unexpected_archives(dist: Path, expected: set[str]) -> None:
    present = {path.name for path in dist.glob("*.tar.gz")}
    unexpected = sorted(present - expected)
    if unexpected:
        raise ManifestError(
            "archives present that the configuration does not expect: "
            + ", ".join(unexpected)
        )


def collect_extensions(
    config: Config, dist: Path, tag: str, repository: str
) -> list[dict]:
    """Describe every expected archive in ``dist`` without touching a clock.

    Parameters
    ----------
    config : Config
        The validated configuration.
    dist : Path
        Directory holding the archives and sidecars.
    tag : str
        Release tag the URLs point at.
    repository : str
        ``owner/name`` of the publishing repository.

    Returns
    -------
    list of dict
        The ``extensions`` array of the manifest.

    Raises
    ------
    ManifestError
        When an expected archive is missing, an unexpected one is present,
        a sidecar disagrees, or a layout rule is broken.
    """
    base_url = f"https://github.com/{repository}/releases/download/{tag}"
    expected_names: set[str] = set()
    extensions = []
    for extension in config.extensions:
        artifacts = []
        for pg_version in config.postgresql_versions:
            for target in config.target_triples:
                name = config.archive_name(extension, pg_version, target)
                expected_names.add(name)
                path = dist / name
                if not path.is_file():
                    raise ManifestError(f"missing archive for {extension.name}: {name}")
                digest, size, files = describe_archive(path)
                artifacts.append(
                    {
                        "postgresql": pg_version,
                        "target": target,
                        "file": name,
                        "url": f"{base_url}/{name}",
                        "sha256": digest,
                        "size": size,
                        "files": list(files),
                    }
                )
        extensions.append(
            {
                "name": extension.name,
                "package": extension.package,
                "version": extension.version,
                "source": {
                    "repository": extension.repository,
                    "tag": extension.tag,
                    "commit": extension.commit,
                },
                "artifacts": artifacts,
            }
        )
    _reject_unexpected_archives(dist, expected_names)
    return extensions


def build_manifest(
    config: Config, dist: Path, tag: str, repository: str, generated_at: str
) -> dict:
    """Assemble the manifest for ``tag`` from the archives in ``dist``.

    Parameters
    ----------
    config : Config
        The validated configuration.
    dist : Path
        Directory holding the archives and sidecars.
    tag : str
        Release tag.
    repository : str
        ``owner/name`` of the publishing repository.
    generated_at : str
        Timestamp to record, supplied by the caller so the builder itself
        stays clock-free.

    Returns
    -------
    dict
        The manifest document.

    Raises
    ------
    ManifestError
        See :func:`collect_extensions`.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "release": tag,
        "generated_at": generated_at,
        "extensions": collect_extensions(config, dist, tag, repository),
    }


def verify_manifest(config: Config, dist: Path, tag: str, repository: str) -> int:
    """Check that ``dist/manifest.json`` matches the archives and configuration.

    Parameters
    ----------
    config : Config
        The validated configuration.
    dist : Path
        Directory holding the manifest, archives and sidecars.
    tag : str
        Release tag the manifest must name.
    repository : str
        ``owner/name`` of the publishing repository.

    Returns
    -------
    int
        Number of artifacts verified.

    Raises
    ------
    ManifestError
        When the manifest or its sidecar is missing, its digest is wrong,
        or its contents differ from the archives on disk.
    """
    manifest_path = dist / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ManifestError(f"missing {MANIFEST_NAME}")
    recorded = read_sidecar(manifest_path)
    if recorded != sha256_of(manifest_path):
        raise ManifestError(f"{MANIFEST_NAME}: sidecar digest does not match contents")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = collect_extensions(config, dist, tag, repository)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(
            f"{MANIFEST_NAME}: schema_version is {manifest.get('schema_version')!r}, "
            f"expected {SCHEMA_VERSION}"
        )
    if manifest.get("release") != tag:
        raise ManifestError(
            f"{MANIFEST_NAME}: release is {manifest.get('release')!r}, expected {tag!r}"
        )
    if manifest.get("extensions") != expected:
        raise ManifestError(
            f"{MANIFEST_NAME}: extensions do not match the archives on disk"
        )
    return sum(len(ext["artifacts"]) for ext in expected)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("extensions.toml"))
    parser.add_argument("--dist", type=Path, help="directory holding the archives")
    parser.add_argument("--tag", help="release tag, for example v1.0.0")
    parser.add_argument("--repository", help="owner/name of the publishing repository")
    parser.add_argument("command", choices=("build", "verify", "check-archive"))
    parser.add_argument(
        "archives", nargs="*", type=Path, help="archives for check-archive"
    )
    return parser


def _run(args: argparse.Namespace, config: Config) -> None:
    if args.command == "check-archive":
        if not args.archives:
            raise ManifestError("check-archive needs at least one archive path")
        for archive in args.archives:
            digest, size, files = describe_archive(archive)
            print(f"{archive.name}: sha256={digest} size={size} files={len(files)}")
        return
    if args.dist is None or args.tag is None or args.repository is None:
        raise ManifestError(f"{args.command} needs --dist, --tag and --repository")
    if args.command == "build":
        generated_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
        manifest = build_manifest(
            config, args.dist, args.tag, args.repository, generated_at
        )
        path = args.dist / MANIFEST_NAME
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        write_sidecar(path, sha256_of(path))
        count = sum(len(ext["artifacts"]) for ext in manifest["extensions"])
        print(f"wrote {path} describing {count} artifacts")
    else:
        count = verify_manifest(config, args.dist, args.tag, args.repository)
        print(f"verified {MANIFEST_NAME} against {count} artifacts")


def main(argv: list[str]) -> int:
    """Run ``build``, ``verify`` or ``check-archive``.

    Parameters
    ----------
    argv : list of str
        Command-line arguments without the program name.

    Returns
    -------
    int
        Process exit status; ``1`` on a configuration or manifest error.
    """
    args = _parser().parse_args(argv)
    try:
        _run(args, load_config(args.config))
    except (ConfigError, ManifestError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
