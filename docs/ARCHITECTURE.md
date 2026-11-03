# Vouch v1 Architecture

## 1. Architecture position

Vouch v1 is a **modular monolith**, not a set of microservices. The web application and future channel adapters call a versioned FastAPI API. PostgreSQL is the transactional system of record. Payments, KYC and SMS sit behind provider-neutral interfaces. Paystack is the intended first payment adapter. Smile ID versus Dojah is still undecided for KYC.

The scope is creative gear only. The launch sequence is **Abuja Phase 0 → Abuja v1 → Lagos**. Which cities are open is configuration (`VOUCH_LAUNCHED_CITIES`, default `abuja`), never code. Phase 0 is concierge-led: automation stays off until the manual controls, the legal and provider approvals, the metrics and the recovery practices pass.

## 2. Current versus target

### 2.1 Current repository (Milestone 2)

Implemented:

- the app factory and the `/api/v1` router; liveness and database-aware readiness; validated `VOUCH_` settings; request ids and structured logs; explicit CORS; a request body cap;
- the schema of migrations 0001–0005 ([`SCHEMA.md`](SCHEMA.md), [`erd.svg`](erd.svg)):
  - the `cities` and `categories` reference tables, the catalogue, and assets as single units;
  - the booking state machine with live-overlap exclusion;
  - the append-only `ledger_entries` (balanced per group at commit) and `audit_events`;
  - `webhook_events`, `vendor_references` and `otp_challenges`;
- phone OTP → HS256 JWT, with role checks and the user reloaded on every request;
- renter KYC to tier 1, and trust tiers 0–3;
- public inventory (`/assets`, `/catalogue/search`);
- every booking endpoint, with the money line, plus the renter and vendor home screens;
- payment webhooks (idempotent, signature-checked);
- the timer job `python -m app.jobs.timers`; and
- `OtpSender`, `KycProvider` and `PaymentProvider` interfaces, with **fake** implementations only.

The endpoints are in [`API_CONTRACT.md`](API_CONTRACT.md). The known gaps and the prerequisites for real adapters are in [`OPERATIONS-NOTES.md`](OPERATIONS-NOTES.md).

Not implemented yet:

- the web frontend;
- real Paystack, KYC and SMS adapters (staging and production answer 503 for OTP, KYC and booking acceptance until they exist);
- refund execution and payout initiation;
- vendor listing management and admin review endpoints;
- private evidence storage;
- command idempotency keys;
- a transactional outbox and real notification channels;
- reconciliation.

### 2.2 Target v1 context

```text
                 Browser web app
                        |
                   HTTPS /api/v1
                        |
             FastAPI modular monolith
  +---------------------+----------------------+
  | identity/KYC | inventory | booking         |
  | payments     | handover  | disputes/admin  |
  | audit        | notification/reconciliation |
  +---------------------+----------------------+
          |                |               |
     PostgreSQL      Private object      Transactional
  + PITR/backups        storage              outbox
          |                |               |
          +---------- background worker ---+
                           |
                +----------+----------+
                |                     |
            Paystack          Smile ID or Dojah
          (first adapter)       (unresolved)
```

The web app is planned. Future WhatsApp and Telegram clients will be adapters to the same use cases. Today the background worker is only the timer job.

## 3. Module boundaries

All modules deploy together. Each owns its rules and is reached through its service functions, not by other modules writing its tables.

| Module | Responsibilities | Code (M2) |
|---|---|---|
| API/platform | Routing, schemas, auth context, errors, request ids, CORS, body limit, health | `app/api`, `app/core`, `app/schemas` |
| Identity/access | Phone OTP, JWTs, role and ownership checks | `services/auth.py`, `core/security.py`, `api/deps.py` |
| Trust/KYC | Tier policy (0–3), KYC through the provider interface | `services/trust.py`, `services/kyc.py`, `providers/kyc.py` |
| Inventory | Public search and detail, the launched-city gate, live-overlap and blocked-window filters | `services/inventory.py` |
| Booking | Requests, the `_TRANSITIONS` state machine, pricing snapshot, timers | `services/booking.py`, `pricing.py`, `timers.py`, `jobs/timers.py` |
| Payments/ledger | Upfront payment accounts, refund and payout rows, posting recipes, the money line, webhooks | `services/payments.py`, `money.py`, `ledger.py`, `money_line.py`, `webhooks.py`, `providers/payment.py` |
| Handover/evidence | Pickup and return checklists, evidence URLs, signatures | `services/handover.py` |
| Disputes/operations | Claims on `bookings` and admin resolution; ops alerts through the notifier | `services/booking.py` (claim, resolve, mark lost) |
| Audit | Append-only events for every transition and money movement | `services/audit.py` |
| Notifications | `Notifier` interface; logging and recording implementations only | `services/notify.py` |

Provider adapters translate external payloads into domain events. Domain services depend only on the interfaces, never on a provider's payload format.

## 4. Core architectural rules

1. PostgreSQL is authoritative for Vouch lifecycle state. The database enforces the invariants (exclusion, CHECKs, append-only and balance triggers), not only the services.
2. The client never sets a role, trust tier, price, commission, payment status or payout eligibility.
3. State changes happen only through named, guarded transitions (`app/services/booking.py`). Every transition writes at least one audit row, and every money movement writes one balanced ledger group.
4. Money never moves without a ledger entry. Corrections are reversing entries.
5. Provider callbacks are signature-verified and durably deduplicated (`webhook_events`).
6. A signed handover side is final. Corrections are claims and audit rows.
7. Externally visible side effects should come from a transactional outbox. M2 sends notifications before commit (see the operations notes).
8. Phase 0 manual approval remains a first-class gate.
9. Components and UI describe the funds flow only as a **deposit hold**, or with wording counsel has approved.

## 5. Trust and booking flow (M2)

### 5.1 Renter verification

```text
Renter -> POST /auth/otp/request, /auth/otp/verify: phone proven, JWT issued (tier 0)
Renter -> POST /me/kyc: ID + selfie + address document
KYC service -> KycProvider: verify_identity + verify_address
KYC service -> kyc_profiles: verified at tier 1 (both pass) or failed; audit kyc.*
```

Tier 1 needs national ID, liveness and address. Tier 2 is computed: three clean completed bookings, or a vendor reference. Tier 3 is per booking: the vendor's approval. Only normalized outcomes are stored, never the ID number or the documents.

### 5.2 Request, acceptance and payment

```text
Renter -> POST /bookings: tier, blocked-window and overlap guards; price snapshot; 4h hold
Vendor -> POST /bookings/{id}/accept: PaymentProvider opens a virtual account for D+C+R+F
Renter -> provider account: one transfer for everything (deposit, contribution, rental, delivery)
Provider -> POST /webhooks/{provider}: signed; deduplicated; amount and reference checked
Booking service: accepted -> deposit_paid -> confirmed; one balanced ledger group
```

The renter pays everything upfront (A14). Nothing is collected at handover. Money that arrives late, for the wrong amount, or twice is captured and returned, never applied. The deposit is held by the platform through a licensed provider, subject to counsel and provider approval of the exact structure. The design does not claim that the provider offers a custodial holding product.

### 5.3 Pickup, return and completion

```text
Both parties -> /handover: checklist, evidence, signatures -> in_progress
Both parties -> /return: checklist, evidence, signatures -> returned (24h claim window)
Timer -> no claim: completed; deposit released (pending refund row for ops)
Vendor -> /claim inside the window: disputed; Admin -> /resolve: release | deduct | forfeit
Timer -> not returned by the end of the last day: at-risk flag, renter frozen
Admin -> mark lost from day 7: deposit forfeited to the vendor, asset paused
```

The vendor's payout is the rental plus delivery, less the commission, plus any deduction or forfeit. Ops pays it out in M2, and the provider's payout webhook posts it.

## 6. Lifecycle ownership

| Aggregate | States | Transition owner |
|---|---|---|
| Booking | `requested`, `accepted`, `deposit_paid`, `confirmed`, `in_progress`, `returned`, `completed`, `declined`, `expired`, `cancelled_by_renter`, `cancelled_by_vendor`, `disputed`, `lost` | `app/services/booking.py` (`_TRANSITIONS`), driven by the API, the timers and webhooks |
| KYC | `pending`, `submitted`, `verified`, `failed` | `app/services/kyc.py`, from the provider's result |
| Payment row | `booking`: `pending → captured \| failed`; `refund`: `pending`; `payout`: `captured` / `paid` | `app/services/payments.py` and `webhooks.py` |
| Deposit hold | Tracked in the ledger (`renter_deposit_liability`), not on payment rows | Booking transitions (release, deduct, forfeit, refund) |
| Handover | `draft`, `vendor_signed`, `renter_signed`, `completed` | `app/services/handover.py`; the database enforces the completion minimums |

The GiST exclusion constraint `ex_bookings_no_live_overlap` blocks overlapping live bookings, and the request service turns a conflict into 409 `dates_unavailable`. Each transition locks its booking row.

## 7. Payments architecture and legal boundary

### 7.1 Payment provider interface

`PaymentProvider` (`app/providers/payment.py`) has `create_virtual_account`, `initiate_payout`, `verify_webhook` and `parse_event`. A real adapter also needs:

- refunds (no M2 call exists);
- independent transaction verification;
- deterministic references;
- timeouts; and
- stable error categories.

Every capability must be checked against the contracted Nigerian product. A capability the provider does not support needs a revised flow that counsel has approved, not a simulated custodial layer.

### 7.2 Accounting and reconciliation

The `payments` table is the provider-facing summary. The append-only `ledger_entries` table is the booking subledger. Its six accounts and every posting recipe are listed in `SCHEMA.md` §8. Posted entries are never edited; a mistake is corrected with a reversing entry. Before automation, a scheduled reconciliation should compare provider transactions and transfers with the ledger by reference, amount, currency and booking. A mismatch should block the affected payout. Phase 0 does this reconciliation by hand, daily.

### 7.3 Commission

The commission is charged to the vendor, at the rate frozen into the booking's `price_snapshot` at request time. It is calculated and rounded on the server. The rate (a placeholder, `VOUCH_COMMISSION_RATE`) and the tax and accounting treatment are still to be settled with commercial, accounting and legal advisers.

## 8. Webhooks, idempotency and background work

### 8.1 Webhook ingress (implemented)

1. Read the raw body, bounded by the body cap.
2. Verify the signature before trusting anything. A failed delivery is recorded under the body's hash, and the body is not kept.
3. Insert `webhook_events` with `ON CONFLICT (provider, provider_event_id) DO NOTHING`. A duplicate does nothing.
4. Apply the event in a savepoint. A domain refusal is recorded as `processing_error` and answered 200. Any other error rolls back the whole transaction, so the provider retries.

The booking row is locked before anything is decided, so a payment and a withdrawal are applied one after the other.

### 8.2 API command idempotency (not in M2)

Booking creation, refunds, payouts and retried admin commands should take idempotency keys stored with request hashes and outcomes. Provider requests use deterministic references, so a retry never charges or pays twice.

### 8.3 Worker model

Today: one timer process (`python -m app.jobs.timers`). Every 60 seconds it expires holds and payment windows, completes returned bookings and flags late returns. Each booking is handled in its own transaction with `FOR UPDATE SKIP LOCKED`, and a second run does nothing. Later, the same process model will carry outbox delivery, provider retries, reconciliation, evidence processing and retention jobs. Those jobs will be at-least-once and idempotent, with bounded backoff and dead-lettering.

## 9. Evidence architecture

M2 stores handover evidence as a list of URLs on `handover_records`. The target is direct uploads from authenticated clients to private object storage, using short-lived upload grants. A worker would check the upload, its hash and a malware scan before the evidence can complete a handover. Target controls:

- private buckets, with no permanent public URLs;
- encryption at rest and in transit;
- unique object keys;
- SHA-256 integrity metadata;
- signed, short-lived authorized reads, with an access audit;
- immutability after completion;
- lifecycle expiry and legal hold.

## 10. Security architecture

### 10.1 Identity and access

- Phone OTP: 6-digit codes, stored only as an HMAC, with a 10-minute life, 5 attempts, single use, and at most 5 requests per phone per hour. The answer is the same whether or not the phone has an account.
- HS256 JWTs with a short-to-moderate life (`VOUCH_JWT_TTL_MINUTES`). Staging and production refuse placeholder or weak secrets.
- Every request reloads the user, so a deactivated, deleted or re-roled user is refused at once. Revoking a token before it expires is not supported yet.
- Authorization happens on the server, by role, ownership and booking relationship. Before live funds, operations and admin users need MFA and segregation of duties.

### 10.2 Application and infrastructure

- TLS everywhere; managed secrets; no credentials in source control.
- A restrictive CORS allowlist. API docs are off in production.
- Parameterized ORM access, a body-size cap, strict schemas, and rate limits (OTP today).
- Never log ID data, documents, payment credentials, signatures, raw unverified webhook bodies or evidence.
- Separate development, staging and production data, with no production PII in lower environments.

## 11. NDPA and privacy architecture

| Principle | Architectural control |
|---|---|
| Lawfulness/transparency | Versioned notices and terms, a purpose and lawful-basis register |
| Purpose limitation | Module and access boundaries for KYC, payments, evidence and fraud |
| Minimization | Provider references and outcomes instead of raw IDs and biometrics; redacted webhooks and logs |
| Accuracy | User correction and provider re-verification, with the audit history kept |
| Storage limitation | Expiry per data class, legal hold, deletion queues, backup expiry |
| Integrity/confidentiality | Encryption, MFA, least privilege, signed URLs, access audit |
| Accountability | Audit events, processor contracts, DPIA, processing records, drills |
| Data-subject rights | An authenticated request workflow, with lawful exceptions |

Provider data locations, transfer safeguards, lawful bases, DPO, DPIA and registration duties, breach notification and retention periods must be **approved by Nigerian privacy counsel or the DPO before production**.

## 12. Audit and observability

`audit_events` is append-only: the database rejects UPDATE, DELETE and TRUNCATE. It records every booking transition, deposit disposition, restriction, asset pause, handover signature, KYC outcome, and every refund or payout recorded from a webhook. Each row carries the actor, the entity, safe before and after data, a reason and the request id. Still to add: denied-transition logging, evidence-access audit, and the metrics and alerts for API errors, invalid webhook signatures, payment mismatches, transition conflicts, `processing_error` rows, pending refunds, reconciliation age and backup health.

## 13. Availability, backup and disaster recovery

Production needs managed PostgreSQL with encryption, automated backups, PITR and HA that matches the approved SLOs. Before live bookings or funds:

- approve the RPO and RTO;
- check the backup and PITR alerts;
- restore to a chosen time in a clean environment;
- verify the constraints, ledger totals and audit continuity after the restore;
- write the runbooks for provider outages, webhook replay and manual operation.

During a provider outage, Vouch must not guess whether a payment or KYC check succeeded.

## 14. Deployment topology

The minimal production topology:

- static hosting behind a CDN/WAF for the web app;
- load-balanced, stateless FastAPI processes;
- the timer process (exactly one loop is enough; a second one is safe because each booking is locked and skipped if busy);
- managed PostgreSQL, private object storage and a secret store;
- centralized logs and metrics;
- outbound provider calls with timeouts.

Migrations run before the application rolls out. Opening a city is a configuration change (`VOUCH_LAUNCHED_CITIES`).

## 15. Phase 0 concierge architecture

Phase 0 uses the same domain records and audit trail as v1, but keeps the critical steps behind operations approval:

| Gate | Manual control before automation |
|---|---|
| Vendor/listing | Review identity, gear scope, serial and evidence, city, price and deposit |
| Booking | Review renter tier, risk and value, availability, vendor approval |
| Payment | Cross-check the signed webhook against the provider dashboard and the internal reference |
| Pickup | Confirm identity, checklist, evidence and both signatures |
| Return | Inspect the return evidence and both signatures; spot disputes |
| Deposit disposition | Apply the approved policy, with dual review |
| Refunds and payouts | Execute pending refund rows and vendor payouts from the provider dashboard, then reconcile |
| Exceptions | Review `webhook_events.processing_error` rows; never resolve anything outside the audit trail |

Automation is switched on one step at a time, once it matches the concierge decisions in shadow runs, has a tested rollback, and has legal and provider approval.

## 16. Acceptance and expansion gates

### Abuja v1 automation gate

- Phase 0 completes at least 20 rentals across 5 active vendors, and at least 90% of them complete without a substantiated damage, loss or payment dispute.
- 100% of confirmed bookings have a verified tier and a provider-confirmed upfront payment.
- 100% of active bookings have a completed, co-signed pickup.
- 100% of payouts have a completed, co-signed return, no unresolved dispute, and a reconciled commission and net amount.
- Duplicate webhooks produce no duplicate domain or financial effects.
- Four consecutive weeks of daily reconciliation pass with no aged, unexplained mismatch.
- The legal and provider, privacy and security, support runbook, backup and PITR restore, and kill-switch gates all pass.

### Lagos gate

- At least three consecutive months of stable Abuja controls and metrics.
- An approved Lagos vendor and listing cohort, and validated demand.
- Local operations, support, handover, dispute and recovery coverage.
- Contractual and operational coverage from the providers and KYC.
- Positive, approved unit economics.
- Explicit leadership approval to add `lagos` to `VOUCH_LAUNCHED_CITIES`.

## 17. Decisions deliberately unresolved

| Decision | Owner | Architecture impact |
|---|---|---|
| Legal nature and licensed provider for collecting, holding and releasing the deposit | Nigerian counsel + provider/compliance | Funds flow, wording, ledger, contracts, operations |
| Paystack capabilities and contracted products | Payments/provider team | Virtual accounts, transfers, refunds, webhooks |
| Smile ID or Dojah | Trust/privacy/security/commercial | Adapter, data flow and location, liveness |
| SMS provider | Product/security | `OtpSender` adapter, global rate limits |
| Commission, cancellation fee and damage contribution rates; tax treatment | Commercial/accounting/counsel | Placeholders today (A8, A13) |
| Dispute and deposit claim rules | Risk/counsel | Claim, resolve, evidence workflow |
| NDPA lawful bases, retention, transfers, DPO/DPIA duties | Counsel/DPO | Storage, deletion, contracts |
| RPO/RTO and service SLOs | Operations/product | HA, backup cadence, capacity, cost |

No third-party product backs the damage contribution in v1. It is a Vouch-run charge, recorded in the `protection_pool` ledger account (the enum value was renamed from `protection_waiver` in M2), and its amounts and terms remain subject to legal approval.
