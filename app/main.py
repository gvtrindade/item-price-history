"""Book Price Finder API.

Receives an ISBN + webhook URL, looks up the book title on Open Library
(via openlibrary-client) and delivers the result -- or the error -- to the
webhook.
"""

from __future__ import annotations

import logging
import re

import httpx
from fastapi import FastAPI
from olclient.openlibrary import OpenLibrary
from pydantic import BaseModel, HttpUrl, field_validator
from starlette.concurrency import run_in_threadpool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("book_price_finder")

WEBHOOK_TIMEOUT_SECONDS = 10.0

ISBN_RE = re.compile(r"^(?:\d{9}[\dXx]|\d{13})$")

app = FastAPI(title="Book Price Finder", version="0.1.0")


class LookupRequest(BaseModel):
    isbn: str
    webhook_url: HttpUrl

    @field_validator("isbn")
    @classmethod
    def normalize_isbn(cls, value: str) -> str:
        value = value.strip().replace("-", "").replace(" ", "")
        if not ISBN_RE.match(value):
            raise ValueError("isbn must be a valid ISBN-10 or ISBN-13")
        return value.upper()


def fetch_title(isbn: str) -> str | None:
    """Blocking lookup via openlibrary-client. Returns None if not found."""
    ol = OpenLibrary()
    edition = ol.Edition.get(isbn=isbn)
    if edition is None:
        return None
    return edition.title


async def deliver_webhook(url: str, payload: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.error("Webhook delivery to %s failed: %s", url, exc)
        return False


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/lookup")
async def lookup(request: LookupRequest) -> dict:
    isbn = request.isbn
    try:
        title = await run_in_threadpool(fetch_title, isbn)
        if title is None:
            payload = {
                "isbn": isbn,
                "status": "error",
                "error": "Book not found in Open Library",
            }
        else:
            payload = {"isbn": isbn, "status": "ok", "title": title}
    except Exception as exc:
        logger.exception("Open Library lookup failed for isbn=%s", isbn)
        payload = {
            "isbn": isbn,
            "status": "error",
            "error": f"Open Library lookup failed: {exc}",
        }

    delivered = await deliver_webhook(str(request.webhook_url), payload)
    return {"webhook_delivered": delivered, **payload}
