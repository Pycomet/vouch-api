"""Which payment provider the app runs with (R28).

``Settings.payment_provider`` names one; :func:`build_payment_provider` builds it
once per app (``app.state.payment_provider``). Only ``"fake"`` exists in M2, and
it is refused outside development and test, where the builder returns ``None``
and the routes that need a provider answer 503 ``payment_unavailable``.
:func:`payment_provider_named` resolves the ``{provider}`` of a webhook URL
against the configured provider (404 for any other name).
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.providers.payment import FakePaymentProvider, PaymentProvider
from app.schemas.errors import ResourceNotFoundError

_PAYMENT_PROVIDERS: dict[str, Callable[[Settings], PaymentProvider]] = {
    "fake": lambda settings: FakePaymentProvider(
        settings.fake_payment_webhook_secret.get_secret_value()
    ),
}


def build_payment_provider(settings: Settings) -> PaymentProvider | None:
    """The provider the settings name (one per app), or ``None`` when there is none."""
    name = settings.effective_payment_provider
    return None if name is None else _PAYMENT_PROVIDERS[name](settings)


def payment_provider_named(name: str, configured: PaymentProvider | None) -> PaymentProvider:
    """``configured`` if it is called ``name``; otherwise 404 ``resource_not_found``."""
    if configured is None or configured.name != name:
        raise ResourceNotFoundError()
    return configured
