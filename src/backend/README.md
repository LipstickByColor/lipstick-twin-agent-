# `src/backend/` — FastAPI service + pipeline

The production source of truth for the dupe price-finder. The root
[`README.md`](../README.md) covers *why* the pipeline is built this way; this file
covers *how to run and reason about* the two files in here.

- **`pipeline.py`** — the two-agent pipeline and all its logic: `run_tie_set()`
  orchestration, `price_one_candidate()` (search → URL recovery → targeted search →
  fetch-and-verify → conflict resolution), the deterministic `optimize()` ranker,
  URL classification, and JSONL trace logging. This is the notebook in
  [`../research/`](../research/) hardened into a module — see that folder's README
  for the small deltas between them.
- **`main.py`** — a thin FastAPI wrapper: request validation, a 6-hour in-process
  result cache, usage logging, and static hosting of the [`../web/`](../web/)
  frontend.

## Run it

```bash
cd src/backend
~/.pyenv/versions/3.11.9/bin/python -m uvicorn main:app --port 8000
```

Then open http://localhost:8000. First run is 1–2 minutes (live agents); repeats hit
the cache and return instantly.

**Requires** `GOOGLE_API_KEY` in the repo-root `.env` (loaded automatically via
`python-dotenv`). Python 3.11+ with `google-adk`, `google-genai`, `mcp`,
`mcp-server-fetch`, `fastapi`, `uvicorn`, `python-dotenv`.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/find-dupes` | Price a tie set and return the winning dupe. Body: `{tie_set: [...], country, strategy, client}`. |
| `POST /api/log` | Lightweight UI-event beacon (e.g. searches that matched nothing). |
| `GET /` + `/*` | Serves the static web app from `../web/`. |

`strategy` is validated to `"cheapest"` (default) or `"closest"` — anything else
returns a 422. `tie_set` accepts 1–6 candidates. The `client` blob is logged for
analysis but never fed to the pipeline, and is excluded from the cache key.

## Configuration

| Knob | Where | Default | Notes |
|---|---|---|---|
| `GOOGLE_API_KEY` | root `.env` | — | Required; ADK reads it. |
| `CANDIDATE_CONCURRENCY` | env var | `1` | Candidates priced sequentially by default — concurrent retailer fetches trip bot-walls. Raise cautiously. |
| `MODEL` | `pipeline.py` | `gemini-2.5-flash` | Model for both agents. |
| `_CACHE_TTL_S` | `main.py` | `6h` | Outcome cache lifetime; prices are stable for hours. |
| `_FETCH_TIMEOUT_S` | `pipeline.py` | `45s` | Max wait for one MCP fetch call. |

The MCP fetch server runs with `--ignore-robots-txt` — a deliberate, scoped choice
documented at the flag in `pipeline.py` and in the root README's "Key design
choices."

## Observability

Every run writes to [`../research/traces/`](../research/traces/) (gitignored):

- one `*.jsonl` per candidate — every ADK event (tool calls, responses, model text);
- `usage.jsonl` — one line per request (winner, tradeoff, prices found, cache hit,
  duration);
- `*_outcome.json` — the full result (every price, URL, page title) for one request.

The cache is in-process only, so these files are the durable record of what ran.
