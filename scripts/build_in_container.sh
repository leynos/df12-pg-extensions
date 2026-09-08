#!/usr/bin/env bash
# Compile one extension against a Theseus PostgreSQL tree and package it.
#
# Runs inside the pinned almalinux:9 container with the repository mounted at
# /work. This is the only place in the estate where an extension is built
# from source: the output is the release asset every consumer downloads.
#
# Steps: install the toolchain, fetch and verify the Theseus archive, check
# out the pinned upstream commit, build with PGXS and portable flags, install
# into a staging root, keep only lib/ and share/extension/, and write a
# reproducible gzip tar with its sha256 sidecar into $DIST_DIR.
set -euo pipefail

for var in EXT_NAME EXT_PACKAGE EXT_VERSION EXT_REPOSITORY EXT_TAG EXT_COMMIT \
           PG_VERSION TARGET ARCHIVE THESEUS_RELEASES_URL DIST_DIR MAX_GLIBC; do
  if [ -z "${!var:-}" ]; then
    echo "build_in_container.sh: $var is required" >&2
    exit 2
  fi
done
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"

# almalinux:9 ships glibc 2.34, the same floor as the Theseus postgres binary.
dnf install -y -q binutils ca-certificates curl-minimal gcc git gzip make tar >/dev/null

work="$(mktemp -d)"
pg_root="$work/postgresql"
stage="$work/stage"
pkg="$work/pkg"
mkdir -p "$pg_root" "$stage" "$pkg"

# --- Theseus PostgreSQL tree, verified against its published sidecar -------
theseus_archive="postgresql-$PG_VERSION-$TARGET.tar.gz"
theseus_url="$THESEUS_RELEASES_URL/$PG_VERSION/$theseus_archive"
echo "fetching $theseus_url"
curl --fail --silent --show-error --location --retry 3 \
  --output "$work/$theseus_archive" "$theseus_url"
curl --fail --silent --show-error --location --retry 3 \
  --output "$work/$theseus_archive.sha256" "$theseus_url.sha256"
(cd "$work" && sha256sum -c "$theseus_archive.sha256")
tar -xzf "$work/$theseus_archive" --strip-components=1 -C "$pg_root"
pg_config="$pg_root/bin/pg_config"
test -x "$pg_config"
"$pg_config" --version
pkglibdir="$("$pg_config" --pkglibdir)"
sharedir="$("$pg_config" --sharedir)"
case "$pkglibdir" in "$pg_root"/*) ;; *) echo "pg_config is not relocatable: $pkglibdir" >&2; exit 1 ;; esac

# --- Upstream source at the pinned commit ---------------------------------
src="$work/src"
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$EXT_TAG" \
  "$EXT_REPOSITORY" "$src"
head="$(git -C "$src" rev-parse HEAD)"
if [ "$head" != "$EXT_COMMIT" ]; then
  echo "tag $EXT_TAG resolves to $head, expected $EXT_COMMIT" >&2
  exit 1
fi

# --- Build with PGXS --------------------------------------------------------
# OPTFLAGS="" removes pgvector's default -march=native so the object runs on
# any CPU of the target architecture. with_llvm=no stops PGXS emitting LLVM
# bitcode (the Theseus tree was configured --with-llvm), which would need
# clang and would only add lib/bitcode files the hook refuses anyway.
make -C "$src" -j "$(nproc)" \
  USE_PGXS=1 PG_CONFIG="$pg_config" OPTFLAGS="" with_llvm=no
make -C "$src" install \
  USE_PGXS=1 PG_CONFIG="$pg_config" OPTFLAGS="" with_llvm=no DESTDIR="$stage"

# --- Keep lib/ and share/extension/ only ------------------------------------
staged_lib="$stage$pkglibdir"
staged_ext="$stage$sharedir/extension"
test -d "$staged_lib"
test -d "$staged_ext"
mkdir -p "$pkg/lib" "$pkg/share/extension"
find "$staged_lib" -maxdepth 1 -type f -name '*.so' -exec cp -p {} "$pkg/lib/" \;
find "$staged_ext" -maxdepth 1 -type f \( -name "$EXT_NAME.control" -o -name "$EXT_NAME--*.sql" \) \
  -exec cp -p {} "$pkg/share/extension/" \;
test -f "$pkg/lib/$EXT_NAME.so"
test -f "$pkg/share/extension/$EXT_NAME.control"
chmod 0755 "$pkg/lib/"*.so
chmod 0644 "$pkg/share/extension/"*

# --- glibc floor ------------------------------------------------------------
# The archive must not require a newer glibc than the Theseus postgres binary
# it loads into, or hosts that run the server would refuse the extension.
for so in "$pkg/lib/"*.so; do
  echo "glibc symbol versions referenced by $(basename "$so"):"
  # grep exits 1 for a library with no GLIBC imports; that is a valid (empty) result.
  versions="$(objdump -T "$so" | { grep -o 'GLIBC_[0-9.]*' || true; } | sed 's/GLIBC_//' | sort -uV)"
  echo "$versions"
  highest="$(echo "$versions" | tail -n 1)"
  if [ -n "$highest" ] && [ "$(printf '%s\n%s\n' "$MAX_GLIBC" "$highest" | sort -V | tail -n 1)" != "$MAX_GLIBC" ]; then
    echo "$(basename "$so") requires GLIBC_$highest, above the permitted floor GLIBC_$MAX_GLIBC" >&2
    exit 1
  fi
done

# --- Package reproducibly ---------------------------------------------------
out="/work/$DIST_DIR"
mkdir -p "$out"
tar --sort=name --owner=0 --group=0 --numeric-owner \
  --mtime="@$SOURCE_DATE_EPOCH" --format=gnu \
  -C "$pkg" -cf - lib share | gzip -n -9 > "$out/$ARCHIVE"
(cd "$out" && sha256sum "$ARCHIVE" > "$ARCHIVE.sha256")
echo "built $ARCHIVE:"
tar -tzvf "$out/$ARCHIVE"
cat "$out/$ARCHIVE.sha256"
