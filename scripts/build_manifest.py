"""Build and verify the release manifest from published archives.

``build`` reads every archive the configuration expects from a directory,
verifies each ``.sha256`` sidecar, validates the archive layout, and writes
``manifest.json`` plus ``manifest.json.sha256``. It refuses to run when any
expected archive is missing, so a release can never be published with a leg
silently absent.

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
    """Return the lower-case hex SHA-256 of ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_sidecar(path: Path) -> str:
    """Return the digest recorded in a ``sha256sum``-format sidecar."""
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
    """Write a ``sha256sum``-format sidecar with a Unix newline."""
    path.with_name(path.name + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )


def describe_archive(path: Path) -> tuple[str, int, tuple[str, ...]]:
    """Return (digest, size, files) for ``path`` after checking its sidecar."""
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


def build_manifest(config: Config, dist: Path, tag: str, repository: str) -> dict:
    """Assemble the manifest for ``tag`` from the archives in ``dist``."""
    base_url = f"https://github.com/{repository}/releases/download/{tag}"
    extensions = []
    for extension in config.extensions:
        artifacts = []
        for pg_version in config.postgresql_versions:
            for target in config.targets:
                name = config.archive_name(extension, pg_version, target)
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
    return {
        "schema_version": SCHEMA_VERSION,
        "release": tag,
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "extensions": extensions,
    }


def verify_manifest(config: Config, dist: Path, tag: str, repository: str) -> int:
    """Check that ``dist/manifest.json`` matches the archives and configuration.

    Returns the number of artifacts verified.
    """
    manifest_path = dist / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ManifestError(f"missing {MANIFEST_NAME}")
    recorded = read_sidecar(manifest_path)
    if recorded != sha256_of(manifest_path):
        raise ManifestError(f"{MANIFEST_NAME}: sidecar digest does not match contents")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = build_manifest(config, dist, tag, repository)
    for key in ("schema_version", "release"):
        if manifest.get(key) != expected[key]:
            raise ManifestError(
                f"{MANIFEST_NAME}: {key} is {manifest.get(key)!r}, "
                f"expected {expected[key]!r}"
            )
    if manifest.get("extensions") != expected["extensions"]:
        raise ManifestError(
            f"{MANIFEST_NAME}: extensions do not match the archives on disk"
        )
    return sum(len(ext["artifacts"]) for ext in expected["extensions"])


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


def main(argv: list[str]) -> int:
    """Entry point for ``build`` and ``verify``."""
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check-archive":
            if not args.archives:
                raise ManifestError("check-archive needs at least one archive path")
            for archive in args.archives:
                digest, size, files = describe_archive(archive)
                print(f"{archive.name}: sha256={digest} size={size} files={len(files)}")
            return 0
        if args.dist is None or args.tag is None or args.repository is None:
            raise ManifestError(f"{args.command} needs --dist, --tag and --repository")
        if args.command == "build":
            manifest = build_manifest(config, args.dist, args.tag, args.repository)
            path = args.dist / MANIFEST_NAME
            path.write_text(
                json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8"
            )
            write_sidecar(path, sha256_of(path))
            count = sum(len(ext["artifacts"]) for ext in manifest["extensions"])
            print(f"wrote {path} describing {count} artifacts")
        else:
            count = verify_manifest(config, args.dist, args.tag, args.repository)
            print(f"verified {MANIFEST_NAME} against {count} artifacts")
    except (ConfigError, ManifestError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
