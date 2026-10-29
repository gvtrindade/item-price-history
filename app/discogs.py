"""Discogs barcode lookup (CDs).

Metadata only: resolves a barcode to the release title and artist. Prices are
never estimated here -- CDs and DVDs are priced manually by the caller.
Discogs does not catalog movies, so DVDs naturally end up as "not found".
"""

from __future__ import annotations

import re

import httpx

from app.config import (
    DISCOGS_BASE_URL,
    DISCOGS_TIMEOUT_SECONDS,
    DISCOGS_TOKEN,
    DISCOGS_USER_AGENT,
    logger,
)

# Discogs disambiguates same-named artists as "Artist (2)".
_DISAMBIGUATION_RE = re.compile(r"\s+\(\d+\)$")


class DiscogsError(RuntimeError):
    """Discogs could not be queried (missing token, auth, rate limit, outage)."""


def _headers() -> dict[str, str]:
    headers = {"User-Agent": DISCOGS_USER_AGENT}
    if DISCOGS_TOKEN:
        headers["Authorization"] = f"Discogs token={DISCOGS_TOKEN}"
    return headers


def _clean_artist(name: str) -> str:
    return _DISAMBIGUATION_RE.sub("", name.strip())


def _join_artists(artists: list[dict]) -> str:
    """Join release artists, honouring Discogs' per-artist separators."""
    parts: list[str] = []
    for index, artist in enumerate(artists):
        name = _clean_artist(str(artist.get("name") or ""))
        if not name:
            continue
        parts.append(name)
        join = str(artist.get("join") or "").strip()
        if join and index < len(artists) - 1:
            parts.append(join)
    return " ".join(parts).strip()


def _parse_search_title(title: str) -> tuple[str, str]:
    """Fallback for search results: discogs titles look like 'Artist - Title'."""
    if " - " in title:
        artist, release = title.split(" - ", 1)
        return release.strip(), artist.strip()
    return title.strip(), ""


def _check_response(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise DiscogsError("discogs_auth_failed")
    if response.status_code == 429:
        raise DiscogsError("discogs_rate_limited")
    if response.status_code >= 400:
        raise DiscogsError(f"discogs_http_{response.status_code}")


def lookup_by_barcode(code: str) -> dict | None:
    """Return ``{"title": ..., "artist": ...}`` for *code*, or ``None``.

    ``None`` means Discogs knows no release with that barcode. A
    :class:`DiscogsError` means the lookup itself failed.
    """
    if not DISCOGS_TOKEN:
        raise DiscogsError("discogs_token_not_configured")

    with httpx.Client(timeout=DISCOGS_TIMEOUT_SECONDS, headers=_headers()) as client:
        try:
            search = client.get(
                f"{DISCOGS_BASE_URL}/database/search",
                params={"barcode": code, "type": "release", "per_page": 5},
            )
        except httpx.HTTPError as exc:
            raise DiscogsError(f"discogs_unreachable: {exc}") from exc

        _check_response(search)
        try:
            results = search.json().get("results") or []
        except ValueError as exc:
            raise DiscogsError(f"discogs_invalid_response: {exc}") from exc
        if not results:
            logger.info("Discogs has no release for barcode %s", code)
            return None

        first = results[0]
        fallback_title, fallback_artist = _parse_search_title(
            str(first.get("title") or "")
        )
        release_id = first.get("id")
        if not release_id:
            return {"title": fallback_title, "artist": fallback_artist}

        # The search title already holds "Artist - Title", but the release
        # endpoint returns the proper artist list; degrade to the search title
        # when that extra request fails.
        try:
            release = client.get(f"{DISCOGS_BASE_URL}/releases/{release_id}")
            _check_response(release)
            data = release.json()
        except (httpx.HTTPError, DiscogsError, ValueError) as exc:
            logger.warning(
                "Discogs release %s fetch failed (%s); using search result title",
                release_id,
                exc,
            )
            return {"title": fallback_title, "artist": fallback_artist}

        title = str(data.get("title") or "").strip() or fallback_title
        artist = _join_artists(data.get("artists") or []) or fallback_artist
        return {"title": title, "artist": artist}
