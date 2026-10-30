"""Notifier interface and the recording fake."""

from __future__ import annotations

import logging
import uuid

from app.services.notify import (
    LoggingNotifier,
    Notifier,
    RecordingNotifier,
    SentNotification,
)


async def test_recording_notifier_records_calls_in_order() -> None:
    notifier = RecordingNotifier()
    user_id = uuid.uuid4()
    data = {"booking_id": "b1"}

    await notifier.send(user_id, "booking.requested", data)
    await notifier.send(None, "ops.booking_lost", {})
    data["booking_id"] = "mutated"

    assert notifier.calls == [
        SentNotification(user_id, "booking.requested", {"booking_id": "b1"}),
        SentNotification(None, "ops.booking_lost", {}),
    ]
    assert notifier.templates() == ["booking.requested", "ops.booking_lost"]


def test_recording_notifier_satisfies_protocol() -> None:
    assert isinstance(RecordingNotifier(), Notifier)


async def test_logging_notifier_logs_template_and_user_but_not_data(vouch_caplog) -> None:
    caplog = vouch_caplog
    notifier = LoggingNotifier()
    user_id = uuid.uuid4()

    await notifier.send(user_id, "booking.confirmed", {"vendor_phone": "+2348011112222"})
    await notifier.send(None, "ops.at_risk", {"renter_id": "renter-secret"})

    assert [record.levelno for record in caplog.records] == [logging.INFO, logging.INFO]
    assert [record.name for record in caplog.records] == ["vouch.notify", "vouch.notify"]
    assert "booking.confirmed" in caplog.text
    assert str(user_id) in caplog.text
    assert "ops.at_risk" in caplog.text
    assert "+2348011112222" not in caplog.text
    assert "renter-secret" not in caplog.text


def test_logging_notifier_satisfies_protocol() -> None:
    assert isinstance(LoggingNotifier(), Notifier)
