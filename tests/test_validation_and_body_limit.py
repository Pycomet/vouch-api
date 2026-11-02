"""422 bodies never echo input; oversize request bodies are 413 (fix round 1)."""

from __future__ import annotations

import base64
import json

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.config import Settings
from app.db.session import get_session
from app.main import create_app

PHONE = "08031234567"
ID_NUMBER = "12345678901"
VERIFY = "/api/v1/auth/otp/verify"
KYC = "/api/v1/me/kyc"
CAP = 1024 * 1024  # the smallest allowed cap


def _app(**over):
    return create_app(Settings(_env_file=None, max_request_bytes=CAP, **over))


@pytest.fixture
async def client():
    app = _app()

    async def no_session():
        yield None  # validation and the body cap fire before any query

    app.dependency_overrides[get_session] = no_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def _no_input(node) -> bool:
    if isinstance(node, dict):
        return "input" not in node and all(_no_input(v) for v in node.values())
    if isinstance(node, list):
        return all(_no_input(v) for v in node)
    return True


async def test_otp_422_does_not_echo_the_phone(client: AsyncClient) -> None:
    response = await client.post(VERIFY, json={"phone": PHONE, "code": "abc"})

    assert response.status_code == 422
    assert PHONE not in response.text
    assert set(response.json()) == {"detail"}
    item = response.json()["detail"][0]
    assert {"type", "loc", "msg"} <= set(item)


async def test_no_422_item_has_an_input_key(client: AsyncClient) -> None:
    for payload in ({}, {"phone": 5}, {"phone": PHONE, "code": "x", "role": "admin"}):
        response = await client.post(VERIFY, json=payload)
        assert response.status_code == 422
        assert _no_input(response.json())


async def test_kyc_missing_field_does_not_echo_the_request(client: AsyncClient) -> None:
    selfie = base64.b64encode(b"SELFIE-BYTES-0123456789").decode()
    payload = {"id_type": "nin", "id_number": ID_NUMBER, "selfie_b64": selfie}
    # Auth fails first with no token (401); the validation error shows up with a bad
    # token too, so drive the schema directly as well.
    from app.schemas.kyc import KycRequest

    with pytest.raises(ValidationError) as caught:
        KycRequest.model_validate(payload)
    assert ID_NUMBER not in str(caught.value.errors(include_input=False))

    response = await client.post(KYC, json=payload)
    assert ID_NUMBER not in response.text
    assert selfie not in response.text


async def test_content_length_over_the_cap_is_413(client: AsyncClient) -> None:
    response = await client.post(VERIFY, content=b"x" * (CAP + 1))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert response.headers["X-Request-ID"]


async def test_body_at_the_cap_is_not_rejected(client: AsyncClient) -> None:
    response = await client.post(VERIFY, content=b"x" * CAP)

    assert response.status_code != 413


async def test_chunked_body_over_the_cap_is_413(client: AsyncClient) -> None:
    async def chunks():
        for _ in range(4):
            yield b"x" * (CAP // 2)

    request = client.build_request("POST", VERIFY, content=chunks())
    assert "content-length" not in request.headers
    response = await client.send(request)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_chunked_body_under_the_cap_passes(client: AsyncClient) -> None:
    async def chunks():
        yield json.dumps({"phone": PHONE, "code": "abc"}).encode()

    response = await client.post(VERIFY, content=chunks())

    assert response.status_code == 422


def test_max_request_bytes_is_validated() -> None:
    assert Settings(_env_file=None).max_request_bytes == 8 * 1024 * 1024
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_request_bytes=CAP - 1)
