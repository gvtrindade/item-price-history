"""Request/response schemas for the lookup endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, HttpUrl, field_validator, model_validator

from app.config import CODE_RE, DISC_CODE_RE

# The caller states what the product is; the barcode shape is never used to
# guess it.
ProductType = Literal["book", "cd", "dvd"]


class LookupRequest(BaseModel):
    product_type: ProductType = "book"
    code: str | None = None
    title: str | None = None
    author: str | None = None
    conservation_state: str
    webhook_url: HttpUrl
    # When the caller already knows the price, only resolve the missing
    # title/author and skip the price estimation entirely.
    skip_valuation: bool = False

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().replace("-", "").replace(" ", "")
        if not CODE_RE.match(value):
            raise ValueError("code must be a valid ISBN-10 or ISBN-13")
        return value.upper()

    @field_validator("conservation_state")
    @classmethod
    def normalize_state(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("conservation_state must not be empty")
        return value

    @model_validator(mode="after")
    def require_identifier(self) -> "LookupRequest":
        if not self.code and not (self.title and self.author):
            raise ValueError("provide either 'code' or both 'title' and 'author'")
        return self


class DiscogsLookupRequest(BaseModel):
    """CD/DVD barcode lookup. Not an ISBN, so no ISBN validation."""

    product_type: ProductType = "cd"
    code: str

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        value = value.strip().replace("-", "").replace(" ", "")
        if not DISC_CODE_RE.match(value):
            raise ValueError("code must be an 8-14 digit EAN/UPC barcode")
        return value
