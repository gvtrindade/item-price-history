"""Shared configuration: environment-driven settings, constants and logging."""

from __future__ import annotations

import logging
import os
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("book_price_finder")

WEBHOOK_TIMEOUT_SECONDS = 10.0

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "llamacpp").lower()

LLAMA_CPP_BASE_URL = os.getenv("LLAMA_CPP_BASE_URL", "http://localhost:8081").rstrip("/")
LLAMA_CPP_MODEL = os.getenv("LLAMA_CPP_MODEL", "local-model")
LLAMA_CPP_TIMEOUT_SECONDS = float(os.getenv("LLAMA_CPP_TIMEOUT_SECONDS", "180"))

def _parse_model_list(raw: str) -> list[str]:
    return [m.strip() for m in raw.split(",") if m.strip()]


# OPENROUTER_MODEL accepts a comma-separated list; on 429 the client walks
# through it and then falls back to the backup provider (*_1 vars).
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api").rstrip("/")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_MODELS = _parse_model_list(OPENROUTER_MODEL)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_TIMEOUT_SECONDS = float(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "180"))

# Backup provider: tried when every primary model is rate limited.
OPENROUTER_BASE_URL1 = os.getenv("OPENROUTER_BASE_URL1", "https://openrouter.ai/api").rstrip("/")
OPENROUTER_MODEL1 = os.getenv("OPENROUTER_MODEL1", "")
OPENROUTER_MODELS1 = _parse_model_list(OPENROUTER_MODEL1)
OPENROUTER_API_KEY1 = os.getenv("OPENROUTER_API_KEY1", "")

LLM_MAX_TOOL_ROUNDS = 15
LLM_MAX_SEARCHES = 3
LLM_MIN_CALL_GAP_SECONDS = 2.0
LLM_RATE_LIMIT_FALLBACK_SECONDS = 5.0

SEARXNG_BASE_URL = os.getenv("SEARXNG_BASE_URL", "http://localhost:8080").rstrip("/")
SEARXNG_TIMEOUT_SECONDS = 15.0
SEARXNG_MAX_RESULTS = 5
SEARXNG_COUNTRY = os.getenv("SEARXNG_COUNTRY", "br")
SEARXNG_LANGUAGE = os.getenv("SEARXNG_LANGUAGE", "pt-BR")

# All books to be valued are used copies; without a used-price sighting,
# the value falls back to this fraction of the new-copy price.
USED_FALLBACK_RATIO = 0.40

ISBN_RE = re.compile(r"^(?:\d{9}[\dXx]|\d{13})$")
