"""Ziontech (megabazar) catalog integration.

Reserves an external product id (``id_produtos``) for a valued product. The id
is stored by the web app as the product's ``external_id`` and rendered by the
printer service in the ``{{e_id}}`` placeholder and QR code.

The upstream endpoint accepts a batch of items and answers with one
``id_produtos`` per item, in the same order:

    POST {ZIONTECH_URL}
    {"itens": [{"descricao": "<isbn> - <title>", "valor": "15.00"}]}
    -> {"itens": [{"descricao": ..., "valor": ..., "id_produtos": "12345", "qr": "E:12345"}]}
"""

from __future__ import annotations

import httpx

from app.config import (
    ZIONTECH_TIMEOUT_SECONDS,
    ZIONTECH_TOKEN,
    ZIONTECH_URL,
    logger,
)

# The upstream endpoint rejects batches larger than 100 items.
MAX_ITEMS_PER_REQUEST = 100


def build_description(isbn: str | None, title: str | None) -> str:
    """Compose the item description: ``<isbn> - <title>``, dropping the missing part."""
    isbn = (isbn or "").strip()
    title = (title or "").strip()
    if isbn and title:
        return f"{isbn} - {title}"
    return isbn or title


def _format_price(price) -> str:
    try:
        value = float(price)
    except (TypeError, ValueError):
        value = 0.0
    return f"{value:.2f}"


async def _request_external_ids(items: list[dict]) -> list[str | None]:
    """Register one batch (at most ``MAX_ITEMS_PER_REQUEST``) and map the ids."""
    results: list[str | None] = [None] * len(items)
    if not items:
        return results

    try:
        async with httpx.AsyncClient(timeout=ZIONTECH_TIMEOUT_SECONDS) as client:
            response = await client.post(
                ZIONTECH_URL,
                headers={
                    "Authorization": f"Bearer {ZIONTECH_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={"itens": items},
            )
            if response.is_error:
                # The upstream explains why it rejected the batch in the body;
                # raise_for_status() alone discards that information.
                logger.error(
                    "Ziontech: external id request rejected "
                    "(status=%d, items=%d): %s",
                    response.status_code,
                    len(items),
                    response.text[:2000],
                )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Ziontech: external id request failed: %s", exc)
        return results

    response_items = data.get("itens") if isinstance(data, dict) else None
    if not isinstance(response_items, list):
        logger.error("Ziontech: unexpected response: %s", data)
        return results

    for index, response_item in enumerate(response_items[: len(items)]):
        if not isinstance(response_item, dict):
            continue
        external_id = response_item.get("id_produtos")
        if external_id is None:
            logger.error(
                "Ziontech: response item has no id_produtos: %s", response_item
            )
            continue
        results[index] = str(external_id)
    return results


async def create_external_ids(
    products: list[dict],
) -> list[str | None]:
    """Reserve external ids for several products, in the same order.

    ``products`` contains dicts with ``code``, ``title`` and ``price``. Products
    without a code or title are skipped (``None`` in the result). The upstream
    endpoint accepts at most 100 items per call, so larger lists are chunked.
    """
    results: list[str | None] = [None] * len(products)
    pending: list[tuple[int, dict]] = []

    for index, product in enumerate(products):
        description = build_description(
            product.get("code"), product.get("title")
        )
        if not description:
            logger.warning(
                "Ziontech: skipping external id, no ISBN or title available"
            )
            continue
        pending.append(
            (
                index,
                {
                    "descricao": description,
                    "valor": _format_price(product.get("price")),
                },
            )
        )

    if not pending:
        return results
    if not ZIONTECH_URL or not ZIONTECH_TOKEN:
        logger.warning("Ziontech: integration not configured, skipping external ids")
        return results

    for start in range(0, len(pending), MAX_ITEMS_PER_REQUEST):
        chunk = pending[start : start + MAX_ITEMS_PER_REQUEST]
        external_ids = await _request_external_ids([item for _, item in chunk])
        for (product_index, _), external_id in zip(chunk, external_ids):
            results[product_index] = external_id

    return results


async def create_external_id(
    isbn: str | None, title: str | None, price
) -> str | None:
    """Reserve a single external product id, or ``None`` when unavailable."""
    external_ids = await create_external_ids(
        [{"code": isbn, "title": title, "price": price}]
    )
    return external_ids[0]
