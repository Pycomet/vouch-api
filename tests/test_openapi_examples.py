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
from app.schemas.blocks import Block, BlockList
from app.schemas.bookings import BookingOut, MoneyLine
from app.schemas.errors import ErrorEnvelope, ValidationErrorResponse
from app.schemas.health import ReadinessStatus, ServiceStatus
from app.schemas.home import RenterHome, VendorHome
from app.schemas.listings import OwnAsset
from app.schemas.pagination import Page
from app.schemas.webhooks import WebhookAck

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
        # A 204 has no body to illustrate (DELETE /vendor/assets/{id}).
        no_body = successes == ["204"] and "content" not in responses["204"]
        if not no_body and not any(
            _has_example(responses[code].get("content")) for code in successes
        ):
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
                "vendor_not_active",
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


LISTINGS = f"{PREFIX}/vendor/assets"
LISTING = f"{LISTINGS}/{{asset_id}}"


@pytest.mark.parametrize(
    ("method", "path", "codes"),
    [
        ("post", LISTINGS, {"vendor_not_active", "catalogue_item_inactive", "duplicate_serial"}),
        (
            "patch",
            LISTING,
            {
                "vendor_not_active",
                "listing_locked",
                "listing_incomplete",
                "catalogue_item_inactive",
                "duplicate_serial",
            },
        ),
        (
            "post",
            f"{LISTING}/submit",
            {"vendor_not_active", "invalid_transition", "listing_incomplete"},
        ),
        ("post", f"{LISTING}/pause", {"vendor_not_active", "invalid_transition"}),
        ("post", f"{LISTING}/resume", {"vendor_not_active", "invalid_transition"}),
        ("delete", LISTING, {"vendor_not_active", "has_live_bookings"}),
    ],
)
def test_listing_routes_name_their_409s(schema, method, path, codes) -> None:
    responses = schema["paths"][path][method]["responses"]

    assert codes <= _named(responses, "409")
    assert {"401", "403", "422"} <= set(responses)


BLOCKS = f"{LISTING}/blocks"


@pytest.mark.parametrize(
    ("method", "path", "codes"),
    [
        ("post", BLOCKS, {"vendor_not_active", "dates_blocked", "dates_unavailable"}),
        ("delete", f"{BLOCKS}/{{block_id}}", {"vendor_not_active"}),
    ],
)
def test_block_routes_name_their_409s(schema, method, path, codes) -> None:
    responses = schema["paths"][path][method]["responses"]

    assert codes <= _named(responses, "409")
    assert {"401", "403", "404", "422"} <= set(responses)


def test_block_routes_document_400_and_bodiless_delete(schema) -> None:
    create = schema["paths"][BLOCKS]["post"]["responses"]
    listing = schema["paths"][BLOCKS]["get"]["responses"]
    remove = schema["paths"][f"{BLOCKS}/{{block_id}}"]["delete"]["responses"]

    assert _named(create, "400") == {"invalid_request"}
    assert "409" not in listing
    assert "content" not in remove["204"]


def test_listing_transition_example_names_the_listing(schema) -> None:
    media = schema["paths"][f"{LISTING}/resume"]["post"]["responses"]["409"]["content"]
    error = media["application/json"]["examples"]["invalid_transition"]["value"]["error"]

    assert error["message"] == "This listing cannot make that transition."
    assert error["details"] == {"from": "paused", "event": "resume"}


def test_delete_listing_is_a_bodiless_204(schema) -> None:
    responses = schema["paths"][LISTING]["delete"]["responses"]

    assert "content" not in responses["204"]


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


def test_late_payment_example_returns_everything_and_owes_nothing(schema) -> None:
    line = examples.LATE_PAYMENT_MONEY_LINE
    MoneyLine.model_validate(line)
    assert {line[part]["status"] for part in ("deposit", "contribution", "rental")} == {"cancelled"}
    paid = sum(Decimal(line[part]["amount"]) for part in ("deposit", "contribution", "rental"))
    assert line["returned_payments"] == [{"amount": str(paid), "reason": "late_payment"}]
    assert "refund" not in line
    home = schema["paths"][f"{PREFIX}/me/home"]["get"]["responses"]["200"]
    past = home["content"]["application/json"]["example"]["past"]
    assert line in [card["money_line"] for card in past]


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
        (BookingOut, examples.ACCEPTED_BOOKING_EXAMPLE),
        (BookingOut, examples.RENTER_ACCEPTED_BOOKING_EXAMPLE),
        (RenterHome, examples.RENTER_HOME_EXAMPLE),
        (VendorHome, examples.VENDOR_HOME_EXAMPLE),
        (ServiceStatus, examples.LIVE_EXAMPLE),
        (ReadinessStatus, examples.READY_EXAMPLE),
        (ReadinessStatus, examples.NOT_READY_EXAMPLE),
        (ErrorEnvelope, examples.error_body("forbidden", "No.")),
        (OtpSent, examples.OTP_SENT_EXAMPLE),
        (TokenOut, examples.TOKEN_EXAMPLE),
        (ValidationErrorResponse, examples._VALIDATION_EXAMPLE),
        (OwnAsset, examples.OWN_ASSET_EXAMPLE),
        (OwnAsset, examples.PAUSED_ASSET_EXAMPLE),
        (Page[OwnAsset], examples.OWN_ASSET_PAGE_EXAMPLE),
        (Block, examples.BLOCK_EXAMPLE),
        (BlockList, examples.BLOCK_LIST_EXAMPLE),
    ],
)
def test_examples_match_their_models(model: type[BaseModel], payload: dict) -> None:
    model.model_validate(payload)


def test_accept_documents_the_payment_provider_errors(schema) -> None:
    responses = schema["paths"][f"{ACTION}/accept"]["post"]["responses"]

    assert _named(responses, "502") == {"payment_provider_error"}
    assert _named(responses, "503") == {"payment_unavailable"}
    example = responses["200"]["content"]["application/json"]["example"]
    assert example["status"] == "accepted"
    # The accepting vendor's view (OpenAPI examples drop null values).
    assert example.get("payment_instructions") is None


def test_webhook_documents_each_answer(schema) -> None:
    operation = schema["paths"][f"{PREFIX}/webhooks/{{provider}}"]["post"]
    responses = operation["responses"]

    named = responses["200"]["content"]["application/json"]["examples"]
    assert set(named) == {"processed", "duplicate", "processing_error"}
    for example in named.values():
        WebhookAck.model_validate(example["value"])
    assert _named(responses, "401") == {"invalid_signature"}
    assert _named(responses, "404") == {"resource_not_found"}
    assert _named(responses, "413") == {"payload_too_large"}
    assert "security" not in operation  # the signature, not a bearer token
    body = operation["requestBody"]["content"]["application/json"]["example"]
    assert body["type"] == "payment_captured"


def test_get_booking_example_shows_the_renters_payment_instructions(schema) -> None:
    responses = schema["paths"][ACTION]["get"]["responses"]
    example = responses["200"]["content"]["application/json"]["example"]
    instructions = example["payment_instructions"]

    assert example["status"] == "accepted"
    assert instructions["amount"] == example["money_line"]["next"]["amount_due"]
    assert schema["components"]["schemas"]["PaymentInstructions"]["required"]


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
