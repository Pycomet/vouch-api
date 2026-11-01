"""Every ``/api/v1`` operation documents examples (Step 4 done-when, spec 3.3).

* each operation has a success (2xx) response with a JSON example;
* each error response (4xx/5xx) it declares carries an example too;
* an operation with a request body has an example for it, either on the body's
  content or on the request model's schema (``json_schema_extra``);
* the example payloads validate against the response models they illustrate.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from app.api import examples
from app.core.config import get_settings
from app.main import create_app
from app.schemas.assets import CatalogueSearchPage, PublicAsset
from app.schemas.bookings import BookingOut
from app.schemas.errors import ErrorEnvelope, ValidationErrorResponse
from app.schemas.health import ReadinessStatus, ServiceStatus
from app.schemas.home import RenterHome, VendorHome
from app.schemas.pagination import Page

PREFIX = get_settings().api_v1_prefix
METHODS = {"get", "post", "put", "patch", "delete"}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return create_app().openapi()


def _has_example(content: dict[str, Any] | None) -> bool:
    media = (content or {}).get("application/json", {})
    return bool(media.get("example") or media.get("examples"))


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> list[dict[str, Any]]:
    """The component schemas a body schema points at (through ``anyOf``)."""
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return [schema["components"]["schemas"][name]]
    found = []
    for option in node.get("anyOf", []):
        found.extend(_resolve(schema, option))
    return found


def _operations(schema: dict[str, Any]):
    for path, item in schema["paths"].items():
        if not path.startswith(PREFIX):
            continue
        for method, operation in item.items():
            if method in METHODS:
                yield f"{method.upper()} {path}", operation


def test_every_operation_has_examples(schema) -> None:
    missing: list[str] = []
    operations = list(_operations(schema))
    assert len(operations) >= 17  # health x2, assets x2, bookings x10, home x2, catalogue

    for name, operation in operations:
        responses = operation["responses"]
        successes = [code for code in responses if code.startswith("2")]
        if not any(_has_example(responses[code].get("content")) for code in successes):
            missing.append(f"{name}: no 2xx example")
        for code, response in responses.items():
            if code[0] in "45" and not _has_example(response.get("content")):
                missing.append(f"{name}: no {code} example")
        body = operation.get("requestBody")
        if body is not None and not _has_example(body.get("content")):
            media = body["content"]["application/json"]["schema"]
            targets = _resolve(schema, media)
            if not targets or not all(target.get("examples") for target in targets):
                missing.append(f"{name}: no request body example")

    assert missing == []


def test_documented_errors_include_auth_and_city(schema) -> None:
    paths = schema["paths"]
    home = paths[f"{PREFIX}/vendor/home"]["get"]["responses"]
    assert {"200", "401", "403"} <= set(home)
    search = paths[f"{PREFIX}/catalogue/search"]["get"]["responses"]["400"]
    named = search["content"]["application/json"]["examples"]
    assert {"city_not_launched", "invalid_request", "invalid_cursor"} <= set(named)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (PublicAsset, examples.PUBLIC_ASSET_EXAMPLE),
        (Page[PublicAsset], examples.PAGE_EXAMPLE),
        (CatalogueSearchPage, examples.SEARCH_EXAMPLE),
        (BookingOut, examples.BOOKING_EXAMPLE),
        (BookingOut, examples.REQUESTED_BOOKING_EXAMPLE),
        (RenterHome, examples.RENTER_HOME_EXAMPLE),
        (VendorHome, examples.VENDOR_HOME_EXAMPLE),
        (ServiceStatus, examples.LIVE_EXAMPLE),
        (ReadinessStatus, examples.READY_EXAMPLE),
        (ReadinessStatus, examples.NOT_READY_EXAMPLE),
        (ErrorEnvelope, examples.error_body("forbidden", "No.")),
        (ValidationErrorResponse, examples._VALIDATION_EXAMPLE),
    ],
)
def test_examples_match_their_models(model: type[BaseModel], payload: dict) -> None:
    model.model_validate(payload)
