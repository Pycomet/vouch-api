"""Model structure tests for users and kyc_profiles (no live DB)."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, Text, UniqueConstraint

import app.db.models  # noqa: F401 — registers all models
from app.db.base import Base
from app.db.base import TimestampMixin as BaseTimestampMixin
from app.db.mixins import TimestampMixin
from app.db.models.user import TrustTier, User, VerificationRequirement, trust_tier_policy


def _table(name: str):
    return Base.metadata.tables[name]


def test_users_table_in_metadata() -> None:
    assert "users" in Base.metadata.tables


def test_kyc_profiles_table_in_metadata() -> None:
    assert "kyc_profiles" in Base.metadata.tables


def test_users_phone_is_indexed_and_unique() -> None:
    phone_col = _table("users").c["phone"]
    assert phone_col.unique is True
    # index defined via __table_args__
    index_names = {idx.name for idx in _table("users").indexes}
    assert "ix_users_phone" in index_names


def test_kyc_profiles_user_id_fk_references_users() -> None:
    user_id_col = _table("kyc_profiles").c["user_id"]
    fk_targets = {fk.target_fullname for fk in user_id_col.foreign_keys}
    assert "users.id" in fk_targets


def test_trust_tier_values() -> None:
    assert [(tier.name, tier.value) for tier in TrustTier] == [
        ("phone_verified", 0),
        ("identity_verified", 1),
        ("established", 2),
        ("vendor_approved", 3),
    ]


def test_identity_verified_requires_national_id_liveness_and_address() -> None:
    assert trust_tier_policy(TrustTier.phone_verified).required_verifications == frozenset()
    assert trust_tier_policy(TrustTier.identity_verified).required_verifications == frozenset(
        {
            VerificationRequirement.national_id,
            VerificationRequirement.liveness,
            VerificationRequirement.address,
        }
    )
    for tier in (TrustTier.established, TrustTier.vendor_approved):
        assert trust_tier_policy(tier).required_verifications == frozenset(VerificationRequirement)


def test_users_restriction_columns() -> None:
    table = _table("users")
    assert table.c["restricted_at"].nullable
    assert table.c["restricted_at"].type.timezone
    assert isinstance(table.c["restricted_reason"].type, Text)
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks["ck_users_restriction_has_reason"] == (
        "restricted_at IS NULL OR restricted_reason IS NOT NULL"
    )


def test_kyc_profiles_tier_uses_explicit_trust_tier_default() -> None:
    tier_col = _table("kyc_profiles").c["tier"]
    assert isinstance(tier_col.type, Integer)
    assert tier_col.default.arg is TrustTier.phone_verified


def test_kyc_profile_invariants_are_persisted() -> None:
    table = _table("kyc_profiles")
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert checks["ck_kyc_profiles_supported_tier"] == "tier BETWEEN 0 AND 1"
    assert (
        checks["ck_kyc_profiles_verified_at_required"]
        == "status <> 'verified' OR verified_at IS NOT NULL"
    )
    assert ("provider_reference",) in unique_columns


def test_timestamp_mixin_has_one_canonical_definition() -> None:
    assert BaseTimestampMixin is TimestampMixin


def test_user_has_handover_signer_relationships() -> None:
    relationships = User.__mapper__.relationships
    assert relationships["handovers_signed_as_vendor"].back_populates == "vendor_signer"
    assert relationships["handovers_signed_as_renter"].back_populates == "renter_signer"


def test_kyc_profile_status_tier_and_timestamp_are_consistent() -> None:
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in _table("kyc_profiles").constraints
        if isinstance(constraint, CheckConstraint)
    }

    consistency = checks["ck_kyc_profiles_verification_consistency"]
    assert "status = 'verified'" in consistency
    assert "verified_at IS NOT NULL" in consistency
    assert "tier = 1" in consistency
    assert "status <> 'verified'" in consistency
    assert "verified_at IS NULL" in consistency
    assert "tier = 0" in consistency
