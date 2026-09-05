"""Request/response schemas for the lookup endpoint."""

from __future__ import annotations

from pydantic import BaseModel, HttpUrl, field_validator, model_validator

from app.config import ISBN_RE


class LookupRequest(BaseModel):
    isbn: str | None = None
    title: str | None = None
    author: str | None = None
    conservation_state: str
    webhook_url: HttpUrl

    @field_validator("isbn")
    @classmethod
    def normalize_isbn(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().replace("-", "").replace(" ", "")
        if not ISBN_RE.match(value):
            raise ValueError("isbn must be a valid ISBN-10 or ISBN-13")
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
        if not self.isbn and not (self.title and self.author):
            raise ValueError("provide either 'isbn' or both 'title' and 'author'")
        return self
