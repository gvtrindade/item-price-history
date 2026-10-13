"""Product Price Finder API.

Flow:
  1. Receive either a code, or a title + author, plus the product's
     conservation state and a webhook URL.
  2. Resolve the missing product data via Open Library (title/author from a
     code, or code from title/author).
  3. Ask a llama.cpp instance (OpenAI-compatible /v1/chat/completions) to
     search the Brazilian web through SearXNG for new/used prices in BRL
     and return an estimated value for the (always used) copy as JSON.
  4. Deliver the estimate (code, title, author, price, estimated value) --
     or the error -- to the webhook.
"""

from __future__ import annotations

from fastapi import FastAPI, BackgroundTasks
from starlette.concurrency import run_in_threadpool

from app.product_lookup import resolve_product
from app.config import logger
from app.schemas import LookupRequest
from app.valuation import estimate_price
from app.webhook import deliver_webhook

app = FastAPI(title="Product Price Finder", version="0.2.0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


async def _process_lookup(request: LookupRequest) -> None:
    """Process the lookup in the background and deliver results via webhook."""
    try:
        product = await run_in_threadpool(
            resolve_product, request.code, request.title, request.author
        )
    except LookupError as exc:
        logger.warning(
            "Product not found in Open Library: code=%s title=%s author=%s (%s)",
            request.code, request.title, request.author, exc,
        )
        payload = {
            "code": request.code or "",
            "title": request.title or "",
            "author": request.author or "",
            "conservation_state": request.conservation_state,
            "status": "not_found",
        }
        await deliver_webhook(str(request.webhook_url), payload)
        return
    except Exception as exc:
        logger.exception(
            "Open Library lookup failed for code=%s title=%s", request.code, request.title
        )
        payload = {
            "code": request.code or "",
            "title": request.title or "",
            "author": request.author or "",
            "status": "error",
            "error": f"Open Library lookup failed: {exc}",
        }
        await deliver_webhook(str(request.webhook_url), payload)
        return

    try:
        valuation = await estimate_price(product, request.conservation_state)
        payload = {
            "code": product.get("code") or "",
            "title": product.get("title") or "",
            "author": product.get("author") or "",
            "status": "ok" if valuation.get("estimated_value") is not None else "error",
            "estimated_value": valuation.get("estimated_value"),
        }
        if payload["status"] == "error":
            payload["error"] = "No BRL price found for this product in Brazilian listings"
    except Exception as exc:
        logger.exception("Price estimation failed for code=%s", product["code"])
        payload = {
            **product,
            "code": product.get("code") or "",
            "title": product.get("title") or "",
            "author": product.get("author") or "",
            "status": "error",
            "error": f"Price estimation failed: {type(exc).__name__}: {exc}",
        }

    await deliver_webhook(str(request.webhook_url), payload)


@app.post("/lookup")
async def lookup(request: LookupRequest, background_tasks: BackgroundTasks) -> dict:
    logger.info(f"Lookup request: code={request.code}, title={request.title}, author={request.author}")
    background_tasks.add_task(_process_lookup, request)
    return {"status": "accepted", "message": "Request received, processing in background"}
