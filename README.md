# Product Price Finder

FastAPI service that receives a product (code, or title + author) plus its
conservation state, resolves the missing data via
[openlibrary-client](https://github.com/internetarchive/openlibrary-client),
asks a local **llama.cpp** instance to search the web (via **SearXNG**) for
the product's price and estimate the copy's value, and delivers the result --
**or the error** -- to a webhook.

## Flow

```
POST /lookup {code | title+author, conservation_state, webhook_url}
  -> Open Library: fill in the missing title/author or code
     (Google Books takes over when the product is not found or the lookup
     fails, e.g. broken author records)
  -> llama.cpp (OpenAI-compatible chat): searches the web through this
     service's SearXNG for new and used prices, adjusts for the
     conservation state, answers with JSON {new_price, used_price,
     estimated_value, currency, ...}
  -> webhook: POST {code, title, author, price, estimated_value, ...}
```

All products are valued as **used copies**, priced in **BRL** from
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
  -d '{"code": "9780140449136", "conservation_state": "used, good condition", "webhook_url": "https://your-hook.example/recv"}'
```

or let the service find the code:

```bash
curl -X POST http://localhost:8000/lookup \
  -H 'Content-Type: application/json' \
  -d '{"title": "The Hobbit", "author": "J.R.R. Tolkien", "conservation_state": "used, like new", "webhook_url": "https://your-hook.example/recv"}'
```

Request body:

| field                | description                                          |
|----------------------|------------------------------------------------------|
| `code`               | code (ISBN-10 or ISBN-13, hyphens allowed); **or** give |
| `title` + `author`   | product title and author so the code gets resolved    |
| `conservation_state` | condition of the (used) copy, e.g. `like new`, `good`, `acceptable, worn` |
| `webhook_url`        | URL to POST the result to (must be http/https)       |

The webhook receives:

```json
{
  "code": "9780140449136",
  "title": "Crime and punishment",
  "author": "Fiódor Dostoievski",
  "conservation_state": "used, good condition",
  "status": "ok",
  "price": 39.9,
  "new_price": 59.9,
  "used_price": 39.9,
  "estimated_value": 47.92,
  "currency": "BRL",
  "summary": "Cópias usadas a R$ 39,90; ..."
}
```

or, on failure (product not found, LLM/SearXNG error, ...):

```json
{"code": "9781234567890", "status": "error", "error": "Product not found in Open Library"}
```

The endpoint's own response echoes the payload plus `webhook_delivered`
(whether the webhook accepted it).

## Bookstore + laya flow

`POST /lookup/bookstore` is an alternative lookup that skips Open
Library, SearXNG and the generative LLM entirely:

```
POST /lookup/bookstore {code | title+author, conservation_state, webhook_url}
  -> bookstore: crawl the search page
     (ISBN: /busca/<isbn>, falling back to /busca?nsCat=Natural&q=<isbn>
      when the path URL hits the bot wall;
      title+author: /busca?nsCat=Natural&q=<title author>&searchField=titulo-autor)
  -> trim: the 200KB-1.5MB HTML is reduced to the embedded
     window.__INITIAL_STATE__ JSON, then to a compact candidate list
     (title, author, isbn, condition, price per listing)
  -> laya (https://github.com/NandhaKishorM/laya, in-process typed-decision
     engine): a choice question identifies which candidate IS the book --
     sellers sometimes attach the wrong ISBN to a listing -- then one
     yes/no question per candidate decides which listings are copies of it
  -> pricing rules: used_price = cheapest used match, new_price = cheapest
     new match, estimated_value = same conservation-state ratios as /lookup
  -> webhook: POST {code, title, author, new_price, used_price,
     estimated_value, currency, summary, source: "bookstore", ...}
```

The request body and webhook contract are the same as `/lookup`.

laya runs in-process via its Python SDK: the first prediction downloads the
pinned checkpoint (multilingual by default, ~650MB) into the Hugging Face
cache. On machines without CUDA, install CPU-only torch **before** laya to
avoid the multi-GB nvidia wheels:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install laya
```

| env var                     | default                            | purpose                          |
|-----------------------------|------------------------------------|----------------------------------|
| `BOOKSTORE_BASE_URL`        | `https://www.estantevirtual.com.br`| search site base URL             |
| `BOOKSTORE_TIMEOUT_SECONDS` | `20`                               | per-request crawl timeout        |
| `BOOKSTORE_MAX_CANDIDATES`  | `20`                               | max listings sent to laya        |
| `LAYA_MATCH_THRESHOLD`      | `0.9`                              | min P(yes) for a listing to match|
| `LAYA_MODEL`                | `multilingual`                     | laya checkpoint per predict call |

Keep the threshold strict: a wrong listing corrupts the minimum price, while
a missed listing rarely changes it. Example saved search pages live in
`examples/` for offline testing of the trim/match steps.

## SearXNG (docker compose)

```bash
docker compose up -d
```

- Web UI: http://localhost:8080
- Health: http://localhost:8080/healthz
- JSON API: `http://localhost:8080/search?q=<query>&format=json`

Config lives in `./searxng/settings.yml` (JSON format enabled, limiter off
for local use; `secret_key` must be rotated before any real deployment).
