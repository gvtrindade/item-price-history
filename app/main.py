"""Product Price Finder API.

Flow:
  1. Receive either a code, or a title + author, plus the product's
     conservation state and a webhook URL.
  2. Resolve the missing product data via Open Library (title/author from a
     code, or code from title/author).
  3. Ask a llama.cpp instance (OpenAI-compatible /v1/chat/completions) to
     search the Brazilian web through SearXNG for new/used prices in BRL
     and return an estimated value for the (always used) copy as JSON.
  4. Reserve the copy's external id in the Ziontech (megabazar) catalog.
  5. Deliver the estimate (code, title, author, price, estimated value,
     external id) -- or the error -- to the webhook.

Products whose valuation succeeded but whose external id could not be reserved
can be registered later in batches through ``POST /external-ids`` (up to 100
products per Ziontech call; larger lists are chunked automatically).

``POST /lookup/bookstore`` is an alternative flow: it crawls the bookstore
search page (by ISBN or title+author), trims the HTML to the embedded
candidate listings, asks laya (a typed-decision engine) which listings are
copies of the requested book, applies the same pricing rules and delivers the
result to the webhook.
"""

from __future__ import annotations

from fastapi import FastAPI, BackgroundTasks
from starlette.concurrency import run_in_threadpool

from app.book_matcher import match_book_listings
from app.bookstore import (
    BookstoreError,
    extract_candidates,
    fetch_search_page,
)
from app.product_lookup import resolve_product
from app.config import logger
from app.schemas import ExternalIdRequest, LookupRequest
from app.valuation import estimate_price
from app.webhook import deliver_webhook
from app.ziontech import MAX_ITEMS_PER_REQUEST, create_external_id, create_external_ids

app = FastAPI(title="Product Price Finder", version="0.4.0")


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
        else:
            try:
                external_id = await create_external_id(
                    product.get("code"),
                    product.get("title"),
                    valuation.get("estimated_value"),
                )
            except Exception:
                # Reserving the external id must never invalidate a good valuation.
                logger.exception("Ziontech external id lookup failed")
                external_id = None
            if external_id is not None:
                payload["external_id"] = external_id
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


async def _process_bookstore_lookup(request: LookupRequest) -> None:
    """Bookstore + laya flow: crawl, trim, match, price, webhook."""
    base_payload = {
        "code": request.code or "",
        "title": request.title or "",
        "author": request.author or "",
        "conservation_state": request.conservation_state,
        "source": "bookstore",
    }
    try:
        html = await fetch_search_page(request.code, request.title, request.author)
        candidates = extract_candidates(html)
    except BookstoreError as exc:
        logger.warning("Bookstore crawl failed: %s", exc)
        await deliver_webhook(
            str(request.webhook_url),
            {**base_payload, "status": "error", "error": str(exc)},
        )
        return
    except Exception as exc:
        logger.exception("Unexpected Bookstore crawl failure")
        await deliver_webhook(
            str(request.webhook_url),
            {
                **base_payload,
                "status": "error",
                "error": f"Bookstore crawl failed: {type(exc).__name__}: {exc}",
            },
        )
        return

    if not candidates:
        await deliver_webhook(
            str(request.webhook_url),
            {**base_payload, "status": "not_found"},
        )
        return

    try:
        result = await match_book_listings(
            candidates,
            request.conservation_state,
            code=request.code,
            title=request.title,
            author=request.author,
        )
    except Exception as exc:
        logger.exception("laya matching failed")
        await deliver_webhook(
            str(request.webhook_url),
            {
                **base_payload,
                "status": "error",
                "error": f"laya matching failed: {type(exc).__name__}: {exc}",
            },
        )
        return

    if result is None:
        await deliver_webhook(
            str(request.webhook_url),
            {**base_payload, "status": "not_found"},
        )
        return

    valuation = result["valuation"]
    payload = {
        **base_payload,
        "code": result["code"] or "",
        "title": result["title"] or "",
        "author": result["author"] or "",
        "status": "ok" if valuation.get("estimated_value") is not None else "error",
        "new_price": valuation.get("new_price"),
        "used_price": valuation.get("used_price"),
        "estimated_value": valuation.get("estimated_value"),
        "currency": valuation.get("currency", "BRL"),
        "summary": valuation.get("summary", ""),
    }
    if payload["status"] == "error":
        payload["error"] = "No BRL price found for this book on the bookstore"
    else:
        try:
            external_id = await create_external_id(
                payload["code"], payload["title"], valuation.get("estimated_value")
            )
        except Exception:
            # Reserving the external id must never invalidate a good valuation.
            logger.exception("Ziontech external id lookup failed")
            external_id = None
        if external_id is not None:
            payload["external_id"] = external_id

    await deliver_webhook(str(request.webhook_url), payload)


@app.post("/lookup/bookstore")
async def lookup_bookstore(
    request: LookupRequest, background_tasks: BackgroundTasks
) -> dict:
    logger.info(
        f"Bookstore lookup request: code={request.code}, "
        f"title={request.title}, author={request.author}"
    )
    background_tasks.add_task(_process_bookstore_lookup, request)
    return {"status": "accepted", "message": "Request received, processing in background"}


@app.post("/external-ids")
async def register_external_ids(request: ExternalIdRequest) -> dict:
    """Reserve Ziontech external ids for products that are missing them.

    The upstream endpoint accepts at most 100 items per call; the Ziontech
    client chunks larger requests automatically.
    """
    logger.info(
        "External id request: %d product(s), batches of %d",
        len(request.products),
        MAX_ITEMS_PER_REQUEST,
    )
    external_ids = await create_external_ids(
        [
            {
                "code": product.code,
                "title": product.title,
                "price": product.price,
            }
            for product in request.products
        ]
    )
    return {
        "results": [
            {"product_id": product.product_id, "external_id": external_id}
            for product, external_id in zip(request.products, external_ids)
        ]
    }
