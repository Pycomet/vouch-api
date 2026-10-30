"""Outbound notifications behind one interface.

Services call ``notifier.send(user_id, template, data)``. ``user_id`` is ``None``
for operations alerts, whose templates are named ``ops.<event>``. M2 ships only
:class:`RecordingNotifier` (tests) and :class:`LoggingNotifier` (the timer job);
a real channel adapter is out of scope.
"""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.logging import get_logger


@runtime_checkable
class Notifier(Protocol):
    async def send(self, user_id: uuid.UUID | None, template: str, data: dict[str, Any]) -> None:
        """Deliver ``template`` rendered with ``data`` to the user (or ops when None)."""
        ...


@dataclass(frozen=True)
class SentNotification:
    user_id: uuid.UUID | None
    template: str
    data: dict[str, Any]


@dataclass
class RecordingNotifier:
    """A notifier that only remembers what it was asked to send, in order."""

    calls: list[SentNotification] = field(default_factory=list)

    async def send(self, user_id: uuid.UUID | None, template: str, data: dict[str, Any]) -> None:
        self.calls.append(SentNotification(user_id, template, copy.deepcopy(data)))

    def templates(self) -> list[str]:
        return [call.template for call in self.calls]


class LoggingNotifier:
    """A notifier for the timer job: one INFO line per notification.

    It logs only the template and the user id, never ``data``: notification data
    may carry phone numbers (the ``booking.confirmed`` contact release).
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or get_logger("notify")

    async def send(self, user_id: uuid.UUID | None, template: str, data: dict[str, Any]) -> None:
        self._logger.info("notification template=%s user_id=%s", template, user_id or "ops")
