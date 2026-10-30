"""Notifier interface and the recording fake."""

from __future__ import annotations

import uuid

from app.services.notify import Notifier, RecordingNotifier, SentNotification


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
