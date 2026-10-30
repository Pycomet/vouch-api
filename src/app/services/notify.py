"""Outbound notifications behind one interface.

Services call ``notifier.send(user_id, template, data)``. ``user_id`` is ``None``
for operations alerts, whose templates are named ``ops.<event>``. M2 ships only
:class:`RecordingNotifier`; a real channel adapter is out of scope.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


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
