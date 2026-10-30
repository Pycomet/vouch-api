# Vouch API Contract

## 1. Status and compatibility

The API base is `/api/v1`. The health and public inventory endpoints are currently implemented. Every identity, booking, payment, handover, admin, and webhook endpoint below is **planned** and must not be represented as available until implemented and published in OpenAPI.

Current endpoints:

| Method | Path | Auth | Behavior |
|---|---|---|---|
| `GET` | `/api/v1/health/live` | None | Process liveness; `200` with service/version |
| `GET` | `/api/v1/health/ready` | None | Dependency readiness; `200` when DB ready, `503` otherwise |
| `GET` | `/api/v1/assets` | None | Search active Lagos inventory; supports category, date-range, daily-rate, limit, and cursor filters |
| `GET` | `/api/v1/assets/{asset_id}` | None | Public detail of a single active Lagos listing; `404` for non-public or unknown assets |

The FastAPI application currently exposes Swagger/ReDoc/OpenAPI outside production only.

## 2. Conventions for planned endpoints

### 2.1 Transport and representation

- HTTPS only in production; JSON request/response bodies except direct-to-object-storage uploads.
- Auth mechanism is unresolved; protected examples use `Authorization: Bearer <token>` as a contract placeholder, not a provider selection.
- UUIDs are strings; dates are `YYYY-MM-DD`; timestamps are RFC 3339 UTC; amounts are decimal strings such as `"25000.00"`; currency is `NGN`.
- Responses include `X-Request-ID`; callers may supply `X-Request-ID`, but the server validates/generates it.
- Collection pagination uses opaque cursors: `?limit=20&cursor=...`, max 100.
- Serial numbers, internal notes, provider raw payloads, evidence storage keys, KYC data, commission internals, and private contact data are omitted unless the authorized role requires them.

### 2.2 Error envelope

```json
{
  "error": {
    "code": "booking_state_conflict",
    "message": "Booking cannot be activated from its current state.",
    "details": {},
    "request_id": "req_..."
  }
}
```

Expected statuses: `400` malformed/domain validation, `401` unauthenticated, `403` unauthorized or trust-tier gate, `404` hidden/not found, `409` state/availability/idempotency conflict, `422` schema validation, `429` throttled, and `503` dependency unavailable. Errors must not leak provider secrets, raw KYC reasons, or private evidence.

### 2.3 Idempotency and concurrency

`Idempotency-Key` is required on planned booking creation, payment initialization, refund, payout, and other money-moving command endpoints. The key is scoped to authenticated principal + operation; replay with the same normalized payload returns the stored result, while reuse with a different payload returns `409 idempotency_key_reused`.

State-changing resources return an `ETag` or version. Commands that can race use `If-Match`; stale writes return `409 resource_version_conflict`. Database transactions, booking row locks/version checks, and the exclusion constraint protect booking confirmation.

### 2.4 Authorization matrix

| Resource/action | Renter | Vendor | Admin/ops |
|---|---|---|---|
| Public asset search/detail | Yes | Yes | Yes |
| Private user/KYC | Self | Self | Authorized operations only |
| Create booking/pay/sign renter side | Self | No | Controlled override |
| Approve booking/manage owned asset/sign vendor side | No | Owner vendor | Controlled override |
| View booking/evidence | Booking party | Booking's vendor | Authorized operations |
| Resolve dispute/refund/payout | No | No | Segregated authorized roles |

Every override requires a reason and audit event. Client-supplied role, tier, ownership, prices, commission, payment status, or payout eligibility are ignored.

## 3. Resource shapes

### 3.1 Public asset

```json
{
  "id": "uuid",
  "name": "Sony FX3",
  "description": "Camera body with cage",
  "category": "camera",
  "daily_rate": "75000.00",
  "deposit_amount": "200000.00",
  "currency": "NGN",
  "city": "lagos",
  "is_available": true,
  "minimum_trust_tier": 1
}
```

`minimum_trust_tier` is planned; current assets do not persist it. Public responses never include `serial_number`.

### 3.2 Booking

```json
{
  "id": "uuid",
  "asset_id": "uuid",
  "renter_id": "uuid",
  "starts_on": "2026-10-10",
  "ends_on": "2026-10-12",
  "status": "pending",
  "total_rental_fee": "225000.00",
  "deposit_amount": "200000.00",
  "currency": "NGN",
  "required_trust_tier": 1,
  "deposit_status": "pending",
  "balance_status": "pending",
  "pickup_status": null,
  "return_status": null,
  "payout_status": "pending",
  "version": 1,
  "created_at": "2026-09-26T12:00:00Z",
  "updated_at": "2026-09-26T12:00:00Z"
}
```

Several aggregate fields are planned projections and do not currently exist as columns.

### 3.3 Payment

```json
{
  "id": "uuid",
  "booking_id": "uuid",
  "type": "deposit",
  "status": "pending",
  "amount": "200000.00",
  "currency": "NGN",
  "provider": "paystack",
  "provider_reference": "redacted-or-safe-reference",
  "payment_url": "short-lived-provider-url",
  "virtual_account": null,
  "created_at": "2026-09-26T12:00:00Z"
}
```

Provider initialization details are returned only to the booking renter. Responses describe the arrangement only as a deposit hold.

### 3.4 Handover

```json
{
  "id": "uuid",
  "booking_id": "uuid",
  "direction": "pickup",
  "status": "draft",
  "condition_checklist": {
    "schema_version": 1,
    "items": [
      {"key": "body", "condition": "good", "notes": "Minor mark on cage"}
    ]
  },
  "vendor_signed_at": null,
  "renter_signed_at": null,
  "evidence": [],
  "notes": null,
  "version": 1
}
```

Permanent object keys and exact coordinates are not public. Authorized evidence reads return short-lived URLs.

## 4. Authentication and account APIs — planned

The identity/session implementation is undecided. The stable resource contract should expose:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/auth/challenges` | Send a phone verification challenge; rate-limited |
| `POST` | `/auth/challenges/verify` | Verify challenge and establish/issue session |
| `POST` | `/auth/refresh` | Rotate session/token if token auth is selected |
| `POST` | `/auth/logout` | Revoke current session |
| `GET` | `/me` | Return current user, role, active state, and safe KYC summary |
| `PATCH` | `/me` | Update allowed profile fields |

Creating or verifying a challenge is idempotent within a short server window and must not reveal whether unrelated phone numbers exist.

## 5. KYC APIs — planned

Smile ID versus Dojah remains **unresolved**. The API exposes provider-neutral outcomes.

| Method | Path | Actor | Purpose |
|---|---|---|---|
| `GET` | `/kyc/me` | User | Current status, tier, required checks; no raw provider payload |
| `POST` | `/kyc/sessions` | User | Initiate/retry provider flow for requested eligible tier |
| `GET` | `/kyc/sessions/{id}` | Owner | Safe status of attempt |
| `POST` | `/webhooks/kyc/{provider}` | Provider | Signed callback/event ingestion |
| `POST` | `/admin/kyc/{profile_id}/review` | Ops | Audited approve/reject/retry decision where policy allows |

`POST /kyc/sessions` request:

```json
{"target_tier": 1, "return_url": "https://approved-origin.example/kyc/return"}
```

The server derives required checks: tier 1 is national ID + liveness; tier 2 adds address. Return URLs must match an allowlist. A successful browser return does not verify KYC; only verified provider results and authorized review can do so.

## 6. Asset and availability APIs — planned

| Method | Path | Actor | Purpose |
|---|---|---|---|
| `GET` | `/assets` | Public | Search active, non-deleted creative gear |
| `GET` | `/assets/{asset_id}` | Public | Public detail and safe availability summary |
| `POST` | `/vendor/assets` | Vendor | Create draft creative-gear listing |
| `GET` | `/vendor/assets` | Vendor | List own listings, including non-public fields |
| `GET` | `/vendor/assets/{asset_id}` | Owner | Own listing detail, including restricted serial number |
| `PATCH` | `/vendor/assets/{asset_id}` | Owner | Edit allowed listing fields; reviewed fields may return listing to review |
| `DELETE` | `/vendor/assets/{asset_id}` | Owner/ops | Soft-delete when policy permits; cannot erase booking history |
| `PUT` | `/vendor/assets/{asset_id}/availability` | Owner | Replace/version availability blocks |
| `POST` | `/admin/assets/{asset_id}/review` | Ops | Approve/reject/pause with reason |

Search parameters: `city=lagos`, `category=camera`, `starts_on`, `ends_on`, `min_daily_rate`, `max_daily_rate`, cursor and limit. v1 serves Lagos until the Abuja gate is explicitly enabled, even though `abuja` exists in the current enum.

Create request uses server validation and decimal strings:

```json
{
  "name": "Sony FX3",
  "description": "Camera body with cage",
  "category": "camera",
  "serial_number": "restricted-value",
  "daily_rate": "75000.00",
  "deposit_amount": "200000.00",
  "city": "lagos",
  "condition_notes": "Operational; inspected"
}
```

## 7. Booking APIs — planned

| Method | Path | Actor | Purpose |
|---|---|---|---|
| `POST` | `/bookings` | Verified renter | Create `pending` booking with server-side quote snapshot |
| `GET` | `/bookings` | User | List bookings visible to current party |
| `GET` | `/bookings/{id}` | Party/ops | Booking aggregate and allowed next actions |
| `POST` | `/bookings/{id}/approve` | Owning vendor/ops | Approve request; Phase 0 remains concierge-gated |
| `POST` | `/bookings/{id}/reject` | Owning vendor/ops | Cancel/reject pending request with reason |
| `POST` | `/bookings/{id}/cancel` | Authorized party/ops | Apply approved cancellation policy |
| `POST` | `/bookings/{id}/disputes` | Booking party/ops | Open dispute and block affected money movement |
| `GET` | `/bookings/{id}/timeline` | Party/ops | Redacted lifecycle and evidence timeline |

`POST /bookings` requires `Idempotency-Key`:

```json
{
  "asset_id": "uuid",
  "starts_on": "2026-10-10",
  "ends_on": "2026-10-12"
}
```

The server verifies asset scope/city/status, trust tier, date availability, price, and deposit. It ignores any client price. Creating `pending` does not reserve against the current database exclusion constraint; confirmation is transactional and may return `409 asset_unavailable` if another booking wins.

### Booking state commands

| Transition | Required conditions |
|---|---|
| `pending → confirmed` | Vendor/concierge approval plus provider-verified deposit collection |
| `confirmed → active` | Provider-verified balance plus completed co-signed pickup |
| `active → completed` | Completed co-signed return and no open dispute |
| `active → disputed` | Authorized claim with reason/evidence |
| `disputed → completed` | Authorized, audited resolution with financial instructions |

Responses expose `allowed_actions`; the server remains authoritative.

## 8. Payment APIs — planned, Paystack first

Paystack is the first integration. Exact support for deposit custody/hold/release, per-booking accounts, splits/transfers, and refunds is **unresolved pending provider validation and Nigerian legal review**. The contract describes Vouch's needed behavior, not a claim that Paystack currently provides a custodial holding product.

| Method | Path | Actor | Purpose |
|---|---|---|---|
| `POST` | `/bookings/{id}/payments/deposit` | Renter | Initialize/retrieve upfront deposit payment |
| `POST` | `/bookings/{id}/payments/balance-account` | Renter/ops | Provision/retrieve per-booking balance account/reference |
| `GET` | `/bookings/{id}/payments` | Party/ops | Safe transaction and reconciliation summary |
| `POST` | `/admin/bookings/{id}/refunds` | Authorized ops | Create full/partial refund instruction with reason |
| `POST` | `/admin/bookings/{id}/payouts` | Authorized ops/system gate | Initiate eligible vendor net payout |
| `POST` | `/webhooks/paystack` | Paystack | Signed, idempotent event ingestion |

Payment initialization and admin money movement require `Idempotency-Key`. The server calculates all amounts. Vendor payout response itemizes gross rental proceeds, vendor-side commission, approved adjustments/tax, and net amount; the commission does not get silently added to renter price.

### Deposit initialization response

```json
{
  "payment": {
    "id": "uuid",
    "type": "deposit",
    "status": "pending",
    "amount": "200000.00",
    "currency": "NGN",
    "provider": "paystack"
  },
  "authorization_url": "https://provider-hosted.example/...",
  "expires_at": "2026-09-26T12:30:00Z"
}
```

### Balance-account response

```json
{
  "payment_id": "uuid",
  "booking_id": "uuid",
  "amount_due": "225000.00",
  "currency": "NGN",
  "account": {
    "bank_name": "provider-supplied",
    "account_name": "provider-supplied",
    "account_number": "masked-or-authorized-value",
    "reference": "booking-specific-reference",
    "expires_at": "2026-10-10T14:00:00Z"
  }
}
```

Pickup cannot complete from a transfer screenshot or client callback; provider-verified settled status is required.

## 9. Handover and evidence APIs — planned

| Method | Path | Actor | Purpose |
|---|---|---|---|
| `POST` | `/bookings/{id}/handovers` | Booking party/ops | Create the unique pickup or return draft |
| `GET` | `/bookings/{id}/handovers/{direction}` | Party/ops | Read authorized handover |
| `PATCH` | `/bookings/{id}/handovers/{direction}` | Party while draft | Update checklist/notes before completion |
| `POST` | `/bookings/{id}/handovers/{direction}/evidence/uploads` | Party | Obtain restricted direct-upload target |
| `POST` | `/bookings/{id}/handovers/{direction}/evidence` | Uploader | Finalize metadata/hash after upload and scan |
| `POST` | `/bookings/{id}/handovers/{direction}/sign` | Correct party | Sign current handover version |

Create request:

```json
{
  "direction": "return",
  "condition_checklist": {
    "schema_version": 1,
    "items": [{"key": "body", "condition": "good", "notes": "No new damage"}]
  },
  "notes": null
}
```

Signing requires `If-Match`, explicit affirmation, and authenticated signer context:

```json
{"affirmed": true}
```

The server derives vendor/renter side from the authenticated user's relationship to the booking. It records signer ID and server timestamp. Completion occurs only when both signatures apply to the same current version and required scanned evidence exists. Any material change after one signature invalidates that signature and requires re-signing. A completed record is immutable; corrections append a dispute/audit record.

Pickup completion additionally requires verified balance status. Return completion can make payout eligible only if no dispute or reconciliation hold exists.

## 10. Dispute APIs — planned

| Method | Path | Actor | Purpose |
|---|---|---|---|
| `POST` | `/bookings/{id}/disputes` | Booking party/ops | Open claim with controlled reason and evidence references |
| `GET` | `/bookings/{id}/disputes` | Party/ops | Read role-appropriate status |
| `POST` | `/admin/disputes/{id}/resolve` | Authorized ops | Record reasoned disposition and financial instructions |

Resolution requires privileged re-authentication/segregated role as operations mature, an idempotency key, reason, evidence summary, and explicit deposit/payout instructions. It does not directly claim that the platform owns or holds funds.

## 11. Admin/concierge APIs — planned

Phase 0 uses these gates before automation:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/queues/listings` | Listings awaiting review |
| `GET` | `/admin/queues/bookings` | Approval/deposit/pickup/return exceptions |
| `GET` | `/admin/queues/reconciliation` | Provider/domain mismatches |
| `GET` | `/admin/queues/payouts` | Eligible, held, failed payouts |
| `POST` | `/admin/bookings/{id}/hold` | Place reasoned payout/deposit hold |
| `POST` | `/admin/bookings/{id}/release-hold` | Remove hold with reason |
| `POST` | `/admin/reconciliation/{id}/resolve` | Document reconciled outcome |
| `GET` | `/admin/audit-events` | Search authorized audit history |

Every admin command writes an audit event; manual money movement requires two-person approval in Phase 0. The precise RBAC split is planned and must be implemented before live funds.

## 12. Webhook contract

### 12.1 Common processing rules

1. Receive the raw body over HTTPS with a strict size limit.
2. Verify the provider's signature using its documented algorithm and secret before parsing trusted fields.
3. Derive a stable deduplication key from provider event ID; if unavailable, use a documented provider/object/event key.
4. Insert/read `webhook_events` under a uniqueness constraint.
5. Apply allowed state transitions transactionally; never trust event ordering.
6. Return `2xx` for already successfully processed duplicates and recognized irrelevant events.
7. Return the provider-required retryable failure status only when processing was not durably accepted; invalid signatures return `401`/`400` and are security-logged.
8. Reconcile uncertain events through provider verification APIs/background jobs.
9. Redact stored payloads and logs; use event/reference IDs for diagnostics.

A duplicate must produce **no duplicate payment, refund, payout, booking transition, notification, or audit decision effect**. Event handlers compare amount, currency, reference, booking, and expected state before applying effects.

### 12.2 `POST /api/v1/webhooks/paystack`

No user auth; provider signature required. Event mapping is adapter-specific. Examples of normalized effects:

- successful deposit collection can satisfy one confirmation gate;
- successful booking-balance collection can satisfy one pickup gate;
- successful refund updates the transaction/ledger but only according to approved refund instruction;
- successful transfer marks payout paid only when reference, amount, recipient, and eligibility match;
- reversals/failures create reconciliation exceptions and must not silently roll lifecycle states backward.

### 12.3 `POST /api/v1/webhooks/kyc/{provider}`

Only configured providers (`smile_id` or `dojah` after selection) are accepted. Signature/IP controls follow provider capabilities. A verified result updates an attempt and profile summary only when required checks and references match. Duplicate or stale results are recorded safely without lowering/raising tier contrary to policy. Raw biometric data is not retained by default.

## 13. Lifecycle response semantics

| Resource | Current enum/state | API requirement |
|---|---|---|
| Booking | `pending/confirmed/active/completed/cancelled/disputed` | Commands enforce the transition table; arbitrary status PATCH is forbidden |
| KYC | `pending/submitted/verified/failed` | Provider-neutral summary; attempts planned |
| Payment | `pending/authorized/captured/failed/refunded` | Provider webhook/reconciliation authoritative |
| Deposit policy | `held/released/claimed` persisted on deposit payment rows | Transition commands require provider reconciliation and approved disposition |
| Payout policy | `pending/eligible/paid` persisted on payout rows | Eligibility requires completed co-signed return and no dispute/reconciliation hold |
| Handover | `draft/vendor_signed/renter_signed/completed` | Sign commands, not arbitrary status writes |

## 14. Audit, retention, and privacy contract

- APIs collect only fields necessary for the documented purpose and expose only role-appropriate data.
- KYC provider references/results, handover evidence, location, and financial details are restricted data.
- Evidence download uses short-lived signed URLs; API audit logs record authorized access.
- Mutation responses carry request/correlation IDs. State transitions, admin reads of sensitive evidence, overrides, money movement, webhook effects, and retention actions produce audit events.
- Data subject requests use a separately authenticated process; APIs must respect financial/fraud/dispute/legal retention and legal holds.
- Exact retention periods, lawful bases, notices, and cross-border safeguards are **unresolved pending NDPA counsel/DPO review**.

## 15. Rate limiting and abuse controls

Stricter limits apply to OTP/challenges, KYC initiation, evidence uploads, booking creation, payment initialization, and webhook endpoints. Limits are keyed using safe combinations of account, device/session, phone hash, IP/network, and provider as appropriate. Public search receives general throttling. Responses use `429` and `Retry-After` without exposing fraud rules.

## 16. Product and legal wording constraints

No endpoint offers third-party cover or `coverage` in v1. The optional damage contribution (Milestone 2, renamed from `protection_waiver`) is the only damage product, and its terms remain subject to legal approval. Public and API copy says **deposit hold** and **damage contribution**. The final licensed funds flow, provider product names, refund/claim rights, commission/tax treatment, and contractual language remain unresolved until counsel/provider approval.
