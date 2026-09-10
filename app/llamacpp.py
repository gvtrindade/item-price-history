"""LLM client (OpenAI-compatible /v1/chat/completions) and prompts."""

from __future__ import annotations

import asyncio

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
    logger,
)

VALUATION_SYSTEM_PROMPT = """\
You are a used-book pricing assistant. The book to be valued is ALWAYS a
USED copy, in the conservation state given. Conservation states are:
- As New: Pristine, original condition as when published. No defects.
- Near Fine (FN): Approaching As New with very minor defects.
- Good (G): Average wear, complete text, intact and readable, shows signs of use.
- Fair: Worn but complete text pages, may lack endpapers/half-title, significant wear on binding/jacket.
Adjust the estimated_value based on the conservation state: better condition = higher value relative to used_price.
Given its ISBN, title and author, find the book's current prices on the BRAZILIAN internet:
- the price of a NEW copy (new_price);
- the price of USED copies (used_price), e.g. on Estante Virtual,
  Mercado Livre, sebos, Amazon.com.br, Americanas or Shopee Brasil.
All prices MUST come from Brazilian listings and be in BRL (R$); never
use prices quoted in another currency. Prefer Portuguese queries: combine
the book's title and author with price terms, e.g. "O Cortiço Aluísio
Azevedo preço sebo usado", "Dom Casmurro Machado de Assis mercado livre
R$" or "Memórias Póstumas comprar preço" (never wrap words in angle
brackets), and try the translated Brazilian title when it differs (e.g.
"Crime e Castigo"). Snippets from Mercado
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
        "max_tokens": 512,
        "repeat_penalty": 1.2,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    logger.info(f"Calling llama.cpp at {LLAMA_CPP_BASE_URL} with model {LLAMA_CPP_MODEL}")
    try:
        async with httpx.AsyncClient(timeout=LLAMA_CPP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{LLAMA_CPP_BASE_URL}/v1/chat/completions", json=payload
            )
            response.raise_for_status()
            result = response.json()["choices"][0]["message"]
            if not (result.get("content") or "").strip() and result.get("reasoning_content"):
                # Older servers ignore chat_template_kwargs; salvage text from CoT.
                result["content"] = result["reasoning_content"]
            logger.info(f"llama.cpp response: {(result.get('content') or '')[:100]}...")
            return result
    except Exception as e:
        logger.error(f"llama.cpp call failed: {e}")
        raise


async def call_openrouter(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Send a chat completion request to OpenRouter and return the assistant message."""
    url = f"{OPENROUTER_BASE_URL}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/anomalyco/book-price-finder",
        "X-Title": "Book Price Finder",
    }
    payload: dict = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 500,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    logger.info("Calling OpenRouter model=%s url=%s", OPENROUTER_MODEL, url)

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=OPENROUTER_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 429 or response.status_code >= 500:
                logger.warning(
                    "OpenRouter attempt %d got status %d, retrying", attempt, response.status_code
                )
                await _sleep(2 ** attempt)
                continue

            if response.status_code == 401:
                raise RuntimeError(
                    "OpenRouter returned 401 Unauthorized — check OPENROUTER_API_KEY"
                )

            response.raise_for_status()
            data = response.json()
            choices = data.get("choices")
            if not choices:
                raise RuntimeError(f"OpenRouter returned no choices: {data!r}")

            message = choices[0].get("message")
            if not message:
                raise RuntimeError(f"OpenRouter choice has no message: {choices[0]!r}")

            content = message.get("content") or ""
            logger.info("OpenRouter response (attempt %d): %s...", attempt, content[:200])
            return message

        except httpx.HTTPStatusError:
            raise
        except httpx.RequestError as e:
            last_error = e
            logger.warning("OpenRouter attempt %d request error: %s", attempt, e)
            await _sleep(2 ** attempt)
        except RuntimeError:
            raise
        except Exception as e:
            last_error = e
            logger.warning("OpenRouter attempt %d unexpected error: %s", attempt, e)
            await _sleep(2 ** attempt)

    raise RuntimeError(f"OpenRouter call failed after 3 attempts: {last_error}")


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def call_llm(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Dispatch to the configured LLM provider."""
    logger.info(f"Using LLM provider: {LLM_PROVIDER}")
    if LLM_PROVIDER == "openrouter":
        return await call_openrouter(messages, tools)
    return await call_llamacpp(messages, tools)
