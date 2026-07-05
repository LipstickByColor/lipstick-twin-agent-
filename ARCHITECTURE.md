# Architecture & repo layout

A map of the codebase. For *why* the system is built this way (the problem, the
two-agent design, the reliability story), see the [README](README.md); this file
covers *where things live* and *how a request flows through them*.

## Directory layout

```
.
├── README.md            project overview — problem, design, demo
├── ARCHITECTURE.md      this file — repo map + request flow
├── CHANGELOG.md         version history
├── LICENSE
├── requirements.txt     Python deps (backend + MCP fetch server)
├── src/                 all application code
│   ├── backend/         FastAPI service + the two-agent pipeline
│   │   ├── main.py         request validation, 6h cache, usage logging, static host
│   │   └── pipeline.py     tie-set loop, search→verify→resolve, ΔE math, ranker, traces
│   ├── web/             no-build single-page UI
│   │   ├── Find Your Lipstick Twin.dc.html   the whole app
│   │   ├── Lipstick.dc.html                  standalone lipstick-tube component
│   │   └── support.js                        generated dc-runtime (do not edit)
│   └── research/        the notebook the pipeline was prototyped in
│       ├── agent_dupe_price_finder.ipynb     linear, teachable walkthrough
│       ├── adk_setup.py                       loads ADK env + keys
│       └── traces/                            JSONL run traces + outcome JSON (gitignored)
├── assets/              README screenshots, demo GIF, pipeline diagram
└── docker/              one-container packaging
    ├── Dockerfile
    ├── compose.yaml
    ├── entrypoint.sh       prompts for / accepts the Gemini API key
    └── README.md           container run instructions
```

Each code folder has its own README with the details:
[`src/backend/`](src/backend/README.md) ·
[`src/web/`](src/web/README.md) ·
[`src/research/`](src/research/README.md) ·
[`docker/`](docker/README.md).

## How the pieces connect

One process does almost everything. `src/backend/main.py` (FastAPI + uvicorn):

- **static-hosts** the `src/web/` UI at `/`, and
- exposes **`POST /api/find-dupes`**, which runs the pipeline in
  `src/backend/pipeline.py`.

The pipeline spawns the **MCP `fetch` server as a subprocess** using the same
Python interpreter (`python -m mcp_server_fetch`) — no separate service — so the
whole thing runs as a single container or a single local uvicorn process.

Paths are anchored to `__file__`, so the `src/` layout matters:
`main.py` finds the UI at `../web` and the pipeline writes traces to
`../research/traces`. The Docker image mirrors this same `src/` tree.

## Request flow

```
Browser (src/web) ──GET /──────────────► main.py serves the .dc.html app
        │  on mount: pull ~9k catalog from Supabase, compute ΔE76 client-side,
        │  build the tie set (anchor + closest twins)
        │
        └──POST /api/find-dupes──────────► main.py
                                             ├─ cache hit? return (6h TTL)
                                             └─ else run_tie_set()  [pipeline.py]
                                                  for each candidate (sequential):
                                                   1. search agent  (Gemini + google_search) → offers w/ evidence
                                                   2. drop excluded retailers, resolve URLs
                                                   3. fetch agent    (MCP fetch subprocess)   → page-verify brand+shade
                                                   4. conflict resolution / quarantine
                                                  → cheapest in-stock twin wins
                                             ├─ write *_outcome.json + per-candidate *.jsonl → src/research/traces
                                             └─ return outcome → UI renders ranked results
```

Deterministic Python owns retries, filtering, dedup, conflict resolution,
ranking, caching, and the final cheapest-price call; the two LLM agents only
gather and verify evidence. See the [README](README.md#architecture) for the
reasoning behind that split.

## Configuration

- **`GOOGLE_API_KEY`** (required) — in the repo-root `.env` locally, or passed to
  the container (`-e` / prompt / compose `env_file`).
- **`CANDIDATE_CONCURRENCY`** (default `1`) — candidates are priced sequentially;
  concurrent retailer fetches trip bot-walls.

Full knob list is in [`src/backend/README.md`](src/backend/README.md#configuration).
