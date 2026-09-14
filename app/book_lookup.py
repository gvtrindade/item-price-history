"""Open Library lookups with Google Books fallback."""

from __future__ import annotations

import time

import httpx
from olclient.openlibrary import OpenLibrary

from app.config import (
    GOOGLE_BOOKS_API_KEY,
    GOOGLE_BOOKS_BASE_URL,
    GOOGLE_BOOKS_TIMEOUT_SECONDS,
    ISBN_RE,
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


def _normalize_isbn(raw: str) -> str | None:
    candidate = raw.strip().replace("-", "").upper()
    return candidate if ISBN_RE.match(candidate) else None


def _find_isbn(ol: OpenLibrary, book) -> str | None:
    for raw in book.identifiers.get("isbns") or []:
        isbn = _normalize_isbn(raw)
        if isbn:
            return isbn
    # Work-level search docs often have no isbns: check its editions.
    work_olid = (book.identifiers.get("olid") or [None])[0]
    if not work_olid:
        return None
    response = ol.session.get(
        f"{ol.base_url}/works/{work_olid}/editions.json", params={"limit": 5}
    )
    for entry in response.json().get("entries", []):
        for raw in entry.get("isbn_13", []) + entry.get("isbn_10", []):
            isbn = _normalize_isbn(raw)
            if isbn:
                return isbn
    return None


def _google_books_search(isbn: str | None = None, title: str | None = None, author: str | None = None) -> dict | None:
    """Search Google Books API. Returns dict with isbn/title/author or None."""
    if isbn:
        query = f"isbn:{isbn}"
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
    found_isbn = None
    for identifier in volume.get("industryIdentifiers", []):
        if identifier.get("type") == "ISBN_13":
            found_isbn = _normalize_isbn(identifier.get("identifier", ""))
            if found_isbn:
                break
    if not found_isbn:
        for identifier in volume.get("industryIdentifiers", []):
            if identifier.get("type") == "ISBN_10":
                found_isbn = _normalize_isbn(identifier.get("identifier", ""))
                if found_isbn:
                    break

    authors = volume.get("authors", [])
    return {
        "isbn": found_isbn or isbn or "",
        "title": volume.get("title", ""),
        "author": ", ".join(authors) if authors else (author or ""),
    }


def resolve_book(isbn: str | None, title: str | None, author: str | None) -> dict:
    """Blocking lookup of the missing book data. Raises LookupError if not found."""
    ol = OpenLibrary()
    try:
        if isbn:
            logger.info(f"Fetching Open Library edition for isbn={isbn}")
            edition = ol.Edition.get(isbn=isbn)
            if edition is None:
                logger.warning(f"Open Library has no edition for isbn={isbn}")
                raise LookupError("Book not found in Open Library")
            return {
                "isbn": isbn,
                "title": edition.title,
                "author": author_names(edition.authors),
            }

        logger.info(f"Searching Open Library for title={title!r} author={author!r}")
        book = ol.Work.search(title=title, author=author)
        if book is None:
            logger.warning(f"Open Library has no work for title={title!r} author={author!r}")
            raise LookupError("Book not found in Open Library")
        found_isbn = _find_isbn(ol, book)
        if not found_isbn:
            logger.warning(f"Open Library work for title={title!r} has no usable ISBN")
            raise LookupError("Matching book has no usable ISBN in Open Library")
        return {
            "isbn": found_isbn,
            "title": book.title,
            "author": author_names(book.authors) or (author or ""),
        }
    except LookupError:
        logger.info("Open Library failed, trying Google Books fallback")
        result = _google_books_search(isbn=isbn, title=title, author=author)
        if result and result.get("title"):
            logger.info(f"Google Books found: {result}")
            return result
        raise
