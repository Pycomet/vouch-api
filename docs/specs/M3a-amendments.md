# Milestone 3a: amendments to the spec

`docs/specs/M3a.md` is the source of truth for Milestone 3a. This table records every deviation from it. Numbering continues from the M2 amendments (A1–A18). Where this table and the spec disagree, this table wins.

| # | Spec says | We do | Source |
|---|---|---|---|
| A19 | §2.3 adds an `alembic check` CI step in Step 1; §7.3 removes the model-only FK `handover_records.vendor_signer_id → users.id` in Step 6 | The §7.3 ORM-drift fix moves into Step 1, so `alembic check` passes from its first CI run. The ERD edge and the drift notes in `docs/SCHEMA.md` and `docs/OPERATIONS-NOTES.md` are updated in the same step | controller ruling, 4 Nov 2026 |
