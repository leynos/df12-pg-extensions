# Developers guide

## Boundaries

Three artefacts have a contract with the consumer hook in
`pg-embed-setup-unpriv`, and changing any of them means changing the hook:

- **Archive layout**: regular files under `lib/` (one level) and
  `share/extension/` only. `scripts/archive_rules.py` is the publisher-side
  implementation; the hook carries the same rules.
- **Manifest schema**: `schema_version` 1 as produced by
  `scripts/build_manifest.py`. Adding a field is compatible; renaming or
  removing one, or changing the matching keys (`postgresql`, `target`,
  `file`, `sha256`, `files`), is a new schema version.
- **Asset names**: `<package>-<version>-pg<postgresql>-<target>.tar.gz` and
  the `.sha256` sidecars, from `Config.archive_name`.

## Configuration model

`extensions.toml` is the single source of truth, parsed by
`scripts/pgx_config.py` into frozen dataclasses:

| Table | Dataclass | Notes |
| --- | --- | --- |
| `[postgresql]` | `Config.postgresql_versions`, `Config.releases_url` | Theseus releases and their download base |
| `[targets.<triple>]` | `Target` | `runner` label and container `platform` |
| `[build]` | `Config.build_image` | Digest-pinned image; must contain `@sha256:` |
| `[smoke]` | `SmokeLeg` | The one leg pull requests build end to end |
| `[[extensions]]` | `Extension` | Pinned by `tag` and `commit`; `smoke_sql` runs after `CREATE EXTENSION` |

Every value is validated on load and any problem raises `ConfigError`;
scripts and tests never fall back to a default for a missing key.

## Scripts

| Script | Runs where | Purpose |
| --- | --- | --- |
| `scripts/matrix.py` | runner | Emits the build matrix (`{"include": [...]}`) or the smoke leg (`--smoke-leg`) as `key=value` lines |
| `scripts/build_extension.sh` | runner | Resolves the image from `extensions.toml`, forwards the leg's environment, runs the container |
| `scripts/build_in_container.sh` | container | Fetches and verifies Theseus, checks out the pinned commit, builds with PGXS, packages `lib/` and `share/extension/` |
| `scripts/smoke_test.sh` | runner | Installs an archive into a fresh Theseus tree and runs `CREATE EXTENSION` plus `smoke_sql` |
| `scripts/build_manifest.py` | runner | `build`, `verify` and `check-archive`; `collect_extensions` is clock-free, the timestamp is injected by the `build` command |

Environment contract between the wrapper and the container script:
`EXT_NAME`, `EXT_PACKAGE`, `EXT_VERSION`, `EXT_REPOSITORY`, `EXT_TAG`,
`EXT_COMMIT`, `PG_VERSION`, `TARGET`, `PLATFORM`, `ARCHIVE`,
`THESEUS_RELEASES_URL`, plus `DIST_DIR` and `SOURCE_DATE_EPOCH`. All of
them come from a matrix leg; none has a hard-coded fallback.

## Container build requirements

The build image is `debian:11`, pinned by index digest. The Theseus
`postgres` binary references glibc symbol versions up to `GLIBC_2.34`; an
extension built on a newer base could reference `GLIBC_2.36` symbols and then
fail to load on a host (RHEL 9, Ubuntu 22.04) that runs the server fine. So
the base must stay at or below 2.34 (debian:11 is at 2.31) and
`[build].max_glibc` records the floor; `build_in_container.sh` reads every
shared object's `GLIBC_*` versions with `objdump` and fails above it. The
build passes `OPTFLAGS=""` (pgvector defaults to `-march=native`) and
`with_llvm=no` (the Theseus tree was configured with LLVM, and PGXS would
otherwise emit bitcode into `lib/bitcode/`, which the layout rules refuse).
Only `lib/*.so`, `<name>.control` and `<name>--*.sql` are packaged; headers
are dropped.

Updating the image digest: run `skopeo inspect --raw docker://debian:11 |
sha256sum` (or read `docker-content-digest` from the registry) and put the
index digest in `[build].image`.

## Workflows

- `ci.yml` (pull requests, `workflow_dispatch`): a `checks` job on
  `ubicloud-standard-2` runs `make check-fmt`, `make shellcheck`,
  `make ruff`, markdownlint and `make test`; a `smoke-build` job builds the
  configured smoke leg, checks the archive, and runs the smoke test.
- `release.yml` (tag push `v*`, one run per ref): `prepare` validates the
  tag and derives the matrix; `create-release` opens a draft;
  `build-assets` builds and uploads each archive and sidecar; `manifest`
  re-downloads every archive, builds `manifest.json` and uploads it with its
  sidecar; `audit` re-downloads everything and runs `verify`; `smoke` loads
  each archive into its PostgreSQL; `publish` undrafts only after `audit`
  and every `smoke` leg pass.

Every `uses:` is pinned to a commit SHA, checkouts never persist
credentials, and no runner step installs a tool or builds from source.

## Tests

`make test` runs pytest with Hypothesis:

- `tests/test_pgx_config.py`: configuration parsing and every rejection.
- `tests/test_archive_rules.py`: path classification (exhaustive cases plus
  property tests) and member validation.
- `tests/test_build_manifest.py`: manifest building, missing and unexpected
  archives, sidecars, tampering, the clock-free collection.
- `tests/test_matrix.py`: matrix derivation and the smoke leg.
- `tests/test_workflow_contracts.py`: the workflow and script contracts.
  Each matches a `run:` command, a `uses:` pin, a runner label or a script
  token, and one test runs `scripts/build_extension.sh` against a fake
  `docker` to prove the container invocation. When you add a contract,
  mutate the protected line once and confirm the test fails.

## Local prerequisites

`uv` (for ruff and pytest), `shellcheck`, `markdownlint-cli2`, and Docker or
Podman for a local build (`DOCKER=podman`). `make all` runs every gate.
