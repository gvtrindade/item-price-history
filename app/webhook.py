"""Webhook delivery."""

from __future__ import annotations

import httpx

from app.config import WEBHOOK_TIMEOUT_SECONDS, logger


async def deliver_webhook(url: str, payload: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.error("Webhook delivery to %s failed: %s", url, exc)
        return False
