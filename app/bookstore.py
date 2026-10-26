"""Bookstore search crawler and page trimming.

Fetches the search results page (by ISBN or by title + author) and trims the
200KB-1.5MB HTML down to the embedded ``window.__INITIAL_STATE__`` JSON, then
further to a compact list of candidate book listings -- only what laya needs
to decide which listings match the requested book.
"""

from __future__ import annotations

import json
from urllib.parse import quote_plus

import httpx

from app.config import (
    BOOKSTORE_BASE_URL,
    BOOKSTORE_MAX_CANDIDATES,
    BOOKSTORE_TIMEOUT_SECONDS,
    logger,
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

_STATE_MARKER = "window.__INITIAL_STATE__="
_BOT_BLOCK_MARKERS = ("validate.perfdrive.com", "Radware Bot Manager", "ShieldSquare")
_JSON_DECODER = json.JSONDecoder()


class BookstoreError(Exception):
    """The search page could not be fetched or had no usable data."""


def search_urls(code: str | None, title: str | None, author: str | None) -> list[str]:
    """URLs to try in order.

    The path-style ISBN URL is tried first, but it sits behind a stricter bot
    rule (Radware/ShieldSquare) than the query-style one, so the query-style
    ISBN search is the fallback. Title+author searches only have the
    query-style form.
    """
    if code:
        return [
            f"{BOOKSTORE_BASE_URL}/busca/{code}",
            f"{BOOKSTORE_BASE_URL}/busca?nsCat=Natural&q={quote_plus(code)}",
        ]
    query = quote_plus(f"{title or ''} {author or ''}".strip())
    return [
        f"{BOOKSTORE_BASE_URL}/busca?nsCat=Natural&q={query}"
        "&searchField=titulo-autor"
    ]


async def fetch_search_page(
    code: str | None = None, title: str | None = None, author: str | None = None
) -> str:
    """GET the search results HTML, skipping bot-blocked responses."""
    urls = search_urls(code, title, author)
    last_error = "no URL to try"
    async with httpx.AsyncClient(
        timeout=BOOKSTORE_TIMEOUT_SECONDS,
        headers=_HEADERS,
        follow_redirects=False,
    ) as client:
        for url in urls:
            logger.info(f"Crawling bookstore: {url}")
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                logger.warning(f"bookstore request failed for {url}: {exc}")
                last_error = str(exc)
                continue
            if response.is_redirect:
                # The only redirect seen from these URLs is the bot wall.
                location = response.headers.get("location", "")
                logger.warning(f"bookstore redirected {url} -> {location[:80]}")
                last_error = f"blocked by bot check (redirect) for {url}"
                continue
            body = response.text
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code} for {url}"
                continue
            if _STATE_MARKER not in body or any(m in body for m in _BOT_BLOCK_MARKERS):
                logger.warning(f"bookstore returned a bot check page for {url}")
                last_error = f"blocked by bot check for {url}"
                continue
            logger.info(f"bookstore page fetched: {len(body)} bytes from {url}")
            return body
    raise BookstoreError(f"bookstore search failed: {last_error}")


def extract_initial_state(html: str) -> dict:
    """Pull the window.__INITIAL_STATE__ JSON object out of the page HTML."""
    marker = html.find(_STATE_MARKER)
    if marker < 0:
        raise BookstoreError("page has no window.__INITIAL_STATE__ blob")
    start = html.find("{", marker)
    if start < 0:
        raise BookstoreError("window.__INITIAL_STATE__ has no JSON object")
    end = html.find("</script>", start)
    raw = html[start : end if end > start else len(html)]
    try:
        state, _ = _JSON_DECODER.raw_decode(raw)
    except json.JSONDecodeError as exc:
        raise BookstoreError(f"window.__INITIAL_STATE__ is not valid JSON: {exc}")
    return state


def trim_candidates(state: dict) -> list[dict]:
    """Trim the full page state to compact candidate listings.

    Keeps only what the matching step needs: title, author, isbn, condition,
    publisher, price (converted from centavos to BRL) and copy counts.
    """
    skus = (state.get("SearchPage") or {}).get("parentSkus") or []
    candidates = []
    for sku in skus[:BOOKSTORE_MAX_CANDIDATES]:
        attrs = {
            attr.get("name"): attr.get("value")
            for attr in sku.get("attributes") or []
        }
        price = sku.get("salePrice") or sku.get("listPrice")
        copies = {
            v.get("condition"): v.get("total")
            for v in sku.get("variations") or []
            if v.get("condition")
        }
        candidates.append(
            {
                "index": len(candidates) + 1,
                "title": sku.get("name") or "",
                "author": attrs.get("author") or "",
                "isbn": attrs.get("isbn") or "",
                "condition": attrs.get("condition") or "",
                "publisher": attrs.get("publisher") or "",
                "price": round(price / 100, 2) if isinstance(price, (int, float)) else None,
                "copies": copies,
            }
        )
    logger.info(f"Trimmed page to {len(candidates)} candidate listing(s)")
    return candidates


def extract_candidates(html: str) -> list[dict]:
    """HTML -> initial state -> compact candidate listings (the full trim)."""
    return trim_candidates(extract_initial_state(html))


def format_candidates(candidates: list[dict]) -> str:
    """Render candidates as a compact numbered list for laya to read."""
    lines = []
    for c in candidates:
        price = f"R$ {c['price']:.2f}" if c["price"] is not None else "sem preço"
        copies = ", ".join(f"{cond}: {n}" for cond, n in sorted(c["copies"].items()))
        lines.append(
            f"{c['index']}. \"{c['title']}\" — {c['author'] or 'autor desconhecido'}"
            f" — {c['condition'] or 'condição não informada'} — {price}"
            f" — ISBN {c['isbn'] or 'não informado'}"
            + (f" — exemplares: {copies}" if copies else "")
        )
    return "\n".join(lines)
