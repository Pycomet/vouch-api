#!/usr/bin/env bash
# Nightly logical backup of the Vouch database (M3a §2.3). See docs/DEPLOY.md.
#
#   BACKUP_DATABASE_URL=postgresql://user@host:5432/vouch PGPASSWORD=... scripts/backup.sh
#
# Writes <BACKUP_DIR>/vouch-<UTC timestamp>.dump (pg_dump --format=custom) and
# prints its path. Exits non-zero, leaving no file behind, if the dump fails or the
# archive cannot be read back. Uploading the file off the host is the operator's job.
#
# Environment:
#   BACKUP_DATABASE_URL  libpq URL of the database (falls back to VOUCH_DATABASE_URL;
#                        a postgresql+asyncpg:// URL is accepted and converted). A
#                        password in the URL is moved into PGPASSWORD before any
#                        command runs, so it never shows in a process listing.
#   PGHOST, PGPORT, PGUSER, PGDATABASE, PGPASSWORD, PGPASSFILE
#                        the standard libpq variables; with PGDATABASE set, no URL
#                        is needed at all
#   BACKUP_DIR           target directory (default ./backups; created if missing)
#
# Needs pg_dump and pg_restore at least as new as the server (PostgreSQL 16).
set -euo pipefail

# Sets `clean_url` to $1 in libpq form without its password, exporting the password
# (if any) as PGPASSWORD. The password must be URL-safe (no percent-encoding).
split_password() {
  clean_url="${1/postgresql+asyncpg:\/\//postgresql://}"
  local pattern='^(postgres(ql)?://)([^:@/]+):([^@/]*)@(.*)$'
  if [[ "$clean_url" =~ $pattern ]]; then
    export PGPASSWORD="${BASH_REMATCH[4]}"
    clean_url="${BASH_REMATCH[1]}${BASH_REMATCH[3]}@${BASH_REMATCH[5]}"
  fi
}

url="${BACKUP_DATABASE_URL:-${VOUCH_DATABASE_URL:-}}"
target_args=()
if [[ -n "$url" ]]; then
  split_password "$url"
  target_args=(--dbname="$clean_url")
elif [[ -z "${PGDATABASE:-}" ]]; then
  echo "backup: set BACKUP_DATABASE_URL (or VOUCH_DATABASE_URL, or the PG* variables)" >&2
  exit 2
fi
dir="${BACKUP_DIR:-./backups}"

mkdir -p "$dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$dir/vouch-$stamp.dump"
partial="$target.partial"
trap 'rm -f "$partial"' EXIT

pg_dump --format=custom --no-owner --no-privileges "${target_args[@]}" --file="$partial"
# The archive must be readable before it replaces anything.
pg_restore --list "$partial" >/dev/null
mv "$partial" "$target"
trap - EXIT

echo "$target"
