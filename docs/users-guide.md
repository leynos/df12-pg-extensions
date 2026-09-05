# Users guide

This guide is for a repository that wants an out-of-tree PostgreSQL
extension inside the embedded cluster that
[`pg-embed-setup-unpriv`](https://github.com/leynos/pg-embed-setup-unpriv)
starts for its tests.

## What a release provides

Each versioned release (`vMAJOR.MINOR.PATCH`) publishes, for every
extension, PostgreSQL version and target in `extensions.toml`:

- an archive named `<package>-<version>-pg<postgresql>-<target>.tar.gz`;
- a `sha256sum`-format sidecar named `<archive>.sha256`;

and one `manifest.json` with its own `manifest.json.sha256`. The manifest
records every archive's digest, size, URL and file list, so a consumer
that pins the manifest digest pins every archive.

Releases are immutable. Nothing is ever rebuilt under an existing tag.

## Supported PostgreSQL versions and targets

The `[postgresql].versions` and `[targets]` tables in `extensions.toml` are
authoritative. At the time of writing:

| Extension | Version | PostgreSQL (Theseus release) | Targets |
| --- | --- | --- | --- |
| `vector` (pgvector) | 0.8.6 | 16.15.0, 17.11.0, 18.6.0 | `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu` |

The consumer hook matches on the PostgreSQL major and minor exactly. Pin
`PG_VERSION_REQ` to one of the listed Theseus releases; a cluster on
another minor gets `ExtensionUnavailable` rather than a silently wrong
module.

## Configuring a consumer

Set four environment variables where the tests run (a `.env` file, the CI
job `env:` block, or the test harness):

```bash
export PG_VERSION_REQ="=17.11.0"
export PG_EXTENSIONS="vector"
export PG_EXTENSIONS_MANIFEST="https://github.com/leynos/df12-pg-extensions/releases/download/v1.0.0/manifest.json"
export PG_EXTENSIONS_MANIFEST_SHA256="<digest from manifest.json.sha256>"
```

- `PG_EXTENSIONS` is a comma-separated list of `CREATE EXTENSION` names.
- `PG_EXTENSIONS_MANIFEST` is the manifest URL for the pinned release, or a
  filesystem path for a locally mirrored manifest.
- `PG_EXTENSIONS_MANIFEST_SHA256` is required for an HTTPS manifest. Read
  it once from the release's `manifest.json.sha256` and commit it; never
  fetch it at run time, because then the digest pins nothing.
- `PG_EXTENSIONS_CACHE_DIR` (optional) holds verified archives between
  runs. It defaults to `$XDG_CACHE_HOME/pg-embedded/extensions`; in CI, cache
  it alongside the PostgreSQL binary cache.

The hook downloads the archive for the running PostgreSQL and target,
checks its digest against the manifest, and installs `lib/` and
`share/extension/` files into the embedded tree before the server starts.
`CREATE EXTENSION vector` then works exactly as it would against a package
from a distribution. The hook's API, error kinds and failure modes are
specified in
[pg-embed-setup-unpriv#222](https://github.com/leynos/pg-embed-setup-unpriv/issues/222).

## Updating to a new release

1. Read the new release's `manifest.json.sha256`.
2. Change `PG_EXTENSIONS_MANIFEST` to the new tag's URL and
   `PG_EXTENSIONS_MANIFEST_SHA256` to the new digest.
3. If the release builds against a newer Theseus PostgreSQL, move
   `PG_VERSION_REQ` at the same time.

## Verifying an archive by hand

```bash
tag=v1.0.0
base="https://github.com/leynos/df12-pg-extensions/releases/download/$tag"
curl -fsSLO "$base/manifest.json" -O "$base/manifest.json.sha256"
sha256sum -c manifest.json.sha256
file=pgvector-0.8.6-pg17.11.0-x86_64-unknown-linux-gnu.tar.gz
curl -fsSLO "$base/$file" -O "$base/$file.sha256"
sha256sum -c "$file.sha256"
tar -tzf "$file"
```

The listing shows only `lib/` and `share/extension/` entries.

## glibc floor

Linux archives are built against glibc 2.31 and are checked to reference no
symbol version above `GLIBC_2.34`, the floor of the Theseus `postgres`
binary itself, so any host that runs the server can load the extension.
