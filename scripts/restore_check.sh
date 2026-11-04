#!/usr/bin/env bash
# Restore drill (M3a §2.3): restore a dump into a throwaway database and run the
# ledger check against it. See docs/DEPLOY.md.
#
#   RESTORE_SERVER_URL=postgresql://user:pass@host:5432/postgres \
#     scripts/restore_check.sh backups/vouch-20261104T020000Z.dump
#
# Creates the database vouch_restore_check_<UTC timestamp> on that server, restores
# the dump into it, prints the migration revision and a few row counts, then runs
# scripts/ledger_check.sql. Exits 0 only if the restore succeeds and the ledger
# check returns no rows. The scratch database is dropped on exit unless
# KEEP_RESTORE_DB=1. Never point this at the production server's live database:
# it only ever creates and drops its own scratch database, but run it on a
# separate server when you can.
#
# Environment:
#   RESTORE_SERVER_URL  libpq URL of a maintenance database on the scratch server
#                       (a role that may CREATE DATABASE); postgresql+asyncpg:// is
#                       accepted and converted
#   KEEP_RESTORE_DB     1 to keep the scratch database for inspection
#
# Needs psql and pg_restore at least as new as the server (PostgreSQL 16).
set -euo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "usage: restore_check.sh <dump file>" >&2
  exit 2
fi
dump="$1"
server="${RESTORE_SERVER_URL:-}"
if [[ -z "$server" ]]; then
  echo "restore_check: set RESTORE_SERVER_URL" >&2
  exit 2
fi
server="${server/postgresql+asyncpg:\/\//postgresql://}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scratch="vouch_restore_check_$(date -u +%Y%m%dT%H%M%SZ)"

# The scratch database's URL: the server URL with its database name replaced.
base="${server%%\?*}"
query=""
if [[ "$server" == *\?* ]]; then
  query="?${server#*\?}"
fi
scratch_url="${base%/*}/$scratch$query"

drop_scratch() {
  if [[ "${KEEP_RESTORE_DB:-0}" == "1" ]]; then
    echo "restore_check: kept database $scratch" >&2
  else
    psql "$server" -q -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$scratch\"" >/dev/null
  fi
}

psql "$server" -q -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$scratch\"" >/dev/null
trap drop_scratch EXIT

pg_restore --no-owner --no-privileges --exit-on-error --dbname="$scratch_url" "$dump"

echo "restored $dump into $scratch"
psql "$scratch_url" -X -q -v ON_ERROR_STOP=1 -A -F ' ' -t -c "
  SELECT 'alembic_version', version_num FROM alembic_version
  UNION ALL SELECT 'users', count(*)::text FROM users
  UNION ALL SELECT 'bookings', count(*)::text FROM bookings
  UNION ALL SELECT 'ledger_entries', count(*)::text FROM ledger_entries
  UNION ALL SELECT 'audit_events', count(*)::text FROM audit_events"

problems="$(psql "$scratch_url" -X -q -v ON_ERROR_STOP=1 -A -F ' | ' -t -f "$here/ledger_check.sql")"
if [[ -n "$problems" ]]; then
  echo "ledger check FAILED:" >&2
  echo "$problems" >&2
  exit 1
fi
echo "ledger check passed (no rows)"
