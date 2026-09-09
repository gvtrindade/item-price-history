"""LLM client (OpenAI-compatible /v1/chat/completions) and prompts."""

from __future__ import annotations

import httpx

from app.config import (
    LLAMA_CPP_BASE_URL,
    LLAMA_CPP_MODEL,
    LLAMA_CPP_TIMEOUT_SECONDS,
    LLM_PROVIDER,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    OPENROUTER_TIMEOUT_SECONDS,
)

VALUATION_SYSTEM_PROMPT = """\
You are a used-book pricing assistant. The book to be valued is ALWAYS a
USED copy, in the conservation state given. Given its ISBN, title and
author, find the book's current prices on the BRAZILIAN internet:
- the price of a NEW copy (new_price);
- the price of USED copies (used_price), e.g. on Estante Virtual,
  Mercado Livre, sebos, Amazon.com.br, Americanas or Shopee Brasil.
All prices MUST come from Brazilian listings and be in BRL (R$); never
use prices quoted in another currency. Prefer Portuguese queries, e.g.
"<titulo> <autor> preço sebo usado", "<titulo> <autor> mercado livre R$"
or "<titulo> <autor> comprar preço", and try the translated Brazilian
title when it differs (e.g. "Crime e Castigo"). Snippets from Mercado
Livre and sebos often contain the price directly ("R$ 88,76"). Search
results may include "prices_found_in_snippets_brl": if you cannot tie a
specific price to new or used copies, use the lowest listed price as
used_price and the highest as new_price.
Keep searching until you have found at least one numeric price --
only report null if at least three different searches found no price.
You do not know today's prices, so you must search the web. Reply with
exactly ONE JSON object and no other text. Per turn, either:
  {"action": "search", "query": "<search terms>"}   -> to run a web search
  (one search per turn; issue several until you have enough price data)
or, when you are done searching:
  {"new_price": <number, price of a new copy in BRL>,
   "used_price": <number in BRL, or null if no used copy price was found>,
   "estimated_value": <number in BRL, this used copy's value adjusted for its conservation state>,
   "currency": "BRL",
   "summary": "<one short sentence explaining the estimate>"}
Strict rules for estimated_value:
- It must NEVER be greater than new_price.
- If no used_price was found, it MUST be exactly 40% of new_price.
- When used_price was found, base the estimate on it, adjusted (usually
  downwards) for the conservation state."""


async def call_llamacpp(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Send a chat completion request to llama.cpp and return the assistant message."""
    payload: dict = {
        "model": LLAMA_CPP_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 160,
        "repeat_penalty": 1.2,
        "stop": ["\n\n"],
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    async with httpx.AsyncClient(timeout=LLAMA_CPP_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{LLAMA_CPP_BASE_URL}/v1/chat/completions", json=payload
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]


async def call_openrouter(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Send a chat completion request to OpenRouter and return the assistant message."""
    payload: dict = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 160,
        "stop": ["\n\n"],
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    async with httpx.AsyncClient(timeout=OPENROUTER_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{OPENROUTER_BASE_URL}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]


async def call_llm(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Dispatch to the configured LLM provider."""
    if LLM_PROVIDER == "openrouter":
        return await call_openrouter(messages, tools)
    return await call_llamacpp(messages, tools)
