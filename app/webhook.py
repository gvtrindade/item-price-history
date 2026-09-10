"""Webhook delivery."""

from __future__ import annotations

import httpx

from app.config import WEBHOOK_TIMEOUT_SECONDS, logger


async def deliver_webhook(url: str, payload: dict) -> bool:
    logger.info(f"Delivering webhook to {url}")
    logger.debug(f"Webhook payload: {payload}")
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        logger.info(f"Webhook delivered successfully to {url}")
        return True
    except httpx.HTTPError as exc:
        logger.error("Webhook delivery to %s failed: %s", url, exc)
        return False
