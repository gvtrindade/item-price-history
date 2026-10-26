"""laya-based matching of bookstore listings to the requested book.

laya is a non-autoregressive typed-decision engine (choice / score / yes-no
questions over a text, in a single forward pass) -- it cannot generate text,
so it is used here purely to DECIDE which candidate listings are copies of the
requested book. Title, author and prices then come from the matched listings'
own structured fields.

The flow has two laya steps (one predict call each):

1. ``identify_book`` -- a ``choice`` question picks the candidate that is the
   requested book. Choice (relative competition between options) is far better
   calibrated than absolute yes/no judgments for this, and it handles sellers
   who attach the wrong ISBN to a listing (the examples folder shows a Dom
   Casmurro ISBN search returning Lucíola copies): the correct book is the
   title/author that repeats consistently.
2. ``filter_matching_listings`` -- one ``noul`` (yes/no) question per
   candidate, asked against a minimal state naming only the identified
   reference book (long listing states wreck the calibration). Candidates
   with P(yes) >= LAYA_MATCH_THRESHOLD are matches; the threshold is strict
   on purpose, since a wrong listing corrupts the minimum price while a
   missed listing rarely changes it.
"""

from __future__ import annotations

import asyncio

from starlette.concurrency import run_in_threadpool

from app.config import LAYA_MATCH_THRESHOLD, LAYA_MODEL, logger
from app.bookstore import format_candidates
from app.valuation import constrain_valuation, set_estimated_price

_router = None
_router_lock = asyncio.Lock()


def _load_router():
    from laya import Router

    return Router()


async def _get_router():
    global _router
    async with _router_lock:
        if _router is None:
            logger.info("Loading laya Router (downloads the checkpoint on first predict)")
            _router = await run_in_threadpool(_load_router)
    return _router


def _predict(router, state: str, questions: dict) -> dict:
    return router.predict(state, questions, model=LAYA_MODEL)


def _query_description(code: str | None, title: str | None, author: str | None) -> str:
    if code:
        return f"o ISBN {code}"
    if title and author:
        return f'o livro "{title}" de "{author}"'
    return f'o livro "{title}"'


async def identify_book(
    query_desc: str, state: str, candidates: list[dict]
) -> dict | None:
    """Ask laya which candidate is the requested book. Returns it (or None)."""
    router = await _get_router()
    criteria = {
        str(c["index"]): f'"{c["title"]}" — {c["author"] or "autor desconhecido"}'
        for c in candidates
    }
    questions = {
        "book": {
            "type": "choice",
            "instructions": (
                f"Os itens da lista apareceram na busca por {query_desc}. "
                "Qual item é um exemplar do livro procurado? Vendedores às vezes "
                "cadastram dados errados; o livro correto costuma ser o "
                "título/autor que se repete com consistência entre os itens."
            ),
            "criteria": criteria,
        }
    }
    result = await run_in_threadpool(_predict, router, state, questions)
    answer = result["answers"]["book"]
    choice = str(answer.get("choice") or "")
    logger.info(
        f"laya identified {query_desc} as listing #{choice} "
        f"(confidence={answer.get('confidence')})"
    )
    for c in candidates:
        if str(c["index"]) == choice:
            return c
    return None


async def filter_matching_listings(
    reference: dict, candidates: list[dict]
) -> list[dict]:
    """One noul question per candidate: is it a copy of the reference book?

    All questions go in a single predict call against a minimal state (only
    the reference book), which keeps the model's calibration sharp. Returns
    the candidates with P(yes) >= LAYA_MATCH_THRESHOLD, best match first.
    """
    if not candidates:
        return []
    router = await _get_router()
    state = f'Livro procurado: "{reference["title"]}" de {reference["author"]}.'
    questions = {}
    for c in candidates:
        questions[f"match_{c['index']}"] = {
            "type": "noul",
            "instructions": (
                "Este anúncio é um exemplar do livro procurado? Anúncio: "
                f'"{c["title"]}" de {c["author"] or "autor desconhecido"}, '
                f"{c['condition']}, R$ {c['price']}. "
                "Aceite pequenas variações de título como 'Livro de Bolso', "
                "'coleção', volume ou prefixos de sebo; título ou autor "
                "diferentes significam NÃO."
            ),
        }
    result = await run_in_threadpool(_predict, router, state, questions)
    matches = []
    for c in candidates:
        answer = result["answers"].get(f"match_{c['index']}") or {}
        probability = answer.get("noul")
        if probability is None:
            continue
        if probability >= LAYA_MATCH_THRESHOLD:
            matches.append({**c, "match_probability": round(float(probability), 4)})
    matches.sort(key=lambda c: c["match_probability"], reverse=True)
    logger.info(
        f"laya matched {len(matches)}/{len(candidates)} listing(s) for "
        f'"{reference["title"]}" de {reference["author"]}'
    )
    return matches


def _valuation_from_matches(matches: list[dict], conservation_state: str) -> dict:
    """Prices from matched listings, then the standard pricing rules."""
    used_prices = [
        c["price"] for c in matches if c["condition"] == "usado" and c["price"]
    ]
    new_prices = [
        c["price"] for c in matches if c["condition"] == "novo" and c["price"]
    ]
    valuation = {
        "new_price": min(new_prices) if new_prices else None,
        "used_price": min(used_prices) if used_prices else None,
    }
    valuation = constrain_valuation(valuation)
    valuation = set_estimated_price(valuation, conservation_state)
    parts = []
    if valuation["used_price"]:
        parts.append(f"usado a partir de R$ {valuation['used_price']:.2f}")
    if valuation["new_price"]:
        parts.append(f"novo a partir de R$ {valuation['new_price']:.2f}")
    valuation["summary"] = (
        f"Bookstore: {len(matches)} anúncio(s) do livro; " + "; ".join(parts)
        if parts
        else "Bookstore: nenhum preço encontrado nos anúncios correspondentes"
    )
    return valuation


async def match_book_listings(
    candidates: list[dict],
    conservation_state: str,
    code: str | None = None,
    title: str | None = None,
    author: str | None = None,
) -> dict | None:
    """Full laya matching flow. Returns book data + valuation, or None when no
    candidate listing matches the requested book."""
    state = format_candidates(candidates)
    query_desc = _query_description(code, title, author)

    reference = await identify_book(query_desc, state, candidates)
    if reference is None:
        logger.warning(f"laya could not identify the book for {query_desc}")
        return None

    matches = await filter_matching_listings(reference, candidates)
    if not matches:
        return None

    valuation = _valuation_from_matches(matches, conservation_state)
    return {
        "code": code or reference["isbn"] or "",
        "title": reference["title"],
        "author": reference["author"],
        "matches": matches,
        "valuation": valuation,
    }
