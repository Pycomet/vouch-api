# Milestone 3a: amendments to the spec

`docs/specs/M3a.md` is the source of truth for Milestone 3a. This table records every deviation from it. Numbering continues from the M2 amendments (A1–A18). Where this table and the spec disagree, this table wins.

| # | Spec says | We do | Source |
|---|---|---|---|
| A19 | §2.3 adds an `alembic check` CI step in Step 1; §7.3 removes the model-only FK `handover_records.vendor_signer_id → users.id` in Step 6 | The §7.3 ORM-drift fix moves into Step 1, so `alembic check` passes from its first CI run. The ERD edge and the drift notes in `docs/SCHEMA.md` and `docs/OPERATIONS-NOTES.md` are updated in the same step | controller ruling, 4 Nov 2026 |
| A20 | §0.1 and §2.2: Phase 0 runs on the fake providers, while the production-safety settings enforced at start-up stay in force (and those refuse the fakes outside development and test) | A new `VOUCH_ENVIRONMENT=phase0`. The fake OTP sender, KYC provider and payment provider are allowed there (and are the default when unset); the JWT secret and the fake payment webhook secret must be strong (not the placeholder, at least 32 bytes); `debug` is refused; `/docs`, `/redoc` and `/openapi.json` are not served. `production` still refuses the fakes | owner decision, 5 Nov 2026 |
| A21 | §2.4: `create-admin` is idempotent on an existing admin (exit 0, no second row) | Idempotent only on an existing **active, not deleted** admin. A phone that belongs to an admin who is inactive (`is_active = false`) or soft-deleted (`deleted_at` set) is refused with exit 1 and nothing changes: the command never reactivates an account | controller ruling, 5 Nov 2026 |
