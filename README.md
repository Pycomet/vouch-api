# Vouch

Vouch is a trusted rental marketplace for high-value assets, starting with media and creative gear. It launches in Abuja first (Phase 0, then v1), with Lagos after. The product rests on verified renter identities, one upfront payment to the platform (the deposit hold, the rental and any damage contribution), and co-signed handover evidence.

This repository is the API: a FastAPI modular monolith on PostgreSQL. Payments, KYC, phone OTP and notifications sit behind interfaces. In Milestone 2 those interfaces have fake implementations only (see *Providers* below).

## What is implemented (Milestone 2)

- Phone OTP login that issues HS256 JWTs, with role-checked routes, and the user reloaded from the database on every request.
- Renter KYC (`POST /me/kyc`) and trust tiers 0–3.
- Public inventory (`/assets`, `/catalogue/search`) over the catalogue, the reference tables and the launched-cities gate.
- The booking state machine: request, approve, accept, payment, cancel / withdraw, no-show, handover, return, claim, resolve, plus timers. It has an append-only, double-entry ledger, an append-only audit trail, and a money line computed on each read.
- Payment-provider webhooks, idempotent through `webhook_events`.
- Renter and vendor home screens.

Reference docs:

- [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md): endpoints, roles, error codes, the money line. The OpenAPI document at `/docs` is the authoritative schema.
- [`docs/SCHEMA.md`](docs/SCHEMA.md) and [`docs/erd.svg`](docs/erd.svg): every table, constraint and trigger, the transition table, and the ledger recipes.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/OPERATIONS-NOTES.md`](docs/OPERATIONS-NOTES.md): the module layout, the known gaps, and what real adapters need first.
- [`docs/specs/M2.md`](docs/specs/M2.md) and [`docs/specs/M2-amendments.md`](docs/specs/M2-amendments.md): the milestone spec and its amendments.

## Setup

Requirements: Python 3.11+ and PostgreSQL 16 (with the `pgcrypto` and `btree_gist` extensions available; the first migration creates them).

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

For a local database:

```bash
docker run -d --name vouch-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=vouch -p 5432:5432 postgres:16
```

## Configuration

Every setting is an environment variable with the `VOUCH_` prefix. They can also go in `.env`. **[`.env.example`](.env.example) lists every key with a comment**, and a test fails if a setting is missing from it. The keys you are most likely to touch:

| Key | Purpose |
|---|---|
| `VOUCH_ENVIRONMENT` | `development`, `test`, `staging` or `production` |
| `VOUCH_DATABASE_URL` | `postgresql+asyncpg://…` |
| `VOUCH_LAUNCHED_CITIES` | Comma-separated city slugs open for inventory (default `abuja`) |
| `VOUCH_JWT_SECRET` | Token signing key. Outside development and test, `change-me` or anything under 32 bytes is refused. |
| `VOUCH_OTP_SENDER`, `VOUCH_KYC_PROVIDER`, `VOUCH_PAYMENT_PROVIDER` | `fake`, or unset (see *Providers*) |
| `VOUCH_FAKE_PAYMENT_WEBHOOK_SECRET` | HMAC key for the fake provider's webhook signatures |
| `VOUCH_COMMISSION_RATE`, `VOUCH_DAMAGE_CONTRIBUTION_RATE`, `VOUCH_RENTER_CANCELLATION_FEE_RATE`, `VOUCH_CANCELLATION_FEE_VENDOR_SHARE` | Placeholder policy rates pending legal advice. Each booking freezes them at request time. |
| `VOUCH_BUSINESS_TIMEZONE` | Business-day boundaries (default `Africa/Lagos`) |

## Migrations

```bash
alembic upgrade head      # 0001 … 0005
alembic downgrade -1      # one step back (several downgrades are lossy; see each migration's docstring)
```

Alembic reads `VOUCH_DATABASE_URL`.

## Running the API

```bash
uvicorn app.main:app --reload --app-dir src
```

The API is served under `/api/v1`. Outside production, the OpenAPI UI is at `/docs` (also `/redoc` and `/openapi.json`). `GET /api/v1/health/live` checks the process; `GET /api/v1/health/ready` checks the database.

## Running the timer job

The timers expire unanswered requests and unpaid acceptances, complete returned bookings once their claim window closes, and flag late returns as at risk. They run as a separate process, from the same code and settings:

```bash
python -m app.jobs.timers          # every 60 seconds until SIGINT/SIGTERM
python -m app.jobs.timers --once   # one run, then exit (non-zero if the run fails), e.g. from cron
```

Each run logs one `timer_run …` line with its counts. Running it twice in a row is safe: the second run does nothing.

## Providers (fakes only in M2)

The OTP sender, the KYC provider and the payment provider each have one implementation in M2, an in-memory **fake**. No SMS, KYC or Paystack SDK is a dependency.

- In `development` and `test`, the fakes are used by default.
- In `staging` and `production`, `fake` is refused at start-up and no real adapter exists yet. Leave these settings unset there, and the API answers **503** for OTP requests (`otp_unavailable`), KYC (`kyc_unavailable`) and accepting a booking (`payment_unavailable`) until real adapters land. See `docs/OPERATIONS-NOTES.md` for what they need first.

## Tests and quality checks

```bash
ruff format --check .
ruff check .
VOUCH_TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/vouch_test \
  pytest --cov=app --cov-report=term-missing
```

Without `VOUCH_TEST_DATABASE_URL`, the tests that need a live PostgreSQL (migrations, constraints, triggers, the booking flows) are skipped. The live tests migrate the database they are given and leave it at `head`, so point them at a throwaway database such as `vouch_test` (`createdb vouch_test`, or `-e POSTGRES_DB=vouch_test` on the container above). Coverage must stay at 90% or more. CI runs the same three commands against a Postgres 16 service.

After a schema change, regenerate the ERD (`tests/test_docs_schema.py` fails until you do):

```bash
python scripts/gen_erd.py
npx -y @mermaid-js/mermaid-cli -i docs/erd.mmd -o docs/erd.svg
```

## Wording

The product describes money as a **deposit hold** and an optional **damage contribution**. `tests/test_wording.py` keeps two terms of art out of the code and docs.

## Backups

Production PostgreSQL must use encrypted automated backups, point-in-time recovery and a regular restore drill. These must be in place and checked before the API handles real bookings or payment records.
