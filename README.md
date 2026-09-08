# llm-places-finder

**Ask a local LLM "sushi near me" — get a real map, not a hallucinated address.**

A local model (Ollama via Open WebUI) decides *when* a place search is needed and *what to search for*. A standalone FastAPI backend does the actual lookup and returns an embedded map that renders inline in the chat — with a plain link fallback. Runs **zero-config on free OpenStreetMap data**; switch to Google Maps with two env vars when you need it.

> New here? **Start with the 60-second quickstart below.** For the reasoning behind the design, see [`docs/DECISIONS.md`](docs/DECISIONS.md).

## 60-second quickstart

No API key, no signup, no configuration. Pick one path:

**Docker (recommended):**

```bash
docker compose up --build
```

**Manual:**

```bash
pip install -r requirements.txt
uvicorn api.main:app --reload
```

Then verify — same check for both paths, no Open WebUI needed:

```bash
curl -X POST localhost:8000/places/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"ramen","near":"Malang"}'
curl localhost:8000/health
```

You should get a JSON payload with `embed_html` (an inline map iframe), `fallback_link`, and a `results` list — plus `{"status":"ok"}` from `/health`. Interactive API docs live at `http://localhost:8000/docs` (ReDoc at `/redoc`; `/` redirects to Swagger UI).

## What a chat looks like

With the Tool enabled in Open WebUI, type any of these:

- `Find sushi near me` → model calls `find_places(query="sushi")` → inline map of the top result.
- `Nearest pharmacy in Malang` → `find_places(query="pharmacy", near="Malang")`.
- Follow-up: `How do I get there from Malang station?` → `get_directions(origin="Malang station", destination_place_id="<from results>")` → inline route map with distance and duration.

End to end: LLM decides → Tool POSTs to `localhost:8000/places/search` → backend queries the maps provider → backend returns `{embed_html, fallback_link, results}` → Tool renders the map inline in chat.

## How it works

Three processes, one rule — **the LLM never touches the maps API**:

- **LLM (Ollama)** — decides when a search is needed and which arguments to pass. Holds no key and no maps logic.
- **Open WebUI Tool** (`tools/find_places.py`) — a thin HTTP client. Forwards the model's call to the backend and renders the returned HTML. No key, no business logic, stdlib + `httpx` only (it gets copied verbatim into Open WebUI).
- **FastAPI backend** (`api/`) — the only thing that talks to a maps provider. Owns the key, the cache, and the rate limiter, so quota protection covers every caller — not just one chat UI.

```
User → LLM → Tool → FastAPI backend → maps provider (OSM / Google)
                       ↑ key, cache, rate limit all live here
```

## Providers: free by default, Google when you need it

| | OSM (default) | Google (`MAPS_PROVIDER=google`) |
|---|---|---|
| Cost / signup | Free, no key, works immediately | Paid — billing + API key required |
| Search | Nominatim | Places Text Search (better fuzzy matching, ratings) |
| Nearby / category | Overpass API | Nearby Search |
| Routing | OSRM demo server | Directions API |
| Tradeoffs | Community servers, rate-limited, no reviews | Stronger search quality, billable per call |

Switching needs no code changes — just two env vars (see Configuration). The endpoint contracts, response shapes, rate limiting, and error types are identical either way.

## Prerequisites

- Python 3.11+ (manual path) or Docker (container path).
- For the full chat experience: [Ollama](https://ollama.com) with a function-calling-capable model pulled (used here: `llama3.1:8b`; `mistral`, `qwen2.5`, and similar work the same way), plus [Open WebUI](https://openwebui.com) (default `http://localhost:3000`) connected to Ollama.
- Google mode only: a Google Cloud project with **billing enabled**, these APIs enabled — **Places API, Geocoding API, Directions API, Maps Embed API** — and an API key **restricted** to those APIs (plus an IP restriction to your server). Set a **budget alert / daily quota cap** in Google Cloud Console — a manual step, not enforced by code.

## Setup (full)

1. Clone and enter the repo:
   ```bash
   git clone <repo-url> llm-places-finder
   cd llm-places-finder
   ```
2. Run it — Docker or manual (see quickstart above). No `.env` file is needed: all settings have working defaults and a clean clone runs immediately on OSM.
3. (Only if customizing) Copy and edit the environment:
   ```bash
   cp .env.example .env
   ```
   Set `MAPS_PROVIDER=google` + `GOOGLE_MAPS_API_KEY` for Google mode, or tune rate limits, timeouts, and CORS origins. `docker compose` picks up `.env` automatically when present; `uvicorn` loads it too.
4. Connect Open WebUI (chat path only — skip for pure API use): copy `tools/find_places.py` into Open WebUI's Tools directory (or paste via Admin UI → Tools), set `BACKEND_API_URL` if your backend isn't on `http://localhost:8000`, and enable it for your model. Ollama and Open WebUI always run separately — they are not part of the container.

## Configuration

Every variable has a working default; `.env` is only needed to override them. See `.env.example` for the full documented list.

| Variable | Default | What it does |
|---|---|---|
| `MAPS_PROVIDER` | `osm` | `osm` (free, no key) or `google` (paid, key required) |
| `GOOGLE_MAPS_API_KEY` | _(empty)_ | Required only when `MAPS_PROVIDER=google`; missing key fails fast with a clear error |
| `BACKEND_API_URL` | `http://localhost:8000` | Where the Open WebUI Tool reaches the backend |
| `MAPS_REQUEST_TIMEOUT_SECONDS` | `10` | Timeout for outbound provider requests |
| `MAX_RESULTS` | `5` | Max places returned per search |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | `30` / `60` | Per-IP sliding-window limit on upstream-calling endpoints |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:3000,http://localhost:8080` | Locked-down caller origins (never `*`) |

## API reference

| Endpoint | Purpose |
|---|---|
| `POST /places/search` | Free-text search (`query`, optional `near`) → embed map + normalized results |
| `POST /places/nearby` | Category search (`category`, `lat`, `lng`, `radius_meters`) → same shape as search |
| `POST /places/directions` | Route (`origin`, `destination_place_id`) → route iframe + distance/duration |
| `GET /health` | Liveness check (`{"status":"ok"}`), unthrottled, used by the Docker healthcheck |

Full request/response schemas with examples: run the server and open `http://localhost:8000/docs`. Error shape is uniform: `400` for bad input, `429` for rate limit, `502` for provider failure or server misconfiguration (e.g. Google mode without a key).

## Project structure

```
api/               standalone FastAPI backend (key, cache, rate limit live here)
  providers/       pluggable backends: base interface + google + osm (+ shared helpers)
tools/             Open WebUI thin client (no key, no maps logic)
tests/             mocked unit + endpoint tests — no real network calls
docs/DECISIONS.md  architecture, tradeoffs, and what was left out (start here for review)
```

## Testing

```bash
python3 -m pytest -q
```

All external calls are mocked; the suite runs offline in under a second.

**Scope and next steps** — deliberate boundaries of the current design, each with a documented follow-up in [`docs/DECISIONS.md`](docs/DECISIONS.md):

- No automatic quota rotation — one key, backend rate limit only.
- Single-language support (whatever the provider returns; no translation layer).
- In-memory TTL cache (5 min) and rate limiter are per-process; lost on restart, not shared across instances — Redis is the documented next step.
- No auth on the backend — localhost-only by design; add an API-key header before exposing it.
- No multi-turn location memory (each call is stateless; "near me" needs an explicit `near`).
- Keyless iframe embeds keep the secret server-side; styling/behavior limited to what the universal embed supports.
