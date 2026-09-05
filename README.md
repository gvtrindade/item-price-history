# Book Price Finder

FastAPI service that receives a book (ISBN, or title + author) plus its
conservation state, resolves the missing data via
[openlibrary-client](https://github.com/internetarchive/openlibrary-client),
asks a local **llama.cpp** instance to search the web (via **SearXNG**) for
the book's price and estimate the copy's value, and delivers the result --
**or the error** -- to a webhook.

## Flow

```
POST /lookup {isbn | title+author, conservation_state, webhook_url}
  -> Open Library: fill in the missing title/author or ISBN
  -> llama.cpp (OpenAI-compatible chat): searches the web through this
     service's SearXNG for new and used prices, adjusts for the
     conservation state, answers with JSON {new_price, used_price,
     estimated_value, currency, ...}
  -> webhook: POST {isbn, title, author, price, estimated_value, ...}
```

All books are valued as **used copies**, priced in **BRL** from
**Brazilian** listings (SearXNG runs with `country=br&language=pt-BR`).
The estimated value never exceeds the found new-copy price, and when no
used-copy price is found it falls back to **40% of the new price**
(enforced server-side, regardless of what the LLM replies).

## Setup

> `openlibrary-client==0.0.30` pins `six`/`requests`/`urllib3` versions that
> are broken on modern Python, and its exact pins conflict with working ones
> in a single resolve step. Install it first, then let `requirements.txt`
> override the stale pins (pip warnings about the conflict are expected).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install "openlibrary-client==0.0.30"
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

Dependencies (started separately):

- **SearXNG**: `docker compose up -d` (see below)
- **llama.cpp**: any OpenAI-compatible server, e.g.
  `llama-server -m model.gguf --port 8081`. Native tool-calling is used
  when the server runs with `--tool-call-parser`; otherwise the LLM
  drives SearXNG through a plain JSON action protocol, which works with
  any chat model.

| env var                    | default                    | purpose                       |
|----------------------------|----------------------------|-------------------------------|
| `LLAMA_CPP_BASE_URL`       | `http://localhost:8081`    | llama.cpp OpenAI-compat URL   |
| `LLAMA_CPP_MODEL`          | `local-model`              | model name sent in requests   |
| `LLAMA_CPP_TIMEOUT_SECONDS`| `120`                      | per-request LLM timeout       |
| `SEARXNG_BASE_URL`         | `http://localhost:8080`    | SearXNG instance URL          |
| `SEARXNG_COUNTRY`          | `br`                       | SearXNG country boost         |
| `SEARXNG_LANGUAGE`         | `pt-BR`                    | SearXNG language/locale       |

## Usage

```bash
curl -X POST http://localhost:8000/lookup \
  -H 'Content-Type: application/json' \
  -d '{"isbn": "9780140449136", "conservation_state": "used, good condition", "webhook_url": "https://your-hook.example/recv"}'
```

or let the service find the ISBN:

```bash
curl -X POST http://localhost:8000/lookup \
  -H 'Content-Type: application/json' \
  -d '{"title": "The Hobbit", "author": "J.R.R. Tolkien", "conservation_state": "used, like new", "webhook_url": "https://your-hook.example/recv"}'
```

Request body:

| field                | description                                          |
|----------------------|------------------------------------------------------|
| `isbn`               | ISBN-10 or ISBN-13 (hyphens allowed); **or** give    |
| `title` + `author`   | book title and author so the ISBN gets resolved      |
| `conservation_state` | condition of the (used) copy, e.g. `like new`, `good`, `acceptable, worn` |
| `webhook_url`        | URL to POST the result to (must be http/https)       |

The webhook receives:

```json
{
  "isbn": "9780140449136",
  "title": "Crime and punishment",
  "author": "Fiódor Dostoievski",
  "conservation_state": "used, good condition",
  "status": "ok",
  "price": 39.9,
  "new_price": 59.9,
  "used_price": 39.9,
  "estimated_value": 47.92,
  "currency": "BRL",
  "summary": "Cópias usadas a R$ 39,90 na Estante Virtual; ..."
}
```

or, on failure (book not found, LLM/SearXNG error, ...):

```json
{"isbn": "9781234567890", "status": "error", "error": "Book not found in Open Library"}
```

The endpoint's own response echoes the payload plus `webhook_delivered`
(whether the webhook accepted it).

## SearXNG (docker compose)

```bash
docker compose up -d
```

- Web UI: http://localhost:8080
- Health: http://localhost:8080/healthz
- JSON API: `http://localhost:8080/search?q=<query>&format=json`

Config lives in `./searxng/settings.yml` (JSON format enabled, limiter off
for local use; `secret_key` must be rotated before any real deployment).
