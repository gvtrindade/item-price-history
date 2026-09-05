"""LLM-driven price valuation: reply parsing, pricing rules and the search loop."""

from __future__ import annotations

import json
import re

from app.config import LLM_MAX_SEARCHES, LLM_MAX_TOOL_ROUNDS, USED_FALLBACK_RATIO
from app.llamacpp import VALUATION_SYSTEM_PROMPT, call_llamacpp
from app.searxng import run_search

JSON_DECODER = json.JSONDecoder()

VALUATION_KEYS = ("estimated_value", "new_price", "used_price")


def extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            data, _ = JSON_DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError(f"No JSON object found in LLM reply: {text[:200]!r}")


def _as_price(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _parse_number(raw: str) -> float | None:
    text = raw.strip().strip('"')
    if text.lower() in ("null", "none", ""):
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    return _as_price(text)


def salvage_valuation(text: str) -> dict | None:
    """Recover price fields from malformed JSON when the model breaks syntax."""
    salvaged = {}
    for key in VALUATION_KEYS:
        match = re.search(
            rf'"{key}"\s*:\s*(null|"[^"]*"|[0-9][0-9.,]*)', text, re.IGNORECASE
        )
        if not match:
            continue
        salvaged[key] = (
            None if match.group(1).lower() == "null" else _parse_number(match.group(1))
        )
    if not salvaged:
        return None
    return salvaged


def constrain_valuation(valuation: dict) -> dict:
    """Enforce the pricing rules regardless of what the LLM returned."""
    new_price = _as_price(valuation.get("new_price"))
    used_price = _as_price(valuation.get("used_price"))
    estimated = _as_price(valuation.get("estimated_value"))

    if used_price is None and new_price is not None:
        estimated = new_price * USED_FALLBACK_RATIO
    elif estimated is None:
        estimated = used_price if used_price is not None else new_price
    if new_price is not None and estimated is not None and estimated > new_price:
        estimated = new_price

    valuation["new_price"] = new_price
    valuation["used_price"] = used_price
    valuation["currency"] = "BRL"
    valuation["estimated_value"] = (
        round(estimated, 2) if estimated is not None else None
    )
    return valuation


async def estimate_price(book: dict, conservation_state: str) -> dict:
    """Drive the llama.cpp <-> SearXNG loop until it returns the valuation JSON."""
    messages = [
        {"role": "system", "content": VALUATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"ISBN: {book['isbn']}\n"
                f"Title: {book['title']}\n"
                f"Author: {book['author']}\n"
                f"Conservation state: {conservation_state}\n"
                "Search Brazilian websites for prices in BRL and reply with the valuation JSON."
            ),
        },
    ]

    nudged = False
    seen_queries: set[str] = set()

    for _ in range(LLM_MAX_TOOL_ROUNDS):
        reply = await call_llamacpp(messages)

        # Native tool calls (if the server runs with a tool-call parser).
        tool_calls = reply.get("tool_calls") or []
        if tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                try:
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                    query = arguments.get("query", "")
                except json.JSONDecodeError:
                    query = ""
                observations = await run_search([query])
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": call["function"]["name"],
                        "content": observations,
                    }
                )
            continue

        # JSON action protocol (works with any model, no tool parser needed).
        raw = reply.get("content") or ""
        messages.append({"role": "assistant", "content": raw})
        try:
            action = extract_json(raw)
        except ValueError:
            action = {}
        query = None
        if (action.get("action") or "").lower() == "search":
            query = action.get("query")
        elif isinstance(action.get("search"), str):  # common mis-format
            query = action["search"]
        if isinstance(query, str) and query.strip():
            key = query.casefold().strip()
            if key in seen_queries:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"You already searched {query!r}; the results are "
                            "in this conversation. Reply now with the final "
                            "valuation JSON in BRL."
                        ),
                    }
                )
                continue
            if len(seen_queries) >= LLM_MAX_SEARCHES:
                messages.append(
                    {
                        "role": "user",
                        "content": "Search limit reached. Reply now with the "
                        "final valuation JSON in BRL, using the results above.",
                    }
                )
                continue
            seen_queries.add(key)
            observations = await run_search([query])
            messages.append(
                {
                    "role": "user",
                    "content": f"Search results for {query!r}:\n{observations}\n"
                    "If you already ran this search, try a different query.",
                }
            )
            continue
        valuation = (
            action
            if any(k in action for k in VALUATION_KEYS)
            else salvage_valuation(raw)
        )
        if valuation:
            if (
                not nudged
                and not any(
                    _as_price(valuation.get(k))
                    for k in ("new_price", "used_price", "estimated_value")
                )
            ):
                nudged = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "No price was found yet. Run more searches before "
                            f'answering, e.g. "{book["title"]} {book["author"]} '
                            f'mercado livre R$" or "{book["title"]} preço comprar sebo".'
                        ),
                    }
                )
                continue
            return constrain_valuation(valuation)
        messages.append(
            {
                "role": "user",
                "content": "That reply did not contain a search action or a "
                "valuation. Answer again with exactly ONE JSON object: "
                '{"action":"search","query":"..."} or '
                '{"new_price":...,"used_price":...,"estimated_value":...,'
                '"currency":"BRL","summary":"..."}.',
            }
        )

    raise RuntimeError("LLM did not produce a valuation within the search limit")
