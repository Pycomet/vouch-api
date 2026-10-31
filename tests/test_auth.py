"""Access tokens and the auth dependencies (Task 4.1).

No database: a tiny test-only router exercises ``get_current_actor`` and
``require_role`` through the real app, error envelope included.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.api.deps import get_current_actor, require_role
from app.core.config import Environment, Settings
from app.core.security import decode_token, issue_token
from app.db.models import ActorRole
from app.main import create_app
from app.schemas.errors import ForbiddenError, UnauthenticatedError, VouchAPIError
from app.services.audit import Actor
from tests.helpers.auth import bearer

USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000a001")


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "environment": Environment.TEST,
        "database_url": "postgresql+asyncpg://postgres:postgres@localhost:5432/vouch_test",
        "jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256"),
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def auth_settings() -> Settings:
    return _settings()


@pytest.fixture
def auth_client(auth_settings: Settings):
    app = create_app(auth_settings)
    router = APIRouter(prefix="/test-auth")

    @router.get("/me")
    async def _me(actor: Actor = Depends(get_current_actor)) -> dict[str, str]:  # noqa: B008
        return {"user_id": str(actor.user_id), "role": actor.role.value}

    @router.get("/vendor-only")
    async def _vendor_only(
        actor: Actor = Depends(require_role(ActorRole.vendor, ActorRole.admin)),  # noqa: B008
    ) -> dict[str, str]:
        return {"role": actor.role.value}

    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


# ---------------------------------------------------------------------------
# issue_token / decode_token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [ActorRole.renter, ActorRole.vendor, ActorRole.admin])
def test_round_trip(auth_settings: Settings, role: ActorRole) -> None:
    token = issue_token(USER_ID, role, auth_settings)

    assert decode_token(token, auth_settings) == Actor(user_id=USER_ID, role=role)


def test_token_claims(auth_settings: Settings) -> None:
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    token = issue_token(USER_ID, ActorRole.renter, auth_settings, now=now)

    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert header["alg"] == "HS256"
    assert claims == {
        "sub": str(USER_ID),
        "role": "renter",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=60 * 24 * 7)).timestamp()),
    }


def test_system_role_gets_no_token(auth_settings: Settings) -> None:
    with pytest.raises(ValueError, match="system"):
        issue_token(USER_ID, ActorRole.system, auth_settings)


def test_expired_token_rejected(auth_settings: Settings) -> None:
    long_ago = datetime.now(UTC) - timedelta(minutes=auth_settings.jwt_ttl_minutes + 1)
    token = issue_token(USER_ID, ActorRole.renter, auth_settings, now=long_ago)

    with pytest.raises(UnauthenticatedError):
        decode_token(token, auth_settings)


def test_token_signed_with_another_secret_rejected(auth_settings: Settings) -> None:
    other = _settings(jwt_secret=SecretStr("another-secret-that-is-long-enough-too"))
    token = issue_token(USER_ID, ActorRole.renter, other)

    with pytest.raises(UnauthenticatedError):
        decode_token(token, auth_settings)


def test_tampered_payload_rejected(auth_settings: Settings) -> None:
    header, _, signature = issue_token(USER_ID, ActorRole.renter, auth_settings).split(".")
    admin = issue_token(USER_ID, ActorRole.admin, _settings(jwt_secret=SecretStr("x" * 40)))
    forged = f"{header}.{admin.split('.')[1]}.{signature}"

    with pytest.raises(UnauthenticatedError):
        decode_token(forged, auth_settings)


def _signed(claims: dict, settings: Settings, algorithm: str = "HS256") -> str:
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=algorithm)


def _claims(**overrides) -> dict:
    now = datetime.now(UTC)
    claims = {"sub": str(USER_ID), "role": "renter", "iat": now, "exp": now + timedelta(hours=1)}
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


@pytest.mark.parametrize(
    "claims",
    [
        _claims(role="superuser"),
        _claims(role="system"),
        _claims(sub="not-a-uuid"),
        _claims(sub=None),
        _claims(role=None),
        _claims(exp=None),
        _claims(iat=None),
    ],
    ids=["unknown-role", "system-role", "bad-sub", "no-sub", "no-role", "no-exp", "no-iat"],
)
def test_bad_claims_rejected(auth_settings: Settings, claims: dict) -> None:
    with pytest.raises(UnauthenticatedError):
        decode_token(_signed(claims, auth_settings), auth_settings)


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "Bearer x"])
def test_malformed_token_rejected(auth_settings: Settings, token: str) -> None:
    with pytest.raises(UnauthenticatedError):
        decode_token(token, auth_settings)


def test_other_algorithm_rejected() -> None:
    settings = _settings(jwt_secret=SecretStr("k" * 64))  # long enough for HS512
    token = _signed(_claims(), settings, algorithm="HS512")

    with pytest.raises(UnauthenticatedError):
        decode_token(token, settings)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_jwt_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.jwt_secret.get_secret_value() == "change-me"
    assert settings.jwt_ttl_minutes == 60 * 24 * 7
    assert "change-me" not in repr(settings)


def test_production_refuses_default_secret() -> None:
    with pytest.raises(ValidationError, match="jwt_secret must be set in production"):
        Settings(_env_file=None, environment=Environment.PRODUCTION)


def test_production_accepts_a_real_secret() -> None:
    settings = Settings(
        _env_file=None,
        environment=Environment.PRODUCTION,
        jwt_secret=SecretStr("a-real-production-secret-of-decent-length"),
    )

    assert settings.is_production


@pytest.mark.parametrize(
    "secret",
    ["", "   ", "short", "x" * 31, " " + "x" * 30 + " "],
    ids=["empty", "blank", "short", "31", "padded"],
)
def test_production_refuses_a_weak_secret(secret: str) -> None:
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(_env_file=None, environment=Environment.PRODUCTION, jwt_secret=SecretStr(secret))


def test_production_accepts_a_32_byte_secret() -> None:
    settings = Settings(
        _env_file=None, environment=Environment.PRODUCTION, jwt_secret=SecretStr("k" * 32)
    )

    assert settings.jwt_secret.get_secret_value() == "k" * 32


def test_production_refuses_an_empty_secret_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty env value is ignored (env_ignore_empty), so the placeholder applies.
    monkeypatch.setenv("VOUCH_JWT_SECRET", "")

    with pytest.raises(ValidationError, match="jwt_secret"):
        Settings(_env_file=None, environment=Environment.PRODUCTION)


@pytest.mark.parametrize("secret", ["", "short"])
def test_development_does_not_check_secret_length(secret: str) -> None:
    assert Settings(_env_file=None, jwt_secret=SecretStr(secret)).jwt_secret is not None


def test_jwt_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_JWT_SECRET", "from-the-environment")
    monkeypatch.setenv("VOUCH_JWT_TTL_MINUTES", "15")

    settings = Settings(_env_file=None)

    assert settings.jwt_secret.get_secret_value() == "from-the-environment"
    assert settings.jwt_ttl_minutes == 15


def test_ttl_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_ttl_minutes=0)


# ---------------------------------------------------------------------------
# Dependencies, through a route
# ---------------------------------------------------------------------------


def test_route_returns_the_actor(auth_client: TestClient, auth_settings: Settings) -> None:
    response = auth_client.get(
        "/test-auth/me", headers=bearer(USER_ID, ActorRole.renter, auth_settings)
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": str(USER_ID), "role": "renter"}


def test_missing_header_401(auth_client: TestClient) -> None:
    response = auth_client.get("/test-auth/me")

    _assert_error(response, 401, "unauthenticated")
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer not-a-token", "Token abc"]
)
def test_bad_header_401(auth_client: TestClient, authorization: str) -> None:
    response = auth_client.get("/test-auth/me", headers={"Authorization": authorization})

    _assert_error(response, 401, "unauthenticated")


def test_expired_token_401(auth_client: TestClient, auth_settings: Settings) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=30)
    token = issue_token(USER_ID, ActorRole.renter, auth_settings, now=long_ago)

    response = auth_client.get("/test-auth/me", headers={"Authorization": f"Bearer {token}"})

    _assert_error(response, 401, "unauthenticated")


def test_tampered_token_401(auth_client: TestClient, auth_settings: Settings) -> None:
    token = issue_token(USER_ID, ActorRole.renter, auth_settings)
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")

    response = auth_client.get("/test-auth/me", headers={"Authorization": f"Bearer {tampered}"})

    _assert_error(response, 401, "unauthenticated")


def test_token_from_another_app_secret_401(auth_client: TestClient) -> None:
    other = _settings(jwt_secret=SecretStr("a-different-app-secret-long-enough"))

    response = auth_client.get("/test-auth/me", headers=bearer(USER_ID, ActorRole.renter, other))

    _assert_error(response, 401, "unauthenticated")


def test_wrong_role_403(auth_client: TestClient, auth_settings: Settings) -> None:
    headers = bearer(USER_ID, ActorRole.renter, auth_settings)

    response = auth_client.get("/test-auth/vendor-only", headers=headers)

    _assert_error(response, 403, "forbidden")


@pytest.mark.parametrize("role", [ActorRole.vendor, ActorRole.admin])
def test_allowed_role_200(auth_client: TestClient, auth_settings: Settings, role) -> None:
    response = auth_client.get(
        "/test-auth/vendor-only", headers=bearer(USER_ID, role, auth_settings)
    )

    assert response.status_code == 200
    assert response.json() == {"role": role.value}


def test_role_route_without_token_401(auth_client: TestClient) -> None:
    _assert_error(auth_client.get("/test-auth/vendor-only"), 401, "unauthenticated")


@pytest.mark.parametrize("error", [VouchAPIError, UnauthenticatedError, ForbiddenError])
def test_error_headers_are_read_only(error: type[VouchAPIError]) -> None:
    with pytest.raises(TypeError):
        error.headers["X-Leak"] = "1"  # type: ignore[index]

    assert "X-Leak" not in VouchAPIError.headers


def test_require_role_needs_a_role() -> None:
    with pytest.raises(ValueError):
        require_role()


def test_bearer_scheme_in_openapi(auth_client: TestClient) -> None:
    schema = auth_client.get("/openapi.json").json()

    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
