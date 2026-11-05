"""Response body of the vendor's own profile (M3a 4.3)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class VendorProfileOut(BaseModel):
    """The vendor's profile. Read-only: bank details change only through an admin."""

    status: str = Field(description="pending, active or suspended.")
    business_type: str = Field(description="rental_house or individual.")
    is_sub_broker: bool
    business_name: str | None
    bank_name: str | None
    account_name: str | None
    account_last4: str | None = Field(description="Last four digits only.")
    has_recipient_code: bool = Field(description="Whether payouts are set up; never the code.")
