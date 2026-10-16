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


async def create_external_id(
    isbn: str | None, title: str | None, price
) -> str | None:
    """Reserve an external product id, or ``None`` when it cannot be created."""
    description = build_description(isbn, title)
    if not description:
        logger.warning("Ziontech: skipping external id, no ISBN or title available")
        return None
    if not ZIONTECH_URL or not ZIONTECH_TOKEN:
        logger.warning("Ziontech: integration not configured, skipping external id")
        return None

    try:
        async with httpx.AsyncClient(timeout=ZIONTECH_TIMEOUT_SECONDS) as client:
            response = await client.post(
                ZIONTECH_URL,
                headers={
                    "Authorization": f"Bearer {ZIONTECH_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "itens": [
                        {"descricao": description, "valor": _format_price(price)}
                    ]
                },
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Ziontech: external id request failed: %s", exc)
        return None

    items = data.get("itens") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        logger.error("Ziontech: unexpected response: %s", data)
        return None

    external_id = items[0].get("id_produtos")
    if external_id is None:
        logger.error("Ziontech: response item has no id_produtos: %s", items[0])
        return None
    return str(external_id)
