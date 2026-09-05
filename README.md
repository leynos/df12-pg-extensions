# df12-pg-extensions

Prebuilt PostgreSQL extension archives for the estate's embedded test
clusters.

[`pg-embed-setup-unpriv`](https://github.com/leynos/pg-embed-setup-unpriv)
starts a real PostgreSQL server for tests from the
[Theseus `postgresql-binaries`](https://github.com/theseus-rs/postgresql-binaries)
archives. Those archives carry only the in-tree contrib extensions, and the
estate rule is that nothing is built from source in a consumer's continuous
integration. This repository is the one place an extension is compiled: its
release workflow builds each configured extension against each Theseus
release and target, verifies the result by loading it into that PostgreSQL,
and publishes per-target archives, `.sha256` sidecars and a manifest to a
versioned GitHub release. The `pg-embed-setup-unpriv` extension hook
downloads an archive from that release, checks its digest against the
manifest, and installs it into the embedded tree before the server starts.

## What a release contains

For every extension, PostgreSQL version and target in `extensions.toml`:

- `<package>-<version>-pg<postgresql>-<target>.tar.gz`, for example
  `pgvector-0.8.6-pg17.11.0-x86_64-unknown-linux-gnu.tar.gz`
- `<archive>.sha256`, a `sha256sum`-format sidecar

plus one `manifest.json` and its `manifest.json.sha256`.

An archive is a gzip tar of regular files under exactly two prefixes
relative to the PostgreSQL install root:

```plaintext
lib/<name>.so
share/extension/<name>.control
share/extension/<name>--<version>.sql
share/extension/<name>--<from>--<to>.sql
```

Nothing else is packaged: no headers, no LLVM bitcode, no symlinks. The
consumer hook refuses any other layout, and `scripts/archive_rules.py`
enforces the same rules on the publisher side.

## Using a release from a consumer

The hook is configured entirely through the environment. Pin the release by
tag and by the manifest digest; the manifest carries every archive digest, so
pinning the manifest pins the archives transitively.

```bash
export PG_VERSION_REQ="=17.11.0"
export PG_EXTENSIONS="vector"
export PG_EXTENSIONS_MANIFEST="https://github.com/leynos/df12-pg-extensions/releases/download/v1.0.0/manifest.json"
export PG_EXTENSIONS_MANIFEST_SHA256="$(curl -fsSL "$PG_EXTENSIONS_MANIFEST.sha256" | cut -d' ' -f1)"
```

Record the digest in the consumer repository rather than fetching it at
run time; the `curl` above is only a way to read it once. The hook matches
on the running PostgreSQL major and minor exactly, so `PG_VERSION_REQ` must
name a Theseus release this repository builds against (see
`extensions.toml`). The hook's design, its failure modes and the exact
environment variables are specified in
[pg-embed-setup-unpriv#222](https://github.com/leynos/pg-embed-setup-unpriv/issues/222).

## Manifest schema

`manifest.json` has `schema_version` 1:

```json
{
  "schema_version": 1,
  "release": "v1.0.0",
  "generated_at": "2026-09-06T00:00:00+00:00",
  "extensions": [
    {
      "name": "vector",
      "package": "pgvector",
      "version": "0.8.6",
      "source": {
        "repository": "https://github.com/pgvector/pgvector",
        "tag": "v0.8.6",
        "commit": "8ee86c96f0fd72390f890aa8a336fda6d3ab4c6c"
      },
      "artifacts": [
        {
          "postgresql": "17.11.0",
          "target": "x86_64-unknown-linux-gnu",
          "file": "pgvector-0.8.6-pg17.11.0-x86_64-unknown-linux-gnu.tar.gz",
          "url": "https://github.com/leynos/df12-pg-extensions/releases/download/v1.0.0/pgvector-0.8.6-pg17.11.0-x86_64-unknown-linux-gnu.tar.gz",
          "sha256": "<64 lower-case hex digits>",
          "size": 102961,
          "files": [
            "lib/vector.so",
            "share/extension/vector--0.8.6.sql",
            "share/extension/vector.control"
          ]
        }
      ]
    }
  ]
}
```

- `name` is the `CREATE EXTENSION` name; `package` is the upstream project.
- `postgresql` is the Theseus release the archive was built against. Its
  third component is a Theseus build number, so consumers match on major
  and minor.
- `target` is a Rust target triple, the same string Theseus uses in its
  asset names and the compile target of `pg-embed-setup-unpriv`.
- `files` lists every regular file in the archive, sorted, so the hook can
  refuse an archive whose contents differ from what was published.
- Every artifact for every configured combination is present, or the
  manifest builder refuses to run.

## How archives are built

`scripts/build_extension.sh` runs `scripts/build_in_container.sh` inside
the container image pinned by digest in `extensions.toml`. The image is
`debian:12`, the same base Theseus compiles its Linux binaries in, so the
glibc symbol versions a shared object references never exceed what the
PostgreSQL binaries already require. Inside the container the script:

1. downloads the Theseus archive for the PostgreSQL version and target and
   checks it against the `.sha256` sidecar Theseus publishes;
2. clones the extension at its pinned tag and refuses to continue if `HEAD`
   is not the pinned commit;
3. builds with PGXS against the Theseus `pg_config`, passing `OPTFLAGS=""`
   so pgvector's default `-march=native` does not leak the build host's
   instruction set into the archive, and `with_llvm=no` so no bitcode is
   emitted;
4. installs into a staging root and keeps only `lib/*.so` and
   `share/extension/<name>.control` plus `<name>--*.sql`;
5. writes a reproducible gzip tar (sorted names, owner 0, fixed mtime,
   `gzip -n`) and its sidecar.

Every leg then runs `scripts/smoke_test.sh`, which unpacks a fresh Theseus
tree, installs the archive over it exactly as the hook does, runs `initdb`,
starts the server, executes `CREATE EXTENSION` and the `smoke_sql` from
`extensions.toml`, and stops it. A release is undrafted only after every
smoke leg passes and the audit job has re-downloaded every asset and
verified the manifest against it.

## Adding or updating an extension

1. Edit `extensions.toml`: add a `[[extensions]]` table with the upstream
   tag and the 40-hex commit it resolves to, or bump `version`, `tag` and
   `commit` together. Add PostgreSQL versions or targets to `[postgresql]`
   when the estate pins change; every target needs a runner mapping in
   `scripts/matrix.py`.
2. Run `make all`. The workflow contracts read `extensions.toml`, so the
   matrix and the manifest expectations follow the file automatically.
3. Open a pull request. CI builds one leg (pgvector on PostgreSQL 17,
   x86_64) end to end and runs the smoke test on it.
4. After the merge, push a `vMAJOR.MINOR.PATCH` tag. The release workflow
   builds every leg, publishes the assets and the manifest, audits and
   smoke-tests them, and undrafts the release.

Releases are immutable. A rebuilt extension or a new PostgreSQL version is
a new tag, and consumers move by updating the manifest URL and digest they
pin.

## Local development

```bash
make test        # unit tests and workflow contracts (uv, pytest)
make lint        # shellcheck, ruff, markdownlint
make check-fmt   # ruff format --check
make matrix      # print the release matrix as JSON
```

To build and smoke-test one leg locally (Docker or Podman; set
`DOCKER=podman` for Podman), export the leg's fields under the names the
scripts expect, then run the same three steps CI runs:

```bash
python3 scripts/matrix.py extensions.toml --select pgvector 17 x86_64-unknown-linux-gnu
export EXT_NAME=vector EXT_PACKAGE=pgvector EXT_VERSION=0.8.6
export EXT_REPOSITORY=https://github.com/pgvector/pgvector EXT_TAG=v0.8.6
export EXT_COMMIT=8ee86c96f0fd72390f890aa8a336fda6d3ab4c6c
export PG_VERSION=17.11.0 TARGET=x86_64-unknown-linux-gnu PLATFORM=linux/amd64
export ARCHIVE=pgvector-0.8.6-pg17.11.0-x86_64-unknown-linux-gnu.tar.gz
bash scripts/build_extension.sh
python3 scripts/build_manifest.py check-archive "dist/$ARCHIVE"
ARCHIVE_PATH="dist/$ARCHIVE" SMOKE_SQL="SELECT '[1,2,3]'::vector <-> '[4,5,6]'::vector" \
  bash scripts/smoke_test.sh
```

## Licence

ISC. Extension sources keep their upstream licences; pgvector is
PostgreSQL-licensed.
