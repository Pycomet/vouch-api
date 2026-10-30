# Vouch Product Requirements Document

## 1. Document status

This document defines Phase 0 and v1 for Vouch. It combines the approved product strategy with the current repository. Statements are classified as:

- **Current**: represented in the checked-in application, ORM models, or migration.
- **Planned**: required for Phase 0/v1 but not yet implemented.
- **Unresolved**: requires legal, compliance, commercial, or provider confirmation before launch.

Vouch describes customer funds only as a **deposit hold** unless Nigerian counsel approves other wording, the selected licensed provider contract supports that terminology and flow, and operations implement the approved controls. Use **platform-controlled payment**, **deposit collection/hold**, or the provider's counsel-approved wording.

## 2. Product summary

Vouch is a trusted rental marketplace for high-value creative gear in Nigeria. It connects verified renters with vendors and adds identity verification, platform-controlled payments, condition evidence, and co-signed handovers to reduce fraud and disputes.

The launch sequence is:

1. **Phase 0 concierge in Lagos**: manually operated, tightly gated transactions that validate demand and controls before automation.
2. **v1 in Lagos**: automate only the workflows proven safe in Phase 0.
3. **Abuja expansion**: enabled only after Lagos meets the expansion gates in this document.

## 3. Problem and proposition

Creative professionals need cameras, lenses, lighting, audio equipment, drones, and gimbals without purchasing them. Vendors need qualified demand but face identity fraud, asset damage, non-return, payment, and evidence risks. Vouch's proposition is not merely discovery: it is a controlled transaction workflow in which renter identity, deposit and balance collection, pickup condition, return condition, and payout eligibility are recorded.

## 4. Users and roles

| Role | Need | v1 capability |
|---|---|---|
| Renter | Find and safely rent suitable gear | Verify identity, search, request booking, fund deposit and balance, co-sign pickup/return |
| Vendor | Earn from idle creative gear with evidence and payment controls | List gear, set availability/rates/deposit, approve requests, co-sign handovers, receive net payout |
| Operations/admin | Safely execute and reconcile marketplace transactions | Review KYC, listings, bookings, evidence, payments, exceptions, disputes, refunds, and payouts |

The current `users.role` model allows one role value (`renter`, `vendor`, or `admin`) per user. Multi-role accounts are not a v1 requirement unless later approved.

## 5. Scope

### 5.1 In scope

- Creative gear only: cameras, lenses, lighting, audio, drones, gimbals, and manually reviewed `other` creative gear.
- Lagos launch, followed by Abuja only after expansion gates pass.
- Web application as the first customer channel.
- Phone-based accounts and authenticated renter/vendor/admin access.
- Verified renter tiers based on national-ID, liveness, and enhanced address verification.
- Vendor onboarding and manually approved listings.
- Search/filter by city, category, date availability, and price.
- Booking requests, approval/rejection, cancellation, and non-overlap controls.
- Security deposit paid upfront to the platform through a licensed payment provider, subject to legal approval of the exact flow.
- Rental balance paid at handover into a per-booking provider account or equivalent provider-supported reference/account.
- Paystack-first payment integration.
- Vendor-side commission deducted from rental proceeds; the renter-facing rental fee must not silently add the commission.
- Co-signed pickup and return condition checklists with evidence.
- Payout only after a completed, co-signed return and reconciliation; disputed returns block automated payout.
- Concierge operations, auditability, notifications, refunds, disputes, and reconciliation.
- PostgreSQL backups, point-in-time recovery (PITR), and restore drills before live funds or bookings.

### 5.2 Out of scope for v1

- Cars, property, event infrastructure, household goods, or a general rental marketplace.
- Third-party cover products. The only damage product is the optional **damage contribution**, subject to legal approval.
- A platform-funded loss guarantee.
- Instant booking without vendor/operations gates.
- Cash payment as a normal supported path.
- Cross-city fulfilment or nationwide launch.
- Native mobile apps.
- WhatsApp or Telegram as full transaction clients; they may become later adapters.
- Dynamic pricing, auctions, subscriptions, loyalty, reviews, social feeds, or multi-currency.
- Automated damage adjudication or automated deposit claims.
- Microservices.

The current `PaymentType.protection_waiver` enum is a reserved implementation artifact, not an approved v1 product. It must not be exposed or sold in v1.

## 6. Authoritative commercial and funds-flow rules

1. **Currency:** NGN only in v1.
2. **Pricing snapshot:** booking rental fee and deposit are copied from the accepted offer/listing and do not change when the listing later changes.
3. **Deposit:** renter pays the security deposit upfront through the platform's selected licensed payment provider before booking confirmation.
4. **Balance:** renter pays the rental balance at handover to a per-booking account/reference supplied by the provider. Pickup is not completed until verified funds confirmation is received.
5. **Payout:** vendor payout becomes eligible only after both parties co-sign the return, required return evidence exists, and no dispute/hold blocks settlement.
6. **Commission:** Vouch's commission is vendor-side and deducted from rental proceeds before vendor payout. The commission rate and tax treatment are **unresolved commercial/legal decisions**.
7. **Deposit disposition:** an undisputed deposit is released/refunded according to the provider-supported flow. A claimed deposit requires a documented dispute decision; operators must not unilaterally move funds outside the approved process.
8. **Deposit hold only:** Paystack-first does not itself establish a custodial holding arrangement. The exact licensed custody/collection, dedicated virtual account, split, transfer, refund, and holding capabilities must be confirmed contractually and technically.
9. **Source of truth:** provider webhooks plus reconciliation determine settled payment state; a browser redirect or client assertion never does.

## 7. Trust infrastructure

### 7.1 Renter verification tiers

| Tier | Current representation | Required evidence | Booking permission |
|---|---|---|---|
| 0 — Unverified | `TrustTier.unverified` | Account only | Browse; cannot transact |
| 1 — Identity verified | `TrustTier.identity_verified` | National ID + liveness | Eligible for lower-risk bookings within an operations-defined limit |
| 2 — Enhanced | `TrustTier.enhanced` | National ID + liveness + address | Eligible for higher-risk/value bookings within an operations-defined limit |

The limits and asset-risk mapping are **unresolved risk-policy decisions**. The server must enforce the required tier; the client must not decide eligibility. KYC provider selection between **Smile ID and Dojah is unresolved**. Raw national-ID images, biometrics, or provider secrets should not be copied into the core database unless a documented necessity and lawful basis are approved.

### 7.2 Listing trust

Every live listing must be owned by an active vendor, fall within creative-gear scope, identify city, rate, deposit, category, and condition, and pass manual Phase 0 review. Serial numbers are restricted and must not be publicly returned in search results.

### 7.3 Handover trust

Each booking has at most one pickup record and one return record. A completed handover requires:

- structured condition checklist;
- vendor and renter signer identities;
- both signature timestamps;
- at least one evidence object;
- optional location and notes; and
- immutable evidence metadata after completion, with corrections represented as append-only audit events rather than silent replacement.

Payout is never made from the pickup record. It depends on the completed return record.

## 8. Core journeys

### 8.1 Renter onboarding

1. User creates/verifies a phone-based account and accepts current terms/privacy notice.
2. User initiates KYC and is redirected to or uses the selected provider flow.
3. Provider callback/webhook is verified and processed idempotently.
4. Vouch stores minimum result metadata and assigns the verified tier only after server-side verification.
5. Failed or ambiguous results enter operations review; no transactional access is granted by client claims.

### 8.2 Vendor and listing onboarding

1. Vendor account is reviewed by operations.
2. Vendor submits creative gear, serial number, location, pricing, deposit, availability, and condition information.
3. Operations verifies scope and evidence before listing activation during Phase 0.
4. Vendor maintains blocked/available dates. Confirmed and active bookings cannot overlap.

### 8.3 Booking and deposit

1. Verified renter searches Lagos inventory and selects dates.
2. Server checks listing availability and required trust tier, calculates immutable fee/deposit snapshots, and creates a `pending` booking.
3. Vendor and/or operations approves under the current concierge policy.
4. Vouch initializes the deposit transaction with Paystack; renter pays via provider-hosted flow.
5. A signed, idempotently processed webhook and provider verification establish successful collection.
6. Booking becomes `confirmed` only when all required approval and deposit conditions are met.

### 8.4 Pickup and balance

1. Provider creates or assigns a per-booking account/reference for the rental balance.
2. At pickup, both parties inspect gear and build the pickup condition checklist with evidence.
3. Renter pays the balance to that booking-specific account/reference.
4. Vouch verifies settled funds through provider webhook/reconciliation.
5. Both parties sign. Only then is pickup completed and booking made `active`.
6. Mismatch, failed payment, or refusal to sign stops handover and triggers concierge handling.

### 8.5 Return, dispute, and payout

1. Both parties complete the return checklist and attach evidence.
2. Both parties sign the return.
3. If condition is accepted and no exception exists, return becomes `completed`, booking becomes `completed`, deposit release/refund is initiated, and vendor payout becomes eligible.
4. The provider pays the vendor the rental proceeds less Vouch's vendor-side commission; successful provider confirmation marks payout paid.
5. If damage, loss, lateness, or disagreement is reported, booking becomes `disputed`; payout and deposit disposition remain blocked pending documented review.

## 9. Lifecycle and state tables

### 9.1 Booking lifecycle

The current enum is `pending`, `confirmed`, `active`, `completed`, `cancelled`, `disputed`.

| From | To | Required gate | Actor |
|---|---|---|---|
| — | `pending` | Valid dates, available in-scope asset, eligible verified renter, server-priced snapshot | Renter/system |
| `pending` | `confirmed` | Approval complete and deposit collection verified | Vendor/ops + system |
| `pending` | `cancelled` | Rejection, timeout, withdrawal, or failed deposit policy | Renter/vendor/ops |
| `confirmed` | `active` | Balance verified and pickup handover completed/co-signed | System after both signers |
| `confirmed` | `cancelled` | Approved cancellation policy; refund workflow recorded | Ops/system |
| `active` | `completed` | Return handover completed/co-signed and no dispute | System |
| `active` | `disputed` | Return exception/damage/loss/lateness claim | Renter/vendor/ops |
| `disputed` | `completed` | Reviewed resolution and settlement instructions recorded | Authorized ops |
| `disputed` | `cancelled` | Only if approved resolution requires cancellation | Authorized ops |

Terminal states are `completed` and `cancelled`, except controlled administrative correction through audited tooling. The database currently prevents date overlap only for `confirmed` and `active` bookings.

### 9.2 KYC lifecycle

| From | To | Meaning |
|---|---|---|
| — | `pending` | Profile created; provider flow not submitted |
| `pending` | `submitted` | Provider accepted verification request |
| `submitted` | `verified` | Required checks passed; `verified_at` and tier set |
| `submitted` | `failed` | Provider or review rejected checks |
| `failed` | `submitted` | New attempt/review; attempt history must remain auditable in planned data |

Current storage has one mutable profile and does not yet preserve attempts or webhook history.

### 9.3 Payment lifecycle

The current persisted payment status is `pending`, `authorized`, `captured`, `failed`, or `refunded`.

| From | To | Trigger |
|---|---|---|
| — | `pending` | Server initializes a deposit, balance, payout, or refund intent |
| `pending` | `authorized` | Provider confirms authorization where supported |
| `pending`/`authorized` | `captured` | Signed webhook plus verification/reconciliation confirms collection |
| `pending`/`authorized` | `failed` | Verified provider failure or expiry |
| `captured` | `refunded` | Provider confirms full refund; partial refunds need planned ledger support |

Current code persists deposit `held → released|claimed` and payout `pending → eligible → paid` states on payment rows with lifecycle timestamps. These summaries support Phase 0 controls but do not replace provider reconciliation, dispute holds, audited transition services, idempotency, or the planned immutable financial ledger.

### 9.4 Handover lifecycle

Current states are `draft`, `vendor_signed`, `renter_signed`, and `completed`.

| From | To | Gate |
|---|---|---|
| — | `draft` | Checklist created for pickup or return |
| `draft` | `vendor_signed` | Vendor identity authorized; signature timestamp recorded |
| `draft` | `renter_signed` | Renter identity authorized; signature timestamp recorded |
| `vendor_signed` | `completed` | Renter signs and required evidence exists |
| `renter_signed` | `completed` | Vendor signs and required evidence exists |

A completed record requires both signer IDs, both timestamps, and a non-empty evidence array at the database layer.

## 10. Phase 0 concierge gates

No transaction automation goes live before the corresponding manual control is proven. During Phase 0, operations must manually:

- approve vendors and every listing;
- approve renter tier exceptions and booking requests;
- confirm Paystack/provider references against the provider dashboard or reconciliation feed;
- authorize booking confirmation after deposit verification;
- supervise or review pickup evidence and balance confirmation before activation;
- review every completed return before release/refund and payout instructions;
- resolve every dispute; and
- perform daily booking/payment/payout reconciliation with a two-person check for manual money movement.

Automated actions must remain feature-gated with a kill switch and an operations override that writes an audit event.

## 11. Functional requirements

| ID | Requirement | Phase |
|---|---|---|
| FR-01 | Authenticate users and enforce role/ownership authorization on every protected resource | v1 |
| FR-02 | Initiate KYC, consume verified provider results, assign tiers, and support manual review without storing unnecessary biometric data | Phase 0/v1 |
| FR-03 | Create, review, publish, update, pause, and soft-delete creative gear listings | Phase 0/v1 |
| FR-04 | Search active listings by city/category/dates/price without exposing serial numbers or private vendor data | v1 |
| FR-05 | Enforce valid date ranges and prevent overlapping confirmed/active bookings | Current model/v1 |
| FR-06 | Price bookings server-side and preserve fee/deposit snapshots | v1 |
| FR-07 | Collect deposit upfront and move booking to confirmed only from verified provider state | Phase 0/v1 |
| FR-08 | Assign a per-booking payment account/reference and verify balance before active pickup | Phase 0/v1 |
| FR-09 | Capture structured, evidenced, co-signed pickup and return checklists | Phase 0/v1 |
| FR-10 | Block payout and deposit disposition when a dispute or reconciliation mismatch exists | Phase 0/v1 |
| FR-11 | Calculate vendor gross, commission, adjustments, and net payout transparently | v1 |
| FR-12 | Process provider and KYC webhooks with signature verification, durable deduplication, transaction boundaries, and retry-safe responses | v1 |
| FR-13 | Give admins searchable audit events and evidence with strict access control | Phase 0/v1 |
| FR-14 | Notify users of state changes without making notifications the source of truth | v1 |

## 12. Non-functional, privacy, and security requirements

### 12.1 NDPA-aligned principles

Vouch must implement Nigerian Data Protection Act (NDPA) obligations with counsel/DPO validation, including:

- **lawfulness, fairness, and transparency:** document purpose and lawful basis for account, KYC, fraud, payment, evidence, and marketing processing;
- **purpose limitation:** do not reuse KYC, location, or handover evidence for unrelated purposes;
- **data minimization:** store provider references and verification outcomes rather than raw ID/biometric material where feasible;
- **accuracy:** allow controlled correction and provider re-verification while preserving audit history;
- **storage limitation:** adopt an approved retention schedule and defensible legal holds;
- **integrity and confidentiality:** encryption in transit/at rest, least privilege, MFA for operations, secret management, access logging, and secure evidence URLs;
- **accountability:** maintain processing records, processor agreements, risk assessments/DPIAs where required, incident response, data-subject request procedures, breach assessment/notification procedures, and cross-border transfer safeguards; and
- **data-subject rights:** provide authenticated access, correction, deletion/objection/restriction workflows where legally applicable without destroying records subject to financial, fraud, dispute, or legal retention duties.

The lawful bases, regulator filings, DPO obligations, age eligibility, exact notices/consents, cross-border locations, and statutory retention periods are **unresolved legal decisions**.

### 12.2 Security and reliability

- Never trust client-supplied price, role, tier, payment success, commission, or payout eligibility.
- Verify webhook signatures against the raw body; reject invalid signatures; deduplicate by provider and event/reference ID.
- Use idempotency keys for money-moving POSTs and transactional state changes.
- Redact PII, KYC payloads, signatures, payment details, and secrets from logs.
- Restrict evidence objects with short-lived signed access and malware/content-type checks.
- Generate correlation IDs and structured operational logs.
- Define service-level objectives before public v1; Phase 0 must at minimum have alerting for failed webhooks, reconciliation mismatches, and backup failures.

### 12.3 Backup and recovery

Before production data:

- use encrypted automated PostgreSQL backups and continuous WAL-based PITR;
- keep backups in a separate failure domain with least-privilege access;
- define approved RPO/RTO targets (**unresolved operational decision**);
- monitor backup/PITR health and failed jobs;
- conduct and record restore drills at least quarterly and before launch;
- include object/evidence storage in backup, versioning, retention, and recovery planning; and
- verify restored relational records still map to evidence objects and audit events.

## 13. Audit and evidence retention

Financial transactions, booking transitions, KYC outcomes, webhook receipts, admin decisions, signatures, and evidence changes require append-only audit events recording actor/service, action, resource, previous/next state or safe diff, correlation/idempotency key, timestamp, and reason. Access to sensitive evidence is itself auditable.

Retention must distinguish operational records, financial/tax records, KYC outcomes, raw provider payloads, handover evidence, audit logs, and backups. Exact periods are **unresolved pending Nigerian counsel, provider contracts, tax/accounting advice, dispute limitation analysis, and NDPA storage-limitation review**. Until approved, production launch is gated; there must be no blanket "retain forever" policy. Legal hold must suspend deletion for identified records, and expiry jobs must produce auditable deletion/tombstone outcomes.

## 14. Acceptance metrics

Phase 0 baselines are collected manually; v1 targets should be ratified after the first concierge cohort.

| Metric | Phase 0 / launch acceptance |
|---|---|
| Scope compliance | 100% live listings are approved creative gear in Lagos |
| Verification gate | 100% confirmed bookings have the policy-required verified renter tier |
| Deposit control | 100% confirmed bookings have provider-verified upfront deposit collection |
| Pickup control | 100% active bookings have verified balance plus completed co-signed pickup evidence |
| Return/payout control | 100% paid payouts trace to a completed co-signed return and no unresolved dispute |
| Commission accuracy | 100% sampled payouts reconcile gross rental fee, vendor-side commission, adjustments, and net |
| Webhook integrity | 100% valid duplicate webhook deliveries create no duplicate financial or lifecycle effect |
| Reconciliation | 100% provider transactions reconciled daily; unresolved mismatches stop affected payout |
| Evidence completeness | At least 95% of initiated handovers complete without evidence/signature remediation; 100% completed records meet required fields |
| Restore readiness | Successful pre-launch PITR restore drill and quarterly drills thereafter |
| Privacy/security | Zero known unauthorized KYC/evidence access; 100% privileged access events logged |
| Concierge viability | At least 20 completed Lagos rentals across at least 5 active vendors before broad automation review |
| Marketplace quality | At least 90% of confirmed rentals complete without a substantiated damage/loss/payment dispute during the gate cohort |

Metrics are safeguards and learning criteria, not promises of loss prevention.

## 15. Expansion and automation gates

### 15.1 Phase 0 to automated Lagos v1

All must hold:

- legal approval of terms, privacy, payment/deposit wording, refunds, disputes, commission/tax, and provider funds flow;
- signed contracts and successful sandbox/production validation for Paystack and the selected licensed payment flow;
- Smile ID or Dojah selected after privacy, security, coverage, liveness, data-location, webhook, support, and commercial review;
- required Phase 0 cohort metrics met;
- no unresolved severity-one security/privacy issue;
- daily reconciliation demonstrated for at least four consecutive weeks;
- backup and PITR restore drill passed;
- support runbooks and payout/deposit kill switches tested; and
- each automation rule shadowed against concierge decisions with an approved error rate.

### 15.2 Lagos to Abuja

All must hold:

- Lagos v1 operates for at least three consecutive months with acceptance controls sustained;
- sufficient verified Abuja supply and demand to launch a curated cohort (target to be approved; suggested minimum 10 vetted vendors and 50 approved listings);
- Abuja-specific operations, handover, support, dispute, and recovery coverage is staffed;
- provider/KYC service coverage and contractual terms apply in Abuja;
- unit economics remain viable after vendor-side commission, support, payment, fraud, refund, and dispute costs; and
- executive risk review approves city activation. City support in the current enum does not itself authorize Abuja launch.

## 16. Dependencies and unresolved decisions

| Decision | Status | Launch consequence |
|---|---|---|
| Licensed provider and legal structure for platform deposit collection/hold/release | **Unresolved — counsel/provider** | Blocks live deposit collection and any custodial-holding claim |
| Paystack product capabilities for per-booking accounts, split/transfer timing, refunds, webhook guarantees | **Unresolved — provider validation** | Blocks final payment implementation |
| Smile ID vs Dojah | **Unresolved — privacy/security/commercial review** | Blocks automated KYC |
| Commission rate, VAT/withholding/tax treatment | **Unresolved — commercial/counsel/accounting** | Blocks payout calculation launch |
| Deposit claim/dispute policy and decision authority | **Unresolved — counsel/risk** | Blocks automated release/claim |
| Cancellation/refund policy | **Unresolved — commercial/counsel** | Blocks self-service cancellation |
| Retention schedule, data location/transfers, DPO/DPIA requirements | **Unresolved — counsel/DPO** | Blocks production personal-data processing |
| Renter age and contracting eligibility | **Unresolved — counsel** | Blocks onboarding terms |
| RPO/RTO and availability SLOs | **Unresolved — operations** | Blocks production readiness sign-off |

## 17. Current implementation boundary

The repository currently provides a FastAPI modular-monolith foundation, `/api/v1/health/live`, `/api/v1/health/ready`, configuration, async PostgreSQL lifecycle, request IDs/logging, CORS, seven ORM-backed tables, and an initial migration. It does **not** currently implement authentication, marketplace APIs, provider adapters, webhook ingestion, audit events, evidence object storage, background jobs, notifications, reconciliation, or a frontend. The planned contracts and architecture in the companion documents must not be mistaken for deployed behavior.
