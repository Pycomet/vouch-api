#!/usr/bin/env bash
# Nightly logical backup of the Vouch database (M3a §2.3). See docs/DEPLOY.md.
#
#   BACKUP_DATABASE_URL=postgresql://user:pass@host:5432/vouch scripts/backup.sh
#
# Writes <BACKUP_DIR>/vouch-<UTC timestamp>.dump (pg_dump --format=custom) and
# prints its path. Exits non-zero, leaving no file behind, if the dump fails or the
# archive cannot be read back. Uploading the file off the host is the operator's job.
#
# Environment:
#   BACKUP_DATABASE_URL  libpq URL of the database (falls back to VOUCH_DATABASE_URL;
#                        a postgresql+asyncpg:// URL is accepted and converted)
#   BACKUP_DIR           target directory (default ./backups; created if missing)
#
# Needs pg_dump and pg_restore at least as new as the server (PostgreSQL 16).
set -euo pipefail

url="${BACKUP_DATABASE_URL:-${VOUCH_DATABASE_URL:-}}"
if [[ -z "$url" ]]; then
  echo "backup: set BACKUP_DATABASE_URL (or VOUCH_DATABASE_URL)" >&2
  exit 2
fi
url="${url/postgresql+asyncpg:\/\//postgresql://}"
dir="${BACKUP_DIR:-./backups}"

mkdir -p "$dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$dir/vouch-$stamp.dump"
partial="$target.partial"
trap 'rm -f "$partial"' EXIT

pg_dump --format=custom --no-owner --no-privileges --dbname="$url" --file="$partial"
# The archive must be readable before it replaces anything.
pg_restore --list "$partial" >/dev/null
mv "$partial" "$target"
trap - EXIT

echo "$target"
