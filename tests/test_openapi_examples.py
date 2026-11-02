"""Every ``/api/v1`` operation documents examples (Step 4 done-when, spec 3.3).

* each operation has a success (2xx) response with a JSON example;
* each error response (4xx/5xx) it declares carries an example too;
* an operation with a request body has an example for it, either on the body's
  content or on the request model's schema (``json_schema_extra``);
* the example payloads validate against the response models they illustrate.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from app.api import examples
from app.core.config import get_settings
from app.main import create_app
from app.schemas.assets import CatalogueSearchPage, PublicAsset
from app.schemas.auth import OtpSent, TokenOut
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
    assert len(operations) >= 18  # health x2, assets x2, bookings x11, home x2, catalogue

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


def _named(responses: dict[str, Any], status: str) -> set[str]:
    """The error codes a response documents (named examples, or the single one)."""
    media = responses[status]["content"]["application/json"]
    if "examples" in media:
        return {example["value"]["error"]["code"] for example in media["examples"].values()}
    return {media["example"]["error"]["code"]}


def _details(responses: dict[str, Any], status: str) -> list[dict[str, Any]]:
    media = responses[status]["content"]["application/json"]
    values = [e["value"] for e in media["examples"].values()] if "examples" in media else []
    values += [media["example"]] if "example" in media else []
    return [value["error"]["details"] for value in values]


BOOKINGS = f"{PREFIX}/bookings"
ACTION = f"{BOOKINGS}/{{booking_id}}"


@pytest.mark.parametrize(
    ("path", "codes"),
    [
        (
            BOOKINGS,
            {
                "dates_unavailable",
                "dates_blocked",
                "tier_insufficient",
                "renter_restricted",
                "self_booking",
            },
        ),
        (
            f"{ACTION}/accept",
            {
                "invalid_transition",
                "hold_expired",
                "vendor_approval_required",
                "tier_insufficient",
                "renter_restricted",
            },
        ),
        (f"{ACTION}/approve", {"invalid_transition", "hold_expired", "approval_not_required"}),
        (
            f"{ACTION}/cancel",
            {"invalid_transition", "cancel_window_closed", "handover_signed", "reason_required"},
        ),
        (f"{ACTION}/handover", {"invalid_transition", "handover_already_signed"}),
        (f"{ACTION}/return", {"invalid_transition", "handover_already_signed"}),
        (f"{ACTION}/claim", {"invalid_transition", "claim_window_closed"}),
        (f"{ACTION}/resolve", {"invalid_transition", "reason_required"}),
        (
            f"{ACTION}/no-show",
            {
                "invalid_transition",
                "no_show_too_early",
                "handover_signed",
                "vendor_signed_handover",
            },
        ),
    ],
)
def test_booking_routes_name_their_guard_codes(schema, path, codes) -> None:
    responses = schema["paths"][path]["post"]["responses"]

    assert codes <= _named(responses, "409")


@pytest.mark.parametrize(
    ("path", "detail_keys"),
    [
        (BOOKINGS, {"max_rental_days", "booking_horizon_days"}),
        (f"{ACTION}/handover", {"max_evidence_urls"}),
        (f"{ACTION}/return", {"max_evidence_urls"}),
        (f"{ACTION}/claim", set()),
        (f"{ACTION}/resolve", set()),
    ],
)
def test_booking_routes_document_their_400s(schema, path, detail_keys) -> None:
    responses = schema["paths"][path]["post"]["responses"]

    assert _named(responses, "400") == {"invalid_request"}
    documented_keys = {key for details in _details(responses, "400") for key in details}
    assert detail_keys <= documented_keys


def test_get_booking_documents_no_409(schema) -> None:
    assert "409" not in schema["paths"][ACTION]["get"]["responses"]


def test_cancelled_example_adds_up() -> None:
    line = examples.CANCELLED_MONEY_LINE
    paid = sum(Decimal(line[part]["amount"]) for part in ("deposit", "contribution", "rental"))
    assert Decimal(line["refund"]["amount"]) + Decimal(line["cancellation_fee"]["amount"]) == paid
    fee = line["cancellation_fee"]
    assert Decimal(fee["vendor_part"]) + Decimal(fee["platform_part"]) == Decimal(fee["amount"])


def test_money_line_statuses_include_cancelled(schema) -> None:
    # A18: a booking that closed before any payment shows "cancelled", not "due".
    components = schema["components"]["schemas"]
    for name in ("DepositLine", "RentalLine", "ContributionLine"):
        assert "cancelled" in components[name]["properties"]["status"]["enum"], name
    decline = schema["paths"][f"{ACTION}/decline"]["post"]["responses"]["200"]
    line = decline["content"]["application/json"]["example"]["money_line"]
    assert {line[part]["status"] for part in ("deposit", "contribution", "rental")} == {"cancelled"}
    assert line["next"] == {"label": "Closed", "amount_due": "0.00"}


def test_no_show_example_refunds_everything(schema) -> None:
    line = examples.NO_SHOW_BOOKING_EXAMPLE["money_line"]
    paid = sum(Decimal(line[part]["amount"]) for part in ("deposit", "contribution", "rental"))
    assert Decimal(line["refund"]["amount"]) == paid
    assert "cancellation_fee" not in line
    assert (line["deposit"]["status"], line["rental"]["status"]) == ("released", "refunded")
    route = schema["paths"][f"{ACTION}/no-show"]["post"]["responses"]["200"]
    example = route["content"]["application/json"]["example"]
    assert (example["status"], example["money_line"]) == ("cancelled_by_vendor", line)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (BookingOut, examples.CANCELLED_BOOKING_EXAMPLE),
        (BookingOut, examples.NO_SHOW_BOOKING_EXAMPLE),
        (BookingOut, examples.DECLINED_BOOKING_EXAMPLE),
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
        (OtpSent, examples.OTP_SENT_EXAMPLE),
        (TokenOut, examples.TOKEN_EXAMPLE),
        (ValidationErrorResponse, examples._VALIDATION_EXAMPLE),
    ],
)
def test_examples_match_their_models(model: type[BaseModel], payload: dict) -> None:
    model.model_validate(payload)


def test_next_payout_description_names_every_closed_source(schema) -> None:
    description = schema["components"]["schemas"]["VendorHome"]["properties"]["next_payout"][
        "description"
    ].lower()

    for source in ("completed", "cancellation fee", "lost", "closed"):
        assert source in description


def test_otp_routes_document_their_errors(schema) -> None:
    request = schema["paths"][f"{PREFIX}/auth/otp/request"]["post"]["responses"]
    verify = schema["paths"][f"{PREFIX}/auth/otp/verify"]["post"]["responses"]

    assert {"202", "422", "429", "503"} <= set(request)
    assert _named(request, "503") == {"otp_unavailable"}
    assert "401" not in request  # the request never reveals anything about the phone
    assert _named(request, "429") == {"rate_limited"}
    assert "Retry-After" in request["429"]["headers"]
    assert {"200", "401", "422"} <= set(verify)
    media = verify["401"]["content"]["application/json"]["example"]
    assert media["error"]["message"] == "The code is wrong or has expired."
