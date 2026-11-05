# Vouch API Contract

## 1. Status and the authoritative schema

The API base is `/api/v1`. Every endpoint in this document is **implemented** (Milestone 2). Items that are still future work are listed in §9, *Not in M2*.

The **authoritative request and response schemas are the OpenAPI document** the app serves at `/docs` (Swagger UI), `/redoc` and `/openapi.json`. These are turned off when `VOUCH_ENVIRONMENT=production`. Every route there carries examples, and `tests/test_openapi_examples.py` keeps them valid. This document summarises the contract: who may call what, the shapes, and the codes. The schema is in [`docs/SCHEMA.md`](SCHEMA.md). The spec is [`docs/specs/M2.md`](specs/M2.md), with amendments [`docs/specs/M2-amendments.md`](specs/M2-amendments.md) (A1–A18, which win where they differ).

## 2. Conventions

- **JSON** request and response bodies. Request bodies refuse unknown fields (422).
- **Ids** are UUID strings. **Dates** are `YYYY-MM-DD` and inclusive at both ends. **Timestamps** are RFC 3339 with an offset (UTC).
- **Money** is in `NGN`. Responses always write it as a string with exactly two decimals, such as `"150000.00"`. Request bodies accept money only as a string with at most 10 digits before the point and at most 2 after (`"150000"`, `"150000.5"` and `"150000.00"` are all accepted). JSON numbers, exponents and signs are 422, so no amount passes through a binary float.
- **Request ids.** A caller may send `X-Request-ID` (1–128 printable ASCII characters); otherwise the server makes one. It is echoed on every response, appears in every error body, and is stored on every audit row the request writes. CORS exposes `X-Request-ID` and `Retry-After`.
- **Pagination** (`/assets`, `/catalogue/search`): `limit` (1–100, default 20) and an opaque `cursor`. The response is `{"items": [...], "pagination": {"limit", "next_cursor"}}`.
- **Body size.** Every request body is capped at `VOUCH_MAX_REQUEST_BYTES` (default 8 MiB). Above it the answer is 413 `payload_too_large`, before anything is parsed.
- **Cities.** Launched cities come from `VOUCH_LAUNCHED_CITIES` (default `abuja`; Abuja launches first). A city filter outside that list is 400 `city_not_launched`. Unknown cities get the same answer. An asset in a city that is not launched is a 404.

## 3. Authentication and authorization

**Phone OTP, then a JWT.** `POST /auth/otp/request` sends a 6-digit code. `POST /auth/otp/verify` exchanges it for an access token. The token is HS256, signed with `VOUCH_JWT_SECRET`, has claims `sub` (user id), `role`, `iat` and `exp`, and lives for `VOUCH_JWT_TTL_MINUTES` (default 7 days). The first successful verify for a phone creates the user as a `renter`. The token's role is always the stored `users.role`, never anything the client sent. No token is ever issued for the `system` role: timers and webhooks act as `system` inside the server.

**On every protected request** the server reads `Authorization: Bearer <token>`, verifies it, and then **reloads the user from the database on the request's own session**. A missing, inactive or soft-deleted user, or one whose stored role is no longer the token's role, gets the same 401 `unauthenticated` as a bad token (with `WWW-Authenticate: Bearer`). The message never says which check failed. A restricted renter still authenticates; the booking guards refuse their requests (`renter_restricted`).

**Roles** are `renter`, `vendor` and `admin`. A valid token with the wrong role gets 403 `forbidden`. On routes about one booking, the checks run in this order, before the body is validated:

1. **Visibility.** The caller must be the booking's renter (renter role), its vendor (vendor role) or an admin. Anyone else gets 404 `resource_not_found`, exactly as for an unknown id.
2. **Role.** A party whose role may not take this action gets 403 `forbidden`.
3. **Transition.** An event the booking cannot take from its status gets 409 `invalid_transition`. A failed guard gets 409 with the guard's own code (§6).

The client never sets a role, tier, price, commission, payment status or payout eligibility.

## 4. Endpoints

| Method | Path | Who | Request | Success | Errors |
|---|---|---|---|---|---|
| GET | `/health/live` | anyone | — | 200 `{status, service, version}` | — |
| GET | `/health/ready` | anyone | — | 200 `{status: "ok", service, version, checks: {database: "ok"}}` | 503, same shape, `"unavailable"` |
| POST | `/auth/otp/request` | anyone | `{phone}` | 202 `{"status": "sent"}`, always, known phone or not | 422, 429 `rate_limited` (+`Retry-After`), 503 `otp_unavailable` |
| POST | `/auth/otp/verify` | anyone | `{phone, code, full_name?}` | 200 `{access_token, token_type: "bearer", expires_in}` | 401 `unauthenticated` (every failure), 422 |
| POST | `/me/kyc` | renter | `{id_type, id_number, selfie_b64, address_doc_b64}` | 200 `{tier, status}` | 401, 403, 422, 503 `kyc_unavailable` |
| GET | `/assets` | anyone | query: `city`, `category`, `starts_on`, `ends_on`, `min_daily_rate`, `max_daily_rate`, `limit`, `cursor` | 200 page of `PublicAsset` | 400 `invalid_request` / `city_not_launched` / `invalid_cursor`, 422 |
| GET | `/assets/{asset_id}` | anyone | — | 200 `PublicAsset` | 404, 422 |
| GET | `/catalogue/search` | anyone | query: `q`, `city`, `from`, `to`, `limit`, `cursor` | 200 page of `PublicAsset` + `equivalents` | 400 `invalid_request` / `city_not_launched` / `invalid_cursor`, 422 |
| POST | `/bookings` | renter | `{asset_id, starts_on, ends_on, fulfilment, damage_contribution}` | 201 `Booking` (`requested`) | 400, 401, 403, 404, 409 guards, 422 |
| GET | `/bookings/{id}` | the booking's renter or vendor, or an admin | — | 200 `Booking` | 401, 403, 404, 422 |
| POST | `/bookings/{id}/accept` | vendor | — | 200 `Booking` (`accepted`) | 401, 403, 404, 409 (incl. `vendor_not_active`), 422, 502 `payment_provider_error`, 503 `payment_unavailable` |
| POST | `/bookings/{id}/decline` | vendor | `{reason?}` | 200 `Booking` (`declined`) | 401, 403, 404, 409, 422 |
| POST | `/bookings/{id}/approve` | vendor | — | 200 `Booking` (`vendor_approved_at` set) | 401, 403, 404, 409, 422 |
| POST | `/bookings/{id}/cancel` | renter or vendor | `{reason?}` (vendor: required) | 200 `Booking` (`cancelled_by_renter` / `cancelled_by_vendor`) | 401, 403, 404, 409, 422 |
| POST | `/bookings/{id}/no-show` | renter | `{reason?}` (≤ 500 chars) | 200 `Booking` (`cancelled_by_vendor`) | 401, 403, 404, 409, 422 |
| POST | `/bookings/{id}/handover` | renter or vendor | `{checklist, evidence_urls, sign}` | 200 `Booking` (`in_progress` once both have signed) | 400, 401, 403, 404, 409, 422 |
| POST | `/bookings/{id}/return` | renter or vendor | `{checklist, evidence_urls, sign}` | 200 `Booking` (`returned` once both have signed) | 400, 401, 403, 404, 409, 422 |
| POST | `/bookings/{id}/claim` | vendor | `{amount, description, evidence_urls}` | 200 `Booking` (`disputed`) | 400, 401, 403, 404, 409, 422 |
| POST | `/bookings/{id}/resolve` | admin | `{decision, reason, amount?}` | 200 `Booking` (`completed`) | 400, 401, 403, 404, 409, 422 |
| GET | `/me/home` | renter | — | 200 `{active, past}` | 401, 403 |
| GET | `/catalogue/items` | anyone | query: `q`, `category` (slug), `limit`, `cursor` | 200 page of `{id, category, make, model, mount, specs, replacement_value_band}` (active items only) | 400 `invalid_request` (unknown category) / `invalid_cursor`, 422 |
| GET | `/vendor/profile` | vendor | — | 200 `{status, business_type, is_sub_broker, business_name, bank_name, account_name, account_last4, has_recipient_code}` (the recipient code itself is never returned) | 401, 403, 404 (no profile) |
| POST | `/vendor/assets` | vendor (active profile) | `{catalogue_item_id, city, serial_number?, condition_grade?, daily_rate, deposit_amount, weekend_rate?, weekly_rate?, description?, condition_notes?, included_accessories?, fulfilment?, minimum_trust_tier?}` | 201 `OwnAsset` (`draft`) | 400 `city_not_launched`, 401, 403, 409 `vendor_not_active` / `catalogue_item_inactive` / `duplicate_serial`, 422 (incl. unknown catalogue item, tier below the band default) |
| GET | `/vendor/assets` | vendor | query: `status`, `limit`, `cursor` | 200 page of `OwnAsset`, newest first | 400 `invalid_cursor`, 401, 403, 422 |
| GET | `/vendor/assets/{id}` | vendor (own listing) | — | 200 `OwnAsset` | 401, 403, 404, 422 |
| PATCH | `/vendor/assets/{id}` | vendor (own listing, active profile) | any create field except `minimum_trust_tier` | 200 `OwnAsset` | 400 `city_not_launched`, 401, 403, 404, 409 `vendor_not_active` / `listing_locked` / `listing_incomplete` / `catalogue_item_inactive` / `duplicate_serial`, 422 |
| POST | `/vendor/assets/{id}/submit` | vendor (own listing, active profile) | — | 200 `OwnAsset` (`review`) | 401, 403, 404, 409 `vendor_not_active` / `invalid_transition` / `listing_incomplete` / `catalogue_item_inactive`, 422 |
| POST | `/vendor/assets/{id}/pause` | vendor (own listing, active profile) | — | 200 `OwnAsset` (`paused`, `paused_by: "vendor"`) | 401, 403, 404, 409 `vendor_not_active` / `invalid_transition`, 422 |
| POST | `/vendor/assets/{id}/resume` | vendor (own listing, active profile) | — | 200 `OwnAsset` (`active`) | 401, 403, 404, 409 `vendor_not_active` / `invalid_transition` (not paused, or paused by an admin), 422 |
| DELETE | `/vendor/assets/{id}` | vendor (own listing, active profile) | — | 204 (soft delete) | 401, 403, 404, 409 `vendor_not_active` / `has_live_bookings`, 422 |
| POST | `/vendor/assets/{id}/blocks` | vendor (own listing, active profile) | `{starts_on, ends_on}` (inclusive dates) | 201 `{id, asset_id, starts_on, ends_on, created_at}` | 400 `invalid_request` (start after end, or before today), 401, 403, 404, 409 `vendor_not_active` / `dates_blocked` / `dates_unavailable`, 422 |
| GET | `/vendor/assets/{id}/blocks` | vendor (own listing) | query: `from` (date; default today in the business zone) | 200 `{items: [block]}`, blocks ending on or after `from`, by start date | 401, 403, 404, 422 |
| DELETE | `/vendor/assets/{id}/blocks/{block_id}` | vendor (own listing, active profile) | — | 204 | 401, 403, 404 (not a block of this listing), 409 `vendor_not_active`, 422 |
| GET | `/vendor/home` | vendor | — | 200 `{requests, today: {handovers, returns}, next_payout, utilisation_7d}` | 401, 403 |
| POST | `/webhooks/{provider}` | the payment provider (no token; `X-Vouch-Signature`) | the provider's raw event | 200 `{status: "processed" \| "duplicate", processing_error}` | 401 `invalid_signature`, 404 (provider is not the configured one), 413, 422 |

### 4.1 Auth and KYC

- `phone` accepts `0XXXXXXXXXX`, `234XXXXXXXXXX` or `+234XXXXXXXXXX` and is normalized to `+234XXXXXXXXXX`. Anything else is 422. `code` is six digits.
- A code lives for 10 minutes and allows 5 wrong guesses. A newer code replaces the older ones. More than **5 requests per phone in a rolling hour** get 429 with `Retry-After` (whole seconds). Every failed verify (no challenge, a wrong, expired, used or exhausted code, or an inactive or deleted account) is the same 401. The verify route commits even on failure, so every guess counts.
- `full_name` is used only when the verify creates the account.
- `POST /me/kyc`: `id_type` is `nin`, `bvn`, `passport` or `drivers_licence`, and `id_number` matches `^[A-Za-z0-9-]{6,24}$`. The two files are base64, at most 2 MB each once decoded. Bad base64, an empty file or an oversize file is 422, and 422 bodies never echo the input. Both checks must pass: then the answer is `verified` at tier 1, otherwise `failed` at tier 0, and the renter may try again. Posting again after a success is an idempotent 200. The documents and the id number are never stored or logged.

### 4.2 Inventory

`PublicAsset` is `{id, name, display_name, make, model, description, category, condition_grade, daily_rate, weekend_rate, weekly_rate, deposit_amount, currency, city, is_available, minimum_trust_tier, created_at, updated_at}`. It never includes the serial number, vendor identity, condition notes or review data. Only active, available, non-deleted listings of active vendors (an active user whose vendor profile is `active`) in launched cities are visible; a pending or suspended vendor's listings vanish from search and their detail is 404.

- `/assets`: `starts_on` and `ends_on` must be given together, with `starts_on <= ends_on` (400 otherwise). With them, units that have a live booking or a blocked window overlapping the range are left out.
- `/catalogue/search`: `q` is a full-text match on make, model and aliases. `from` and `to` work like the `/assets` dates. If nothing is free and `q` matched catalogue items, the search runs again over those items' categories without `q`, and the response says `"equivalents": true`.

### 4.3 Bookings

**`Booking`** is `{id, status, asset_id, renter_id, vendor_id, starts_on, ends_on, fulfilment_choice, hold_expires_at, deposit_due_at, claim_window_ends_at, vendor_approved_at, price, money_line, payment_instructions?}`.

- `price` is `{rental_total, delivery_fee, deposit, damage_contribution, currency}`, frozen at request time.
- `payment_instructions` is `{bank, account_number, reference, amount, due_by}`. It is shown only to the booking's renter, only while the booking is `accepted` and unpaid. `amount` is the whole upfront payment: deposit + damage contribution + rental + delivery fee (A14). `due_by` is `deposit_due_at`, 12 hours after acceptance.

Behaviour, in short (the full transition table, with every guard, is in `docs/SCHEMA.md` §5):

- **Request.** Holds the unit for 4 hours (`hold_expires_at`) while the vendor answers. A tier-3 item needs `/approve` before `/accept`.
- **Accept.** Opens the renter's virtual account for the whole amount. Without a configured payment provider the answer is 503 and nothing changes. If the provider call fails the answer is 502 and nothing changes.
- **Payment.** The provider's `payment_captured` webhook confirms the booking (`accepted` → `deposit_paid` → `confirmed`). The renter pays **everything upfront**; nothing is paid at handover. An unpaid acceptance expires at `deposit_due_at`.
- **Cancel.** A renter may **withdraw for free** while the booking is `requested`, or `accepted` and unpaid (A13). After payment, a renter cancels until 00:00 (business timezone) on `starts_on`, and gets back everything paid minus a cancellation fee: `rental × VOUCH_RENTER_CANCELLATION_FEE_RATE`, shared with the vendor. A vendor cancellation (of an `accepted` or paid booking) needs a reason, refunds the renter in full, and records a reliability penalty. Nobody can cancel once both parties have signed the pickup.
- **No-show** (A17). From 00:00 on `starts_on`, while there is no completed pickup and the vendor has not signed its side of it, the renter may report a no-show. The booking becomes `cancelled_by_vendor`, the renter gets a full refund with no fee, and the vendor gets a reliability penalty.
- **Handover / return.** Each party submits its own checklist (at most 50 flat entries) and evidence URLs (1–20 per call, at most 40 per record), optionally signing. A signed side is final. When both have signed, the booking moves on. A return opens a 24-hour claim window.
- **Claim.** Inside the window, the vendor claims against the deposit. The deposit and the payout freeze (`disputed`). Ops is alerted.
- **Resolve.** An admin decides `release`, `deduct` (with `amount`, capped at the deposit; `amount` is required for deduct and refused otherwise, 422) or `forfeit`. The decision needs a reason. The booking is then `completed`.
- Without a claim, the timer completes a returned booking once the window closes, and the deposit hold is released.

### 4.4 The money line

Every booking response, and every home-screen card, carries `money_line`. It is computed on each read from the ledger, the refund rows, the audit trail and the booking status, and never stored:

```json
"money_line": {
  "deposit":      {"amount": "150000.00", "status": "held", "held_by": "platform"},
  "contribution": {"amount": "7500.00",   "status": "received"},
  "rental":       {"amount": "150000.00", "status": "received"},
  "next":         {"label": "Handover Fri 13 Nov", "amount_due": "0.00"}
}
```

| Part | Present | `status` values |
|---|---|---|
| `deposit` (D) | always | `due` before payment; `held` while the deposit hold is open; `released`, `deducted` or `forfeited` once it ends (`released` after a paid cancellation); `cancelled` when the booking closed before any payment |
| `contribution` (C) | only when C > 0 | `due`, `received`, `refunded`, `cancelled` |
| `rental` (R + F) | always | `due`, `received` (once the upfront payment is captured), `refunded` (paid cancellation), `paid_out` (payouts cover the vendor's whole payable), `cancelled` |
| `cancellation_fee` | only after a paid renter cancellation | `{amount, vendor_part, platform_part}` |
| `refund` | only on a booking cancelled after payment | `{amount}` = everything paid less any cancellation fee. `refund + cancellation_fee` always equals everything paid. |
| `returned_payments` | only when there are some | A list of `{amount, reason}`: money that reached the booking's account but was never applied, and is returned in full. `reason` is `late_payment`, `amount_mismatch`, `duplicate_payment` or the refusing check's code. It never changes the other parts' status. |
| `next` | always | `{label, amount_due}`: `Awaiting vendor response`; `Payment due <time>` with `amount_due` = D + C + R + F; `Handover <day>`; `Return by <day>`; `Claim window closes <time>`; `Under review`; `Closed` |

A booking that ends `declined`, `expired`, `cancelled_by_renter` or `cancelled_by_vendor` with no payment ever captured shows `cancelled` for each part, and `next` is `Closed` / `"0.00"` (A18). A no-show shows deposit `released`, rental and contribution `refunded`, and a refund of the whole amount paid. Parts that do not apply are left out of the JSON, not sent as `null`.

### 4.5 Home screens

Each card is `{id, status, asset_id, asset_name, starts_on, ends_on, hold_expires_at, money_line}`.

- `GET /me/home`: `active` is the live (or disputed) booking that starts soonest, or `null`. `past` holds closed bookings, newest start first, at most 20.
- `GET /vendor/home`:
  - `requests`: unanswered requests whose hold has not lapsed, ordered by `hold_expires_at` ascending;
  - `today.handovers`: paid bookings starting today; `today.returns`: rentals ending today;
  - `next_payout`: what the vendor's closed bookings still owe the vendor, net of payouts;
  - `utilisation_7d`: booked asset-days over today and the next six days, divided by active listings × 7, as a two-decimal string.

### 4.6 Webhooks

`POST /webhooks/{provider}`. `{provider}` must be the configured payment provider (404 otherwise). There is no bearer token: the body must carry `X-Vouch-Signature`, the HMAC-SHA256 (hex) of the raw body. The fake provider signs with `VOUCH_FAKE_PAYMENT_WEBHOOK_SECRET`. The fake's event shape is:

```json
{"id": "evt_…", "type": "payment_captured",
 "data": {"booking_id": "uuid", "reference": "fake-booking-…", "amount": "312500.00"}}
```

`payout_completed` events also carry `data.recipient_code`.

- A bad or missing signature is recorded (`signature_ok = false`, keyed by the body's hash) and answered 401 `invalid_signature`. Nothing else happens.
- An event seen before (same provider and event id) is answered 200 `{"status": "duplicate"}`, and nothing else happens.
- Otherwise the event is recorded and applied, and answered 200 `{"status": "processed"}`. `processing_error` is set when a genuine event cannot be applied (for example `unsupported_event`, `payment_not_found`, `amount_mismatch`, `payout_exceeds_payable`). The 200 stops the provider retrying an event that can never succeed. Money that arrives but cannot be applied is recorded and returned to the renter (see `money_line.returned_payments`).

The route commits in every case, including the 401. The full semantics are in `docs/SCHEMA.md` §10.

### 4.7 Vendor listings

`OwnAsset` is the owning vendor's full view: `{id, catalogue_item_id, make, model, category, city, serial_number, condition_grade, condition_notes, description, daily_rate, weekend_rate, weekly_rate, deposit_amount, currency, included_accessories, fulfilment: {pickup, delivery, delivery_fee}, listing_status, minimum_trust_tier, paused_by, review_reason, reviewed_at, is_available, created_at, updated_at}`. Nobody else ever sees it; the public view stays `PublicAsset`. Another vendor's listing, and a deleted one, are 404 on every route, exactly like an unknown id.

- **Vendor status.** Reads work whatever the vendor's profile status. Every write (create, edit, submit, pause, resume, delete, and adding or removing a block) needs an `active` profile: 409 `vendor_not_active` otherwise.
- **Create.** The listing starts as `draft`. `category` comes from the catalogue item and is never accepted from the client. An unknown catalogue item is 422 (a bad body field, in FastAPI's `detail` shape, `loc: ["body", "catalogue_item_id"]`); an inactive one is 409 `catalogue_item_inactive`. `city` must be a launched city slug. `minimum_trust_tier` defaults from the item's band (A→1, B→2, C→3); the vendor may raise it up to 3, and a value below the band default is 422 (`loc: ["body", "minimum_trust_tier"]`). A serial the vendor already lists for the same item (any letter case) is 409 `duplicate_serial`. `fulfilment` defaults to pickup only; `delivery_fee` is a money string.
- **States.** `draft`/`rejected` →`submit`→ `review` →(admin approve)→ `active` ⇄ `paused` (`pause`/`resume`); review →(admin reject)→ `rejected`. Any other event is 409 `invalid_transition` with `details: {from, event}`. A vendor resumes only a listing they paused themselves (`paused_by: "vendor"`).
- **Submit** needs a serial number, a condition grade, `daily_rate > 0`, `deposit_amount >= 0`, pickup or delivery enabled, and a `delivery_fee` (zero counts) when delivery is enabled; otherwise 409 `listing_incomplete` with `details.missing`. A listing whose catalogue item has since been retired is 409 `catalogue_item_inactive`.
- **Edits.** Only the fields sent change, and a value equal to the current one is not a change. In `review` every edit is 409 `listing_locked`. Identity fields (`catalogue_item_id`, `serial_number`, `city`) change only in `draft` or `rejected` (409 `listing_locked`, `details.fields` naming them); commercial fields (rates, deposit, accessories, fulfilment, description, condition notes and grade) also in `active` and `paused`. A booking keeps the `price_snapshot` it was requested with, so an edit never changes an existing booking. An `active` or `paused` listing must stay complete by the submit rules: an edit that would leave something missing (for example delivery without a `delivery_fee`, neither pickup nor delivery, or no condition grade) is 409 `listing_incomplete` with `details.missing`, and nothing changes; a `draft` or `rejected` listing may stay incomplete until it is submitted. `minimum_trust_tier` cannot be sent (422): only an admin changes it. A new catalogue item re-derives the category, and a tier below the new item's band default is raised to it. Each edit that changes something writes one `asset.updated` audit row with the before and after of the changed fields only.
- **Pause** removes the listing from search; live bookings continue. **Delete** is a soft delete (204), refused with 409 `has_live_bookings` while any booking on the listing is live or `disputed`; the serial can then be listed again.
- **Availability blocks** close dates on the listing to new requests (a request on them is 409 `dates_blocked`). A block may be added in any listing status. Both dates are inclusive; the start may not be after the end, nor before today in the business zone (400 `invalid_request`). A block that overlaps another block of the listing is 409 `dates_blocked` (the exclusion constraint on `availability_windows` is the backstop and gets the same answer); one that overlaps a live booking is 409 `dates_unavailable`. `GET` lists blocks ending on or after `from` (default today), ordered by start date. `DELETE` removes the block (204), and its dates can be requested again; a block id that is not a block of this listing is 404. Each add or removal writes one audit row (`availability.blocked` / `availability.unblocked`, with the asset and the dates).

## 5. Error envelope

Every error except a 422 uses this envelope:

```json
{"error": {"code": "hold_expired", "message": "The request hold has expired.",
           "details": {"hold_expires_at": "2026-11-02T13:00:00+00:00"},
           "request_id": "req_…"}}
```

A 422 is FastAPI's validation body, `{"detail": [{"type", "loc", "msg", "ctx"?}]}`. The rejected `input` is removed, so submitted data is never echoed.

## 6. Error codes

| Status | `code` | Meaning |
|---|---|---|
| 400 | `invalid_request` | The input is well-formed but not acceptable: bad date range, a start in the past, too long or too far ahead, fulfilment not offered, a claim without amount, description or evidence, too much evidence, a bad resolve decision |
| 400 | `city_not_launched` | The `city` filter is not a launched city (unknown cities included) |
| 400 | `invalid_cursor` | The pagination cursor cannot be decoded |
| 401 | `unauthenticated` | Missing, bad or expired token; the token's user is missing, inactive, deleted or has another role; any failed OTP verify. Sent with `WWW-Authenticate: Bearer`. |
| 401 | `invalid_signature` | The webhook's `X-Vouch-Signature` is missing or wrong |
| 403 | `forbidden` | Authenticated, but this role may not use the route or action |
| 404 | `resource_not_found` | Unknown or hidden: an asset that is not public (including every listing of a vendor whose profile is not `active`), a booking the caller is not party to, an unknown webhook provider |
| 409 | `invalid_transition` | The booking (or listing) cannot take this event from its status, or this actor may not send it. `details: {from, event}` |
| 409 | `vendor_not_active` | The vendor's profile is `pending` or `suspended` (or missing): no listing write at all (create, `PATCH`, submit, pause, resume, delete, adding or removing a block) and no accepting a request. Reads, handovers and returns still work. `details: {status}` |
| 409 | `listing_locked` | The vendor's listing cannot take this edit: any edit while it is in `review`, or an identity field (`catalogue_item_id`, `serial_number`, `city`) outside `draft`/`rejected`. `details: {status, fields?}` |
| 409 | `listing_incomplete` | A listing submitted for review lacks something it needs, or an edit would leave an `active` or `paused` listing without it (nothing is changed). `details: {missing: [...]}` (`serial_number`, `condition_grade`, `daily_rate`, `deposit_amount`, `fulfilment`, `delivery_fee`) |
| 409 | `catalogue_item_inactive` | The listing names a catalogue item that is no longer active |
| 409 | `duplicate_serial` | The vendor already lists a unit of this catalogue item with this serial number (any letter case) |
| 409 | `has_live_bookings` | A listing with a live or disputed booking cannot be deleted |
| 409 | `tier_insufficient` | The renter's effective trust tier is below the item's minimum. `details: {required, effective}` |
| 409 | `vendor_approval_required` | A tier-3 item: the vendor must approve the renter before accepting |
| 409 | `approval_not_required` | `/approve` on an item that does not need it |
| 409 | `dates_blocked` | The vendor has blocked some of the dates: a booking request on a block, or a new block that overlaps another block of the listing |
| 409 | `dates_unavailable` | A live booking holds overlapping dates: a booking request on them, or a new block over them |
| 409 | `renter_restricted` | The renter's account is restricted, inactive or deleted |
| 409 | `self_booking` | The renter is the item's own vendor |
| 409 | `hold_expired` | The 4-hour request hold has passed. `details: {hold_expires_at}` |
| 409 | `amount_mismatch` | The payment is not the amount the booking expects |
| 409 | `cancel_window_closed` | A renter cancellation after 00:00 on `starts_on`. `details: {cancel_before}` |
| 409 | `handover_signed` | Both parties have signed the pickup; the booking can no longer be cancelled or reported as a no-show |
| 409 | `handover_incomplete` | Both parties have not signed the handover yet |
| 409 | `handover_already_signed` | The caller's side of this handover record is already signed |
| 409 | `claim_window_closed` | The 24-hour claim window has passed |
| 409 | `reason_required` | A vendor cancellation, a resolve or a mark-lost needs a reason |
| 409 | `no_show_too_early` | A no-show reported before 00:00 on `starts_on`. `details: {report_from}` |
| 409 | `vendor_signed_handover` | The vendor has signed the pickup, so a no-show cannot be reported |
| 413 | `payload_too_large` | The request body is over `VOUCH_MAX_REQUEST_BYTES` |
| 422 | (FastAPI `detail` list) | The request fails schema validation (types, lengths, unknown fields, money not a string, bad phone or base64) |
| 429 | `rate_limited` | Too many OTP requests for the phone; `Retry-After` gives the wait in seconds |
| 502 | `payment_provider_error` | The payment provider could not open the renter's account on accept; nothing changed, so retry |
| 503 | `otp_unavailable` | No OTP sender is configured |
| 503 | `kyc_unavailable` | No KYC provider is configured |
| 503 | `payment_unavailable` | No payment provider is configured, so a booking cannot be accepted |

`GET /health/ready` answers 503 with its own readiness body, not the envelope. An unexpected server error is a plain 500.

## 7. Providers in each environment

M2 ships **fakes only** for the OTP sender, the KYC provider and the payment provider (`VOUCH_OTP_SENDER`, `VOUCH_KYC_PROVIDER` and `VOUCH_PAYMENT_PROVIDER` = `fake`). In `development` and `test`, the fakes are used when these settings are unset. In `staging` and `production` the fakes are refused at start-up, and nothing else exists yet, so those environments answer **503** for OTP requests (`otp_unavailable`), KYC (`kyc_unavailable`) and accepting a booking (`payment_unavailable`), until real adapters land.

## 8. Wording

API fields and copy say **deposit hold** and **damage contribution**. The damage contribution is an optional Vouch-run charge held in the `protection_pool` account (A4). No third-party product backs it, and its rate and terms remain placeholders pending legal advice (A8).

## 9. Not in M2

- An HTTP route for marking a booking lost (`mark_lost` exists as an admin service transition).
- Refund execution and payout initiation through the provider. Refund rows stay `pending` for ops, and only the provider's `payout_completed` webhook is handled.
- Vendor listing management (create, edit, availability) and admin review endpoints. Listings and blocks are written by operations directly in M2.
- `Idempotency-Key` on commands, `ETag` / `If-Match` versioning, booking lists and timelines, KYC webhooks, private evidence uploads, and a transactional outbox.
- Real Paystack, KYC and SMS adapters, the web client and the WhatsApp adapter.
