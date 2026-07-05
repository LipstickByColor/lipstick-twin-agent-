# Changelog

Running log of what changed and why — kept for the Kaggle capstone submission.

## 2026-07-05 (backend moved under `src/`, paths updated)

Backend-only; the API contract and pipeline behavior are unchanged.

- **The FastAPI service and pipeline moved from `agent_backend/` to `src/backend/`**
  as part of the repo reorg (code under `src/`, media in `assets/`, container files
  in `docker/`). No request/response changes — `main.py` and `pipeline.py` are the
  same modules in a new home.
- **Two `__file__`-anchored paths updated to the new layout.** `main.py` now
  static-hosts the UI from `../web` (was `../web_app`), and `pipeline.py` writes
  JSONL traces to `../research/traces` (was `../agent_notebook/traces`). uvicorn
  still runs from the backend dir with `main:app`, so the run command is unchanged
  apart from `cd src/backend`.

Verified: local and in-container boot serve `GET / → 200`, and a live
`POST /api/find-dupes` wrote its per-candidate traces and `_outcome.json` to the
new `src/research/traces` path.

## 2026-07-05 (Walmart product pages allowed back in)

- **Consumer Walmart is no longer blanket-excluded.** Walmart was dropped wholesale
  because grounding often surfaced Walmart *search/browse* pages whose quoted price
  belonged to a different variant. But that also threw away legitimate product pages:
  a run recommended a labomme shade at $20.00 while discarding an in-stock, high-
  confidence L'Oréal at **$15.94** whose only fault was a `url: null` from the model —
  the resolvable `walmart.com/ip/…` link was sitting unused in the grounding chunk.
  Now `walmart.com` survives the up-front exclusion, gets its redirect resolved and
  URL backfilled, and is kept only when it lands on a genuine product page
  (`walmart.com/ip/…`); search/browse/category pages are dropped by the new Walmart
  gate. `business.walmart.com` (B2B) and eBay/Target stay hard-excluded.
- **Walmart product pages are always fetch-verified**, even at `confidence: high` —
  Walmart's documented failure mode is confident-but-wrong snippets, so the page
  itself (brand + shade token match) is the only trustworthy check.

## 2026-07-04 (shade pill, product photos)

Two front-end refinements, no backend changes.

- **The shade search bar settles into a pill, like the brand's.** Picking a shade used to
  leave the second search bar as a live input holding loose text; it now collapses into the
  same tagged pill the brand gets (`shade` label + name + "change"), so both steps read the
  same once answered.
- **Product photos throughout.** The Supabase catalog's `image_url` column (all 9,149 rows
  have one) now renders as photo tiles next to the staged shade, the results header, the
  cheapest-twin banner, each dupe card, and the detail screen. Photos are looked up
  client-side from the catalog — the backend request/response contract is untouched. The hex
  swatch always stays alongside: it's the color-match evidence the ΔE runs on, while the
  photo is product recognition. Tiles are CSS backgrounds over the shade's own markup, so a
  dead brand CDN (avon's cert is broken, for example) degrades silently — no broken-image
  icon — and shades without a photo render exactly as before, fetching nothing.

Verified with the JXA harness, extended to 42 assertions (photo tile resolution incl. the
no-image and catalog-miss paths, banner tile follows the winning twin, pill/input swap).

## 2026-07-04 (out-of-stock twins, retry keeps "discontinued", one find link)

Driven by a live run where MAC "Do Not Disturb" — genuinely discontinued — showed no label,
a sold-out twin vanished from results, and the detail screen stacked four Google Shopping
links each polluted with a retailer name.

- **First-try "discontinued" now survives the retry.** When the primary search bails with a
  discontinued sentinel and the short-query retry then finds offers, those offers are usually
  marketplaces/resale stock of a genuinely retired shade — they don't refute the bail. The
  sentinel now rides along into conflict resolution (parked, status lifted) instead of being
  discarded as "wrong." A `not_found` bail is still discarded — real offers do refute that one.
- **Sold-out twins render instead of vanishing.** A candidate with prices but no in-stock
  offer gets a greyed card at the end of the list: "out of stock" (or "stock unknown" for
  blog-only sightings), priced as the compact last-seen range (e.g. `$14–$25`). They never
  compete for the cheapest-twin crown. Detail rows tag out-of-stock listings and link "view"
  instead of "buy"; the discontinued banner gained an "every listing we found was already out
  of stock" variant; the .txt report ranks them last under "Last seen:".
- **One Google Shopping link, clean query.** Offers without a working store URL (and blog
  price checks) now collapse into a single "Google Shopping — find" row showing the price
  range and a "price seen at …" note, instead of one row per phantom retailer. Retailer names
  are dropped from the search query everywhere (detail rows and .txt links) — they only
  skewed Shopping's matching. The green CHEAPEST tag follows the offer that actually set the
  deal price, wherever it lives.

Verified with 8 backend conflict/carry test cases and a 32-assertion JXA harness that now
evals the page's real extracted component (no stale copy) against simulated outcomes.

## 2026-07-04 (discontinued-status surfacing)

The search agent has flagged discontinued shades since 2026-07-03, but the signal died in the
backend and never reached the UI. It now flows end to end, built on the rule that
discontinued ≠ unavailable: retired shades often remain in stock at some retailers, so the
status must coexist with real prices, never replace them.

- **Fixed conflict resolution discarding real prices.** `_resolve_conflicts` short-circuited
  whenever any entry carried a `status`, so a discontinued sentinel arriving alongside real
  offers silently erased those offers. Now the sentinel only wins when there are no offers;
  in a mixed list the offers are kept and the sentinel is parked in the dropped list
  (`_dropped_reason: "sentinel_with_offers"`) so its status stays readable.
- **Candidate-level `status` field.** Each candidate in the API response now carries a
  top-level `status`. `"discontinued"` survives alongside real prices; `"not_found"` is
  dropped once offers exist, since offers contradict it. Kept out of the price field
  deliberately — a string there would break best-price selection, sorting, and savings math.
- **UI surfaces for the status:** the your-shade header shows "Discontinued" in place of a
  missing price (or a small "discontinued" note next to a real one); dupe cards get a red
  "discontinued" mini-tag; the detail screen shows a banner ("The stores below still had
  stock when we checked — it may not last," with a no-stock variant); the .txt report gains
  matching status lines. Wording stays hedged ("appears to be") since it's a model inference.
- **Telemetry:** usage-log summaries now include a per-candidate `statuses` map, and the
  per-candidate run log prints the status next to the price count.
- **Evaluated and rejected "AI Mode via Gemini API"** as a conflict/fallback mechanism: no
  official AI Mode API exists (only unofficial scrapers), and the Gemini API's equivalent —
  Grounding with Google Search — is what the pipeline already uses. Noted as a possible
  future experiment: Gemini 3 allows combining built-in search with function tools, enabling
  a single-agent refactor.

Verified with 6 backend conflict-resolution test cases (including regressions for
same-retailer quarantine and cross-retailer preservation) and a 14-assertion JS harness
driving the UI's render logic with simulated outcomes.

## 2026-07-04 (code-review hardening)

Worked through six external code-review findings, verifying each against the code and the
installed ADK/MCP library sources before acting. Three led to changes, one to a documented
decision, and two were confirmed false positives.

- **Fixed blocking I/O in the async event loop.** Vertex AI redirect resolution used
  synchronous `urllib` inside async pipeline code, which froze the whole uvicorn event loop
  (all concurrent requests) during each network round-trip. Both call sites now run through
  `asyncio.to_thread`, and Stage 1a resolves all redirect links concurrently with
  `asyncio.gather`.
- **Fixed trace/outcome filename collisions.** Trace and outcome filenames used
  second-resolution timestamps, so two requests finishing in the same second silently
  overwrote each other's files. Both now use `%f` microsecond timestamps (kept over UUIDs so
  files still sort chronologically).
- **Replaced all `print()` calls with Python `logging`.** In preparation for hosting the demo
  publicly: 22 sites in `backend/pipeline.py` converted to leveled logger calls (info = run
  narration, warning = degraded paths like 429 waits and fetch timeouts, error = search/retry
  failures). `logging.basicConfig(level=INFO, format="%(message)s")` in `backend/main.py`
  routes output to stderr — never block-buffered under Docker/PaaS, so logs can't be lost or
  reordered — while the bare-message format keeps the indented demo narration unchanged.
- **Documented the deliberate Stage-1-only 429 retry.** Reviewer flagged incomplete 429
  coverage in Stages 1.5/2. Decision: keep as-is — a Stage 1 failure is fatal (zero results,
  worth waiting ~60s), while Stage 1.5/2 failures only degrade verification, and the free-tier
  retry delay (~60s) exceeds Stage 2's 45s fetch timeout anyway. Users shouldn't wait for
  cosmetic recovery; rationale now in a code comment.


## 2026-07-04

- **Fixed false "not found" results (NYX bug).** When the model bailed out with a single
  `not_found`/`discontinued` sentinel entry, the short-query fallback never fired because the
  sentinel counted as a result. The pipeline now detects a sentinel-only response and retries
  once with the short `brand + shade` query before accepting "not found." Verified: NYX
  "On a Mission" now prices correctly (Ulta, $13, direct SKU link).
- **Added `source_type` classification (ecommerce / blog / other).** The search agent now
  labels where each price was seen. Blog/review prices (e.g. temptalia.com) still count as
  grounded evidence, but the UI gives them a "find" (Google Shopping search) link instead of
  a "buy" link, tags them "price check" instead of "in stock," and the downloaded .txt report
  marks them as "price seen on review site."
- **Best-price selection is now store-first.** A blog price can no longer win as the deal
  price when a real store carries the product (Chanel was showing temptalia's $40 instead of
  Ulta's $53). Blog prices are only used when no store has a price, so blog-only candidates
  stay visible. Applied in both the backend strategy scoring and the UI offer sort.
- **UI polish:** the "doppelgänger" match tier changed from yellow to light green so the
  four-tier meter reads as a single green ramp (deep green → green → yellow-green → light green).
- **README:** documented the source-type / find-vs-buy behavior and the short-query retry;
  added a "How it works" mermaid flowchart of the full two-stage pipeline.

## 2026-07-03

- **Fixed "0 results but traces show offers."** Diagnosed and repaired the disconnect between
  the agent traces and the empty UI result set.
- **Retailer-domain grounding fixes:** Vertex AI redirect URLs are resolved to real retailer
  domains, missing domains are backfilled from grounding metadata, and results re-resolved —
  so exclusion rules and verification act on true hosts.
- **Conflict resolution across offers:** per-retailer grouping (with anonymous groups for
  retailer-less entries) reconciles disagreeing prices; Stage 2 fetch-verification marks
  offers `_verified`.
- **Retailer exclusions:** Target, "Ulta Beauty at Target," Walmart, and eBay are filtered
  out (name substring + exact domain match); ulta.com itself is kept.
- **Evidence-based prompt rules** for the search agent: only report prices actually seen in
  grounded sources, with status sentinels for not-found/discontinued products.
- **Telemetry:** per-browser session ids logging `find_dupes`, `results_shown`, `buy_click`,
  and `download_txt` events to `agent/traces/usage.jsonl`, plus full per-run outcome JSON and
  per-candidate trace files.

## 2026-07-03 (project setup)

- Two-agent Google ADK pipeline on Gemini 2.5 Flash (`backend/pipeline.py`): Stage 1
  grounded-search agent finds prices, Stage 2 MCP-fetch agent verifies them; sequential
  candidate processing (`CANDIDATE_CONCURRENCY=1`).
- FastAPI server (`backend/main.py`, port 8000) statically serving the single-page UI
  (`web_app/Find Your Lipstick Twin.dc.html`, DC framework, no build step).
- ΔE76 color matching against the verified shade catalog, with match tiers
  (identical twin / separated at birth / fraternal twin / doppelgänger).
