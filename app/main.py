"""Book Price Finder API.

Flow:
  1. Receive either an ISBN, or a title + author, plus the book's
     conservation state and a webhook URL.
  2. Resolve the missing book data via Open Library (title/author from an
     ISBN, or ISBN from title/author).
  3. Ask a llama.cpp instance (OpenAI-compatible /v1/chat/completions) to
     search the Brazilian web through SearXNG for new/used prices in BRL
     and return an estimated value for the (always used) copy as JSON.
  4. Deliver the estimate (isbn, title, author, price, estimated value) --
     or the error -- to the webhook.
"""

from __future__ import annotations

from fastapi import FastAPI, BackgroundTasks
from starlette.concurrency import run_in_threadpool

from app.book_lookup import resolve_book
from app.config import logger
from app.schemas import LookupRequest
from app.valuation import estimate_price
from app.webhook import deliver_webhook

app = FastAPI(title="Book Price Finder", version="0.2.0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


async def _process_lookup(request: LookupRequest) -> None:
    """Process the lookup in the background and deliver results via webhook."""
    try:
        book = await run_in_threadpool(
            resolve_book, request.isbn, request.title, request.author
        )
    except LookupError as exc:
        payload = {
            "isbn": request.isbn,
            "title": request.title,
            "author": request.author,
            "status": "error",
            "error": str(exc),
        }
        await deliver_webhook(str(request.webhook_url), payload)
        return
    except Exception as exc:
        logger.exception(
            "Open Library lookup failed for isbn=%s title=%s", request.isbn, request.title
        )
        payload = {
            "isbn": request.isbn,
            "title": request.title,
            "author": request.author,
            "status": "error",
            "error": f"Open Library lookup failed: {exc}",
        }
        await deliver_webhook(str(request.webhook_url), payload)
        return

    try:
        valuation = await estimate_price(book, request.conservation_state)
        payload = {
            "isbn": book["isbn"],
            "title": book["title"],
            "author": book["author"],
            "conservation_state": request.conservation_state,
            "status": "ok" if valuation.get("estimated_value") is not None else "error",
            "price": (
                valuation["used_price"]
                if valuation.get("used_price") is not None
                else valuation.get("new_price")
            ),
            "new_price": valuation.get("new_price"),
            "used_price": valuation.get("used_price"),
            "estimated_value": valuation.get("estimated_value"),
            "currency": valuation.get("currency"),
            "summary": valuation.get("summary"),
        }
        if payload["status"] == "error":
            payload["error"] = "No BRL price found for this book in Brazilian listings"
    except Exception as exc:
        logger.exception("Price estimation failed for isbn=%s", book["isbn"])
        payload = {
            **book,
            "conservation_state": request.conservation_state,
            "status": "error",
            "error": f"Price estimation failed: {type(exc).__name__}: {exc}",
        }

    await deliver_webhook(str(request.webhook_url), payload)


@app.post("/lookup")
async def lookup(request: LookupRequest, background_tasks: BackgroundTasks) -> dict:
    logger.info(f"Lookup request: isbn={request.isbn}, title={request.title}, author={request.author}")
    background_tasks.add_task(_process_lookup, request)
    return {"status": "accepted", "message": "Request received, processing in background"}
