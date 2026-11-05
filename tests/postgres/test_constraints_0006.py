"""Live PostgreSQL tests for the constraints migration 0006 adds (M3a spec 3.6).

Vendor profiles, listing pause attribution, the per-vendor serial index, the
asset/catalogue category FK, webhook resolutions and the notification outbox.
Every row is written with raw SQL on ``db_session`` (rolled back at teardown), so
these tests check the database, not the models. Each refused write runs in its own
SAVEPOINT so the test can go on after it.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import RenterRole
from tests.postgres.factories import create_asset, create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)


async def _refused(session: AsyncSession, sql: str, params: dict, constraint: str) -> None:
    """``sql`` fails, naming ``constraint``; the session stays usable."""
    with pytest.raises(DBAPIError, match=constraint):
        async with session.begin_nested():
            await session.execute(text(sql), params)


async def _ok(session: AsyncSession, sql: str, params: dict) -> None:
    async with session.begin_nested():
        await session.execute(text(sql), params)


# ---------------------------------------------------------------------------
# vendor_profiles (3.1)
# ---------------------------------------------------------------------------

PROFILE_SQL = (
    "INSERT INTO vendor_profiles (user_id, status, business_type, account_last4, "
    "verified_at, suspended_at, suspended_reason) "
    "VALUES (:user_id, CAST(:status AS vendorstatus), 'individual', :last4, "
    ":verified_at, :suspended_at, :reason)"
)


def _profile(user_id: uuid.UUID, **overrides) -> dict:
    values = {
        "user_id": user_id,
        "status": "pending",
        "last4": None,
        "verified_at": None,
        "suspended_at": None,
        "reason": None,
    }
    values.update(overrides)
    return values


async def _vendor(session: AsyncSession) -> uuid.UUID:
    return (await create_user(session, role=RenterRole.vendor)).id


async def test_active_profile_without_verified_at_is_refused(db_session: AsyncSession) -> None:
    vendor_id = await _vendor(db_session)
    await _refused(
        db_session,
        PROFILE_SQL,
        _profile(vendor_id, status="active"),
        "ck_vendor_profiles_active_verified",
    )
    await _ok(db_session, PROFILE_SQL, _profile(vendor_id, status="active", verified_at=_now()))


async def test_suspended_profile_needs_both_time_and_reason(db_session: AsyncSession) -> None:
    vendor_id = await _vendor(db_session)
    for overrides in (
        {"status": "suspended", "suspended_at": _now()},  # no reason
        {"status": "suspended", "reason": "Fraud report"},  # no time
        {"status": "pending", "suspended_at": _now(), "reason": "Stale"},  # not suspended
    ):
        await _refused(
            db_session,
            PROFILE_SQL,
            _profile(vendor_id, **overrides),
            "ck_vendor_profiles_suspension",
        )
    await _ok(
        db_session,
        PROFILE_SQL,
        _profile(vendor_id, status="suspended", suspended_at=_now(), reason="Fraud report"),
    )


@pytest.mark.parametrize("last4", ["12a4", "123", "abcd", "12 4"])
async def test_bad_account_last4_is_refused(db_session: AsyncSession, last4: str) -> None:
    vendor_id = await _vendor(db_session)
    await _refused(
        db_session,
        PROFILE_SQL,
        _profile(vendor_id, last4=last4),
        "ck_vendor_profiles_last4_digits",
    )
    await _ok(db_session, PROFILE_SQL, _profile(vendor_id, last4="0042"))


async def test_recipient_code_is_unique(db_session: AsyncSession) -> None:
    first, second = await _vendor(db_session), await _vendor(db_session)
    sql = (
        "INSERT INTO vendor_profiles (user_id, business_type, recipient_code) "
        "VALUES (:user_id, 'rental_house', 'RCP_1')"
    )
    await _ok(db_session, sql, {"user_id": first})
    await _refused(db_session, sql, {"user_id": second}, "uq_vendor_profiles_recipient_code")


# ---------------------------------------------------------------------------
# Listing pause attribution and the serial index (3.2)
# ---------------------------------------------------------------------------

SET_PAUSE_SQL = (
    "UPDATE assets SET listing_status = CAST(:status AS listingstatus), paused_by = :paused_by "
    "WHERE id = :id"
)


async def test_paused_without_paused_by_is_refused(db_session: AsyncSession) -> None:
    asset = await create_asset(db_session)
    await _refused(
        db_session,
        SET_PAUSE_SQL,
        {"id": asset.id, "status": "paused", "paused_by": None},
        "ck_assets_paused_by_matches_status",
    )
    for who in ("vendor", "admin"):
        await _ok(db_session, SET_PAUSE_SQL, {"id": asset.id, "status": "paused", "paused_by": who})


async def test_paused_by_on_a_non_paused_asset_is_refused(db_session: AsyncSession) -> None:
    asset = await create_asset(db_session)
    for status in ("draft", "review", "active", "rejected"):
        await _refused(
            db_session,
            SET_PAUSE_SQL,
            {"id": asset.id, "status": status, "paused_by": "admin"},
            "ck_assets_paused_by_matches_status",
        )


async def test_paused_by_only_takes_vendor_or_admin(db_session: AsyncSession) -> None:
    asset = await create_asset(db_session)
    await _refused(
        db_session,
        SET_PAUSE_SQL,
        {"id": asset.id, "status": "paused", "paused_by": "system"},
        "ck_assets_paused_by_values",
    )


COPY_ASSET_SQL = (
    "INSERT INTO assets (vendor_id, catalogue_item_id, category_id, city_id, daily_rate, "
    "deposit_amount, serial_number, deleted_at) "
    "SELECT :vendor_id, catalogue_item_id, category_id, city_id, daily_rate, deposit_amount, "
    ":serial, :deleted_at FROM assets WHERE id = :id"
)


async def _serialised_asset(session: AsyncSession, serial: str):
    asset = await create_asset(session)
    await session.execute(
        text("UPDATE assets SET serial_number = :serial WHERE id = :id"),
        {"serial": serial, "id": asset.id},
    )
    return asset


async def test_same_vendor_item_and_serial_is_refused_in_any_case(
    db_session: AsyncSession,
) -> None:
    asset = await _serialised_asset(db_session, "SN-ABC-1")
    for serial in ("SN-ABC-1", "sn-abc-1", "Sn-AbC-1"):
        await _refused(
            db_session,
            COPY_ASSET_SQL,
            {"id": asset.id, "vendor_id": asset.vendor_id, "serial": serial, "deleted_at": None},
            "uq_assets_vendor_item_serial",
        )


async def test_same_serial_under_another_vendor_is_allowed(db_session: AsyncSession) -> None:
    asset = await _serialised_asset(db_session, "SN-ABC-1")
    other = await _vendor(db_session)
    await _ok(
        db_session,
        COPY_ASSET_SQL,
        {"id": asset.id, "vendor_id": other, "serial": "sn-abc-1", "deleted_at": None},
    )


async def test_soft_deleted_duplicate_does_not_block(db_session: AsyncSession) -> None:
    asset = await _serialised_asset(db_session, "SN-ABC-1")
    params = {"id": asset.id, "vendor_id": asset.vendor_id, "serial": "SN-ABC-1"}
    # A deleted copy beside the live one, and a live one beside a deleted original.
    await _ok(db_session, COPY_ASSET_SQL, {**params, "deleted_at": _now()})
    await db_session.execute(
        text("UPDATE assets SET deleted_at = now() WHERE id = :id"), {"id": asset.id}
    )
    await _ok(db_session, COPY_ASSET_SQL, {**params, "deleted_at": None})


async def test_assets_without_a_serial_never_collide(db_session: AsyncSession) -> None:
    asset = await create_asset(db_session)
    params = {"id": asset.id, "vendor_id": asset.vendor_id, "serial": None, "deleted_at": None}
    await _ok(db_session, COPY_ASSET_SQL, params)
    await _ok(db_session, COPY_ASSET_SQL, params)


# ---------------------------------------------------------------------------
# Category consistency (3.3)
# ---------------------------------------------------------------------------


async def _category_id(session: AsyncSession, slug: str) -> int:
    return (
        await session.execute(text("SELECT id FROM categories WHERE slug = :s"), {"s": slug})
    ).scalar_one()


async def test_asset_category_must_match_its_catalogue_item(db_session: AsyncSession) -> None:
    asset = await create_asset(db_session, category_slug="camera")
    lens = await _category_id(db_session, "lens")
    await _refused(
        db_session,
        "UPDATE assets SET category_id = :c WHERE id = :id",
        {"c": lens, "id": asset.id},
        "fk_assets_item_category",
    )
    # Inserting a new mismatched asset is refused too.
    await _refused(
        db_session,
        "INSERT INTO assets (vendor_id, catalogue_item_id, category_id, city_id, daily_rate, "
        "deposit_amount) SELECT vendor_id, catalogue_item_id, :c, city_id, daily_rate, "
        "deposit_amount FROM assets WHERE id = :id",
        {"c": lens, "id": asset.id},
        "fk_assets_item_category",
    )


async def test_item_category_cannot_move_under_its_assets(db_session: AsyncSession) -> None:
    asset = await create_asset(db_session, category_slug="camera")
    lens = await _category_id(db_session, "lens")
    await _refused(
        db_session,
        "UPDATE catalogue_items SET category_id = :c WHERE id = :id",
        {"c": lens, "id": asset.catalogue_item_id},
        "fk_assets_item_category",
    )


# ---------------------------------------------------------------------------
# Webhook event resolution (3.4)
# ---------------------------------------------------------------------------

RESOLVE_SQL = (
    "UPDATE webhook_events SET resolution = :resolution, resolved_at = :at, "
    "resolved_by = :by, resolution_note = :note WHERE id = :id"
)


async def _failed_event(session: AsyncSession) -> int:
    return (
        await session.execute(
            text(
                "INSERT INTO webhook_events (provider, provider_event_id, payload, "
                "signature_ok, processing_error) "
                "VALUES ('fake', :evt, '{}'::jsonb, true, 'amount mismatch') RETURNING id"
            ),
            {"evt": f"evt-{uuid.uuid4().hex}"},
        )
    ).scalar_one()


async def test_resolution_needs_all_four_fields(db_session: AsyncSession) -> None:
    event_id = await _failed_event(db_session)
    admin = (await create_user(db_session, role=RenterRole.admin)).id
    full = {
        "id": event_id,
        "resolution": "returned",
        "at": _now(),
        "by": admin,
        "note": "Refunded by hand",
    }
    for missing in ("resolution", "at", "by", "note"):
        await _refused(
            db_session,
            RESOLVE_SQL,
            {**full, missing: None},
            "ck_webhook_events_resolution_complete",
        )
    # Only one field set is refused as well.
    await _refused(
        db_session,
        RESOLVE_SQL,
        {"id": event_id, "resolution": None, "at": None, "by": None, "note": "Looked at"},
        "ck_webhook_events_resolution_complete",
    )
    for resolution in ("returned", "reposted", "written_off"):
        await _ok(db_session, RESOLVE_SQL, {**full, "resolution": resolution})


async def test_resolution_value_is_checked(db_session: AsyncSession) -> None:
    event_id = await _failed_event(db_session)
    admin = (await create_user(db_session, role=RenterRole.admin)).id
    await _refused(
        db_session,
        RESOLVE_SQL,
        {"id": event_id, "resolution": "ignored", "at": _now(), "by": admin, "note": "x"},
        "ck_webhook_events_resolution_values",
    )


# ---------------------------------------------------------------------------
# Notification outbox (3.5)
# ---------------------------------------------------------------------------

OUTBOX_SQL = (
    "INSERT INTO notification_outbox (template, status, sent_at, attempts) "
    "VALUES ('booking.confirmed', CAST(:status AS outboxstatus), :sent_at, :attempts)"
)


async def test_sent_outbox_row_needs_sent_at(db_session: AsyncSession) -> None:
    await _refused(
        db_session,
        OUTBOX_SQL,
        {"status": "sent", "sent_at": None, "attempts": 1},
        "ck_outbox_sent_at",
    )
    await _ok(db_session, OUTBOX_SQL, {"status": "sent", "sent_at": _now(), "attempts": 1})


@pytest.mark.parametrize("status", ["pending", "dead"])
async def test_sent_at_on_a_non_sent_row_is_refused(db_session: AsyncSession, status: str) -> None:
    await _refused(
        db_session,
        OUTBOX_SQL,
        {"status": status, "sent_at": _now(), "attempts": 0},
        "ck_outbox_sent_at",
    )
    await _ok(db_session, OUTBOX_SQL, {"status": status, "sent_at": None, "attempts": 0})


async def test_outbox_attempts_cannot_go_negative(db_session: AsyncSession) -> None:
    await _refused(
        db_session,
        OUTBOX_SQL,
        {"status": "pending", "sent_at": None, "attempts": -1},
        "ck_outbox_attempts_nonnegative",
    )


async def test_outbox_defaults(db_session: AsyncSession) -> None:
    row = (
        await db_session.execute(
            text(
                "INSERT INTO notification_outbox (template) VALUES ('ops.webhook_failed') "
                "RETURNING user_id, data, status::text, attempts, next_attempt_at, sent_at"
            )
        )
    ).one()
    assert row.user_id is None  # an operations alert
    assert row.data == {}
    assert (row.status, row.attempts, row.sent_at) == ("pending", 0, None)
    assert row.next_attempt_at is not None


def _now() -> datetime:
    return datetime.now(UTC)
