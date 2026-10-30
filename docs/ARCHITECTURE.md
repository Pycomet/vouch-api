# Vouch v1 Architecture

## 1. Architecture position

Vouch v1 is a **modular monolith**, not a microservice system. The web application and future channel adapters call a versioned FastAPI API. PostgreSQL is the transactional system of record. Paystack is the first payment adapter. Smile ID versus Dojah remains undecided behind a provider-neutral KYC port.

The architecture supports creative gear only, launches in Lagos, and enables Abuja only after explicit product/operations gates. Phase 0 is concierge-led: automation is disabled until manual controls, legal/provider approvals, metrics, and recovery practices pass.

## 2. Current versus target

### 2.1 Current repository

Implemented now:

- FastAPI application factory and `/api/v1` router;
- liveness and database-aware readiness endpoints;
- async SQLAlchemy/PostgreSQL engine and sessions;
- environment-prefixed validated settings;
- request correlation IDs and structured completion logging (honours caller-supplied `X-Request-ID`);
- explicit CORS configuration;
- seven domain tables and initial Alembic migration (0001);
- model/application/migration tests;
- asset listing-publication state (`draft / review / active / paused / rejected`), minimum trust
  tier, review provenance fields, and migration 0002;
- shared API error envelope (`400 invalid_request`, `404 resource_not_found`) with trace IDs;
- opaque cursor-based pagination helpers; and
- public Lagos asset search (`GET /api/v1/assets`) and detail (`GET /api/v1/assets/{id}`)
  endpoints with strict visibility predicates — only active, non-deleted Lagos assets owned by
  active vendors are returned; serial numbers and internal fields are never exposed.
  **Note**: inventory ingestion and review (vendor draft submission, admin approve/reject/pause)
  are not yet implemented; public inventory requires manual database operations in Phase 0.

Not implemented now:

- customer authentication or authorization;
- web frontend;
- marketplace services/routes beyond health;
- Paystack or KYC clients/webhooks;
- evidence object storage;
- audit event store, idempotency store, financial ledger, outbox, queues/workers;
- notifications, reconciliation, disputes, refunds, or payouts.

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

The web app is planned, not present. Future WhatsApp/Telegram clients are adapters to the same use cases and are out of v1 transaction scope.

## 3. Module boundaries

All modules deploy together but own their rules and expose application interfaces rather than reaching into one another's tables arbitrarily.

| Module | Responsibilities | Current foundation |
|---|---|---|
| API/platform | Routing, schemas, auth context, errors, request IDs, CORS, health | App/router/health exist |
| Identity/access | Accounts, sessions, role/ownership authorization | `users` model only |
| Trust/KYC | Provider-neutral attempts, checks, tier policy, manual review | `kyc_profiles` and tier policy exist |
| Inventory | Creative-gear listings, review, availability, city gates | `assets`, `availability_windows` exist |
| Booking | Quotes, request/approval, state machine, overlap, cancellation | `bookings` and overlap constraint exist |
| Payments | Deposit, balance account, refunds, vendor-side commission, payout, reconciliation | `payments` persists transaction, deposit, payout, recipient, fee, and settlement summaries; immutable ledger/reconciliation remain planned |
| Handover/evidence | Pickup/return checklists, evidence, signatures | `handover_records` exists |
| Disputes/operations | Holds, claims, decisions, concierge queues | Planned |
| Audit/compliance | Append-only events, evidence access, retention/legal hold | Planned |
| Notifications | State-change messages from outbox | Planned |

Provider adapters translate external event/product vocabulary to domain commands. Domain services never depend directly on Paystack-, Smile ID-, or Dojah-specific payloads.

## 4. Core architectural rules

1. PostgreSQL is authoritative for Vouch lifecycle state; providers are authoritative for their settled transaction/verification results.
2. The client never sets role, trust tier, booking price, commission, payment success, or payout eligibility.
3. State changes occur through named commands and guarded transitions, not generic status updates.
4. Externally visible side effects are driven through a transactional outbox; retries are idempotent.
5. Provider callbacks are signature-verified, durably deduplicated, and reconciled.
6. Completed handover evidence is immutable; corrections are append-only events/disputes.
7. Sensitive objects are private and served through authorized, short-lived links.
8. Phase 0 manual approval remains a first-class gate rather than an undocumented workaround.
9. Components and UI describe the funds flow only as a **deposit hold**, or with counsel-approved wording.

## 5. Trust and booking flow

### 5.1 Renter verification

```text
Renter -> API: initiate target trust tier
API -> KYC adapter: create verification session
KYC provider -> Webhook: signed result
Webhook ingress -> webhook_events: verify + deduplicate
Trust service -> PostgreSQL: record attempt/outcome
Trust service -> audit/outbox: tier decision + notification
Ops -> Admin API: review exceptions during Phase 0
```

Tier 1 requires national ID and liveness; tier 2 additionally requires address. Smile ID/Dojah selection is unresolved. The core database should retain normalized outcomes/references, not raw ID or biometric material unless a documented lawful necessity is approved.

### 5.2 Booking/deposit confirmation

```text
Renter -> Booking API: request asset + dates (Idempotency-Key)
Booking service -> PostgreSQL: validate tier/scope/availability; snapshot price; pending
Vendor/Ops -> Booking API: approve (concierge gate)
Renter -> Payment API: initialize deposit
Payment adapter -> Paystack: provider-supported collection flow
Paystack -> Webhook ingress: signed success event
Payment service -> PostgreSQL: dedupe, verify amount/reference, record collection
Booking service -> PostgreSQL: pending -> confirmed in guarded transaction
Outbox -> Worker: notifications/reconciliation follow-up
```

The deposit is paid upfront to the platform through a licensed provider, subject to counsel/provider approval of the exact structure. The design does not assert that Paystack provides a custodial holding product or that Vouch may hold regulated funds.

### 5.3 Pickup/balance activation

```text
Payment service -> Provider: assign per-booking account/reference
Renter -> Provider account: pay rental balance at handover
Both parties -> Handover API: checklist, evidence, signatures
Provider -> Webhook ingress: balance settled
Booking service: require settled balance + completed pickup
Booking service -> PostgreSQL: confirmed -> active
```

Screenshots and browser callbacks are insufficient. If payment, evidence, signatures, identity, or condition mismatch, Phase 0 operations stop pickup and resolve the exception.

### 5.4 Return, deposit disposition, and payout

```text
Both parties -> Handover API: return checklist/evidence/signatures
Handover service -> PostgreSQL: completed return (both signers required)
Dispute service: confirm no open dispute/hold
Payment service: derive deposit release/refund instruction and payout eligibility
Ops approval gate: review in Phase 0
Payment adapter -> Provider: refund/release and vendor net payout instructions
Provider -> Webhook: confirmed outcomes
Reconciliation -> Ledger/payment state: paid/refunded or exception
```

Vendor net payout is rental proceeds less the vendor-side commission and approved adjustments/taxes. Payout occurs only after the co-signed return and no blocking dispute. Damage/loss claims keep both deposit disposition and affected payout controlled pending an audited decision.

## 6. Lifecycle ownership

| Aggregate | States | Transition owner |
|---|---|---|
| Booking | `pending`, `confirmed`, `active`, `completed`, `cancelled`, `disputed` | Booking service; guarded by payment/handover/dispute facts |
| KYC | `pending`, `submitted`, `verified`, `failed` | Trust service from verified provider result/authorized review |
| Payment | `pending`, `authorized`, `captured`, `failed`, `refunded` | Payment service from provider verification/reconciliation |
| Deposit policy | `held → released|claimed` | Persisted on deposit payment rows; provider reconciliation, dispute controls, and audited commands govern transitions |
| Payout policy | `pending → eligible → paid` | Persisted on payout rows; completed return is a required domain-policy gate and disputes/holds must block eligibility |
| Handover | `draft`, `vendor_signed`, `renter_signed`, `completed` | Handover service; DB enforces completion minimums |

The current GiST exclusion constraint blocks overlapping `confirmed`/`active` bookings. Services must also use a transaction/lock or version check when confirming to turn database conflicts into deterministic `409` responses.

## 7. Payments architecture and legal boundary

### 7.1 Required adapter capabilities

The `PaymentProvider` port should support:

- initialize deposit collection;
- provision or identify a per-booking account/reference for the rental balance;
- verify a transaction independently of client redirect;
- initiate full/partial refunds where legally and technically supported;
- initiate vendor payout/transfer with deterministic references;
- validate webhooks;
- fetch transaction/transfer state for reconciliation; and
- return stable error categories without leaking sensitive provider payloads.

Paystack is first, but each capability must be verified against the contracted Nigerian product. Unsupported capabilities require a revised, counsel-approved flow—not a simulated custodial layer.

### 7.2 Accounting and reconciliation

The existing `payments` table is a transaction summary. Before automation, add an immutable booking subledger for deposit liability/disposition, rental collection, vendor payable, commission, refund, claim, fee/tax, payout, and reversal. Never edit posted entries; reverse and repost.

A scheduled reconciliation worker compares expected internal transactions to provider transactions/transfers by reference, amount, currency, booking, and destination. Mismatches create an operations queue and block affected payout. Phase 0 performs daily manual reconciliation and two-person review of manual fund movement.

### 7.3 Commission

Commission is charged to the vendor and snapshotted at booking acceptance. Calculation and rounding are server-side. Commission rate, VAT/withholding/tax handling, invoices, and accounting classification are unresolved with commercial, accounting, and legal advisers.

## 8. Webhooks, idempotency, and background work

### 8.1 Webhook ingress

Payment and KYC ingress follows one pattern:

1. Capture bounded raw bytes.
2. Verify provider signature before trusting the event.
3. Normalize provider event identity.
4. Insert a `webhook_events` row under a unique provider/event constraint.
5. In one transaction, apply valid domain effects and write audit/outbox records.
6. Mark processed or retry/dead-letter with a safe error code.
7. Acknowledge duplicates without reapplying effects.

Events may arrive duplicated or out of order. Handlers inspect current provider/domain state and require expected amount/reference/currency. Unknown or contradictory events become reconciliation exceptions.

### 8.2 API command idempotency

Booking creation, payment initialization, refunds, payouts, and retried admin commands require idempotency keys persisted with request hashes and outcomes. The same key plus same payload returns the original outcome; same key plus different payload fails. Provider requests use deterministic references so application retries do not create duplicate charges/transfers.

### 8.3 Worker model

Start with one separately deployed worker process using the same codebase and PostgreSQL transactional outbox/job tables. It handles:

- provider command retries and verification;
- webhook retry/dead-letter processing;
- payment and payout reconciliation;
- booking/payment expiry;
- notifications;
- evidence scan/finalization and retention expiry;
- deposit/refund/payout eligibility evaluation; and
- audit-safe cleanup/retention jobs.

Jobs are at-least-once and idempotent, use bounded exponential backoff, and dead-letter after configured attempts. Money movement never retries without a deterministic provider reference and a verification step.

## 9. Evidence architecture

Evidence uploads go directly from authenticated clients to private object storage using short-lived, content-type/size-limited upload grants. The API records expected object metadata; a worker verifies upload, hash, and malware scan before evidence can satisfy handover completion.

Controls:

- private buckets/containers; no permanent public URLs;
- encryption at rest and in transit;
- unique object keys detached from user filenames;
- SHA-256 integrity metadata;
- least-privilege service identities;
- signed, short-lived authorized reads;
- access audit for sensitive evidence;
- object versioning/immutability after completion;
- lifecycle expiry and legal-hold support; and
- backup/recovery consistency with PostgreSQL.

Location is optional and purpose-limited. The UI must explain collection; exact lawful basis and retention require NDPA counsel/DPO approval.

## 10. Security architecture

### 10.1 Identity and access

The authentication provider/mechanism is unresolved. Regardless of implementation:

- sessions/tokens are short-lived, revocable, securely stored, and protected against replay/CSRF as applicable;
- OTP/challenge endpoints are rate-limited and enumeration-resistant;
- authorization is server-side by role, ownership, booking relationship, and operation;
- operations/admin users require MFA and stronger session controls;
- money movement, KYC review, evidence access, and overrides use least privilege and eventually segregation of duties;
- disabled/soft-deleted users cannot transact.

### 10.2 Application and infrastructure

- TLS everywhere; managed secrets, never source-controlled credentials.
- Restrictive CORS allowlist and production docs disabled (already supported by current app settings).
- Parameterized ORM access, request/body limits, schema validation, upload scanning, and rate limits.
- Encrypt managed PostgreSQL, object storage, backups, and queues.
- Do not log ID data, biometric material, payment credentials, signatures, raw webhook bodies, or evidence URLs.
- Dependency/container scanning, patching, protected CI, migration review, and production change controls.
- Separate development, staging, and production accounts/data; no production PII in lower environments.

## 11. NDPA and privacy architecture

Vouch must operationalize NDPA principles rather than treating privacy as a notice:

| Principle | Architectural control |
|---|---|
| Lawfulness/transparency | Versioned notices/terms, purpose and lawful-basis register, consent only where appropriate |
| Purpose limitation | Module/access boundaries for KYC, payments, evidence, fraud, and marketing |
| Minimization | Provider references/outcomes over raw IDs/biometrics; redacted webhooks/logs |
| Accuracy | User correction and provider re-verification with preserved audit history |
| Storage limitation | Per-class expiry, legal hold, deletion queues, backup expiry |
| Integrity/confidentiality | Encryption, MFA, least privilege, signed URLs, access audit, incident controls |
| Accountability | Audit events, processor contracts, DPIA/risk assessment, processing records, restore/incident drills |
| Data-subject rights | Authenticated request workflow, export/correction/restriction/deletion decisions with lawful exceptions |

Provider hosting/data locations, international transfer safeguards, lawful bases, DPO/DPIA/registration duties, breach notifications, and retention periods are **unresolved and must be approved by Nigerian privacy counsel/DPO before production**.

## 12. Audit and observability

### 12.1 Audit trail

Append-only audit events cover:

- account/role status and KYC outcomes;
- listing reviews and restricted-field changes;
- every booking transition and denied transition;
- payment/refund/payout instructions and verified outcomes;
- handover signatures, evidence finalization/access, and completion;
- disputes, holds, decisions, and admin overrides;
- webhook receipt/deduplication/processing;
- retention deletion and legal-hold actions; and
- configuration/feature-gate changes affecting transaction safety.

Events include actor/service, resource, action, safe before/after state, reason, request/correlation/idempotency IDs, and server time. Audit stores redact sensitive payloads and deny update/delete to normal application roles.

### 12.2 Operational telemetry

Metrics/alerts include API latency/error rate, DB pool/readiness, invalid webhook signatures, webhook lag/retries/dead letters, duplicate rate, payment mismatch, booking transition conflicts, failed evidence scans, payout/refund failures, reconciliation age, queue depth, backup/PITR health, and privileged access anomalies. Logs use current request IDs and propagate correlation into workers/provider references.

## 13. Availability, backup, and disaster recovery

The production database must be managed PostgreSQL with encryption, automated backups, continuous WAL archiving/PITR, high-availability configuration appropriate to approved SLOs, and backups isolated from production credentials/failure domains. Object evidence requires versioning/backup and lifecycle policies.

Before live bookings/funds:

- approve RPO/RTO (**currently unresolved**);
- validate automated backup and PITR alerts;
- perform a clean-environment restore to a chosen timestamp;
- verify migrations, constraints, ledger/reconciliation totals, audit continuity, and evidence-object hashes/links;
- document failover, provider outage, webhook replay, and read-only/manual-operation runbooks; and
- repeat restore drills at least quarterly and after major storage changes.

During provider outages, Vouch must not guess payment/KYC success. It holds the lifecycle state, communicates the delay, queues verification, and routes urgent handovers to concierge policy.

## 14. Deployment topology

A minimal production topology:

- CDN/WAF and static hosting for the web app;
- load-balanced FastAPI containers/processes with no local durable state;
- worker containers from the same application artifact;
- managed PostgreSQL;
- private object storage;
- managed secret store;
- centralized structured logs, metrics, tracing, and alerting; and
- provider egress with timeouts and circuit-breaking/bounded retries.

Deployments run backward-compatible migrations before application rollout. Risky automation and Abuja availability are feature-gated. A kill switch disables deposit initialization, balance provisioning, payout, or provider-specific automation independently while read/admin/reconciliation paths remain available.

## 15. Phase 0 concierge architecture

Phase 0 uses the same domain records and audit trail intended for v1 but keeps critical transitions behind operations approval:

| Gate | Manual control before automation |
|---|---|
| Vendor/listing | Review identity, gear scope, serial/evidence, city, price/deposit |
| Booking | Review renter tier, risk/value, availability, vendor approval |
| Deposit | Cross-check signed event/provider dashboard and internal reference |
| Pickup | Confirm balance settlement, identity, checklist, evidence, both signatures |
| Return | Inspect return evidence and both signatures; identify dispute |
| Deposit disposition | Apply approved policy and dual review |
| Payout | Reconcile gross, vendor-side commission, adjustments, net, destination, return completion |
| Exceptions | Create hold/dispute; never resolve outside the audit trail |

Automation is enabled one transition at a time after shadow comparison against concierge decisions, acceptable error rates, tested rollback/kill switch, and legal/provider approval.

## 16. Acceptance and expansion gates

### Lagos v1 automation gate

- Phase 0 completes at least 20 rentals across 5 active vendors, with at least 90% completing without a substantiated damage/loss/payment dispute.
- 100% of confirmed bookings have verified tier and provider-confirmed upfront deposit.
- 100% of active bookings have settled balance and completed co-signed pickup.
- 100% of payouts have completed co-signed return, no unresolved dispute, and reconciled commission/net.
- Duplicate webhooks produce zero duplicate domain/financial effects in tests and production shadowing.
- Four consecutive weeks of daily reconciliation complete without aged unexplained mismatch.
- Legal/provider, privacy/security, support runbooks, backup/PITR restore, and kill-switch gates pass.

### Abuja gate

- At least three consecutive months of stable Lagos controls/metrics.
- Approved Abuja vendor/listing cohort and validated demand.
- Local operations, support, handover, dispute, and recovery coverage.
- Provider/KYC contractual and operational coverage.
- Positive approved unit economics after commission, payment, support, fraud/refund, and dispute costs.
- Explicit leadership risk approval activates the city feature flag; the existing enum alone is insufficient.

## 17. Decisions deliberately unresolved

| Decision | Owner | Architecture impact |
|---|---|---|
| Legal nature and licensed provider for deposit collection/hold/release | Nigerian counsel + provider/compliance | Funds flow, wording, ledger, contracts, operations |
| Paystack capabilities and contracted products | Payments/provider team | Virtual accounts/references, splits/transfers, refunds, webhooks |
| Smile ID or Dojah | Trust/privacy/security/commercial | Adapter, data flow/location, liveness, callback controls |
| Auth/session provider | Security/product | Web/session architecture and recovery |
| Commission/tax treatment | Commercial/accounting/counsel | Ledger, payout, invoicing |
| Dispute/deposit claim rules | Risk/counsel | Holds, evidence, decision workflow |
| NDPA lawful bases, retention, transfers, DPO/DPIA duties | Counsel/DPO | Storage, deletion, contracts, evidence/KYC flow |
| RPO/RTO and service SLOs | Operations/product | HA, backup cadence, capacity/cost |

There is no third-party cover product in v1. The `protection_waiver` code value becomes `damage_contribution` in Milestone 2; its amounts and terms remain subject to legal approval.
