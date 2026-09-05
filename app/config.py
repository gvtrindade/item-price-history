"""Shared configuration: environment-driven settings, constants and logging."""

from __future__ import annotations

import logging
import os
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("book_price_finder")

WEBHOOK_TIMEOUT_SECONDS = 10.0

LLAMA_CPP_BASE_URL = os.getenv("LLAMA_CPP_BASE_URL", "http://localhost:8081").rstrip("/")
LLAMA_CPP_MODEL = os.getenv("LLAMA_CPP_MODEL", "local-model")
LLAMA_CPP_TIMEOUT_SECONDS = float(os.getenv("LLAMA_CPP_TIMEOUT_SECONDS", "180"))
LLM_MAX_TOOL_ROUNDS = 8
LLM_MAX_SEARCHES = 3

SEARXNG_BASE_URL = os.getenv("SEARXNG_BASE_URL", "http://localhost:8080").rstrip("/")
SEARXNG_TIMEOUT_SECONDS = 15.0
SEARXNG_MAX_RESULTS = 5
SEARXNG_COUNTRY = os.getenv("SEARXNG_COUNTRY", "br")
SEARXNG_LANGUAGE = os.getenv("SEARXNG_LANGUAGE", "pt-BR")

# All books to be valued are used copies; without a used-price sighting,
# the value falls back to this fraction of the new-copy price.
USED_FALLBACK_RATIO = 0.40

ISBN_RE = re.compile(r"^(?:\d{9}[\dXx]|\d{13})$")
