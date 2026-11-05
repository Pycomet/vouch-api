# Deploying the Vouch API

This is how to run the API on one host with Docker Compose, and how to look after its database. It is provider-neutral: there is no cloud SDK or infrastructure code in this repository (M3a §2). The same image runs anywhere that can run a container and reach PostgreSQL 16.

## 1. What runs

One image (`Dockerfile`), several commands. `compose.yml` chooses the command for each service:

| Service | Command | Lifetime |
|---|---|---|
| `postgres` | `postgres:16`, data in the `pgdata` named volume | long-running, with a `pg_isready` healthcheck |
| `migrate` | `alembic upgrade head` | one-shot; must exit 0 |
| `api` | `uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers` (the image default) | long-running; the image `HEALTHCHECK` calls `GET /api/v1/health/ready` |
| `timers` | `python -m app.jobs.timers` | long-running; one run every 60 s |
| `outbox` | `python -m app.jobs.outbox` | arrives in M3a step 5; commented out until then |

Start-up order is `postgres` (healthy), then `migrate` (exited 0), then `api` and `timers`. **Migrations never run when the API starts.** If `migrate` fails, `api` and `timers` do not start.

The image is `python:3.11-slim`, installs the package without dev extras, runs as the non-root user `vouch` (uid 10001), and contains no `.env`. Its healthcheck uses the Python standard library, so the image has no `curl`.

The API does not trust `X-Forwarded-For` or other proxy headers (`--no-proxy-headers`). The client IP it sees is the socket peer. Trusting a proxy header is configured in M3a §7.2, and only for a proxy the API can be reached through exclusively.

## 2. First start

```bash
cp .env.example .env
# edit .env: VOUCH_ENVIRONMENT=phase0, VOUCH_DEBUG=false, POSTGRES_PASSWORD,
# VOUCH_JWT_SECRET, VOUCH_FAKE_PAYMENT_WEBHOOK_SECRET, VOUCH_CORS_ORIGINS ...
docker compose up -d --build
docker compose ps          # migrate "Exited (0)", api "Up (healthy)", timers "Up"
```

The API is published on host port `API_HOST_PORT` (default 8000). PostgreSQL publishes no host port; reach it with `docker compose exec postgres psql -U vouch vouch`.

Then create the first admin (§5).

## 3. Environment variables

Every application setting is a `VOUCH_` environment variable. [`.env.example`](../.env.example) lists each one with a comment and a safe placeholder, and `tests/test_config.py` fails if a setting is missing from it. In compose, every service reads `.env` (`env_file`), and `compose.yml` sets `VOUCH_DATABASE_URL` itself so the containers reach the `postgres` service.

Required, or to review before any shared deployment:

| Variable | Notes |
|---|---|
| `POSTGRES_PASSWORD` | Compose only. The `postgres` service's password; `compose.yml` refuses to start without it. Not read by the application. |
| `API_HOST_PORT` | Compose only. Host port for the API (default 8000). |
| `VOUCH_ENVIRONMENT` | `development`, `test`, `staging`, `production` or `phase0`. A Phase 0 deployment uses `phase0`. See the start-up rules below. |
| `VOUCH_DATABASE_URL` | `postgresql+asyncpg://user:password@host:5432/db`. Set by `compose.yml`; set it yourself anywhere else. |
| `VOUCH_JWT_SECRET` | Token signing key: a random value of 32 bytes or more (for example `python -c "import secrets; print(secrets.token_urlsafe(48))"`). It also keys the OTP code hashes. |
| `VOUCH_JWT_TTL_MINUTES` | Access-token lifetime (default 7 days). |
| `VOUCH_CORS_ORIGINS` | JSON array of the web client origins. |
| `VOUCH_LAUNCHED_CITIES` | Comma-separated city slugs open for inventory. |
| `VOUCH_DEBUG` | `false` outside local development. |
| `VOUCH_OTP_SENDER`, `VOUCH_KYC_PROVIDER`, `VOUCH_PAYMENT_PROVIDER` | `fake` or unset. |
| `VOUCH_FAKE_PAYMENT_WEBHOOK_SECRET` | HMAC key for the fake payment provider's webhooks. |
| `VOUCH_COMMISSION_RATE`, `VOUCH_DAMAGE_CONTRIBUTION_RATE`, `VOUCH_RENTER_CANCELLATION_FEE_RATE`, `VOUCH_CANCELLATION_FEE_VENDOR_SHARE` | Placeholder policy rates, frozen into each booking at request time. |

The remaining settings (`VOUCH_APP_NAME`, `VOUCH_APP_VERSION`, `VOUCH_API_V1_PREFIX`, the pool sizes, `VOUCH_BUSINESS_TIMEZONE`, the booking limits, `VOUCH_MAX_REQUEST_BYTES`) have working defaults. The image healthcheck assumes the default `VOUCH_API_V1_PREFIX` (`/api/v1`).

**Start-up rules already enforced** (`app/core/config.py`). A process with an invalid configuration exits at start-up instead of running:

- In `staging`, `production` and `phase0`, `VOUCH_JWT_SECRET` must not be the `change-me` placeholder and must be at least 32 bytes.
- In `staging` and `production`, `fake` is refused for `VOUCH_OTP_SENDER`, `VOUCH_KYC_PROVIDER` and `VOUCH_PAYMENT_PROVIDER`. Leave them unset there: OTP requests, KYC and accepting a booking then answer 503 until real adapters exist (M3b).
- In `production` and `phase0`, `VOUCH_DEBUG=true` is refused, and the OpenAPI pages (`/docs`, `/redoc`, `/openapi.json`) are not served.

**Phase 0 runs as `VOUCH_ENVIRONMENT=phase0`** (amendment A20). It is the one shared environment where the fake OTP sender, KYC provider and payment provider are allowed; unset, each of the three settings means the fake. Everything else is held to production's rules: a strong `VOUCH_JWT_SECRET`, a strong `VOUCH_FAKE_PAYMENT_WEBHOOK_SECRET` (checked whenever the fake payment provider is in use), `VOUCH_DEBUG=false`, and no interactive API docs. `development` and `test` are for a local checkout and CI only; never deploy them. `production` still refuses the fakes.

## 4. Migrations

The `migrate` service runs `alembic upgrade head` on every `docker compose up`; it is a no-op when the database is already at head. To run it by hand, for example before rolling out a new image:

```bash
docker compose build
docker compose run --rm migrate                      # alembic upgrade head
docker compose run --rm migrate alembic current      # the revision the database is at
docker compose up -d api timers                      # then restart onto the new image
```

Outside compose, run the image with the `alembic upgrade head` command and the same environment, once, before starting the new API and jobs.

- Take a backup (§6) before any migration in production.
- Several downgrades lose data (each migration's docstring says which). Recover from a bad migration by restoring the backup, not by `alembic downgrade`.
- CI runs `alembic check` against a database at head, so a model change without its migration fails the build.

## 5. The first admin

Nothing in the API creates an admin. Bootstrap one from the image:

```bash
docker compose run --rm --no-deps api python -m app.cli create-admin --phone +234XXXXXXXXXX --name "Full Name"
```

- The phone is normalised as the OTP flow does (`0XXXXXXXXXX`, `234XXXXXXXXXX` and `+234XXXXXXXXXX` are accepted).
- It prints only the new user's id. The admin then logs in with the normal phone OTP flow.
- It writes the audit event `user.admin_created` (`actor_role = 'system'`).
- Re-running it for an existing active admin changes nothing and prints the same id (exit 0).
- It refuses (exit 1, nothing changed) a phone that belongs to a renter or a vendor, or to an inactive or soft-deleted admin (amendment A21): a role is never changed silently and an account is never reactivated. A malformed phone, or a name that is blank or longer than 200 characters, is exit 2.

Keep admin accounts to one or two people; there is no admin MFA in M3a.

## 6. Nightly backup

`scripts/backup.sh` writes a `pg_dump --format=custom` archive to `BACKUP_DIR/vouch-<UTC timestamp>.dump`, checks that the archive reads back (`pg_restore --list`), and prints its path. It exits non-zero, leaving no file, on any failure. It does not upload anything: copying the file off the host, encrypting it at rest and pruning old files are the operator's job.

| Variable | Meaning |
|---|---|
| `BACKUP_DATABASE_URL` | libpq URL of the database. Falls back to `VOUCH_DATABASE_URL`; the `postgresql+asyncpg://` form is accepted. |
| `BACKUP_DIR` | Target directory (default `./backups`). |

It needs `pg_dump` and `pg_restore` from PostgreSQL 16 or newer. On a compose host, the `postgres:16` image has them, so run the script from that image on the compose network:

```bash
docker compose run --rm --no-deps \
  -v "$PWD/scripts:/scripts:ro" -v /var/backups/vouch:/backups \
  -e BACKUP_DATABASE_URL="postgresql://vouch:${POSTGRES_PASSWORD}@postgres:5432/vouch" \
  -e BACKUP_DIR=/backups \
  --entrypoint /scripts/backup.sh postgres
```

Schedule it nightly (cron or a systemd timer) at a quiet hour, for example 02:00 Africa/Lagos, and alert on a non-zero exit. A managed PostgreSQL service should also have its own encrypted automated backups and point-in-time recovery turned on; the logical dump is the copy you control and can restore anywhere.

## 7. Restore drill

`scripts/restore_check.sh <dump>` proves that a backup can be restored and that its ledger is sound. It creates a scratch database `vouch_restore_check_<UTC timestamp>` on `RESTORE_SERVER_URL`'s server, restores the dump into it, prints the migration revision and some row counts, and runs `scripts/ledger_check.sql`. It exits 0 only if the restore succeeds and the check returns no rows. It drops the scratch database on exit (set `KEEP_RESTORE_DB=1` to keep it for inspection).

```bash
docker compose run --rm --no-deps \
  -v "$PWD/scripts:/scripts:ro" -v /var/backups/vouch:/backups \
  -e RESTORE_SERVER_URL="postgresql://vouch:${POSTGRES_PASSWORD}@postgres:5432/postgres" \
  --entrypoint /scripts/restore_check.sh postgres /backups/vouch-20261104T010000Z.dump
```

`RESTORE_SERVER_URL` names a maintenance database (such as `postgres`) and a role that may `CREATE DATABASE`. Prefer a separate scratch server to the production one.

Run the drill at least monthly, on the latest nightly file, and before the API handles real bookings or payment records. Record the date, the dump file and the result.

`scripts/ledger_check.sql` can also run against the live database at any time (`psql "$URL" -f scripts/ledger_check.sql`). It returns no rows when healthy, and one row per problem:

- `unbalanced_txn_group`: a `txn_group` whose debits differ from its credits;
- `booking_balance_mismatch`: a booking whose booking-scoped `renter_deposit_liability` or `protection_pool` balance differs from what the `docs/SCHEMA.md` §8 recipes leave in its current status (the deposit hold D while paid and live, 0 after; the damage contribution C once paid, unless a cancellation refunded it). The comment at the top of the file has the full table and what it does not check;
- `booking_snapshot_missing_deposit`: a booking whose `price_snapshot` has no deposit, so its balances cannot be judged.

Bookings backfilled by migration 0003 (`price_snapshot.basis = "legacy"`) have no ledger postings and are skipped.

Any row is an incident: stop deploys, keep the evidence, and reconcile before posting a reversing entry through a reviewed script (never an API, never an UPDATE).

## 8. Deploy freeze

**No deploys from Thursday 12:00 to Monday 09:00 Africa/Lagos** (the PRD rule, M3a §2.3). That covers new images, migrations and configuration changes.

Outside the window, a deploy is: take a backup (§6), build, run `migrate`, restart `api` and `timers`, then check `docker compose ps` (api healthy) and `GET /api/v1/health/ready`.
