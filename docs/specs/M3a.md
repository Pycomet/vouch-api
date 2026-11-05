# Vouch Backend Spec: Milestone 3a. Run Phase 0 on the platform

**Repo:** `github.com/Pycomet/vouch-api`. Work from `main`.
**Version:** 1.0, 4 Nov 2026. Written against `main` at commit `2d75f31` (M2 step 6 merged; 1,171 tests passing, 98.85% coverage).
**Copy this file into the repo as `docs/specs/M3a.md` in your first commit.** It is the source of truth for M3a. Where it contradicts the code, the code wins until you have stopped and asked (see §0.3).
**Read first:** `docs/ARCHITECTURE.md`, `docs/SCHEMA.md`, `docs/API_CONTRACT.md`, `docs/OPERATIONS-NOTES.md`, `docs/specs/M2.md`, `docs/specs/M2-amendments.md`.

---

## 0. Brief for the agent

### 0.1 What Vouch is, and why this milestone exists

Vouch is a managed peer-to-peer rental marketplace for creative gear (cameras, lenses, lighting, audio, drones, gimbals, accessories). It launches in Abuja, with Lagos next. Vendors list gear, renters are ID-verified and earn trust tiers, Vouch holds the deposit, and both sides co-sign a photo checklist at handover and return.

M2 built the booking engine: the state machine, the ledger, OTP, KYC and payment seams, webhooks. It cannot yet be *operated*. Today:

- nothing in the code can create a vendor or an admin (`POST /auth/otp/verify` only ever creates renters);
- vendors cannot create listings or block dates, and there is no catalogue lookup or catalogue seed, so a vendor could not pick an item even if they could list;
- admins can only call `resolve`; they cannot review a listing or a KYC case, mark an item lost, or see a refund, a payout, a disputed booking or a failed webhook;
- a renter frozen by the late-return rule is never unfrozen;
- notifications are sent before the transaction commits, and there is no way to run the service anywhere (no Dockerfile).

M3a closes those gaps so that Phase 0 (hand-brokered rentals, run by the founder and two ambassadors) can run on the platform with fake providers behind it. Real Paystack, KYC and SMS adapters, photo storage and the web client are **M3b** (§10).

### 0.2 Order of work (one PR per step; stop and report after each)

| # | Step | Spec | Est. days | Done when |
|---|---|---|---|---|
| 1 | Deployable service and admin bootstrap | §2 | 1.0 | `docker compose up` runs migrate, api, timers; CI builds the image; `create-admin` works |
| 2 | Migration `0006_vendor_ops_outbox`, models, constraint tests | §3 | 0.75 | Every check in §3.6 passes; `upgrade head` and `downgrade -1` work on an empty DB and on one seeded at 0005 |
| 3 | Catalogue, vendor profiles, vendor listings, availability blocks | §4 | 2.25 | §4 endpoints, rules and tests pass; Phase 0 vendor can be onboarded and list a unit end to end |
| 4 | Admin operations | §5 | 2.0 | §5 endpoints and tests pass; every manual money flow in `OPERATIONS-NOTES.md` §1 has an admin route |
| 5 | Transactional outbox and dispatcher | §6 | 1.25 | No notification exists for a change that did not commit; dispatcher retries and dead-letters |
| 6 | Hardening and docs | §7, §9 | 1.0 | Timeout, OTP caps, drift check, regenerated docs; full suite green, coverage ≥ 90% |
| | **Total** | | **~8.25** | |

If a step takes more than 1.5 times its estimate, stop and report instead of pushing on.

### 0.3 Hard rules

1. **No real payment, KYC, SMS or WhatsApp integrations, and no new third-party runtime dependencies.** Fakes and interfaces only. Adapters are M3b.
2. **The database enforces the rules.** Use CHECK constraints, foreign keys, partial unique indexes and exclusion constraints. The service layer adds to them; it never replaces them.
3. **Money** is `Numeric(12,2)` NGN and Python `Decimal`. No floats, no arithmetic outside `Decimal`. API money fields are strings (`MoneyStr`), as in M2.
4. **Every state change writes an audit event** (`record_audit`, with the actor, a `reason` where this spec asks for one, and the request id). **Every movement of money writes ledger entries.** The ledger and audit tables stay append-only. Corrections are reversing entries, never updates. M3a adds **no** endpoint that writes ledger entries directly.
5. **Wording.** Never use the two words banned by `tests/test_wording.py` in code, comments, API fields or docs. Say "deposit hold" and "damage contribution". That test scans `docs/`, and this spec is written to pass it; keep it passing in `docs/specs/M3a.md` and every doc you add.
6. **Reference data** keeps the live request portal's slugs: cities `abuja`, `lagos`; categories `camera`, `lens`, `lighting`, `audio`, `drone`, `gimbal`, `accessory`. Cities come from `VOUCH_LAUNCHED_CITIES`; never hard-code one.
7. **Stay in scope.** §10 lists what is out. Do not add it, even if it looks small.
8. **No secrets in the repo.** `.env.example` holds placeholders only.
9. **Deviations.** Record every deviation from this spec in `docs/specs/M3a-amendments.md`, numbered from **A19** (M2 ended at A18), in the same table format as `M2-amendments.md`. If the spec contradicts a hard rule, or you need to change something a rule protects, **stop and ask**. Do not guess.
10. **Commits and PRs.** Small commits with clear messages. One PR per step against `main`. CI (ruff format, ruff lint, pytest with coverage) must be green.

### 0.4 Report back after each step

- what was built, with the PR link;
- test results (counts, anything skipped);
- anything in the spec that was unclear or wrong, and the choice you made;
- anything you would change in later steps because of what you learned.

---

## 1. Scope and non-negotiables

**In:** a runnable container image and compose file; an admin bootstrap command; catalogue lookup, admin catalogue management and a seed loader; vendor profiles; vendor listing management with a review workflow; vendor availability blocks; the admin operations in §5; a transactional outbox with a dispatcher and a logging channel; the hardening in §7.

**Out:** see §10.

**Non-negotiables carried from M2:** the database enforces invariants; booking state is the only source of truth; the ledger is append-only and balanced per `txn_group`; public output never contains a serial number, vendor identity, condition notes or review data (`PublicAsset` is unchanged).

**Three findings from reading the code that this spec fixes** (confirm each, then fix as described):

1. `request_booking` checks blocked windows with a plain SELECT and does **not** lock the asset row. A vendor block and a booking request for the same dates can both succeed. §4.6 requires one lock order for both.
2. `users.restricted_at` is set by the late-return rule and by `mark_lost`, and is **never cleared**. A renter who returns one day late is frozen forever. §5.5 adds an admin unrestrict.
3. `assets.category_id` and `assets.catalogue_item_id` are independent FKs, so an asset can name a lens catalogue item and a camera category. §3.3 adds a composite FK.

---

## 2. Step 1: Deployable service and admin bootstrap

Provider-neutral. **No cloud SDK and no Terraform in this repo.** Infrastructure code will live in a separate repo; this step only makes the service portable.

### 2.1 Container image

- `Dockerfile`: multi-stage, `python:3.11-slim`, installs the package without dev extras, runs as a non-root user, `PYTHONUNBUFFERED=1`, no `.env` copied in. `.dockerignore` excludes `.git`, tests, docs, caches and `.env*`.
- One image, three commands (selected by `command:` in compose, not three images):
  - API: `uvicorn` on port 8000 (use the app entry the README already documents);
  - timers: `python -m app.jobs.timers`;
  - outbox dispatcher: `python -m app.jobs.outbox` (added in step 5; until then the compose service is commented out with a note).
- `HEALTHCHECK` on the API uses `GET /api/v1/health/ready` through the Python standard library (no `curl` in the image).
- **Migrations are a separate one-shot command** (`alembic upgrade head`), never run on API start. The API and jobs wait for it in compose (`depends_on: condition: service_completed_successfully`).
- Do not trust `X-Forwarded-For` by default. Proxy header handling is configured in §7.2 only.

### 2.2 Compose

`compose.yml` for local and single-host use: `postgres:16` (named volume, healthcheck), `migrate`, `api`, `timers`. Environment comes from an `.env` file; document each variable. Include the production-safety settings that are already enforced at start-up (JWT secret, fake providers refused outside development and test).

### 2.3 Scripts and CI

- `scripts/backup.sh`: `pg_dump --format=custom` to a dated file in a configurable directory; exits non-zero on failure. Provider-neutral; uploading the file is the operator's job.
- `scripts/restore_check.sh`: restores a dump into a throwaway database and runs `scripts/ledger_check.sql`.
- `scripts/ledger_check.sql`: returns zero rows when healthy. It must find (a) any `txn_group` whose debits differ from its credits, (b) any booking whose booking-scoped accounts do not net to the amount the `docs/SCHEMA.md` §8 recipes say they should in its current status, if that can be expressed simply; otherwise (a) alone, and say so in the PR.
- CI gains a `docker build` job (no push) and an `alembic check` step (fails if the models and migrations drift).
- `docs/DEPLOY.md` (new): required environment variables, running migrations, the nightly backup and the restore drill, and the deploy freeze rule from the PRD (no deploys Thursday 12:00 to Monday 09:00 Africa/Lagos).

### 2.4 Admin bootstrap

Nothing creates an admin today. Add `python -m app.cli create-admin --phone +234XXXXXXXXXX --name "…"`:

- normalises the phone as the OTP flow does;
- creates a user with role `admin`, or exits with an error if the phone already belongs to a user with another role (never silently change a role);
- writes an audit event (`actor_role='system'`, action `user.admin_created`);
- prints the user id and nothing sensitive.

Tests: creates; refuses an existing renter; refuses a bad phone; idempotent on an existing admin (exit 0, no second row).

---

## 3. Step 2: Migration `0006_vendor_ops_outbox`

`revision = "0006_vendor_ops_outbox"`, `down_revision = "0005_otp_challenges"`. One reversible revision. Update the models, `docs/SCHEMA.md` and `docs/erd.svg` in step 6.

### 3.1 `vendor_profiles`

```sql
CREATE TYPE vendorstatus AS ENUM ('pending', 'active', 'suspended');
CREATE TYPE vendorbusinesstype AS ENUM ('rental_house', 'individual');

CREATE TABLE vendor_profiles (
  user_id                   UUID PRIMARY KEY REFERENCES users(id),
  status                    vendorstatus NOT NULL DEFAULT 'pending',
  business_type             vendorbusinesstype NOT NULL,
  is_sub_broker             BOOLEAN NOT NULL DEFAULT false,   -- self-declared, shown on the profile
  business_name             TEXT,
  business_registration_ref TEXT,
  bank_name                 TEXT,
  account_name              TEXT,
  account_last4             CHAR(4),                          -- the full account number is never stored
  recipient_code            TEXT UNIQUE,                      -- the provider's transfer recipient, entered by ops
  verified_by               UUID REFERENCES users(id),
  verified_at               TIMESTAMPTZ,
  suspended_at              TIMESTAMPTZ,
  suspended_reason          TEXT,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_vendor_profiles_active_verified
    CHECK (status <> 'active' OR verified_at IS NOT NULL),
  CONSTRAINT ck_vendor_profiles_last4_digits
    CHECK (account_last4 IS NULL OR account_last4 ~ '^[0-9]{4}$'),
  CONSTRAINT ck_vendor_profiles_suspension
    CHECK ((status = 'suspended') = (suspended_at IS NOT NULL AND suspended_reason IS NOT NULL))
);
```

Backfill: every existing user with role `vendor` gets a profile with status `active`, `business_type = 'individual'`, `verified_at = users.created_at`, bank fields NULL. (No production data exists yet; this keeps seeded test databases working.) The service, not the CHECK, requires bank fields before a *new* activation (§5.1).

The full bank account number is **not stored anywhere** (restricted financial data; NDPA advice is pending). `account_name` is what the vendor's bank returned or ops read from the account; ops compares it with the verified name by eye.

### 3.2 Listing columns

```sql
ALTER TABLE assets ADD COLUMN paused_by TEXT;
ALTER TABLE assets ADD CONSTRAINT ck_assets_paused_by_values
  CHECK (paused_by IN ('vendor', 'admin'));
ALTER TABLE assets ADD CONSTRAINT ck_assets_paused_by_matches_status
  CHECK ((listing_status = 'paused') = (paused_by IS NOT NULL));
-- Backfill: every currently paused asset gets paused_by = 'admin' (mark_lost pauses are admin acts).

CREATE UNIQUE INDEX uq_assets_vendor_item_serial
  ON assets (vendor_id, catalogue_item_id, lower(serial_number))
  WHERE deleted_at IS NULL AND serial_number IS NOT NULL;
```

Update `_mark_lost` in `app/services/booking.py` to set `paused_by = 'admin'` when it pauses the asset. If the unique index cannot be created because a seeded database holds duplicates, fail loudly with a clear message; do not delete rows.

### 3.3 Category consistency

```sql
ALTER TABLE catalogue_items ADD CONSTRAINT uq_catalogue_items_id_category UNIQUE (id, category_id);
ALTER TABLE assets ADD CONSTRAINT fk_assets_item_category
  FOREIGN KEY (catalogue_item_id, category_id) REFERENCES catalogue_items (id, category_id);
```

Backfill first: set `assets.category_id` from the item where they disagree, and log the count.

### 3.4 Webhook event resolution

```sql
ALTER TABLE webhook_events
  ADD COLUMN resolution TEXT,
  ADD COLUMN resolved_at TIMESTAMPTZ,
  ADD COLUMN resolved_by UUID REFERENCES users(id),
  ADD COLUMN resolution_note TEXT;
ALTER TABLE webhook_events ADD CONSTRAINT ck_webhook_events_resolution_values
  CHECK (resolution IN ('returned', 'reposted', 'written_off'));
ALTER TABLE webhook_events ADD CONSTRAINT ck_webhook_events_resolution_complete
  CHECK ((resolution IS NULL) = (resolved_at IS NULL)
     AND (resolution IS NULL) = (resolved_by IS NULL)
     AND (resolution IS NULL) = (resolution_note IS NULL));
```

Only events with a `processing_error` may be resolved (service rule, §5.7). `reposted` means a developer posted corrective ledger entries through a reviewed script; M3a provides no endpoint that does this (hard rule 4).

### 3.5 Outbox and OTP

```sql
CREATE TYPE outboxstatus AS ENUM ('pending', 'sent', 'dead');

CREATE TABLE notification_outbox (
  id              BIGSERIAL PRIMARY KEY,
  user_id         UUID REFERENCES users(id),     -- NULL = an operations alert (template ops.*)
  template        TEXT NOT NULL,
  data            JSONB NOT NULL DEFAULT '{}',   -- may hold phone numbers: never log it
  status          outboxstatus NOT NULL DEFAULT 'pending',
  attempts        SMALLINT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at         TIMESTAMPTZ,
  CONSTRAINT ck_outbox_sent_at CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
  CONSTRAINT ck_outbox_attempts_nonnegative CHECK (attempts >= 0)
);
CREATE INDEX ix_outbox_due ON notification_outbox (next_attempt_at, id) WHERE status = 'pending';

ALTER TABLE otp_challenges ADD COLUMN ip_hash TEXT;   -- HMAC of the client IP, keyed like code_hash; NULL when unknown
CREATE INDEX ix_otp_challenges_ip_created ON otp_challenges (ip_hash, created_at) WHERE ip_hash IS NOT NULL;
```

### 3.6 Migration acceptance

- [ ] `alembic upgrade head` and `downgrade -1` succeed on an empty database and on one seeded at 0005 (with a vendor, assets, a paused asset, bookings and webhook events).
- [ ] Backfill: legacy vendors get active profiles; the paused asset gets `paused_by='admin'`; an asset with a mismatched category is corrected.
- [ ] Test: `active` without `verified_at` is refused; `suspended` without a reason is refused; a bad `account_last4` is refused.
- [ ] Test: `paused` without `paused_by`, and `paused_by` on a non-paused asset, are both refused.
- [ ] Test: a second asset with the same vendor, item and serial (any letter case) is refused; the same serial under another vendor is allowed; a soft-deleted duplicate does not block.
- [ ] Test: an asset whose `category_id` differs from its catalogue item's is refused.
- [ ] Test: a resolution without all four fields is refused.
- [ ] Test: an `ok` outbox row cannot be `sent` without `sent_at`.
- [ ] `alembic check` reports no drift between models and migrations.

---

## 4. Step 3: Catalogue, vendor profiles, vendor listings, availability blocks

All routes are under `/api/v1`. Use the M2 conventions: the error envelope, `MoneyStr`, cursor `Page[...]`, OpenAPI examples for every endpoint (`tests/test_openapi_examples.py` enforces this), and `documented(...)` response maps.

### 4.1 Catalogue lookup (public)

| Method | Path | Notes |
|---|---|---|
| GET | `/catalogue/items?q=&category=&limit=&cursor=` | Active items only. `q` uses the same search vector as `/catalogue/search`. Returns `id, category, make, model, mount, specs, replacement_value_band`. No auth (the data is not sensitive). |

### 4.2 Catalogue seed loader

`python -m app.cli seed-catalogue --file path.csv`: idempotent upsert on `(make, model)`. Columns: `category,make,model,mount,band,aliases` (aliases separated by `|`; band `A|B|C`; category must be a known slug). Rejects the whole file on the first bad row, naming the line; prints inserted/updated/unchanged counts. Commit a 10-row sample at `data/catalogue_seed.sample.csv` for tests. **Do not invent the real ~150-item catalogue or its bands**: the owner supplies it (§11, D2).

### 4.3 Vendor profile (read-only for the vendor)

| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/vendor/profile` | vendor | Status, business type, `is_sub_broker`, business name, bank name, account name, `account_last4`, whether `recipient_code` is set (the code itself is not returned). Vendors **cannot edit bank details**: a changed payout account is a fraud vector, so only admins change it (§5.1). |

### 4.4 Vendor listings

A vendor whose profile is not `active` cannot create or submit listings (409 `vendor_not_active`). `GET`s still work.

| Method | Path | Notes |
|---|---|---|
| POST | `/vendor/assets` | Create a listing as `draft`. 201 `OwnAsset`. |
| GET | `/vendor/assets?status=&limit=&cursor=` | The vendor's own listings (excluding soft-deleted), newest first. |
| GET | `/vendor/assets/{id}` | Own listing, including serial number, condition notes, review reason and `paused_by`. Someone else's id is 404. |
| PATCH | `/vendor/assets/{id}` | Partial update; rules in §4.5. |
| POST | `/vendor/assets/{id}/submit` | `draft` or `rejected` → `review`. Requires a complete listing. |
| POST | `/vendor/assets/{id}/pause` | `active` → `paused` (`paused_by='vendor'`). |
| POST | `/vendor/assets/{id}/resume` | `paused` → `active`, **only if `paused_by='vendor'`** (else 409 `invalid_transition`). |
| DELETE | `/vendor/assets/{id}` | Soft delete (`deleted_at`), 204. Refused with 409 `has_live_bookings` while any booking on the asset is in `LIVE_BOOKING_STATUSES` or `disputed`. |

**Create body:** `catalogue_item_id`, `city` (slug, must be launched), `serial_number`, `condition_grade` (`excellent|good|fair`), `daily_rate`, `deposit_amount`, optional `weekend_rate`, `weekly_rate`, `description`, `condition_notes`, `included_accessories` (list of strings), `fulfilment` (`{"pickup": bool, "delivery": bool, "delivery_fee": "…"}`), optional `minimum_trust_tier`.

- `category_id` is copied from the catalogue item, never accepted from the client.
- `minimum_trust_tier` defaults from the item's band (A→1, B→2, C→3, amendment A3). A vendor may **raise** it (up to 3) but not lower it below the band default. Only an admin can lower it (§5.3).
- An inactive catalogue item is refused (409 `catalogue_item_inactive`).
- Duplicate serial for the same vendor and item is 409 `duplicate_serial` (the §3.2 index, translated).

**`submit` completeness** (409 `listing_incomplete`, `details: {missing: [...]}`): serial number, condition grade, `daily_rate > 0`, `deposit_amount >= 0`, at least one of pickup or delivery enabled, and a delivery fee present when delivery is enabled.

`OwnAsset` is a new schema. `PublicAsset` is unchanged and must keep hiding the serial number (the existing test stays green).

### 4.5 Listing state machine and edit rules

```
draft ──submit──► review ──approve (admin)──► active ◄──resume──┐
  ▲                 │                          │  ▲             │
  │                 └──reject (admin)──► rejected │  └──pause──► paused
  └─────────────────── (edit, then submit) ─────┘
```

- `rejected → review` by `submit` after the vendor edits.
- `review` is **locked**: vendor edits get 409 `listing_locked` until an admin approves or rejects.
- **Identity fields** (`catalogue_item_id`, `serial_number`, `city`) are editable only in `draft` or `rejected`.
- **Commercial fields** (`daily_rate`, `weekend_rate`, `weekly_rate`, `deposit_amount`, `included_accessories`, `fulfilment`, `description`, `condition_notes`, `condition_grade`) are editable in `draft`, `rejected`, `active` and `paused`. A change never touches an existing booking, because each booking froze `price_snapshot` at request time. Add a test that proves it.
- `minimum_trust_tier` is not vendor-editable after create (admin only).
- Edits write `asset.updated` with a before/after of the changed fields only.
- Pausing an asset with live bookings is allowed; those bookings continue. Pausing only removes it from search.

### 4.6 Availability blocks and the lock order

| Method | Path | Notes |
|---|---|---|
| POST | `/vendor/assets/{id}/blocks` | Body `{starts_on, ends_on}` (inclusive). 201. Creates an `availability_windows` row with `is_blocked = true`. |
| GET | `/vendor/assets/{id}/blocks?from=` | Blocks ending on or after `from` (default today in the business zone). |
| DELETE | `/vendor/assets/{id}/blocks/{block_id}` | 204. |

Rules: `starts_on <= ends_on`; a block may not start in the past (400 `invalid_request`); a block that overlaps a **live booking** is refused (409 `dates_unavailable`); a block that overlaps another block is refused (409 `dates_blocked`, translating the exclusion constraint). Audit `availability.blocked` and `availability.unblocked`.

**Race fix (finding 1):** both block creation and `request_booking` must take `SELECT … FOR UPDATE` on the **asset row first**, then check blocks and live bookings, in that order. Add a concurrency test: a block and a booking request for the same dates fired together; exactly one succeeds, and the other fails with the right code. Add a test that two concurrent requests for the same unit still resolve to one (the M2 test must stay green).

### 4.7 Suspended vendors

Make the vendor profile part of visibility and booking guards:

- Public search and detail require `vendor_profiles.status = 'active'` (a suspended vendor's listings vanish from `/assets` and `/catalogue/search`; their detail is 404).
- `request_booking` on such an asset is 404 (it is hidden), consistent with an unpublished asset.
- A suspended vendor **cannot accept** a new request (409 `vendor_not_active`) but **can still sign handovers and returns** on live bookings. Do not set `users.is_active = false` for a suspension: that would lock them out of live bookings.

### 4.8 New error codes

Add to `docs/API_CONTRACT.md` §6 with the same table format: `vendor_not_active` (409), `listing_locked` (409), `listing_incomplete` (409, `details.missing`), `catalogue_item_inactive` (409), `duplicate_serial` (409), `has_live_bookings` (409), `role_conflict` (409), `refund_not_pending` (409), `already_resolved` (409), `catalogue_item_exists` (409). Reuse `invalid_transition` (details `{from, event}`) for illegal listing and vendor transitions.

---

## 5. Step 4: Admin operations

Every route here requires role `admin` (401/403 as elsewhere). Every write is one transaction, writes an audit event with the actor, the request id and a `reason` where noted, and uses the injected clock. Lists use cursor pagination, newest first unless stated.

### 5.1 Vendors

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/admin/vendors` | `phone, full_name, business_type, is_sub_broker, business_name?, business_registration_ref?` | Creates a user with role `vendor` and a `pending` profile. If the phone is an existing **renter** with no live bookings and no open claims, promote that user instead; any other existing role, or a renter with live bookings, is 409 `role_conflict`. Audit `vendor.created`. The vendor then logs in with phone OTP. (A promoted user's old token stops working because the stored role changed; they log in again.) |
| GET | `/admin/vendors?status=&missing_bank=` | | List with the user's verified name next to `account_name`. |
| PATCH | `/admin/vendors/{id}/bank` | `bank_name, account_name, account_last4, recipient_code?, reason` | Sets payout details. Audit `vendor.bank_changed` (before/after of everything except the code's value) and notify the vendor (`vendor.bank_changed`). |
| POST | `/admin/vendors/{id}/activate` | `id_checked: true, bank_name_matches: true, note?` | `pending` → `active`. Both booleans must be `true` (else 422), and `bank_name`, `account_name`, `account_last4` must be set (else 409 `listing_incomplete`-style `vendor_incomplete` with `details.missing`). Sets `verified_by`, `verified_at`. Audit `vendor.activated`. |
| POST | `/admin/vendors/{id}/suspend` | `reason` | `active` → `suspended`; sets the suspension fields; **pauses all their `active` listings** (`paused_by='admin'`, audit `asset.paused` each). Live bookings are untouched. Notify. |
| POST | `/admin/vendors/{id}/reinstate` | `reason` | `suspended` → `active`. Listings are **not** auto-resumed; an admin resumes them one by one (§5.3). |

### 5.2 KYC review

| Method | Path | Notes |
|---|---|---|
| GET | `/admin/kyc?status=` | Default `submitted` and `failed`. Shows the user, status, tier, provider, provider reference, timestamps. Never returns documents, ID numbers or selfies (none are stored). |
| POST | `/admin/kyc/{user_id}/override` | Body `{decision: "verify" \| "fail", reason}`. `verify` sets status `verified`, tier 1, `verified_at = now`, provider `manual` (create the row if none). `fail` sets status `failed`, tier 0, `verified_at` NULL. The existing `kyc_profiles` CHECK constraints must hold. Audit `kyc.overridden`; notify the renter (`kyc.verified` or `kyc.failed`). |

### 5.3 Listing review and control

| Method | Path | Body | Effect |
|---|---|---|---|
| GET | `/admin/listings?status=review` | | The review queue (oldest first). Each item: vendor summary (name, status, business type, `is_sub_broker`), catalogue item and band, serial, rates, deposit, default and current `minimum_trust_tier`, and `duplicate_serial_elsewhere` (true if another vendor lists the same serial for the same item). |
| POST | `/admin/listings/{id}/approve` | `minimum_trust_tier?, note?` | `review` → `active`. Requires the vendor to be `active` (409 `vendor_not_active`). Sets `review_actor_id`, `reviewed_at`, clears `review_reason`. If `minimum_trust_tier` is given it replaces the default (0–3) and is audited. Notify (`listing.approved`). |
| POST | `/admin/listings/{id}/reject` | `reason` | `review` → `rejected`, stores `review_reason`. Notify (`listing.rejected`). |
| POST | `/admin/listings/{id}/pause` | `reason` | `active` → `paused` (`paused_by='admin'`). |
| POST | `/admin/listings/{id}/resume` | `reason` | `paused` → `active`, from any `paused_by`. Requires an active vendor. |
| POST | `/admin/listings/{id}/trust-tier` | `minimum_trust_tier, reason` | Sets the tier (0–3) on a listing in any non-deleted state. Audit `asset.trust_tier_changed`. |

Listing review in Phase 0 happens off-platform (the reviewer checks the unit's photos with the vendor over WhatsApp), because photo storage is M3b.

### 5.4 Catalogue management

| Method | Path | Notes |
|---|---|---|
| POST | `/admin/catalogue/items` | Create. 409 `catalogue_item_exists` on a duplicate `(make, model)`. |
| PATCH | `/admin/catalogue/items/{id}` | Edit aliases, specs, mount, band, `is_active`. Changing `category` is refused (409) once any asset uses the item. Changing the band does **not** retroactively change existing assets' tiers. Setting `is_active = false` hides the item from lookup but leaves existing listings alone. Audit `catalogue.item_updated`. |

### 5.5 Bookings, loss and freezes

| Method | Path | Notes |
|---|---|---|
| GET | `/admin/bookings?status=&at_risk=&asset_id=&vendor_id=&renter_id=&limit=&cursor=` | The incident board's data: disputed, at-risk (`at_risk_at` set), returned-with-claim, lost. Returns the existing `Booking` shape. This is needed so ops can find what to `resolve` or mark lost. |
| POST | `/admin/bookings/{id}/mark-lost` | Body `{reason}`. Calls the existing `mark_lost` transition. Its guards stay in the service (not before the 7th business day after `ends_on`, a reason is required); a refused call is the same 409 as elsewhere. |
| POST | `/admin/users/{id}/unrestrict` | Body `{reason, confirm_lost_item?: bool}`. Clears `restricted_at` and `restricted_reason` (finding 2). If the reason on file is `lost_item`, `confirm_lost_item` must be `true` (else 409). Only renters; anyone else is 409 `role_conflict`. Audit `user.unrestricted`; notify. |

### 5.6 Refunds and payouts (the manual money flows)

These close the "recorded, not executed" gaps in `OPERATIONS-NOTES.md` §1 without moving money from code.

| Method | Path | Notes |
|---|---|---|
| GET | `/admin/refunds?status=pending` | `payment` rows of type `refund`, oldest first, with booking id, amount, `metadata.reason`, `metadata.refund_of` and the renter's phone. |
| POST | `/admin/refunds/{payment_id}/mark-sent` | Body `{settlement_reference, note?}`. Only for a `pending` refund row (else 409 `refund_not_pending`). Sets status `refunded`, `settled_at = now`, `settlement_reference`. **No ledger entry**: the ledger already posted the money as gone when the refund row was created. Audit `refund.marked_sent`. Check the `payments` constraints before choosing the status value; if `refunded` conflicts with an existing CHECK, stop and ask. |
| GET | `/admin/payouts/owed` | One row per vendor with an unpaid balance: vendor name and phone, bank name, account name, `account_last4`, whether `recipient_code` is set, the total owed, and the list of completed bookings behind it. **Read-only.** Derive "owed" from the ledger (`vendor_payable` credits less debits, for bookings in `completed`), per `docs/SCHEMA.md` §8. It is the ops worklist for paying vendors by hand from the provider dashboard; the existing `payout_completed` webhook still posts the payout. |

### 5.7 Webhook reconciliation

| Method | Path | Notes |
|---|---|---|
| GET | `/admin/webhook-events?unresolved=true&limit=&cursor=` | Events with a `processing_error` and no resolution. Shows provider, event id, error, received time and a payload summary (no raw payload; it can hold personal data). |
| POST | `/admin/webhook-events/{id}/resolve` | Body `{resolution: "returned" \| "reposted" \| "written_off", note}`. `note` is required. Only events with a `processing_error` (else 409). Already resolved is 409 `already_resolved`. Audit `webhook.resolved`. |

### 5.8 Outbox visibility

| Method | Path | Notes |
|---|---|---|
| GET | `/admin/outbox?status=dead&limit=&cursor=` | Dead (or pending) rows: id, template, user id, attempts, last error. Never returns `data`. |
| POST | `/admin/outbox/{id}/retry` | `dead` → `pending`, `attempts = 0`, `next_attempt_at = now`. Audit `outbox.retried`. |

---

## 6. Step 5: Transactional outbox

Goal: a notification exists if and only if the change that caused it committed. This replaces "notify before commit" (`OPERATIONS-NOTES.md` §2).

### 6.1 Notifier

- Keep the `Notifier` protocol and every existing call site unchanged: `await notifier.send(user_id, template, data)`.
- Add `OutboxNotifier(session)`: `send` inserts a `notification_outbox` row **on the caller's session, without committing**. If the caller's transaction rolls back, the row goes with it.
- Wire it: `get_notifier` (API routes and webhooks) returns an `OutboxNotifier` bound to the request's session. The timer job builds one per unit of work (`app/services/timers.py` opens a session per booking; bind it there). `RecordingNotifier` stays for tests and `LoggingNotifier` stays for local runs.
- **Template registry:** add `app/services/notify_templates.py` holding the set of every template name (the existing `booking.*` and `ops.*` ones plus `vendor.bank_changed`, `vendor.suspended`, `kyc.verified`, `kyc.failed`, `listing.approved`, `listing.rejected`, `user.unrestricted`). `RecordingNotifier.send` and `OutboxNotifier.send` assert the template is registered, so every existing test enforces it.

### 6.2 Dispatcher

`python -m app.jobs.outbox [--once]`, modelled on `app/jobs/timers.py` (graceful SIGINT/SIGTERM, a failing run is logged and the loop continues, `--once` exits non-zero on error).

- Claims due rows (`status='pending' AND next_attempt_at <= now`) in id order with `FOR UPDATE SKIP LOCKED`, in small batches, so two dispatchers never send the same row.
- Delivers through a `NotificationChannel` protocol: `async deliver(recipient, template, data, idempotency_key)`, where `recipient` carries `user_id` and the user's current phone (looked up at send time, not stored in the row) and `idempotency_key` is the outbox id. A real channel must be idempotent on that key.
- M3a ships only `LoggingChannel`, which logs the template and ids, **never `data` or phone numbers**.
- A delivery has a bounded timeout (`VOUCH_PROVIDER_TIMEOUT_SECONDS`, §7.1). On success: `sent`, `sent_at`. On failure: `attempts += 1`, `last_error` (truncated, no personal data), and the next attempt after 1 min, 5 min, 30 min, 2 h, 6 h. After the 5th failure the row becomes `dead`.
- Delivery is at-least-once, so the channel contract says it must dedupe on the idempotency key. Because transitions are now atomic with their outbox rows, a repeated timer run no longer produces duplicate rows.
- The job deletes `sent` rows older than 30 days on each run.

### 6.3 Tests

- A transition that raises after `send` leaves no outbox row (rollback test through a real session).
- A committed transition leaves exactly the expected rows, in order.
- Timer idempotency: running the timers twice yields no duplicate rows.
- Dispatcher: delivers, retries with the backoff schedule using the injected clock, dead-letters on the 5th failure, never sends a row twice with two dispatchers racing (`SKIP LOCKED`), purges old sent rows, and logs no `data`.
- `/admin/outbox` and `retry`.

---

## 7. Step 6: Hardening

### 7.1 Provider timeout (closes the "no timeout" gap)

Add `VOUCH_PROVIDER_TIMEOUT_SECONDS` (default 10, range 1–60). In `accept`, wrap `PaymentProvider.create_virtual_account` in `asyncio.timeout`. On timeout, raise 502 `payment_provider_error`, change nothing, and release the booking row lock. Use a deterministic provider reference (derived from the booking id) so a retry cannot create a second account. Test with a fake provider that never returns.

### 7.2 OTP abuse limits (closes the "per phone only" gap)

- Global cap: `VOUCH_OTP_GLOBAL_PER_HOUR` (default 300). Over it, every OTP request gets 429 `rate_limited` with `Retry-After`.
- Per client IP: `VOUCH_OTP_PER_IP_PER_HOUR` (default 20), counted on `otp_challenges.ip_hash`.
- Client IP source: `VOUCH_CLIENT_IP_HEADER` (unset by default, meaning the socket peer; set to `CF-Connecting-IP` behind Cloudflare). Document that the header is only trustworthy when the API is reachable *only* through that proxy.
- Store only an HMAC of the IP, keyed like `code_hash`. Never store or log the raw IP.
- Keep the per-phone limit (5 per hour). Tests cover all three limits and the unset-header case.

### 7.3 Other fixes

- **ORM drift:** the *column* `handover_records.vendor_signer_id` stays. Only the model's extra plain `ForeignKey("users.id")` on it goes, because no migration creates that FK and the composite FK to `bookings(id, vendor_id)` already enforces the rule. Removing it breaks the `HandoverRecord.vendor_signer` relationship, which needs an FK to infer its join. Either give the relationship an explicit `primaryjoin` (and `viewonly=True` if needed) or drop both `HandoverRecord.vendor_signer` and `User.handovers_signed_as_vendor` after confirming nothing reads them (`grep` first; `lazy="raise"` means any use would already be explicit). Then `alembic check` must pass, and it becomes a CI step (§2.3). Regenerate the ERD so the extra edge disappears, and delete the drift notes in `docs/SCHEMA.md` and `docs/OPERATIONS-NOTES.md`.
- **Admin token lifetime:** `VOUCH_ADMIN_JWT_TTL_MINUTES` (default 60). Admin tokens expire in an hour; renter and vendor tokens keep `VOUCH_JWT_TTL_MINUTES`. There is no admin MFA in M3a; keep admin accounts to one or two people.

---

## 8. Tests required (in addition to §3.6, §4, §5, §6, §7)

- Every new route: 401 without a token, 403 with the wrong role, 422 on a bad body, and its documented 409s.
- A vendor can never read or change another vendor's listing or blocks (404).
- `PublicAsset` output still contains no serial number, vendor identity or review data. A suspended vendor's listing disappears from search and detail.
- An end-to-end Phase 0 test with fakes and no network: `create-admin` → admin creates and activates a vendor → vendor lists a unit and submits → admin approves → renter KYC → booking request → accept → payment webhook → handover → return → complete → the unit appears in `/admin/payouts/owed`. Assert the ledger balances at each step.
- Every admin write produces exactly one audit event with the actor, the request id and (where required) the reason.
- A listing price change does not change an existing booking's `price_snapshot`.
- Coverage stays at or above 90% (`fail_under` in `pyproject.toml`). Ruff format and lint clean. `tests/test_wording.py` green.

---

## 9. Docs to update in step 6

`docs/SCHEMA.md` (new tables and columns, §8 recipes unchanged), `docs/erd.svg` (regenerate with `scripts/gen_erd.py`), `docs/API_CONTRACT.md` (all new routes, error codes, remove items from "Not in M2" that now exist), `docs/OPERATIONS-NOTES.md` (close the gaps that M3a closes; keep the ones that remain, and say which), `docs/ARCHITECTURE.md` (outbox, jobs, admin module), `README.md` (what is implemented, how to run with compose), `docs/DEPLOY.md` (new, §2.3), `.env.example` (every new variable, placeholders only), `docs/specs/M3a-amendments.md`.

---

## 10. Out of scope (M3b and later)

Do not build these in M3a:

- Real Paystack, KYC and SMS adapters, and widening the `otp_sender` / `kyc_provider` / `payment_provider` settings types. Refund execution and payout initiation through the provider.
- Private object storage: listing photos, handover and claim evidence uploads (`evidence_urls` stays client-supplied URLs for now), signed URLs.
- `Idempotency-Key` on commands, `ETag` / `If-Match`, booking timelines, KYC webhooks.
- The WhatsApp adapter and message templates; any real notification channel.
- Vendor self-serve application, vendor-requested catalogue additions, admin MFA, a blacklist, reviews, kits, delivery zones, search ranking, disputes UI.
- The web client.
- Terraform / OpenTofu and any cloud-provider code (separate infrastructure repo).
- A ledger-adjustment endpoint. Corrective postings are done by a reviewed script, never by an API.

---

## 11. Decisions the owner must make (do not guess; use the placeholder and flag it)

| # | Decision | Placeholder in M3a |
|---|---|---|
| D1 | Deposit floor and cap per value band | **Not enforced.** The listing reviewer checks the deposit by eye. Phase 0 data sets the numbers. |
| D2 | The real catalogue (~150 items) and the replacement-value thresholds for bands A, B and C (PRD Q9, Q13) | Sample CSV of 10 rows only; no thresholds in code. |
| D3 | Vendor activation checks beyond "ID checked" and "bank name matches" | Those two booleans, recorded in the audit event. |
| D4 | Whether a vendor may be a renter on the same account | No. One role per user, as the PRD says. |
| D5 | Commission, damage contribution and cancellation fee values | Unchanged M2 placeholders (A8, A13), still pending legal advice. |
| D6 | Which channel and provider send OTP and notifications | `LoggingChannel` and the fake OTP sender. |

*End.*
