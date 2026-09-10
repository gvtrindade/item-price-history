"""LLM client (OpenAI-compatible /v1/chat/completions) and prompts."""

from __future__ import annotations

import asyncio
import json
import re
import time

import httpx

from app.config import (
    LLM_MAX_SEARCHES,
    LLM_MAX_TOOL_ROUNDS,
    LLM_MIN_CALL_GAP_SECONDS,
    LLM_RATE_LIMIT_FALLBACK_SECONDS,
    LLAMA_CPP_BASE_URL,
    LLAMA_CPP_MODEL,
    LLAMA_CPP_TIMEOUT_SECONDS,
    LLM_PROVIDER,
    OPENROUTER_API_KEY,
    OPENROUTER_API_KEY1,
    OPENROUTER_BASE_URL,
    OPENROUTER_BASE_URL1,
    OPENROUTER_MODELS,
    OPENROUTER_MODELS1,
    OPENROUTER_TIMEOUT_SECONDS,
    logger,
)
from app.searxng import run_search

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


SEARXNG_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "searxng_search",
        "description": (
            "Search the web through a local SearXNG instance (Brazilian "
            "listings, pt-BR). Use it to find current BRL prices for the "
            "book; one query per call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search terms, e.g. 'Dom Casmurro Machado de Assis "
                        "mercado livre R$'."
                    ),
                }
            },
            "required": ["query"],
        },
    },
}

_JSON_DECODER = json.JSONDecoder()


def _find_json_object(text: str) -> dict | None:
    """Return the first top-level JSON object embedded in text, if any."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            obj = json.loads(fenced.group(1))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            data, _ = _JSON_DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


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
        await _throttle_llm_calls()
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
    """Drive the OpenRouter <-> SearXNG tool loop until the reply contains JSON.

    Flow: call the API with the tools; the model reasons and issues a
    searxng_search tool call; the search result is appended to ``messages``
    and sent back to the API; repeat until the assistant answers with a
    message containing a JSON object (the valuation), or the round limit is
    hit. ``messages`` is mutated so the caller keeps the full history.
    """
    tool_specs = tools if tools is not None else [SEARXNG_SEARCH_TOOL]
    searches_done = 0
    last_message: dict | None = None

    for round_num in range(1, LLM_MAX_TOOL_ROUNDS + 1):
        logger.info("OpenRouter round %d/%d", round_num, LLM_MAX_TOOL_ROUNDS)
        message = await _openrouter_chat(messages, tool_specs)
        last_message = message

        # The model asked to run one or more searches: execute them and feed
        # the observations back into the conversation.
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                function = call.get("function", {})
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                query = str(arguments.get("query", ""))
                messages.append(await _run_search_tool(call, query))
                searches_done += 1
            continue

        content = message.get("content") or ""
        action = _find_json_object(content)

        # Fallback for models that follow the {"action":"search"} JSON
        # protocol from the system prompt instead of native tool calls.
        if action and str(action.get("action", "")).lower() == "search":
            if searches_done >= LLM_MAX_SEARCHES:
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": "Search limit reached. Reply now with the "
                        "final valuation JSON in BRL, using the results above.",
                    }
                )
                continue
            query = re.sub(r"[<>]", " ", str(action.get("query", "")))
            query = re.sub(r"\s+", " ", query).strip()
            observations = await run_search([query])
            searches_done += 1
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": f"Search results for {query!r}:\n{observations}",
                }
            )
            continue

        # Any other JSON object is the final valuation answer.
        if action:
            logger.info(
                "OpenRouter returned final JSON after %d round(s): %s...",
                round_num,
                content[:200],
            )
            return message

        logger.warning("OpenRouter round %d: no tool call and no JSON, nudging", round_num)
        messages.append({"role": "assistant", "content": content})
        messages.append(
            {
                "role": "user",
                "content": "That reply had no search action and no valuation. "
                "Answer with exactly ONE JSON object: "
                '{"new_price":...,"used_price":...,"estimated_value":...,'
                '"currency":"BRL","summary":"..."}.',
            }
        )

    logger.error("OpenRouter did not produce JSON after %d rounds", LLM_MAX_TOOL_ROUNDS)
    if last_message is not None and (last_message.get("content") or "").strip():
        return last_message
    raise RuntimeError(
        f"OpenRouter did not return a JSON answer after {LLM_MAX_TOOL_ROUNDS} rounds"
    )


async def _run_search_tool(call: dict, query: str) -> dict:
    """Execute one searxng_search tool call and return the tool response message."""
    function = call.get("function", {})
    name = function.get("name", "searxng_search")
    if query.strip():
        observations = await run_search([query])
    else:
        observations = json.dumps(
            [{"query": query, "error": "missing 'query' argument"}], ensure_ascii=False
        )
    return {
        "role": "tool",
        "tool_call_id": call.get("id", ""),
        "name": name,
        "content": observations,
    }


class _ModelUnavailableError(Exception):
    """Model/provider could not serve the request; advance the fallback chain."""

    def __init__(self, model: str, reason: str, rate_limited: bool = False):
        super().__init__(reason)
        self.model = model
        self.rate_limited = rate_limited


def _model_chain() -> list[tuple[str, str, str, str]]:
    """(base_url, api_key, model, label) pairs: primary list then backup list."""
    chain = [
        (OPENROUTER_BASE_URL, OPENROUTER_API_KEY, model, "primary")
        for model in OPENROUTER_MODELS
    ]
    if OPENROUTER_API_KEY1 and OPENROUTER_MODELS1:
        chain += [
            (OPENROUTER_BASE_URL1, OPENROUTER_API_KEY1, model, "backup")
            for model in OPENROUTER_MODELS1
        ]
    return chain


async def _openrouter_chat(messages: list[dict], tools: list[dict]) -> dict:
    """Send a chat completion, walking the model list on rate limits.

    On 429: wait LLM_RATE_LIMIT_FALLBACK_SECONDS and advance to the next
    model in the OPENROUTER_MODEL list; when every model is rate limited the
    backup provider (OPENROUTER_*1 env vars) takes over. Transient 5xx or
    network failures are retried per model before also advancing.
    """
    chain = _model_chain()
    if not chain:
        raise RuntimeError("No OPENROUTER_MODEL configured")

    last_error: Exception | None = None
    for index, (base_url, api_key, model, label) in enumerate(chain):
        if label == "backup" and chain[index - 1][3] == "primary":
            logger.warning(
                "All primary models exhausted; falling back to backup provider %s", base_url
            )
        try:
            return await _openrouter_request(
                base_url, api_key, model, label, messages, tools
            )
        except _ModelUnavailableError as e:
            last_error = e
            if index + 1 >= len(chain):
                break
            if e.rate_limited:
                logger.warning(
                    "%s model %s rate limited (429), waiting %ss then switching to next model",
                    label,
                    model,
                    int(LLM_RATE_LIMIT_FALLBACK_SECONDS),
                )
                await _sleep(LLM_RATE_LIMIT_FALLBACK_SECONDS)
            else:
                logger.warning("%s model %s unavailable (%s), switching", label, model, e)

    if chain[0][3] == "primary" and chain[-1][3] == "primary":
        logger.warning(
            "No backup provider configured (set OPENROUTER_API_KEY1/OPENROUTER_MODEL1)"
        )
    raise RuntimeError(f"All models rate limited or unavailable: {model} ({last_error})")


async def _openrouter_request(
    base_url: str,
    api_key: str,
    model: str,
    label: str,
    messages: list[dict],
    tools: list[dict],
) -> dict:
    """One model's slot: 3 tries on transient errors; 429 advances the chain."""
    url = f"{base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }

    logger.info("Calling %s provider model=%s url=%s", label, model, url)

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            await _throttle_llm_calls()
            async with httpx.AsyncClient(timeout=OPENROUTER_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 429:
                raise _ModelUnavailableError(model, "rate limited (429)", rate_limited=True)

            if response.status_code >= 500:
                last_error = RuntimeError(f"status {response.status_code}")
                logger.warning(
                    "%s model %s attempt %d got status %d", label, model, attempt, response.status_code
                )
                if attempt < 3:
                    await _sleep(2 ** attempt)
                continue

            if response.status_code == 401:
                raise RuntimeError(
                    f"{label} provider returned 401 Unauthorized — check its API key"
                )

            response.raise_for_status()
            data = response.json()
            choices = data.get("choices")
            if not choices:
                raise RuntimeError(f"{label} provider returned no choices: {data!r}")

            message = choices[0].get("message")
            if not message:
                raise RuntimeError(f"{label} provider choice has no message: {choices[0]!r}")

            reasoning = message.get("reasoning") or ""
            if reasoning:
                logger.info("%s reasoning: %s...", label, reasoning[:200])
            return message

        except _ModelUnavailableError:
            raise
        except httpx.HTTPStatusError:
            raise
        except httpx.RequestError as e:
            last_error = e
            logger.warning("%s model %s attempt %d request error: %s", label, model, attempt, e)
            if attempt < 3:
                await _sleep(2 ** attempt)
        except RuntimeError:
            raise
        except Exception as e:
            last_error = e
            logger.warning("%s model %s attempt %d unexpected error: %s", label, model, attempt, e)
            if attempt < 3:
                await _sleep(2 ** attempt)

    raise _ModelUnavailableError(model, f"failed after 3 attempts: {last_error}")


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


_last_llm_call_ts = 0.0
_llm_call_lock = asyncio.Lock()


async def _throttle_llm_calls() -> None:
    """Ensure at least LLM_MIN_CALL_GAP_SECONDS between consecutive LLM requests."""
    global _last_llm_call_ts
    async with _llm_call_lock:
        now = time.monotonic()
        wait = LLM_MIN_CALL_GAP_SECONDS - (now - _last_llm_call_ts)
        if wait > 0:
            logger.debug("Throttling LLM call for %.2fs", wait)
            await _sleep(wait)
        _last_llm_call_ts = time.monotonic()


async def call_llm(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Dispatch to the configured LLM provider."""
    logger.info(f"Using LLM provider: {LLM_PROVIDER}")
    if LLM_PROVIDER == "api":
        return await call_openrouter(messages, tools)
    return await call_llamacpp(messages, tools)
