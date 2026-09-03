# Book Price Finder

FastAPI service that receives an ISBN + a webhook URL, resolves the book
title via [openlibrary-client](https://github.com/internetarchive/openlibrary-client),
and delivers the result -- **or the error** -- to the webhook.

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

## Usage

```bash
curl -X POST http://localhost:8000/lookup \
  -H 'Content-Type: application/json' \
  -d '{"isbn": "9780140449136", "webhook_url": "https://your-hook.example/recv"}'
```

Request body:

| field        | description                                    |
|--------------|------------------------------------------------|
| `isbn`       | ISBN-10 or ISBN-13 (hyphens allowed)           |
| `webhook_url`| URL to POST the result to (must be http/https) |

The webhook receives:

```json
{"isbn": "9780140449136", "status": "ok", "title": "Crime and punishment"}
```

or, on failure (book not found, Open Library error, ...):

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
