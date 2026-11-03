# Vouch Data Schema

## 1. Purpose, sources and notation

This document describes the **current** schema: what migrations `0001` to `0005` build, and how the Milestone 2 services use it. Everything under §4 exists today. The few items that are still future work are listed in §11, *Not in M2*.

Sources of truth, in order:

1. `migrations/versions/0001_initial_schema.py` … `0005_otp_challenges.py` (what the database has);
2. `src/app/db/models/` (the ORM; `tests/test_migrations*.py` checks it against the migrations, but that drift check does not catch foreign-key drift: see the `handover_records` drift note in §4);
3. the services that write the rows: `src/app/services/booking.py` (the `_TRANSITIONS` registry), `money.py` (ledger recipes), `payments.py`, `webhooks.py` and `money_line.py`.

The spec is [`docs/specs/M2.md`](specs/M2.md). Where it differs from the code, [`docs/specs/M2-amendments.md`](specs/M2-amendments.md) (A1–A18) wins, and this document follows the amendments.

The entity–relationship diagram is [`docs/erd.svg`](erd.svg). It is rendered from [`docs/erd.mmd`](erd.mmd), which `scripts/gen_erd.py` generates from `Base.metadata`. `tests/test_docs_schema.py` fails when `erd.mmd` is stale, or when a table is missing from this document or the diagram. After a schema change:

```bash
python scripts/gen_erd.py
npx -y @mermaid-js/mermaid-cli -i docs/erd.mmd -o docs/erd.svg
```

**Money rule.** Every amount is `NUMERIC(12,2)` in `NGN` in the database, and a Python `Decimal` in code. Never use floating point, and never do money arithmetic outside `Decimal`. Quantize with `Decimal("0.01")` and `ROUND_HALF_UP`. The API writes amounts as strings with exactly two decimal places, such as `"150000.00"`. Request bodies take money as a string with at most 10 digits before the point and at most 2 after (`"150000"`, `"150000.5"` and `"150000.00"` are all accepted), and refuse JSON numbers for money (422). A test greps the services and schemas for `float(`.

Other conventions:

- Every timestamp is `TIMESTAMPTZ`, written and read in UTC. Business days (the renter cancellation cut-off, the no-show window, the at-risk and lost dates, "today" for request dates) use `VOUCH_BUSINESS_TIMEZONE` (default `Africa/Lagos`).
- UUID primary keys come from `gen_random_uuid()` (`pgcrypto`). The append-only tables and `webhook_events` use `BIGSERIAL`. The reference tables use `SMALLSERIAL`.
- "Python default" means the ORM sets the value on insert. The column has no database default.
- `created_at` / `updated_at` come from `TimestampMixin`: both default to `now()`, and the ORM refreshes `updated_at` on update. No database trigger maintains `updated_at`.
- No query, schema or docstring names a city. Launched cities come from `VOUCH_LAUNCHED_CITIES` (A2). City slugs appear only as data and configuration: the seed rows of migration `0003`, the settings default `launched_cities = ["abuja"]` (`app/core/config.py`), `VOUCH_LAUNCHED_CITIES=abuja` in `.env.example`, and the name of 0002's index `ix_assets_public_lagos`, which 0003 dropped.

## 2. Extensions, functions and triggers

| Object | Kind | Defined by | What it does |
|---|---|---|---|
| `pgcrypto` | extension | 0001 | `gen_random_uuid()` |
| `btree_gist` | extension | 0001 | Lets the GiST exclusion constraints compare `asset_id` with `=` |
| `catalogue_search_vector(make text, model text, aliases text[])` | SQL function, `IMMUTABLE PARALLEL SAFE` | 0003 | `to_tsvector('simple', make \|\| ' ' \|\| model \|\| ' ' \|\| array_to_string(aliases, ' '))`. The spec's index expression cannot be indexed directly, because `array_to_string` is only STABLE. The same expression is wrapped in this function (A11). Both `ix_catalogue_items_search` and the inventory `q` filter call it, so the planner can use the index. |
| `reject_mutation()` | trigger function (plpgsql) | 0003 | Raises `'<table> is append-only'`. Its `search_path` is pinned to `pg_catalog, public`. |
| `assert_txn_group_balanced()` | trigger function (plpgsql) | 0003 | Sums the debits and credits of `NEW.txn_group` and raises `'txn_group … is not balanced'` if they differ. `search_path` is pinned. |
| `trg_ledger_no_update` | trigger on `ledger_entries` | 0003 | `BEFORE UPDATE OR DELETE … FOR EACH ROW EXECUTE FUNCTION reject_mutation()` |
| `trg_ledger_no_truncate` | trigger on `ledger_entries` | 0003 | `BEFORE TRUNCATE … FOR EACH STATEMENT`, so a `TRUNCATE … CASCADE` from a parent table is refused too |
| `trg_ledger_balanced` | constraint trigger on `ledger_entries` | 0003 | `AFTER INSERT … DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION assert_txn_group_balanced()`. It runs at COMMIT, so a transaction may insert a group row by row. Each group must balance on its own: two groups that only balance against each other fail. |
| `trg_audit_no_update` | trigger on `audit_events` | 0003 | `BEFORE UPDATE OR DELETE … FOR EACH ROW EXECUTE FUNCTION reject_mutation()` |
| `trg_audit_no_truncate` | trigger on `audit_events` | 0003 | `BEFORE TRUNCATE … FOR EACH STATEMENT` |

Corrections to the ledger or the audit trail are new rows (reversing entries), never edits.

## 3. Enums

Postgres enum types:

| Type | Values | Column |
|---|---|---|
| `renterrole` | `renter`, `vendor`, `admin` | `users.role` |
| `kycstatus` | `pending`, `submitted`, `verified`, `failed` | `kyc_profiles.status` |
| `listingstatus` | `draft`, `review`, `active`, `paused`, `rejected` | `assets.listing_status` |
| `bookingstatus` | `requested`, `accepted`, `deposit_paid`, `confirmed`, `in_progress`, `returned`, `completed`, `declined`, `expired`, `cancelled_by_renter`, `cancelled_by_vendor`, `disputed`, `lost` | `bookings.status` (see §5) |
| `handoverdirection` | `pickup`, `return` | `handover_records.direction` |
| `handoverstatus` | `draft`, `vendor_signed`, `renter_signed`, `completed` | `handover_records.status` |
| `paymenttype` | `deposit`, `rental_balance`, `payout`, `refund`, `damage_contribution`, `booking` | `payments.type`. In M2 only `booking`, `refund` and `payout` rows are written (§7). `damage_contribution` was `protection_waiver` before 0003 (A4). 0004 added `booking` (A14). |
| `paymentstatus` | `pending`, `authorized`, `captured`, `failed`, `refunded` | `payments.status` |
| `depositstatus` | `held`, `released`, `claimed` | `payments.deposit_status` (unused in M2, §7) |
| `payoutstatus` | `pending`, `eligible`, `paid` | `payments.payout_status` |

The 0001 enums `city` and `assetcategory` were replaced by the `cities` and `categories` tables in 0003, and then dropped.

Text columns with a CHECKed value set:

| Column | Values |
|---|---|
| `audit_events.actor_role` | `renter`, `vendor`, `admin`, `system` |
| `ledger_entries.account` | `renter_deposit_liability`, `platform_cash`, `vendor_payable`, `platform_revenue`, `protection_pool`, `processing_fees` |
| `ledger_entries.direction` | `dr`, `cr` |
| `catalogue_items.replacement_value_band` | `A`, `B`, `C` |
| `assets.condition_grade` | `excellent`, `good`, `fair` (or NULL) |
| `bookings.fulfilment_choice` | `pickup`, `delivery` |

Trust tiers (`TrustTier`, an `IntEnum` in code): `0 phone_verified`, `1 identity_verified`, `2 established`, `3 vendor_approved` (§9).

## 4. Tables

There are fifteen tables, plus Alembic's `alembic_version`. Each table section lists its columns, then its constraints, indexes and triggers.

### `cities`

Reference table (0003). It replaced the `city` enum. Seeded with `abuja` and `lagos`. Whether a city is open for inventory is decided by `VOUCH_LAUNCHED_CITIES`, not by this table.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | SMALLINT | NOT NULL | `nextval` (SMALLSERIAL) |
| `slug` | TEXT | NOT NULL | |
| `name` | TEXT | NOT NULL | |
| `is_active` | BOOLEAN | NOT NULL | `true` |

- PK `pk_cities (id)`; UNIQUE `uq_cities_slug (slug)`.

### `categories`

Reference table (0003). It replaced the `assetcategory` enum. Seeded with seven slugs: `camera`, `lens`, `lighting`, `audio`, `drone`, `gimbal` and `accessory`. The old enum value `other` was backfilled to `accessory` (A5).

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | SMALLINT | NOT NULL | `nextval` (SMALLSERIAL) |
| `slug` | TEXT | NOT NULL | |
| `name` | TEXT | NOT NULL | |
| `checklist_template` | JSONB | NOT NULL | |
| `is_active` | BOOLEAN | NOT NULL | `true` |

- PK `pk_categories (id)`; UNIQUE `uq_categories_slug (slug)`.
- `checklist_template` is the handover checklist for the category: `{"mode": "serial" | "count", "items": [...]}`. Lighting and accessory are `count`; the others are `serial`.

### `users`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | UUID | NOT NULL | `gen_random_uuid()` |
| `phone` | VARCHAR | NOT NULL | Stored normalized as `+234XXXXXXXXXX` by the OTP flow |
| `full_name` | VARCHAR | NULL | Nullable since 0005: a user created at their first OTP verify may not have given a name |
| `email` | VARCHAR | NULL | |
| `role` | `renterrole` | NOT NULL | Python default `renter` |
| `is_active` | BOOLEAN | NOT NULL | Python default `true` |
| `created_at`, `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` |
| `deleted_at` | TIMESTAMPTZ | NULL | Soft-delete marker |
| `restricted_at` | TIMESTAMPTZ | NULL | Renter freeze (0003, A10) |
| `restricted_reason` | TEXT | NULL | `at_risk` (a late return) or `lost_item` (a booking marked lost) |
| `phone_verified_at` | TIMESTAMPTZ | NULL | When the user last proved the phone through OTP (0005) |

- PK `users_pkey (id)`; UNIQUE `users_phone_key (phone)`, `users_email_key (email)`; index `ix_users_phone (phone)`.
- CHECK `ck_users_restriction_has_reason`: `restricted_at IS NULL OR restricted_reason IS NOT NULL`.
- A missing, inactive or soft-deleted user, or one whose stored role differs from the token's role, cannot authenticate. A restricted user can still authenticate, but the booking guards refuse their requests (`renter_restricted`).

### `kyc_profiles`

One row per user: the verified KYC level only (A6).

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | UUID | NOT NULL | `gen_random_uuid()` |
| `user_id` | UUID | NOT NULL | |
| `provider` | VARCHAR | NOT NULL | e.g. `fake` |
| `provider_reference` | VARCHAR | NULL | |
| `status` | `kycstatus` | NOT NULL | Python default `pending` |
| `tier` | INTEGER | NOT NULL | Python default `0` |
| `national_id_type` | VARCHAR | NULL | `nin`, `bvn`, `passport` or `drivers_licence`. The ID number and the documents are never stored. |
| `verified_at` | TIMESTAMPTZ | NULL | Stamped from the injected clock |
| `created_at`, `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` |

- PK `kyc_profiles_pkey (id)`; FK `kyc_profiles_user_id_fkey (user_id) → users(id)`; UNIQUE `kyc_profiles_user_id_key (user_id)`, `uq_kyc_profiles_provider_reference (provider_reference)`.
- CHECK `ck_kyc_profiles_supported_tier`: `tier BETWEEN 0 AND 1`.
- CHECK `ck_kyc_profiles_verified_at_required`: `status <> 'verified' OR verified_at IS NOT NULL`.
- CHECK `ck_kyc_profiles_verification_consistency`: either (`verified`, `verified_at` set, tier 1), or (not `verified`, `verified_at` NULL, tier 0).
- Tier 1 means national ID, liveness and address all passed through the provider (`POST /me/kyc`). Data migrated by 0003: old tier 2 became tier 1; old tier 1 (no address) became `submitted`, tier 0, `verified_at` NULL.

### `otp_challenges`

One row per login code sent (0005; spec §4, A10).

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | UUID | NOT NULL | `gen_random_uuid()` |
| `phone` | TEXT | NOT NULL | Normalized `+234…` |
| `code_hash` | TEXT | NOT NULL | HMAC-SHA256 (hex) of `"<phone>:<code>"`, keyed with the JWT secret, so a code is bound to its phone. The code itself is never stored. |
| `expires_at` | TIMESTAMPTZ | NOT NULL | Ten minutes after it is sent |
| `attempts` | SMALLINT | NOT NULL | `0`. Each wrong guess adds one. |
| `consumed_at` | TIMESTAMPTZ | NULL | "Can no longer be used": used, replaced by a newer code, or cleared by a successful login |
| `created_at` | TIMESTAMPTZ | NOT NULL | `now()` |

- PK `pk_otp_challenges (id)`; CHECK `ck_otp_challenges_attempts_range`: `attempts BETWEEN 0 AND 5` (at 5 the challenge is used up).
- Index `ix_otp_challenges_phone_created_at (phone, created_at)` serves both the latest-challenge lookup and the rolling-hour count. More than 5 requests for one phone in an hour get 429.
- A new code marks the phone's open challenges consumed, so only the newest code works. A correct code consumes every open challenge for the phone. Both requests and verifies take a per-phone, transaction-scoped advisory lock.

### `vendor_references`

A vendor vouching for a renter (0003), which is one way to reach tier 2.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | UUID | NOT NULL | `gen_random_uuid()` |
| `renter_id` | UUID | NOT NULL | |
| `vendor_id` | UUID | NOT NULL | |
| `booking_id` | UUID | NOT NULL | |
| `created_at` | TIMESTAMPTZ | NOT NULL | `now()` |

- PK `pk_vendor_references (id)`; UNIQUE `uq_vendor_references_renter_vendor (renter_id, vendor_id)`.
- FK `fk_vendor_references_renter_id (renter_id) → users(id)`, `fk_vendor_references_vendor_id (vendor_id) → users(id)`.
- Composite FKs `fk_vendor_references_booking_renter (booking_id, renter_id) → bookings(id, renter_id)` and `fk_vendor_references_booking_vendor (booking_id, vendor_id) → bookings(id, vendor_id)`: the booking must be between this renter and this vendor.

### `catalogue_items`

The make-and-model catalogue (0003, spec §2.2).

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | UUID | NOT NULL | `gen_random_uuid()` |
| `category_id` | SMALLINT | NOT NULL | |
| `make` | TEXT | NOT NULL | |
| `model` | TEXT | NOT NULL | |
| `mount` | TEXT | NULL | `E`, `RF`, `L`, `MFT`…; NULL for non-optics |
| `specs` | JSONB | NOT NULL | `'{}'` |
| `replacement_value_band` | CHAR(1) | NOT NULL | |
| `aliases` | TEXT[] | NOT NULL | `'{}'` |
| `is_active` | BOOLEAN | NOT NULL | `true` |
| `created_at` | TIMESTAMPTZ | NOT NULL | `now()` |

- PK `pk_catalogue_items (id)`; FK `fk_catalogue_items_category_id (category_id) → categories(id)`; UNIQUE `uq_catalogue_items_make_model (make, model)`.
- CHECK `ck_catalogue_items_replacement_value_band_valid`: `replacement_value_band IN ('A','B','C')`.
- Index `ix_catalogue_items_search`: `GIN (catalogue_search_vector(make, model, aliases))` (A11). The migrations own this index; the ORM cannot express it, and autogenerate skips it through `app.db.migration_filters`.
- The band sets the default `assets.minimum_trust_tier` for a new listing: A→1, B→2, C→3 (A3). Backfilled items (one per pre-0003 asset) have make `Unknown`, band `B` and `specs = {"needs_review": true}`.

### `assets`

One rentable unit: one serial number, quantity 1. Several units of a model are several assets.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | UUID | NOT NULL | `gen_random_uuid()` |
| `vendor_id` | UUID | NOT NULL | |
| `name` | VARCHAR | NULL | Display override only (nullable since 0003); otherwise make and model are shown |
| `description` | TEXT | NULL | |
| `serial_number` | VARCHAR | NULL | Never in public output |
| `daily_rate` | NUMERIC(12,2) | NOT NULL | |
| `deposit_amount` | NUMERIC(12,2) | NOT NULL | |
| `is_available` | BOOLEAN | NOT NULL | `true` |
| `condition_notes` | TEXT | NULL | Never in public output |
| `created_at`, `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` |
| `deleted_at` | TIMESTAMPTZ | NULL | Soft-delete marker |
| `listing_status` | `listingstatus` | NOT NULL | `'draft'` (0002) |
| `minimum_trust_tier` | INTEGER | NOT NULL | `0`. The one booking-guard column (A3). |
| `review_actor_id` | UUID | NULL | The admin who reviewed the listing (0002) |
| `reviewed_at` | TIMESTAMPTZ | NULL | |
| `review_reason` | TEXT | NULL | |
| `city_id` | SMALLINT | NOT NULL | (0003) |
| `category_id` | SMALLINT | NOT NULL | (0003) |
| `catalogue_item_id` | UUID | NOT NULL | (0003) |
| `condition_grade` | TEXT | NULL | |
| `included_accessories` | JSONB | NOT NULL | `'[]'` |
| `weekend_rate` | NUMERIC(12,2) | NULL | Charged for exactly Friday to Sunday |
| `weekly_rate` | NUMERIC(12,2) | NULL | Charged per full week of a span of 7 days or more |
| `fulfilment` | JSONB | NOT NULL | `'{"pickup": true, "delivery": false}'`. An optional `delivery_fee` key sets the delivery fee (default 0, A8). |

- PK `assets_pkey (id)`.
- FKs: `assets_vendor_id_fkey (vendor_id) → users(id)`, `assets_review_actor_id_fkey (review_actor_id) → users(id)`, `fk_assets_city_id (city_id) → cities(id)`, `fk_assets_category_id (category_id) → categories(id)`, `fk_assets_catalogue_item_id (catalogue_item_id) → catalogue_items(id)`.
- CHECKs: `ck_assets_daily_rate_positive` (`daily_rate > 0`), `ck_assets_deposit_amount_nonnegative` (`deposit_amount >= 0`), `ck_assets_weekend_rate_positive` and `ck_assets_weekly_rate_positive` (NULL or `> 0`), `ck_assets_condition_grade_valid`, `ck_assets_minimum_trust_tier_valid` (`minimum_trust_tier BETWEEN 0 AND 3`; widened from 0–2 by 0003).
- Indexes: `ix_assets_vendor_id (vendor_id)`, and the partial index `ix_assets_public (city_id, category_id, daily_rate, created_at, id) WHERE listing_status = 'active' AND is_available = true AND deleted_at IS NULL`. It replaced 0002's `ix_assets_public_lagos`.
- Public visibility (the inventory service, used by `/assets` and `/catalogue/search`): `listing_status = 'active'`, available, not deleted, an active and not-deleted vendor, and a city in the launched set. An asset in a city that is not launched is a 404. A city filter outside the launched set is 400 `city_not_launched`. Marking a booking lost pauses its asset (`listing_status = 'paused'`, audit `asset.paused`).

### `availability_windows`

Vendor-declared date ranges. Only rows with `is_blocked = true` affect bookings.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | UUID | NOT NULL | `gen_random_uuid()` |
| `asset_id` | UUID | NOT NULL | |
| `starts_at` | DATE | NOT NULL | Inclusive |
| `ends_at` | DATE | NOT NULL | Inclusive |
| `is_blocked` | BOOLEAN | NOT NULL | `false` |
| `created_at`, `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` |

- PK `availability_windows_pkey (id)`; FK `availability_windows_asset_id_fkey (asset_id) → assets(id)`; index `ix_availability_windows_asset_id (asset_id)`.
- CHECK `ck_availability_windows_valid_date_range`: `starts_at <= ends_at`.
- EXCLUDE `ex_availability_windows_no_blocked_overlap`: `USING gist (asset_id WITH =, daterange(starts_at, ends_at, '[]') WITH &&) WHERE (is_blocked)`. Blocked windows of one asset never overlap.
- A booking request whose dates overlap a blocked window is refused (`dates_blocked`). Search leaves out units with an overlapping blocked window.

### `bookings`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | UUID | NOT NULL | `gen_random_uuid()` |
| `asset_id` | UUID | NOT NULL | |
| `renter_id` | UUID | NOT NULL | |
| `vendor_id` | UUID | NOT NULL | The asset's vendor, copied at request time |
| `starts_on` | DATE | NOT NULL | First rental day, inclusive |
| `ends_on` | DATE | NOT NULL | Last rental day, inclusive |
| `status` | `bookingstatus` | NOT NULL | `'requested'` |
| `total_rental_fee` | NUMERIC(12,2) | NOT NULL | R, the same as `price_snapshot.rental_total` |
| `deposit_amount` | NUMERIC(12,2) | NOT NULL | D, the same as `price_snapshot.deposit` |
| `platform_note` | TEXT | NULL | Internal |
| `cancelled_at` | TIMESTAMPTZ | NULL | Set by every cancellation, withdrawal and no-show |
| `cancellation_reason` | VARCHAR | NULL | The caller's reason. A no-show is `vendor_no_show` or `vendor_no_show: <note>`. |
| `created_at`, `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` |
| `hold_expires_at` | TIMESTAMPTZ | NULL | Request time + 4 h |
| `deposit_due_at` | TIMESTAMPTZ | NULL | Accept time + 12 h. The whole upfront payment is due by then (A14); the name is kept from the spec. |
| `claim_window_ends_at` | TIMESTAMPTZ | NULL | Return time + 24 h |
| `vendor_approved_at` | TIMESTAMPTZ | NULL | Tier 3: the vendor's approval of this renter (A3) |
| `at_risk_at` | TIMESTAMPTZ | NULL | Late-return flag. This is a column, not a status (A15). |
| `price_snapshot` | JSONB | NOT NULL | Frozen at request time (§6) |
| `fulfilment_choice` | TEXT | NOT NULL | `pickup` or `delivery` |
| `claim_amount` | NUMERIC(12,2) | NULL | The vendor's claim (A10) |
| `claim_description` | TEXT | NULL | |
| `claim_evidence` | JSONB | NULL | A list of evidence URLs |
| `claimed_at` | TIMESTAMPTZ | NULL | |

- PK `bookings_pkey (id)`; UNIQUE `uq_bookings_id_renter (id, renter_id)` and `uq_bookings_id_vendor (id, vendor_id)` (targets of the composite FKs from `handover_records` and `vendor_references`).
- FKs: `bookings_asset_id_fkey → assets(id)`, `bookings_renter_id_fkey → users(id)`, `bookings_vendor_id_fkey → users(id)`.
- Indexes: `ix_bookings_asset_id`, `ix_bookings_renter_id`, `ix_bookings_vendor_id`.
- CHECKs:
  - `ck_bookings_total_rental_fee_positive` (`total_rental_fee > 0`), `ck_bookings_deposit_amount_nonnegative` (`deposit_amount >= 0`), `ck_bookings_valid_date_range` (`starts_on <= ends_on`);
  - `ck_bookings_fulfilment_choice_valid`, `ck_bookings_claim_amount_positive` (`claim_amount > 0`), `ck_bookings_not_self` (`renter_id <> vendor_id`);
  - `ck_bookings_requested_has_hold`: `status <> 'requested' OR hold_expires_at IS NOT NULL`;
  - `ck_bookings_accepted_has_deposit_due`: `status <> 'accepted' OR deposit_due_at IS NOT NULL`;
  - `ck_bookings_returned_has_claim_window`: `status NOT IN ('returned','disputed') OR claim_window_ends_at IS NOT NULL`;
  - `ck_bookings_claim_complete`: the claim columns are either all NULL, or amount, description and `claimed_at` are all set (evidence is optional at the database level; the API requires at least one URL).
- EXCLUDE `ex_bookings_no_live_overlap`: `USING gist (asset_id WITH =, daterange(starts_on, ends_on, '[]') WITH &&) WHERE (status IN ('requested','accepted','deposit_paid','confirmed','in_progress','returned'))`. The WHERE list is built from `LIVE_BOOKING_STATUSES` (§5). Two concurrent requests for overlapping dates get one success and one 409 `dates_unavailable`. `declined`, `expired` and `cancelled_*` free the dates by leaving the set. `returned` keeps them held until `completed`, to cover the claim window and the turnaround.

### `handover_records`

The pickup and return checklists, with each party's signature.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | UUID | NOT NULL | `gen_random_uuid()` |
| `booking_id` | UUID | NOT NULL | |
| `direction` | `handoverdirection` | NOT NULL | |
| `status` | `handoverstatus` | NOT NULL | `'draft'` |
| `condition_checklist` | JSONB | NOT NULL | `{"vendor": {...}, "renter": {...}}`: each party writes only its own side |
| `vendor_signer_id` | UUID | NULL | |
| `renter_signer_id` | UUID | NULL | |
| `vendor_signed_at` | TIMESTAMPTZ | NULL | |
| `renter_signed_at` | TIMESTAMPTZ | NULL | |
| `evidence_urls` | JSONB | NULL | A list, de-duplicated, at most 40 per record (at most 20 per submission) |
| `location_lat`, `location_lng` | NUMERIC(9,6) | NULL | Not written in M2 |
| `notes` | TEXT | NULL | |
| `created_at`, `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` |

- PK `handover_records_pkey (id)`; UNIQUE `uq_handover_records_booking_direction (booking_id, direction)` (at most one pickup and one return per booking); index `ix_handover_records_booking_id`.
- FKs: `handover_records_booking_id_fkey (booking_id) → bookings(id)`, `handover_records_renter_signer_id_fkey (renter_signer_id) → users(id)`, and the composite `fk_handover_records_booking_vendor (booking_id, vendor_signer_id) → bookings(id, vendor_id)` and `fk_handover_records_booking_renter (booking_id, renter_signer_id) → bookings(id, renter_id)`. A signer must be that booking's own party.
- CHECK `ck_handover_records_completed_requirements`: `completed` needs both signer ids, both signing times and a non-empty JSON array of evidence URLs.
- Drift note: the ORM also declares a plain FK `vendor_signer_id → users(id)`, which the migrations never created (0001 creates only the renter one). The composite FK already ties `vendor_signer_id` to `bookings.vendor_id`, which references `users`. The ERD, generated from the ORM, shows the extra edge.
- A side that has signed is final (`handover_already_signed`). When both sides have signed, the record is `completed` and the booking event `handover_completed` (pickup) or `return_completed` (return) fires in the same transaction.

### `payments`

The provider-facing record of money in and out. It holds provider state and is **not** the source of truth for balances; the ledger is.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | UUID | NOT NULL | `gen_random_uuid()` |
| `booking_id` | UUID | NOT NULL | |
| `type` | `paymenttype` | NOT NULL | |
| `status` | `paymentstatus` | NOT NULL | Python default `pending` |
| `amount` | NUMERIC(12,2) | NOT NULL | |
| `currency` | VARCHAR | NOT NULL | Python default `NGN` |
| `provider` | VARCHAR | NOT NULL | The provider's name (`fake` in M2) |
| `provider_reference` | VARCHAR | NULL | |
| `provider_virtual_account` | VARCHAR | NULL | The account number the renter pays into |
| `vendor_recipient_code` | VARCHAR | NULL | Payout rows |
| `vendor_subaccount_code` | VARCHAR | NULL | Unused in M2 |
| `platform_fee` | NUMERIC(12,2) | NULL | Payout rows: the booking's commission K |
| `settlement_reference` | VARCHAR | NULL | Unused in M2 |
| `deposit_status` | `depositstatus` | NULL | Unused in M2 |
| `payout_status` | `payoutstatus` | NULL | Payout rows |
| `authorized_at`, `captured_at`, `failed_at` | TIMESTAMPTZ | NULL | |
| `deposit_held_at`, `deposit_released_at`, `deposit_claimed_at` | TIMESTAMPTZ | NULL | Unused in M2 |
| `payout_eligible_at`, `paid_at` | TIMESTAMPTZ | NULL | Payout rows |
| `settled_at` | TIMESTAMPTZ | NULL | Unused in M2 |
| `metadata` | JSONB | NULL | `booking`: `{"bank": ...}` (plus the expected amount after a mismatch); `refund`: `{"reason": ..., "refund_of": "<payment id>"}` |
| `created_at`, `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` |

- PK `payments_pkey (id)`; FK `payments_booking_id_fkey (booking_id) → bookings(id)`; UNIQUE `payments_provider_reference_key (provider_reference)`; index `ix_payments_booking_id`.
- CHECKs:
  - `ck_payments_amount_positive` (`amount > 0`): a refund of 0.00 is never recorded;
  - `ck_payments_platform_fee_nonnegative`;
  - `ck_payments_payout_fields_required`: a payout needs `vendor_recipient_code` and `platform_fee`;
  - `ck_payments_deposit_lifecycle_type`: only `deposit` rows carry `deposit_status` and the `deposit_*` times;
  - `ck_payments_payout_lifecycle_type`: only `payout` rows carry `payout_status`, `payout_eligible_at` and `paid_at`;
  - `ck_payments_deposit_status_timestamps`, `ck_payments_payout_status_timestamps`: each status has its timestamp.
- The lifecycle of each row type is in §7.

### `ledger_entries`

Append-only double entry (0003, spec §2.5).

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | BIGINT | NOT NULL | BIGSERIAL |
| `booking_id` | UUID | NULL | Every M2 posting has one |
| `payment_id` | UUID | NULL | The payment row the group moved money for (§8) |
| `account` | TEXT | NOT NULL | One of the six accounts (§8) |
| `direction` | CHAR(2) | NOT NULL | `dr` or `cr` |
| `amount` | NUMERIC(12,2) | NOT NULL | Always positive |
| `currency` | CHAR(3) | NOT NULL | `'NGN'` |
| `txn_group` | UUID | NOT NULL | One group per money movement, with two or more rows |
| `memo` | TEXT | NULL | The recipe name (§8) |
| `created_at` | TIMESTAMPTZ | NOT NULL | `now()` |

- PK `ledger_entries_pkey (id)`; FKs `ledger_entries_booking_id_fkey → bookings(id)` and `ledger_entries_payment_id_fkey → payments(id)`.
- CHECKs: `ck_ledger_entries_account_valid`, `ck_ledger_entries_direction_valid`, `ck_ledger_entries_amount_positive` (`amount > 0`).
- Indexes: `ix_ledger_booking (booking_id)`, `ix_ledger_group (txn_group)`.
- Triggers: `trg_ledger_no_update`, `trg_ledger_no_truncate` (append-only, including TRUNCATE) and the deferred `trg_ledger_balanced` (`sum(dr) = sum(cr)` per `txn_group` at COMMIT) (§2).
- Balances are derived, never stored. A credit-normal balance is `SUM(cr) − SUM(dr)` (`app.services.ledger.account_balance`).

### `audit_events`

Append-only history (0003, spec §2.6).

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | BIGINT | NOT NULL | BIGSERIAL |
| `actor_id` | UUID | NULL | NULL only for `system` (timers and webhooks) |
| `actor_role` | TEXT | NOT NULL | `renter`, `vendor`, `admin` or `system` |
| `entity` | TEXT | NOT NULL | `booking`, `user`, `asset`, `payment`, `handover_record` or `kyc_profile` |
| `entity_id` | UUID | NOT NULL | |
| `action` | TEXT | NOT NULL | e.g. `booking.confirmed` (below) |
| `before`, `after` | JSONB | NULL | The changed columns, plus handler extras (amounts, payment ids, the refund breakdown) |
| `reason` | TEXT | NULL | |
| `request_id` | TEXT | NULL | The HTTP request's `X-Request-ID` |
| `created_at` | TIMESTAMPTZ | NOT NULL | `now()` |

- PK `audit_events_pkey (id)`; FK `audit_events_actor_id_fkey (actor_id) → users(id)`.
- CHECKs: `ck_audit_events_actor_role_valid`, and `ck_audit_events_actor_required` (`actor_role = 'system' OR actor_id IS NOT NULL`).
- Index `ix_audit_events_entity (entity, entity_id)` for audit-trail lookups (A12).
- Triggers: `trg_audit_no_update`, `trg_audit_no_truncate` (append-only, including TRUNCATE).
- Actions written in M2:
  - `booking.<new status>` for every transition (`booking.requested`, `booking.accepted`, `booking.deposit_paid`, `booking.confirmed`, …);
  - `booking.vendor_approved` and `booking.at_risk` (no status change);
  - `deposit.released`, `deposit.deducted` and `deposit.forfeited`, beside the booking's own row;
  - `vendor.reliability_penalty` (entity `user`) on a vendor cancellation or a no-show;
  - `user.restricted` and `asset.paused`;
  - `handover.updated` and `handover.signed`;
  - `kyc.verified` and `kyc.failed`;
  - `payment.refunded_late`, `payment.refunded_mismatch`, `payment.refunded_unapplied`, `payment.refunded_duplicate` and `payout.paid`.

  Every booking transition writes at least one row. An illegal transition, or a failed guard, writes none.

### `webhook_events`

One row per provider delivery (0003, spec §2.6), for idempotency.

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | BIGINT | NOT NULL | BIGSERIAL |
| `provider` | TEXT | NOT NULL | The payment provider's name |
| `provider_event_id` | TEXT | NOT NULL | See below |
| `payload` | JSONB | NOT NULL | See below |
| `signature_ok` | BOOLEAN | NOT NULL | |
| `received_at` | TIMESTAMPTZ | NOT NULL | `now()` |
| `processed_at` | TIMESTAMPTZ | NULL | Set on every verified event |
| `processing_error` | TEXT | NULL | Why a genuine event was not applied |

- PK `webhook_events_pkey (id)`; UNIQUE `uq_webhook_events_provider_event (provider, provider_event_id)`.
- Semantics are in §10.

## 5. Booking statuses and transitions

`LIVE_BOOKING_STATUSES` (defined once, in `app/db/models/booking.py`) = `requested`, `accepted`, `deposit_paid`, `confirmed`, `in_progress`, `returned`. A live booking holds the unit for its dates. The exclusion constraint and the inventory date filter are both built from it.

`TERMINAL_BOOKING_STATUSES` = `completed`, `declined`, `expired`, `cancelled_by_renter`, `cancelled_by_vendor`, `lost`. No transition leaves these. `disputed` is neither live nor terminal.

`deposit_paid` is transient: the upfront payment moves a booking through it to `confirmed` in one transaction, and writes an audit row for each step.

The transition table is the `_TRANSITIONS` registry in `app/services/booking.py`, following spec §3.1 as amended by A13–A17. Each row is `(from status, event) → handler`, with the roles allowed to send it. Renters and vendors must be that booking's own party. Admin and system actors are never party-checked. A missing row, a role that is not allowed, or a non-party all give 409 `invalid_transition`, and nothing is written. Each transition locks the booking (`SELECT … FOR UPDATE`) and writes at least one audit row. Times marked "business day" are 00:00 in the business timezone.

| From | Event | Who | To | Guards (409 code) | Effects |
|---|---|---|---|---|---|
| — | `request` | renter | `requested` | Asset publicly visible (else 404); dates valid, not past, at most `VOUCH_MAX_RENTAL_DAYS`, starting within `VOUCH_BOOKING_HORIZON_DAYS`, fulfilment offered (else 400); renter active and not restricted (`renter_restricted`); effective tier meets `minimum_trust_tier`, where a tier-3 asset may be requested at effective tier 2 (`tier_insufficient`); no blocked window (`dates_blocked`); no live overlap (`dates_unavailable`, from the exclusion constraint); not the renter's own asset (`self_booking`) | `hold_expires_at = now + 4h`; `price_snapshot` frozen; audit `booking.requested`; vendor notified |
| `requested` | `approve_vendor` | vendor | `requested` | Not yet approved (else `invalid_transition`); before `hold_expires_at` (`hold_expired`); asset is tier 3 (`approval_not_required`) | `vendor_approved_at = now`; audit `booking.vendor_approved` |
| `requested` | `accept` | vendor | `accepted` | Before `hold_expires_at` (`hold_expired`); renter guards run again (`renter_restricted`, `tier_insufficient`); tier 3 needs `vendor_approved_at` (`vendor_approval_required`); a payment provider is configured (503 `payment_unavailable`) | The provider opens a virtual account for D + C + R + F, and a `booking` payment row is recorded `pending` (a provider failure is 502 `payment_provider_error` and changes nothing); `deposit_due_at = now + 12h`; the renter is notified with the payment instructions |
| `requested` | `decline` | vendor | `declined` | — | Audit with the optional reason; renter notified |
| `requested` | `cancel` | renter | `cancelled_by_renter` | — | Free withdrawal (A13): no money moves; `cancelled_at`; vendor notified `booking.withdrawn` |
| `requested` | `expire_hold` | system (timer) | `expired` | `now > hold_expires_at` | Dates freed; renter notified |
| `accepted` | `payment_captured` | system (webhook) | `deposit_paid` → `confirmed` | Amount = D + C + R + F exactly (`amount_mismatch`); the payload's `payment_id` must be this booking's captured `booking` payment (missing, malformed or another booking's: 400 `invalid_request`), and that row's amount must equal the payload's (`amount_mismatch`) | One ledger group, `payment_captured` (§8); audit `booking.deposit_paid` then `booking.confirmed`; both parties notified with each other's phone |
| `accepted` | `cancel` | renter | `cancelled_by_renter` | — | Free withdrawal (A13); the pending `booking` payment is marked `failed`; vendor notified `booking.withdrawn` |
| `accepted` | `cancel` | vendor | `cancelled_by_vendor` | Reason required (`reason_required`) | The pending payment is marked `failed`; audit `vendor.reliability_penalty`; both notified |
| `accepted` | `expire_deposit` | system (timer) | `expired` | `now > deposit_due_at` | The pending payment is marked `failed`; both notified |
| `confirmed` | `cancel` | renter | `cancelled_by_renter` | Pickup not completed (`handover_signed`); before 00:00 business day on `starts_on` (`cancel_window_closed`) | The capture is reversed in full, then a fee group keeps `rental × cancellation_fee_rate`, split vendor/Vouch at the frozen share (A13); one `refund` row for D + C + R + F − fee; both notified |
| `confirmed` | `cancel` | vendor | `cancelled_by_vendor` | Pickup not completed (`handover_signed`); reason required (`reason_required`) | Full refund, with no fee; audit `vendor.reliability_penalty`; both notified |
| `confirmed` | `report_no_show` | renter | `cancelled_by_vendor` | From 00:00 business day on `starts_on` (`no_show_too_early`); pickup not completed (`handover_signed`); the vendor has not signed its side of the pickup (`vendor_signed_handover`) | Full refund, with no fee: the same reversals as a vendor cancellation (A17); `cancellation_reason = "vendor_no_show[: note]"`; audit `booking.cancelled_by_vendor` and `vendor.reliability_penalty` (reason `vendor_no_show`); notifies the renter (`booking.no_show_refunded`), the vendor (`booking.no_show_reported`) and ops (`ops.vendor_no_show`) |
| `confirmed` | `handover_completed` | renter, vendor or system | `in_progress` | The pickup record is `completed` (`handover_incomplete`) | Both notified. Nothing is paid at handover: everything was paid upfront (A14). |
| `in_progress` | `return_completed` | renter, vendor or system | `returned` | The return record is `completed` (`handover_incomplete`) | `claim_window_ends_at = now + 24h`; both notified |
| `in_progress` | `flag_at_risk` | system (timer) | `in_progress` | Not yet flagged, and `now >= start_of_business_day(ends_on + 1 day)` (A15) | `at_risk_at = now`; renter restricted (`at_risk`; an existing restriction is kept); audit `booking.at_risk`; ops alert |
| `in_progress` | `mark_lost` | admin | `lost` | `now >= start_of_business_day(ends_on + 7 days)` (A16; else `invalid_transition`); reason required (`reason_required`) | Deposit forfeited to the vendor (`lost_deposit_forfeited`); audit `deposit.forfeited`; renter restricted (`lost_item`, replacing any earlier reason); asset paused; both and ops notified. There is no HTTP route in M2 (§11). |
| `returned` | `claim_raised` | vendor | `disputed` | Before `claim_window_ends_at` (`claim_window_closed`); positive amount, description and evidence (400) | `claim_*` columns set; the deposit and the payout stay frozen, and nothing is posted; ops alert `ops.claim_raised` |
| `returned` | `complete` | system (timer) | `completed` | `now > claim_window_ends_at` and no claim | Deposit released (`deposit_released`, with a `refund` row for D); audit `deposit.released`; both notified |
| `disputed` | `resolve` | admin | `completed` | Reason required (`reason_required`); decision is `release`, `deduct` or `forfeit` (400); `deduct` needs a positive amount, capped at D | `resolve_release`, `resolve_deduct` or `resolve_forfeit` group; a `refund` row for whatever goes back to the renter (D, D − X, or nothing); audit `deposit.<released\|deducted\|forfeited>`; both notified |

`balance_captured` (the spec's rental payment at handover) does not exist: the renter pays everything upfront (A14). Nothing can be cancelled once both parties have signed the pickup.

**Timers** (`python -m app.jobs.timers`, every 60 s, spec §3.2): each run sweeps expire holds, expire deposit windows, complete returned bookings and flag at-risk bookings, in that order. Each action is an ordinary transition sent as `system`, in its own transaction, re-selected with `FOR UPDATE SKIP LOCKED` on the sweep's predicate, so a second run does nothing.

## 6. Price snapshot

`bookings.price_snapshot` is frozen at request time (`app/services/pricing.py`). Every value is a string. Money is written as `"150000.00"`. Keys whose value is null are left out.

| Key | Meaning |
|---|---|
| `rate_basis` | `weekly` (7 days or more and the asset has a weekly rate: full weeks × weekly + remaining days × daily), `weekend` (exactly Friday–Sunday with a weekend rate) or `daily` (daily × days) |
| `days` | Inclusive day count |
| `daily_rate`, `weekend_rate`, `weekly_rate` | The asset's rates at request time (the last two may be absent) |
| `rental_total` | R |
| `delivery_fee` | F: `assets.fulfilment.delivery_fee` for delivery, otherwise `0.00` |
| `deposit` | D |
| `damage_contribution` | C: `rental × VOUCH_DAMAGE_CONTRIBUTION_RATE` when the renter chose it, otherwise `0.00` (A4) |
| `commission_rate` | `VOUCH_COMMISSION_RATE` at request time (K = R × rate, half up) |
| `cancellation_fee_rate`, `cancellation_fee_vendor_share` | `VOUCH_RENTER_CANCELLATION_FEE_RATE` and `VOUCH_CANCELLATION_FEE_VENDOR_SHARE` at request time (A13) |
| `currency` | `NGN` |

Rows backfilled by 0003 have `{"basis": "legacy", "rental": …, "deposit": …}`.

## 7. Payment rows

| `type` | Written by | Lifecycle |
|---|---|---|
| `booking` | `accept` (A14, R21) | Recorded `pending` with the provider's reference and virtual account number (the bank in `metadata`), for D + C + R + F. The payment webhook marks it `captured` (`captured_at`). If the booking leaves `accepted` unpaid (deposit expiry, a withdrawal, a vendor cancellation), it is marked `failed` (`failed_at`). Money that still arrives for a `failed` row is a late payment: captured, then returned (§10). |
| `refund` | Every return of money to the renter (R22) | Recorded `pending` for the amount, with the provider of the captured payment, and `metadata = {"reason", "refund_of"}`. Never for 0.00. Reasons: `cancelled_by_renter` or `cancelled_by_vendor` (a paid cancellation), `vendor_no_show`, `deposit_released` (completion), `resolve_release` and `resolve_deduct` (an admin decision), and, from webhooks, `late_payment`, `amount_mismatch`, `duplicate_payment` or the refusing guard's code. Ops carries out the refund outside the API. No M2 code moves it past `pending` (§11). |
| `payout` | The `payout_completed` webhook | Created (or updated) `captured`, `payout_status = paid`, with `paid_at`, `payout_eligible_at`, `vendor_recipient_code` and `platform_fee` = K. M2 does not initiate payouts (§11). |
| `deposit`, `rental_balance`, `damage_contribution` | — | Not written in M2. The deposit hold is tracked by the ledger (`renter_deposit_liability`), not by a payment row, so the `deposit_*` columns stay NULL. |

## 8. Ledger accounts and posting recipes

| Account | Meaning |
|---|---|
| `platform_cash` | Money Vouch's provider account has received (dr) or sent out (cr) |
| `renter_deposit_liability` | Deposit holds owed back to renters (credit-normal) |
| `protection_pool` | Damage contributions (A4) |
| `vendor_payable` | What Vouch owes vendors: R + F − K, cancellation-fee shares, deductions and forfeits |
| `platform_revenue` | Commission K and Vouch's share of cancellation fees |
| `processing_fees` | Reserved; not posted in M2 |

Symbols: D deposit, C damage contribution, R rental, F delivery fee, K commission (= R × `commission_rate`, quantized half up), X deduction, P payout. Every amount comes from `price_snapshot`, never from recomputed prices. Zero-amount lines are dropped, and a group whose total is zero is not posted. Each recipe is one `txn_group`. The memo is the name in the first column.

| Memo | Recipe (`app/services/money.py`) | Lines | Posted by | `payment_id` |
|---|---|---|---|---|
| `payment_captured` | `payment_captured_postings` | dr platform_cash D+C+R+F / cr renter_deposit_liability D / cr protection_pool C / cr vendor_payable R+F−K / cr platform_revenue K | `payment_captured`; also the first half of a late payment | the `booking` row |
| `cancel_deposit_refund` | `deposit_release_postings` | dr renter_deposit_liability D / cr platform_cash D | paid `cancel`, `report_no_show` | the `refund` row |
| `cancel_contribution_refund` | `refund_contribution_postings` | dr protection_pool C / cr platform_cash C | paid `cancel`, `report_no_show` | the `refund` row |
| `cancel_rental_refund` | `rental_refund_postings` | dr vendor_payable R+F−K / dr platform_revenue K / cr platform_cash R+F | paid `cancel`, `report_no_show` | the `refund` row |
| `cancellation_fee` | `cancellation_fee_postings` | dr platform_cash fee / cr vendor_payable vendor part / cr platform_revenue Vouch part | renter `cancel` of a paid booking (A13) | the `refund` row |
| `deposit_released` | `deposit_release_postings` | dr renter_deposit_liability D / cr platform_cash D | `complete` | the `refund` row |
| `resolve_release` | `deposit_release_postings` | as above | `resolve` (release) | the `refund` row |
| `resolve_deduct` | `deposit_deduct_postings` | dr renter_deposit_liability D / cr vendor_payable X / cr platform_cash D−X (X capped at D) | `resolve` (deduct) | the `refund` row (none when X = D) |
| `resolve_forfeit` | `deposit_forfeit_postings` | dr renter_deposit_liability D / cr vendor_payable D | `resolve` (forfeit) | none |
| `lost_deposit_forfeited` | `deposit_forfeit_postings` | as above | `mark_lost` | none |
| `late_payment_refund` | `reversal_postings` of the capture | every capture line, with dr and cr swapped | webhook: money for a booking that is no longer `accepted` | the `refund` row |
| `unapplied_payment` | `unapplied_payment_postings` | dr platform_cash X / cr renter_deposit_liability X | webhook: a wrong amount, a second payment, or a capture the booking refused | the receiving row (for a second payment, the `refund` row) |
| `unapplied_payment_refund` | `reversal_postings` of the above | dr renter_deposit_liability X / cr platform_cash X | posted straight after `unapplied_payment` | the `refund` row |
| `payout` | `payout_postings` | dr vendor_payable P / cr platform_cash P | webhook `payout_completed` (P ≤ the booking's vendor_payable) | the `payout` row |

The cancellation fee is `rental × cancellation_fee_rate`, quantized half up. The vendor part is `fee × cancellation_fee_vendor_share`, quantized half up. Vouch's part is the rest, so the two always add up to the fee. A paid cancellation reverses every booking account to 0.00 before the fee group is posted. Happy-path end state: `renter_deposit_liability` 0.00, `protection_pool` C, `platform_revenue` K, `vendor_payable` R + F − K until paid out.

**Money line.** `money_line` on every booking response is computed on each read from these postings, the `refund` rows, the last `deposit.*` audit row and the booking status. It is never stored (`app/services/money_line.py`; see `docs/API_CONTRACT.md`).

## 9. Trust tiers

| Tier | Requirement | Stored where |
|---|---|---|
| 0 | Phone verified (OTP) | default |
| 1 | National ID + liveness + address, all through the KYC provider | `kyc_profiles.tier = 1`, `status = verified` |
| 2 | Tier 1, and either 3 completed bookings with no claim, or one `vendor_references` row | computed (`app/services/trust.py`), never stored |
| 3 | Effective tier 2, and the vendor's approval of this booking | `bookings.vendor_approved_at` |

`assets.minimum_trust_tier` (0–3) is the only guard column (A3). The catalogue band only sets its default.

## 10. Webhook events

`POST /api/v1/webhooks/{provider}` (`app/services/webhooks.py`):

1. **Signature.** `X-Vouch-Signature` is checked (HMAC-SHA256 hex of the raw body). If it fails, the row is stored with `signature_ok = false` under `provider_event_id = 'unverified:<sha256 of body>'`, never under the id the body claims. That way a forged body cannot take a real event's id and make the genuine delivery look like a replay. Only `{"body_sha256", "size", "claimed_event_id"?}` is kept as `payload`. The unverified body itself is never stored. The answer is 401.
2. **Idempotency.** A verified event is inserted with `ON CONFLICT (provider, provider_event_id) DO NOTHING`. If no row comes back, the event was seen before: the answer is 200 `duplicate` and nothing else happens. A body that does not parse is keyed by its own `id`, or by `sha256:<hash>`, and gets `processing_error = malformed_event`.
3. **Dispatch** by type, in a savepoint. A domain refusal rolls the savepoint back and records its code in `processing_error`. The event row stays, and the answer is still 200, so the provider does not retry an event that can never succeed. Any other error is a 500 and rolls everything back, event row included, so the provider's retry runs it again.
4. `processed_at` is set on every verified event.

`processing_error` values: `unsupported_event`, `malformed_event`, `booking_not_found`, `payment_not_found` (no `booking` payment with that reference, so unknown-reference money is kept only as this row; see `docs/OPERATIONS-NOTES.md`), `amount_mismatch`, `payment_already_captured`, a booking guard code, `payout_not_found`, `payout_already_paid`, `recipient_missing` and `payout_exceeds_payable`. A refused `payout_completed` (any of its codes) posts nothing and records or updates no payout row, although the transfer has left; like `payment_not_found`, `malformed_event` and `unsupported_event`, it must be reconciled by ops against the provider (`docs/OPERATIONS-NOTES.md` §1).

`payment_captured` outcomes:

- **Applied.** The row becomes `captured`, and the booking moves through `payment_captured`.
- **Wrong amount.** The row is captured for what actually arrived (the expected amount is kept in `metadata`). The money goes through `unapplied_payment` and `unapplied_payment_refund`, with a `refund` row and audit `payment.refunded_mismatch`. The booking is not touched; it expires at `deposit_due_at`.
- **Booking no longer `accepted`** (R23). The money is captured, then fully reversed (`late_payment_refund`), with audit `payment.refunded_late`.
- **Row already captured** (a second transfer). The money is held and returned, with refund reason `duplicate_payment` and audit `payment.refunded_duplicate`.
- **Booking refuses the capture.** The money is held and returned, with audit `payment.refunded_mismatch` or `payment.refunded_unapplied`.

Each refund of unapplied money notifies the renter (`payment.refunded`, with `next_step` of `request_again` or null) and ops (`ops.payment_refunded`).

`payout_completed` posts `payout` and marks the `payout` row paid. A payout for a booking that is neither `completed` nor `lost` is still posted (the money has already left), but ops is alerted (`ops.payout_unexpected`) and the audit row has `note = "booking_not_completed"`.

## 11. Not in M2

- An HTTP route for `mark_lost` (the service transition exists; ops calls it directly).
- Moving `refund` rows past `pending` (ops carries them out manually), and starting vendor payouts (only the provider's `payout_completed` webhook is handled).
- Real SMS, KYC and payment adapters. Only fakes exist, and they are refused outside development and test.
- KYC attempt history (`kyc_attempts`), disputes as their own table (claims live on `bookings`), evidence objects in private storage (handover evidence is a list of URLs), idempotency keys for commands, and a transactional outbox (notifications are sent before commit).
- Reviews, kits, delivery zones, search ranking, the web client and the WhatsApp adapter (spec §0).

## 12. Data handling

| Class | Examples | Controls |
|---|---|---|
| Public marketplace | Listing make/model, category, rates, deposit, city | No serial number, vendor identity, condition notes or review data in public output |
| Account PII | Name, phone, email | Role and ownership checks; soft delete is not erasure |
| Restricted identity | KYC status, tier, ID type | ID numbers and documents are never stored or logged; 422 bodies never echo input |
| Restricted evidence | Handover checklists, evidence URLs, signatures | Booking parties and admins only |
| Restricted financial | Payments, ledger, provider references | Append-only ledger; no card or bank credentials stored |
| Security and audit | Request ids, actor, action, safe diffs | Append-only audit trail; unverified webhook bodies are reduced to a hash |

Retention periods, lawful bases and cross-border safeguards are still to be decided with counsel or a DPO. Production PostgreSQL needs encrypted backups, PITR and regular restore drills before it holds real bookings. Use reviewed Alembic migrations only, and never edit an applied one.
