"""Open Library lookups: fill in the book data missing from the request."""

from __future__ import annotations

from olclient.openlibrary import OpenLibrary

from app.config import ISBN_RE


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


def resolve_book(isbn: str | None, title: str | None, author: str | None) -> dict:
    """Blocking lookup of the missing book data. Raises LookupError if not found."""
    ol = OpenLibrary()
    if isbn:
        edition = ol.Edition.get(isbn=isbn)
        if edition is None:
            raise LookupError("Book not found in Open Library")
        return {
            "isbn": isbn,
            "title": edition.title,
            "author": author_names(edition.authors),
        }

    book = ol.Work.search(title=title, author=author)
    if book is None:
        raise LookupError("Book not found in Open Library")
    found_isbn = _find_isbn(ol, book)
    if not found_isbn:
        raise LookupError("Matching book has no usable ISBN in Open Library")
    return {
        "isbn": found_isbn,
        "title": book.title,
        "author": author_names(book.authors) or (author or ""),
    }
