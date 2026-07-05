# `src/research/` — the notebook walkthrough

This folder holds the **pedagogical version** of the dupe price-finder:

- **`agent_dupe_price_finder.ipynb`** — a linear, cell-by-cell walkthrough of the
  whole pipeline, written to be *read*. Every stage is explained inline before the
  code that implements it: the two-agent split, the deterministic `optimize()`
  ranker, URL classification, fetch-and-verify, and conflict resolution.
- **`adk_setup.py`** — loads the ADK environment and API keys from `.env`.
- **`traces/`** — JSONL run traces and outcome JSON files (gitignored; regenerated
  on every run).

## Notebook vs. `src/backend/pipeline.py`

The notebook is the teaching artifact; **[`src/backend/pipeline.py`](../backend/pipeline.py)
is the production source of truth** that the FastAPI web app actually serves. The
two share the same architecture and most functions are identical. `pipeline.py` has
since picked up a few small robustness fixes that the notebook does not carry, so it
stays a clean read:

- **Grounding title fallback** — newer Gemini API responses put the bare domain in
  the `title` field instead of `domain`; `pipeline.py` accepts either when
  backfilling dropped URLs.
- **Store-over-blog price preference** — `optimize()` prefers an e-commerce price
  over a blog/review price when both exist, so the "buy here" link always points at
  a real store.
- **Sentinel-vs-offer conflict rule** — when a "discontinued"/"not found" sentinel
  shows up alongside real offers, the offers win (a shade can be discontinued yet
  still in stock somewhere), instead of the sentinel erasing them.
- **Optional concurrency** — `run_tie_set()` can fan out candidates with an
  `asyncio.Semaphore`; the notebook runs them sequentially, which is simpler to
  follow and gentler on retailer bot-walls.

If you want to understand *how the pipeline thinks*, read the notebook. If you want
the exact code that runs in the deployed app, read `src/backend/pipeline.py`.
