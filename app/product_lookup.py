"""Open Library lookups with Google Books fallback."""

from __future__ import annotations

import time

import httpx
from olclient.openlibrary import OpenLibrary

from app.config import (
    GOOGLE_BOOKS_API_KEY,
    GOOGLE_BOOKS_BASE_URL,
    GOOGLE_BOOKS_TIMEOUT_SECONDS,
    CODE_RE,
    logger,
)


def author_names(authors: list) -> str:
    """Join author names from either Author objects or search-result dicts."""
    names = []
    for author in authors or []:
        if isinstance(author, dict):
            names.append(author.get("name") or "")
        else:
            names.append(getattr(author, "name", "") or "")
    return ", ".join(name for name in names if name)


def _normalize_code(raw: str) -> str | None:
    candidate = raw.strip().replace("-", "").upper()
    return candidate if CODE_RE.match(candidate) else None


def _find_code(ol: OpenLibrary, product) -> str | None:
    for raw in product.identifiers.get("isbns") or []:
        code = _normalize_code(raw)
        if code:
            return code
    # Work-level search docs often have no isbns: check its editions.
    work_olid = (product.identifiers.get("olid") or [None])[0]
    if not work_olid:
        return None
    response = ol.session.get(
        f"{ol.base_url}/works/{work_olid}/editions.json", params={"limit": 5}
    )
    for entry in response.json().get("entries", []):
        for raw in entry.get("isbn_13", []) + entry.get("isbn_10", []):
            code = _normalize_code(raw)
            if code:
                return code
    return None


def _google_books_search(code: str | None = None, title: str | None = None, author: str | None = None) -> dict | None:
    """Search Google Books API. Returns dict with code/title/author or None."""
    if code:
        query = f"isbn:{code}"
    elif title:
        parts = [f'intitle:"{title}"']
        if author:
            parts.append(f'inauthor:"{author}"')
        query = "+".join(parts)
    else:
        return None

    params = {"q": query, "maxResults": 1}
    if GOOGLE_BOOKS_API_KEY:
        params["key"] = GOOGLE_BOOKS_API_KEY

    logger.info(f"Searching Google Books: {query}")
    for attempt in range(3):
        try:
            with httpx.Client(timeout=GOOGLE_BOOKS_TIMEOUT_SECONDS) as client:
                response = client.get(f"{GOOGLE_BOOKS_BASE_URL}/volumes", params=params)
                if response.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Google Books rate limited, waiting {wait}s")
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                data = response.json()
                break
        except httpx.HTTPStatusError:
            raise
        except Exception as exc:
            logger.warning(f"Google Books search failed: {exc}")
            return None
    else:
        logger.warning("Google Books rate limited after retries")
        return None

    items = data.get("items", [])
    if not items:
        return None

    volume = items[0].get("volumeInfo", {})
    found_code = None
    for identifier in volume.get("industryIdentifiers", []):
        if identifier.get("type") == "ISBN_13":
            found_code = _normalize_code(identifier.get("identifier", ""))
            if found_code:
                break
    if not found_code:
        for identifier in volume.get("industryIdentifiers", []):
            if identifier.get("type") == "ISBN_10":
                found_code = _normalize_code(identifier.get("identifier", ""))
                if found_code:
                    break

    authors = volume.get("authors", [])
    return {
        "code": found_code or code or "",
        "title": volume.get("title", ""),
        "author": ", ".join(authors) if authors else (author or ""),
    }


def _open_library_lookup(code: str | None, title: str | None, author: str | None) -> dict:
    """Lookup product data on Open Library. Raises LookupError if not found."""
    ol = OpenLibrary()
    if code:
        logger.info(f"Fetching Open Library edition for code={code}")
        edition = ol.Edition.get(isbn=code)
        if edition is None:
            logger.warning(f"Open Library has no edition for code={code}")
            raise LookupError("Product not found in Open Library")
        return {
            "code": code,
            "title": edition.title,
            "author": author_names(edition.authors),
        }

    logger.info(f"Searching Open Library for title={title!r} author={author!r}")
    product = ol.Work.search(title=title, author=author)
    if product is None:
        logger.warning(f"Open Library has no work for title={title!r} author={author!r}")
        raise LookupError("Product not found in Open Library")
    found_code = _find_code(ol, product)
    if not found_code:
        logger.warning(f"Open Library work for title={title!r} has no usable ISBN")
        raise LookupError("Matching product has no usable ISBN in Open Library")
    return {
        "code": found_code,
        "title": product.title,
        "author": author_names(product.authors) or (author or ""),
    }


def resolve_product(code: str | None, title: str | None, author: str | None) -> dict:
    try:
        product = _open_library_lookup(code, title, author)
    except Exception as exc:
        logger.info(f"Open Library lookup failed ({exc}), trying Google Books fallback")
        result = _google_books_search(code=code, title=title, author=author)
        if result and result.get("title"):
            logger.info(f"Google Books found: {result}")
            return result
        raise

    if not product.get("categories"):
        logger.info("Open Library returned no categories, trying Google Books")
        try:
            fallback = _google_books_search(
                code=product.get("code") or code,
                title=product.get("title") or title,
                author=product.get("author") or author,
            )
        except Exception as exc:
            logger.warning(f"Google Books category lookup failed: {exc}")
            fallback = None
        if fallback and fallback.get("categories"):
            logger.info(f"Google Books provided categories: {fallback['categories']}")
            product["categories"] = fallback["categories"]

    return product
