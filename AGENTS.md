# Assistant Instructions

This repository publishes prebuilt PostgreSQL extension archives for the
`pg-embed-setup-unpriv` extension hook. Read `README.md` first; it states
the archive layout, the manifest schema and the build rules that consumers
rely on.

## Rules

- `extensions.toml` is the single source of truth. Workflows derive their
  matrix from it and the contract tests read it; never hard-code a version,
  target or archive name anywhere else.
- Every extension is pinned to an upstream tag **and** the commit it
  resolves to; the build refuses a checkout whose `HEAD` differs.
- The container image is pinned by digest. Compilation happens only inside
  `scripts/build_in_container.sh`; runner-level steps never install tools
  or build from source, and a contract test enforces that.
- Archives contain regular files under `lib/` and `share/extension/` only.
  `scripts/archive_rules.py` is the reference implementation of that rule and
  mirrors the consumer hook; change both together or neither.
- Releases come from `vMAJOR.MINOR.PATCH` tags through `release.yml`, never
  by hand, and are never rebuilt in place.
- GitHub Actions are referenced by 40-hex commit SHA.
- Shell scripts start with `set -euo pipefail` and carry the exec bit.
- Prose uses en-GB-oxendict spelling ("-ize", "-yse", "-our"); quoted
  identifiers keep their upstream spelling.

## Commit gates

Run `make all` before committing. It executes `make check-fmt`,
`make lint` (shellcheck, ruff, markdownlint) and `make test` (pytest unit
tests, Hypothesis property tests and the workflow contracts). Each contract
matches the mechanism it protects, the `run:` command or the exact label,
so mutate the protected line once when you add one and confirm the test
fails.

Commit messages use the imperative mood, a subject of about 50 characters,
and a body wrapped at 72 columns explaining what changed and why. Do not add
attribution or session trailers.
