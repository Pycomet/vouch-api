# Milestone 2: amendments to the spec

`docs/specs/M2.md` is the source of truth for Milestone 2. It was written against PR #2. The repository has moved on since then, and the agent brief adds hard rules. Where the spec and this table disagree, this table wins. Anything not listed here follows the spec.

| # | Spec says | We do | Source |
|---|---|---|---|
| A1 | Migration 0002 | `0003_schema_alignment`, `down_revision="0002_listing_publication_fields"`. The existing 0002 (listing status, review columns) is kept. | brief |
| A2 | Lagos-first; city hard-coded in inventory | Launched cities come from `VOUCH_LAUNCHED_CITIES` (comma list, default `abuja`). No city literal in any query, schema or docstring. A city that isn't launched returns 400; an unlaunched asset returns 404. | brief |
| A3 | The band on `catalogue_items` is the booking guard | `assets.minimum_trust_tier` is the only guard column, and its CHECK widens to `BETWEEN 0 AND 3`. The band sets the default when a listing is created or backfilled: A→1, B→2, C→3. Tier 3 means renter effective tier ≥ 2 **and** `bookings.vendor_approved_at IS NOT NULL`. | owner decision, 30 Oct 2026 |
| A4 | `waiver: boolean`; `paymenttype.protection_waiver` | `damage_contribution: boolean`; the enum value is renamed `damage_contribution`; the snapshot key is `damage_contribution`. The `protection_pool` ledger account is kept. | owner decision, 30 Oct 2026 |
| A5 | Category enum value `other` | Backfilled to the slug `accessory`, to match the request portal. | brief rule 5 |
| A6 | `kyc_profiles.tier` (old: 1 = ID + liveness, 2 = + address) | New tier 1 = ID + liveness + address. Data: old 2 → 1; old 1 → `status='submitted', tier=0, verified_at=NULL` (address not yet verified). CHECK `tier BETWEEN 0 AND 1`. | spec §2.3 |
| A7 | `/catalogue/search`, alongside the existing `/assets` | Both endpoints are kept and share one inventory service. The `/assets` date filter switches to the live booking statuses. | judgement |
| A8 | Policy values the spec doesn't give | Config placeholders, flagged in every step report: `VOUCH_COMMISSION_RATE=0.15`; `VOUCH_DAMAGE_CONTRIBUTION_RATE=0.05` (of rental); a renter cancelling before the start date gets a full refund of everything paid; the delivery fee comes from `assets.fulfilment.delivery_fee` (default 0). | spec gap; pending legal advice |
| A9 | "Atlas ERD" | `docs/erd.svg` is rendered from a Mermaid file generated from `Base.metadata` with `@mermaid-js/mermaid-cli`. Atlas is used only if it's already installed. | judgement |
| A10 | The spec doesn't say where claims, renter freezes, the at-risk flag or OTP challenges live | 0003 adds `bookings.claim_*`, `bookings.at_risk_at` and `users.restricted_at` / `restricted_reason`. 0004 (Step 5) adds `otp_challenges` and `users.phone_verified_at`, and makes `users.full_name` nullable. | spec gap |
