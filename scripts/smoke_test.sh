#!/usr/bin/env bash
# Load an extension archive into a Theseus PostgreSQL tree and exercise it.
#
# Downloads and verifies the Theseus archive for PG_VERSION/TARGET, unpacks
# the extension archive over it exactly as the pg-embed-setup-unpriv hook
# does, runs initdb, starts the server on a Unix socket, runs
# CREATE EXTENSION plus the configured smoke SQL, and stops the server.
#
# Required environment: EXT_NAME PG_VERSION TARGET ARCHIVE_PATH SMOKE_SQL
# Optional: THESEUS_RELEASES_URL WORK_DIR
set -euo pipefail

for var in EXT_NAME PG_VERSION TARGET ARCHIVE_PATH SMOKE_SQL; do
  if [ -z "${!var:-}" ]; then
    echo "smoke_test.sh: $var is required" >&2
    exit 2
  fi
done
THESEUS_RELEASES_URL="${THESEUS_RELEASES_URL:-https://github.com/theseus-rs/postgresql-binaries/releases/download}"
WORK_DIR="${WORK_DIR:-$(mktemp -d)}"
mkdir -p "$WORK_DIR"
pg_root="$WORK_DIR/postgresql"
data_dir="$WORK_DIR/data"
# Unix socket paths are capped at 107 bytes, so the socket directory lives in
# the system temporary directory rather than under a possibly deep WORK_DIR.
socket_dir="$(mktemp -d -t pgsmoke.XXXXXX)"
mkdir -p "$pg_root"

theseus_archive="postgresql-$PG_VERSION-$TARGET.tar.gz"
theseus_url="$THESEUS_RELEASES_URL/$PG_VERSION/$theseus_archive"
if [ ! -f "$WORK_DIR/$theseus_archive" ]; then
  curl --fail --silent --show-error --location --retry 3 \
    --output "$WORK_DIR/$theseus_archive" "$theseus_url"
  curl --fail --silent --show-error --location --retry 3 \
    --output "$WORK_DIR/$theseus_archive.sha256" "$theseus_url.sha256"
fi
(cd "$WORK_DIR" && sha256sum -c "$theseus_archive.sha256")
tar -xzf "$WORK_DIR/$theseus_archive" --strip-components=1 -C "$pg_root"

# Install the extension the way the hook does: plain files under lib/ and
# share/extension/, nothing else.
tar -xzf "$ARCHIVE_PATH" -C "$pg_root"
test -f "$pg_root/lib/$EXT_NAME.so"
test -f "$pg_root/share/extension/$EXT_NAME.control"

"$pg_root/bin/initdb" --pgdata="$data_dir" --username=postgres --auth=trust \
  --no-sync >"$WORK_DIR/initdb.log" 2>&1
port="$((20000 + RANDOM % 20000))"
cleanup() {
  "$pg_root/bin/pg_ctl" -D "$data_dir" -m fast stop >/dev/null 2>&1 || true
  rm -f "$socket_dir"/.s.PGSQL.*
  rmdir "$socket_dir" 2>/dev/null || true
}
trap cleanup EXIT
"$pg_root/bin/pg_ctl" -D "$data_dir" -w -l "$WORK_DIR/postgres.log" \
  -o "-p $port -k $socket_dir -c listen_addresses=''" start
psql="$pg_root/bin/psql"
"$psql" -h "$socket_dir" -p "$port" -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION $EXT_NAME" \
  -c "SELECT extname, extversion FROM pg_extension WHERE extname = '$EXT_NAME'" \
  -c "$SMOKE_SQL"
echo "smoke test passed for $EXT_NAME on PostgreSQL $PG_VERSION ($TARGET)"
