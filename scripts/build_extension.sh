#!/usr/bin/env bash
# Build one extension archive inside the pinned almalinux:9 container.
#
# Runs on the GitHub runner. Everything that compiles happens inside the
# container (scripts/build_in_container.sh); this wrapper only resolves the
# image, mounts the checkout and collects the archive from dist/.
#
# Required environment (supplied by the release matrix):
#   EXT_NAME EXT_PACKAGE EXT_VERSION EXT_REPOSITORY EXT_TAG EXT_COMMIT
#   PG_VERSION TARGET PLATFORM ARCHIVE THESEUS_RELEASES_URL MAX_GLIBC
# Optional: BUILD_IMAGE (default: build.image from extensions.toml),
#   DIST_DIR (default dist), DOCKER (default docker).
set -euo pipefail

for var in EXT_NAME EXT_PACKAGE EXT_VERSION EXT_REPOSITORY EXT_TAG EXT_COMMIT \
           PG_VERSION TARGET PLATFORM ARCHIVE THESEUS_RELEASES_URL MAX_GLIBC; do
  if [ -z "${!var:-}" ]; then
    echo "build_extension.sh: $var is required" >&2
    exit 2
  fi
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export DIST_DIR="${DIST_DIR:-dist}"
if [ -z "${BUILD_IMAGE:-}" ]; then
  BUILD_IMAGE="$(sed -n 's/^image = "\(.*\)"$/\1/p' "$repo_root/extensions.toml")"
fi
if [ -z "$BUILD_IMAGE" ]; then
  echo "build_extension.sh: could not resolve build.image from extensions.toml" >&2
  exit 2
fi
export EXT_NAME EXT_PACKAGE EXT_VERSION EXT_REPOSITORY EXT_TAG EXT_COMMIT \
       PG_VERSION TARGET PLATFORM ARCHIVE THESEUS_RELEASES_URL MAX_GLIBC
DOCKER="${DOCKER:-docker}"

mkdir -p "$repo_root/$DIST_DIR"
echo "building $ARCHIVE in $BUILD_IMAGE ($PLATFORM)"
"$DOCKER" run --rm \
  --platform "$PLATFORM" \
  --volume "$repo_root:/work" \
  --workdir /work \
  --env EXT_NAME --env EXT_PACKAGE --env EXT_VERSION --env EXT_REPOSITORY \
  --env EXT_TAG --env EXT_COMMIT --env PG_VERSION --env TARGET --env ARCHIVE \
  --env THESEUS_RELEASES_URL --env MAX_GLIBC --env DIST_DIR \
  --env "SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-0}" \
  "$BUILD_IMAGE" \
  bash scripts/build_in_container.sh

test -f "$repo_root/$DIST_DIR/$ARCHIVE"
test -f "$repo_root/$DIST_DIR/$ARCHIVE.sha256"
(cd "$repo_root/$DIST_DIR" && sha256sum -c "$ARCHIVE.sha256")
