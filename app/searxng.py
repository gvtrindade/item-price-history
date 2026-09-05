"""SearXNG web search (Brazil, pt-BR) and BRL price extraction from snippets."""

from __future__ import annotations

import json
import re

import httpx

from app.config import (
    SEARXNG_BASE_URL,
    SEARXNG_COUNTRY,
    SEARXNG_LANGUAGE,
    SEARXNG_MAX_RESULTS,
    SEARXNG_TIMEOUT_SECONDS,
    logger,
)

BRL_PRICE_RE = re.compile(
    r"R\$\s*(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
)


async def search_searxng(query: str) -> list[dict]:
    """Query the local SearXNG JSON API (Brazil, pt-BR) and return trimmed results."""
    params = {
        "q": query,
        "format": "json",
        "country": SEARXNG_COUNTRY,
        "language": SEARXNG_LANGUAGE,
        "locale": SEARXNG_LANGUAGE.replace("-", "_"),
    }
    async with httpx.AsyncClient(timeout=SEARXNG_TIMEOUT_SECONDS) as client:
        response = await client.get(f"{SEARXNG_BASE_URL}/search", params=params)
        response.raise_for_status()
        results = response.json().get("results", [])[:SEARXNG_MAX_RESULTS]
    return [
        {
            "title": item.get("title"),
            "url": item.get("url"),
            "snippet": (item.get("content") or "")[:200],
        }
        for item in results
    ]


def extract_brl_prices(text: str) -> list[float]:
    """Pull R$ prices out of search snippets (Brazilian comma decimals)."""
    prices = []
    for match in BRL_PRICE_RE.findall(text):
        normalized = match.replace(".", "").replace(",", ".")
        try:
            price = float(normalized)
        except ValueError:
            continue
        if 0 < price < 100000 and price not in prices:
            prices.append(price)
    return prices


async def run_search(queries: list[str]) -> str:
    """Run SearXNG for each query and return a JSON observations blob."""
    observations = []
    for query in queries:
        try:
            results = await search_searxng(query or "")
            observation = {"query": query, "results": results}
            blob = json.dumps(results, ensure_ascii=False)
            prices = extract_brl_prices(blob)
            if prices:
                observation["prices_found_in_snippets_brl"] = sorted(prices)
            observations.append(observation)
        except Exception as exc:
            logger.warning("SearXNG search failed for %r: %s", query, exc)
            observations.append({"query": query, "error": str(exc)})
    return json.dumps(observations, ensure_ascii=False)
