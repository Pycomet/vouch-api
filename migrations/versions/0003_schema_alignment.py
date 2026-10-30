"""Schema alignment for Milestone 2.

Revision ID: 0003_schema_alignment
Revises: 0002_listing_publication_fields
Create Date: 2026-10-30

One revision, built from parts. ``upgrade()`` runs the parts in order and
``downgrade()`` runs them in reverse. Later parts append their own helpers.

Part A: reference tables, catalogue and assets
----------------------------------------------
* ``cities`` and ``categories`` reference tables replace the ``city`` and
  ``assetcategory`` enums. Seeds: the two cities and the seven categories, each
  category with a handover ``checklist_template`` (``serial`` or ``count`` mode).
* ``catalogue_items`` with the GIN search index ``ix_catalogue_items_search`` over
  the IMMUTABLE function ``catalogue_search_vector(make, model, aliases)``, which
  wraps the spec's ``to_tsvector('simple', ...)`` expression (see below).
* ``assets.city_id`` / ``assets.category_id`` FKs, backfilled by slug (the old
  category ``other`` becomes ``accessory``). The enum columns, the enum types and
  ``ix_assets_public_lagos`` are dropped.
* ``assets`` gains ``catalogue_item_id`` (NOT NULL), ``condition_grade``,
  ``included_accessories``, ``weekend_rate``, ``weekly_rate`` and ``fulfilment``;
  ``assets.name`` becomes nullable (a display override). Each existing asset gets
  its own catalogue row: make ``Unknown``, model ``<name> #<first 8 of id>``,
  band ``B`` and ``specs = {"needs_review": true}``.
* ``ck_assets_minimum_trust_tier_valid`` widens to ``BETWEEN 0 AND 3``; backfilled
  band-B assets get ``GREATEST(minimum_trust_tier, 2)`` so no tier goes down.
* Partial index ``ix_assets_public`` on
  ``(city_id, category_id, daily_rate, created_at, id)`` for public inventory.

Part A downgrade (lossy where noted)
------------------------------------
* Enums come back from the reference slugs (``accessory`` maps to ``other``). A
  city or category slug added after this revision has no enum value and fails.
* ``assets.name`` is filled with ``make || ' ' || model`` where it is NULL, then
  restored to NOT NULL. The new asset columns and the catalogue are dropped.
* ``minimum_trust_tier`` 3 is clamped to 2 before the 0-2 CHECK is restored. The
  tiers raised to 2 by the upgrade backfill are not lowered again.

Part B: users, trust tiers, vendor references, bookings, payments
-----------------------------------------------------------------
* ``users.restricted_at`` / ``restricted_reason`` (a restriction needs a reason).
* ``kyc_profiles.tier`` stores only the verified level, 0 or 1. Tier 1 now means
  national ID + liveness + address. Data (A6): old tier 2 becomes tier 1; old tier
  1 (no address) becomes ``status = 'submitted', tier = 0, verified_at = NULL``.
  ``ck_kyc_profiles_supported_tier`` is ``tier BETWEEN 0 AND 1`` and
  ``ck_kyc_profiles_verification_consistency`` makes verified <=> tier 1.
* ``vendor_references`` (unique per renter and vendor); its booking must belong to
  the same renter and vendor (composite FKs onto ``uq_bookings_id_renter`` and
  ``uq_bookings_id_vendor``).
* ``bookingstatus`` becomes the 13-value state machine (the old type is renamed
  ``bookingstatus_old``, the column is cast through a CASE, the old type dropped):
  pending -> requested, confirmed -> confirmed, active -> in_progress,
  completed -> completed, cancelled -> cancelled_by_renter, disputed -> disputed.
  The default becomes ``requested``.
* ``bookings`` gains ``hold_expires_at``, ``deposit_due_at``,
  ``claim_window_ends_at``, ``vendor_approved_at``, ``at_risk_at``,
  ``price_snapshot`` (NOT NULL; backfill ``{"basis": "legacy", "rental": "<fee>",
  "deposit": "<deposit>"}`` with string amounts), ``fulfilment_choice`` (NOT NULL,
  backfill ``pickup``) and the claim columns ``claim_amount``,
  ``claim_description``, ``claim_evidence``, ``claimed_at``. Backfills that let
  the CHECKs hold: ``requested`` rows get ``hold_expires_at = created_at + 4
  hours`` (already past for old rows, so the timer expires them); legacy
  ``disputed`` rows get ``claim_window_ends_at = updated_at``.
* CHECKs: ``ck_bookings_requested_has_hold``, ``ck_bookings_accepted_has_deposit_due``,
  ``ck_bookings_returned_has_claim_window``, ``ck_bookings_claim_complete``,
  ``ck_bookings_not_self``, ``ck_bookings_fulfilment_choice_valid`` and
  ``ck_bookings_claim_amount_positive``.
* ``ex_bookings_no_active_asset_overlap`` is replaced by ``ex_bookings_no_live_overlap``
  over the live statuses (``LIVE_STATUS_PREDICATE`` below, equal to the model's
  ``live_status_predicate()``).
* ``availability_windows`` gains ``ex_availability_windows_no_blocked_overlap``.
* ``paymenttype`` value ``protection_waiver`` is renamed ``damage_contribution`` (A4).

Upgrade fails, by design, on existing data that breaks the new invariants: a
self-booking, two old ``pending``/``confirmed``/``active`` rows of one asset on
overlapping dates, or two overlapping blocked windows. Fix such rows first.

Part B downgrade (lossy)
------------------------
* ``bookingstatus`` maps back: requested, accepted, deposit_paid -> pending;
  confirmed -> confirmed; in_progress, returned -> active; completed -> completed;
  declined, expired, cancelled_by_renter, cancelled_by_vendor -> cancelled;
  disputed, lost -> disputed. The distinctions are lost, and so are the new
  booking columns (snapshot, fulfilment, timers, claims).
* ``kyc_profiles``: verified tier 1 becomes tier 2. Rows the upgrade moved from old
  tier 1 to ``submitted`` / tier 0 are not restored (their ``verified_at`` is gone).
* ``vendor_references`` and the user restriction columns are dropped.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision metadata
# ---------------------------------------------------------------------------

revision: str = "0003_schema_alignment"
down_revision: str | Sequence[str] | None = "0002_listing_publication_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _upgrade_reference_and_catalogue()
    _upgrade_users_trust_bookings()


def downgrade() -> None:
    _downgrade_users_trust_bookings()
    _downgrade_reference_and_catalogue()


# ===========================================================================
# Part A: reference tables, catalogue and assets
# ===========================================================================

# Seed data. Migrations are the one place city and category literals live.
CITIES = (("abuja", "Abuja"), ("lagos", "Lagos"))

CATEGORIES: tuple[tuple[str, str, dict], ...] = (
    (
        "camera",
        "Camera",
        {
            "mode": "serial",
            "items": ["Body (serial)", "Battery", "Charger", "Body cap", "Memory card"],
        },
    ),
    (
        "lens",
        "Lens",
        {"mode": "serial", "items": ["Lens (serial)", "Front cap", "Rear cap", "Lens hood"]},
    ),
    (
        "lighting",
        "Lighting",
        {"mode": "count", "items": ["Light heads", "Power supplies", "Stands", "Modifiers"]},
    ),
    (
        "audio",
        "Audio",
        {"mode": "serial", "items": ["Unit (serial)", "Windscreen", "Cables", "Batteries"]},
    ),
    (
        "drone",
        "Drone",
        {
            "mode": "serial",
            "items": ["Aircraft (serial)", "Controller", "Batteries", "Propellers", "Charger"],
        },
    ),
    (
        "gimbal",
        "Gimbal",
        {"mode": "serial", "items": ["Gimbal (serial)", "Battery", "Quick-release plate"]},
    ),
    (
        "accessory",
        "Accessory",
        {"mode": "count", "items": ["Items", "Cases", "Cables"]},
    ),
)

# Old enum values, recreated on downgrade (from 0001).
OLD_CITY_VALUES = ("lagos", "abuja")
OLD_CATEGORY_VALUES = ("camera", "lens", "lighting", "audio", "drone", "gimbal", "other")

PUBLIC_WHERE = "listing_status = 'active' AND is_available = true AND deleted_at IS NULL"
FULFILMENT_DEFAULT = '\'{"pickup": true, "delivery": false}\'::jsonb'
# Postgres rejects the spec's index expression as written: ``array_to_string`` is
# only STABLE, and index expressions must be IMMUTABLE. The same expression is
# wrapped in an IMMUTABLE SQL function (safe: every input is text). The index and
# the inventory search both call this function, so the planner can use the index.
SEARCH_FUNCTION = "catalogue_search_vector"
CREATE_SEARCH_FUNCTION = (
    f"CREATE FUNCTION {SEARCH_FUNCTION}(make text, model text, aliases text[]) "
    "RETURNS tsvector LANGUAGE sql IMMUTABLE PARALLEL SAFE AS "
    "$$ SELECT to_tsvector('simple'::regconfig, "
    "make || ' ' || model || ' ' || array_to_string(aliases, ' ')) $$"
)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _upgrade_reference_and_catalogue() -> None:
    # 1. Reference tables and seeds.
    op.create_table(
        "cities",
        sa.Column("id", sa.SmallInteger(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_cities"),
        sa.UniqueConstraint("slug", name="uq_cities_slug"),
    )
    op.create_table(
        "categories",
        sa.Column("id", sa.SmallInteger(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("checklist_template", postgresql.JSONB(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_categories"),
        sa.UniqueConstraint("slug", name="uq_categories_slug"),
    )
    city_rows = ", ".join(f"({_sql_literal(s)}, {_sql_literal(n)})" for s, n in CITIES)
    op.execute(f"INSERT INTO cities (slug, name) VALUES {city_rows}")
    # json.dumps puts a space after each colon, so no ":word" is read as a bind param.
    category_rows = ", ".join(
        f"({_sql_literal(s)}, {_sql_literal(n)}, {_sql_literal(json.dumps(t))}::jsonb)"
        for s, n, t in CATEGORIES
    )
    op.execute(f"INSERT INTO categories (slug, name, checklist_template) VALUES {category_rows}")

    # 2. Catalogue.
    op.create_table(
        "catalogue_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("category_id", sa.SmallInteger(), nullable=False),
        sa.Column("make", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("mount", sa.Text(), nullable=True),
        sa.Column(
            "specs",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("replacement_value_band", sa.CHAR(1), nullable=False),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_catalogue_items"),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], name="fk_catalogue_items_category_id"
        ),
        sa.UniqueConstraint("make", "model", name="uq_catalogue_items_make_model"),
        sa.CheckConstraint(
            "replacement_value_band IN ('A', 'B', 'C')",
            name="ck_catalogue_items_replacement_value_band_valid",
        ),
    )
    op.execute(CREATE_SEARCH_FUNCTION)
    op.execute(
        "CREATE INDEX ix_catalogue_items_search ON catalogue_items "
        f"USING gin ({SEARCH_FUNCTION}(make, model, aliases))"
    )

    # 3. assets.city / assets.category -> FKs to the reference tables.
    op.add_column("assets", sa.Column("city_id", sa.SmallInteger(), nullable=True))
    op.add_column("assets", sa.Column("category_id", sa.SmallInteger(), nullable=True))
    op.execute("UPDATE assets a SET city_id = c.id FROM cities c WHERE c.slug = a.city::text")
    op.execute(
        "UPDATE assets a SET category_id = c.id FROM categories c "
        "WHERE c.slug = CASE a.category::text WHEN 'other' THEN 'accessory' "
        "ELSE a.category::text END"
    )
    op.alter_column("assets", "city_id", nullable=False)
    op.alter_column("assets", "category_id", nullable=False)
    op.create_foreign_key("fk_assets_city_id", "assets", "cities", ["city_id"], ["id"])
    op.create_foreign_key("fk_assets_category_id", "assets", "categories", ["category_id"], ["id"])
    op.drop_index("ix_assets_public_lagos", table_name="assets")
    op.drop_column("assets", "city")
    op.drop_column("assets", "category")
    op.execute("DROP TYPE city")
    op.execute("DROP TYPE assetcategory")

    # 4. Catalogue link and listing detail columns.
    op.add_column(
        "assets", sa.Column("catalogue_item_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("assets", sa.Column("condition_grade", sa.Text(), nullable=True))
    op.add_column(
        "assets",
        sa.Column(
            "included_accessories",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("assets", sa.Column("weekend_rate", sa.Numeric(12, 2), nullable=True))
    op.add_column("assets", sa.Column("weekly_rate", sa.Numeric(12, 2), nullable=True))
    op.add_column(
        "assets",
        sa.Column(
            "fulfilment",
            postgresql.JSONB(),
            server_default=sa.text(FULFILMENT_DEFAULT),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_assets_condition_grade_valid",
        "assets",
        "condition_grade IN ('excellent', 'good', 'fair')",
    )
    op.create_check_constraint(
        "ck_assets_weekend_rate_positive", "assets", "weekend_rate IS NULL OR weekend_rate > 0"
    )
    op.create_check_constraint(
        "ck_assets_weekly_rate_positive", "assets", "weekly_rate IS NULL OR weekly_rate > 0"
    )
    # One catalogue row per existing asset. The CTE is materialised (volatile
    # gen_random_uuid), so the INSERT and the UPDATE see the same item ids.
    op.execute(
        "WITH src AS ("
        "  SELECT id AS asset_id, gen_random_uuid() AS item_id, category_id,"
        "         name || ' #' || left(id::text, 8) AS model"
        "  FROM assets"
        "), inserted AS ("
        "  INSERT INTO catalogue_items"
        "    (id, category_id, make, model, specs, replacement_value_band)"
        "  SELECT item_id, category_id, 'Unknown', model,"
        "         jsonb_build_object('needs_review', true), 'B'"
        "  FROM src"
        ") "
        "UPDATE assets a SET catalogue_item_id = src.item_id "
        "FROM src WHERE a.id = src.asset_id"
    )
    op.alter_column("assets", "catalogue_item_id", nullable=False)
    op.create_foreign_key(
        "fk_assets_catalogue_item_id",
        "assets",
        "catalogue_items",
        ["catalogue_item_id"],
        ["id"],
    )
    op.alter_column("assets", "name", nullable=True)

    # 5. Trust tier 0-3; backfilled band-B assets need at least tier 2.
    op.drop_constraint("ck_assets_minimum_trust_tier_valid", "assets", type_="check")
    op.execute("UPDATE assets SET minimum_trust_tier = GREATEST(minimum_trust_tier, 2)")
    op.create_check_constraint(
        "ck_assets_minimum_trust_tier_valid", "assets", "minimum_trust_tier BETWEEN 0 AND 3"
    )

    # 6. Public inventory index.
    op.create_index(
        "ix_assets_public",
        "assets",
        ["city_id", "category_id", "daily_rate", "created_at", "id"],
        postgresql_where=sa.text(PUBLIC_WHERE),
    )


def _downgrade_reference_and_catalogue() -> None:
    # 6. Public inventory index.
    op.drop_index("ix_assets_public", table_name="assets")

    # 5. Trust tier back to 0-2 (tier 3 is clamped to 2).
    op.drop_constraint("ck_assets_minimum_trust_tier_valid", "assets", type_="check")
    op.execute("UPDATE assets SET minimum_trust_tier = 2 WHERE minimum_trust_tier = 3")
    op.create_check_constraint(
        "ck_assets_minimum_trust_tier_valid", "assets", "minimum_trust_tier BETWEEN 0 AND 2"
    )

    # 4. Listing detail columns and the catalogue link; name is required again.
    op.execute(
        "UPDATE assets a SET name = c.make || ' ' || c.model "
        "FROM catalogue_items c WHERE c.id = a.catalogue_item_id AND a.name IS NULL"
    )
    op.alter_column("assets", "name", nullable=False)
    op.drop_constraint("fk_assets_catalogue_item_id", "assets", type_="foreignkey")
    op.drop_constraint("ck_assets_weekly_rate_positive", "assets", type_="check")
    op.drop_constraint("ck_assets_weekend_rate_positive", "assets", type_="check")
    op.drop_constraint("ck_assets_condition_grade_valid", "assets", type_="check")
    for column in (
        "fulfilment",
        "weekly_rate",
        "weekend_rate",
        "included_accessories",
        "condition_grade",
        "catalogue_item_id",
    ):
        op.drop_column("assets", column)

    # 3. Enum columns come back from the reference slugs.
    city_values = ", ".join(_sql_literal(v) for v in OLD_CITY_VALUES)
    category_values = ", ".join(_sql_literal(v) for v in OLD_CATEGORY_VALUES)
    op.execute(f"CREATE TYPE city AS ENUM ({city_values})")
    op.execute(f"CREATE TYPE assetcategory AS ENUM ({category_values})")
    op.add_column(
        "assets",
        sa.Column("category", postgresql.ENUM(name="assetcategory", create_type=False)),
    )
    op.add_column("assets", sa.Column("city", postgresql.ENUM(name="city", create_type=False)))
    op.execute("UPDATE assets a SET city = c.slug::city FROM cities c WHERE c.id = a.city_id")
    op.execute(
        "UPDATE assets a SET category = (CASE c.slug WHEN 'accessory' THEN 'other' "
        "ELSE c.slug END)::assetcategory FROM categories c WHERE c.id = a.category_id"
    )
    op.alter_column("assets", "city", nullable=False)
    op.alter_column("assets", "category", nullable=False)
    op.drop_constraint("fk_assets_category_id", "assets", type_="foreignkey")
    op.drop_constraint("fk_assets_city_id", "assets", type_="foreignkey")
    op.drop_column("assets", "category_id")
    op.drop_column("assets", "city_id")
    op.create_index(
        "ix_assets_public_lagos",
        "assets",
        ["city", "category", "daily_rate", "created_at", "id"],
        postgresql_where=sa.text(PUBLIC_WHERE),
    )

    # 2 and 1. Catalogue and reference tables.
    op.execute("DROP INDEX ix_catalogue_items_search")
    op.drop_table("catalogue_items")
    op.execute(f"DROP FUNCTION {SEARCH_FUNCTION}(text, text, text[])")
    op.drop_table("categories")
    op.drop_table("cities")


# ===========================================================================
# Part B: users, trust tiers, vendor references, bookings, payments
# ===========================================================================

NEW_BOOKING_STATUSES = (
    "requested",
    "accepted",
    "deposit_paid",
    "confirmed",
    "in_progress",
    "returned",
    "completed",
    "declined",
    "expired",
    "cancelled_by_renter",
    "cancelled_by_vendor",
    "disputed",
    "lost",
)
OLD_BOOKING_STATUSES = ("pending", "confirmed", "active", "completed", "cancelled", "disputed")

# Old value -> new value on upgrade.
BOOKING_STATUS_UP = {
    "pending": "requested",
    "confirmed": "confirmed",
    "active": "in_progress",
    "completed": "completed",
    "cancelled": "cancelled_by_renter",
    "disputed": "disputed",
}
# New value -> old value on downgrade (lossy: several new values share one old one).
BOOKING_STATUS_DOWN = {
    "requested": "pending",
    "accepted": "pending",
    "deposit_paid": "pending",
    "confirmed": "confirmed",
    "in_progress": "active",
    "returned": "active",
    "completed": "completed",
    "declined": "cancelled",
    "expired": "cancelled",
    "cancelled_by_renter": "cancelled",
    "cancelled_by_vendor": "cancelled",
    "disputed": "disputed",
    "lost": "disputed",
}

# Must equal ``app.db.models.booking.live_status_predicate()`` (asserted by a test);
# migrations do not import application code.
LIVE_STATUS_PREDICATE = (
    "status IN ('requested', 'accepted', 'deposit_paid', 'confirmed', 'in_progress', 'returned')"
)
OLD_ACTIVE_PREDICATE = "status IN ('confirmed', 'active')"
OVERLAP_ELEMENTS = "asset_id WITH =, daterange(starts_on, ends_on, '[]') WITH &&"

BOOKING_TIMESTAMP_COLUMNS = (
    "hold_expires_at",
    "deposit_due_at",
    "claim_window_ends_at",
    "vendor_approved_at",
    "at_risk_at",
)
BOOKING_CHECKS = (
    ("ck_bookings_fulfilment_choice_valid", "fulfilment_choice IN ('pickup', 'delivery')"),
    ("ck_bookings_claim_amount_positive", "claim_amount > 0"),
    ("ck_bookings_requested_has_hold", "status <> 'requested' OR hold_expires_at IS NOT NULL"),
    ("ck_bookings_accepted_has_deposit_due", "status <> 'accepted' OR deposit_due_at IS NOT NULL"),
    (
        "ck_bookings_returned_has_claim_window",
        "status NOT IN ('returned', 'disputed') OR claim_window_ends_at IS NOT NULL",
    ),
    (
        "ck_bookings_claim_complete",
        "(claim_amount IS NULL AND claim_description IS NULL AND claim_evidence IS NULL "
        "AND claimed_at IS NULL) OR "
        "(claim_amount IS NOT NULL AND claim_description IS NOT NULL AND claimed_at IS NOT NULL)",
    ),
    ("ck_bookings_not_self", "renter_id <> vendor_id"),
)

KYC_CONSISTENCY_NEW = (
    "(status = 'verified' AND verified_at IS NOT NULL AND tier = 1) OR "
    "(status <> 'verified' AND verified_at IS NULL AND tier = 0)"
)
KYC_CONSISTENCY_OLD = (
    "(status = 'verified' AND verified_at IS NOT NULL AND tier BETWEEN 1 AND 2) OR "
    "(status <> 'verified' AND verified_at IS NULL AND tier = 0)"
)


def _swap_booking_status(type_values: Sequence[str], mapping: dict[str, str]) -> None:
    """Replace the ``bookingstatus`` type with ``type_values``, mapping each row."""
    cases = " ".join(
        f"WHEN {_sql_literal(src)} THEN {_sql_literal(dst)}" for src, dst in mapping.items()
    )
    values = ", ".join(_sql_literal(v) for v in type_values)
    op.execute("ALTER TABLE bookings ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE bookingstatus RENAME TO bookingstatus_old")
    op.execute(f"CREATE TYPE bookingstatus AS ENUM ({values})")
    op.execute(
        "ALTER TABLE bookings ALTER COLUMN status TYPE bookingstatus "
        f"USING (CASE status::text {cases} END)::bookingstatus"
    )
    op.execute("DROP TYPE bookingstatus_old")


def _upgrade_users_trust_bookings() -> None:
    # 1. users: renter restriction (A10).
    op.add_column("users", sa.Column("restricted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("restricted_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_users_restriction_has_reason",
        "users",
        "restricted_at IS NULL OR restricted_reason IS NOT NULL",
    )

    # 2. kyc_profiles: tier 1 now needs the address too (A6). Old tier 1 moves
    #    first, so the rows moved down from old tier 2 are not caught by it.
    op.drop_constraint("ck_kyc_profiles_supported_tier", "kyc_profiles", type_="check")
    op.drop_constraint("ck_kyc_profiles_verification_consistency", "kyc_profiles", type_="check")
    op.execute(
        "UPDATE kyc_profiles SET status = 'submitted', tier = 0, verified_at = NULL WHERE tier = 1"
    )
    op.execute("UPDATE kyc_profiles SET tier = 1 WHERE tier = 2")
    op.create_check_constraint(
        "ck_kyc_profiles_supported_tier", "kyc_profiles", "tier BETWEEN 0 AND 1"
    )
    op.create_check_constraint(
        "ck_kyc_profiles_verification_consistency", "kyc_profiles", KYC_CONSISTENCY_NEW
    )

    # 3. bookingstatus: the 13-value state machine. The old exclusion names old
    #    values, so it goes first.
    op.drop_constraint("ex_bookings_no_active_asset_overlap", "bookings", type_=None)
    _swap_booking_status(NEW_BOOKING_STATUSES, BOOKING_STATUS_UP)
    op.execute("ALTER TABLE bookings ALTER COLUMN status SET DEFAULT 'requested'")

    # 4. bookings: state-machine, pricing and claim columns, backfilled.
    for column in BOOKING_TIMESTAMP_COLUMNS:
        op.add_column("bookings", sa.Column(column, sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("price_snapshot", postgresql.JSONB(), nullable=True))
    op.add_column("bookings", sa.Column("fulfilment_choice", sa.Text(), nullable=True))
    op.add_column("bookings", sa.Column("claim_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("bookings", sa.Column("claim_description", sa.Text(), nullable=True))
    op.add_column("bookings", sa.Column("claim_evidence", postgresql.JSONB(), nullable=True))
    op.add_column("bookings", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE bookings SET price_snapshot = jsonb_build_object("
        "'basis', 'legacy', 'rental', total_rental_fee::text, "
        "'deposit', deposit_amount::text), fulfilment_choice = 'pickup'"
    )
    op.alter_column("bookings", "price_snapshot", nullable=False)
    op.alter_column("bookings", "fulfilment_choice", nullable=False)
    op.execute(
        "UPDATE bookings SET hold_expires_at = created_at + interval '4 hours' "
        "WHERE status = 'requested'"
    )
    # Legacy disputes predate the claim window; treat it as closed when last updated.
    op.execute("UPDATE bookings SET claim_window_ends_at = updated_at WHERE status = 'disputed'")

    # 5. bookings CHECKs.
    for name, condition in BOOKING_CHECKS:
        op.create_check_constraint(name, "bookings", condition)

    # 6. A live request holds the unit.
    op.execute(
        "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_live_overlap "
        f"EXCLUDE USING gist ({OVERLAP_ELEMENTS}) WHERE ({LIVE_STATUS_PREDICATE})"
    )

    # 7. vendor_references: the booking must belong to the same renter and vendor.
    op.create_table(
        "vendor_references",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("renter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_vendor_references"),
        sa.ForeignKeyConstraint(["renter_id"], ["users.id"], name="fk_vendor_references_renter_id"),
        sa.ForeignKeyConstraint(["vendor_id"], ["users.id"], name="fk_vendor_references_vendor_id"),
        sa.ForeignKeyConstraint(
            ["booking_id", "renter_id"],
            ["bookings.id", "bookings.renter_id"],
            name="fk_vendor_references_booking_renter",
        ),
        sa.ForeignKeyConstraint(
            ["booking_id", "vendor_id"],
            ["bookings.id", "bookings.vendor_id"],
            name="fk_vendor_references_booking_vendor",
        ),
        sa.UniqueConstraint("renter_id", "vendor_id", name="uq_vendor_references_renter_vendor"),
    )

    # 8. Blocked availability windows cannot overlap each other.
    op.execute(
        "ALTER TABLE availability_windows ADD CONSTRAINT "
        "ex_availability_windows_no_blocked_overlap EXCLUDE USING gist "
        "(asset_id WITH =, daterange(starts_at, ends_at, '[]') WITH &&) WHERE (is_blocked)"
    )

    # 9. paymenttype value rename (A4).
    op.execute("ALTER TYPE paymenttype RENAME VALUE 'protection_waiver' TO 'damage_contribution'")


def _downgrade_users_trust_bookings() -> None:
    # 9. paymenttype.
    op.execute("ALTER TYPE paymenttype RENAME VALUE 'damage_contribution' TO 'protection_waiver'")

    # 8. Blocked windows.
    op.drop_constraint(
        "ex_availability_windows_no_blocked_overlap", "availability_windows", type_=None
    )

    # 7. vendor_references.
    op.drop_table("vendor_references")

    # 6 and 5. Overlap and CHECKs (they name new status values).
    op.drop_constraint("ex_bookings_no_live_overlap", "bookings", type_=None)
    for name, _ in reversed(BOOKING_CHECKS):
        op.drop_constraint(name, "bookings", type_="check")

    # 4. bookings columns.
    for column in (
        "claimed_at",
        "claim_evidence",
        "claim_description",
        "claim_amount",
        "fulfilment_choice",
        "price_snapshot",
        *reversed(BOOKING_TIMESTAMP_COLUMNS),
    ):
        op.drop_column("bookings", column)

    # 3. bookingstatus back to the old six values (lossy, see the module docstring).
    _swap_booking_status(OLD_BOOKING_STATUSES, BOOKING_STATUS_DOWN)
    op.execute("ALTER TABLE bookings ALTER COLUMN status SET DEFAULT 'pending'")
    op.execute(
        "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_active_asset_overlap "
        f"EXCLUDE USING gist ({OVERLAP_ELEMENTS}) WHERE ({OLD_ACTIVE_PREDICATE})"
    )

    # 2. kyc_profiles: verified tier 1 (ID + liveness + address) is old tier 2.
    op.drop_constraint("ck_kyc_profiles_verification_consistency", "kyc_profiles", type_="check")
    op.drop_constraint("ck_kyc_profiles_supported_tier", "kyc_profiles", type_="check")
    op.execute("UPDATE kyc_profiles SET tier = 2 WHERE tier = 1")
    op.create_check_constraint(
        "ck_kyc_profiles_supported_tier", "kyc_profiles", "tier BETWEEN 0 AND 2"
    )
    op.create_check_constraint(
        "ck_kyc_profiles_verification_consistency", "kyc_profiles", KYC_CONSISTENCY_OLD
    )

    # 1. users.
    op.drop_constraint("ck_users_restriction_has_reason", "users", type_="check")
    op.drop_column("users", "restricted_reason")
    op.drop_column("users", "restricted_at")
