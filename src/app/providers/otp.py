"""Phone OTP delivery behind :class:`OtpSender` (spec 4, R26).

:class:`LoggingOtpSender` writes one INFO line per code sent, naming only the
masked phone: it NEVER logs the code. :class:`FakeOtpSender` also remembers each
``(phone, code)`` in memory so tests can read the code back. ``Settings.otp_sender``
picks one through :func:`build_otp_sender`; only ``"fake"`` exists until a real
SMS adapter is chosen.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from app.core.config import Settings
from app.core.logging import get_logger


@runtime_checkable
class OtpSender(Protocol):
    async def send(self, phone: str, code: str) -> None:
        """Deliver ``code`` to the E.164 ``phone``."""
        ...


def mask_phone(phone: str) -> str:
    """``phone`` with all but the country code and the last four digits hidden."""
    if len(phone) <= 8:
        return "*" * len(phone)
    return phone[:4] + "*" * (len(phone) - 8) + phone[-4:]


class LoggingOtpSender:
    """Logs that a code went to a (masked) phone. The code itself is never logged."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or get_logger("otp")

    async def send(self, phone: str, code: str) -> None:
        self._logger.info("otp sent phone=%s", mask_phone(phone))


class FakeOtpSender(LoggingOtpSender):
    """Records every code it is asked to send, in order, in memory."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        super().__init__(logger)
        self.sent: list[tuple[str, str]] = []

    async def send(self, phone: str, code: str) -> None:
        self.sent.append((phone, code))
        await super().send(phone, code)

    def last_code(self, phone: str) -> str:
        """The most recent code sent to ``phone``; ``LookupError`` if none was."""
        for sent_to, code in reversed(self.sent):
            if sent_to == phone:
                return code
        raise LookupError("no code was sent to this phone")


_SENDERS: dict[str, type[LoggingOtpSender]] = {"fake": FakeOtpSender}


def build_otp_sender(settings: Settings) -> OtpSender:
    """The sender ``settings.otp_sender`` names (one per app)."""
    return _SENDERS[settings.otp_sender]()
