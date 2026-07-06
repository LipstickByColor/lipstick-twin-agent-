# Find Your Lipstick Twin — dupe price-finder

My capstone for Kaggle's [**5-Day AI Agents Intensive Course with
Google**](https://www.kaggle.com/learn-guide/5-day-agents).

A two-agent Gemini pipeline that takes a lipstick shade, finds its perceptual "twins," and returns live, page-verified prices so the cheapest
in-stock dupe wins.

Lipstick is just the domain. Under the hood this is a **live
retrieval-and-verification pipeline** for structured product data:

<p align="center"><sub><b>color match → candidate generation → grounded search → page verification → evidence aggregation → ranking</b></sub></p>

The same pipeline would price shoes, shirts, or furniture.

One real run: a **$50 Chanel** shade resolved to a close-match **NYX twin at
$13 which is 74% cheaper**, page-verified on Ulta ([see it screen by
screen](#a-run-screen-by-screen)).

## Course concepts demonstrated

| Key concept | Where | What to look at |
|---|---|---|
| **Agent / multi-agent system (ADK)** | Code | Two ADK `LlmAgent`s — a search agent with `google_search` ([pipeline.py:267](src/backend/pipeline.py#L267)) and a page-verification agent ([pipeline.py:317](src/backend/pipeline.py#L317)) — sequenced by deterministic Python. Rationale in [Why agents?](#why-agents), design in [Architecture](#architecture). |
| **MCP server** | Code | The verification agent's only tool is the `fetch` MCP server, run over stdio via ADK's `McpToolset` ([pipeline.py:274](src/backend/pipeline.py#L274)). It loads each candidate product page to confirm brand and shade before a price can win. |
| **Deployability** | Video + code | The whole system — backend, UI, and MCP fetch server — ships as one Docker container ([docker/](docker/)); build-and-run is two commands ([Run it yourself](#run-it-yourself)) and is shown working in the video. |
| **Security features** | Code | The API key is never stored in the repo or the image: `.env` is gitignored and the container prompts for the key with hidden input ([entrypoint.sh](docker/entrypoint.sh)). Fetched retailer pages are treated as untrusted input — deterministic verification ([pipeline.py:680](src/backend/pipeline.py#L680)) decides what a page can claim, so page content can't steer the final pick. |

The video follows the same arc as this README: [the problem](#the-problem) →
[why agents](#why-agents) → [architecture](#architecture) → live
[demo](#demo) → [the build](#the-build).

## Contents

- [The problem](#the-problem)
- [Why agents?](#why-agents)
- [Architecture](#architecture)
- [Demo](#demo)
- [The build](#the-build)
- [How I built it](#how-i-built-it)
- [How well it works](#how-well-it-works)
- [What I learned](#what-i-learned)

## The problem

The same lipstick color can cost $5 at the drugstore or $100+ at the luxury
counter, yet the question every shopper has:

> *"Is there a **cheaper** lipstick in a **similar color**?"*

... has no good answer. Shade names ("Velvet Plum," "Midnight Berry") don't
describe colors, and retailer color filters are too coarse to find a match.
And even then, prices vary by retailer and products go out of stock.

I solved the color half in a previous project, [Lipstick Color Finder](https://github.com/ConstanzaSchibber/lipstick_color_extraction),
which measured the true [CIELAB color](https://lipstickbycolor.github.io/color-guide.html)
of 9,000+ products. That catalog is the front door of this demo: pick a shade
and instantly see its closest color twins.

**This project solves the price half: get me a price I can trust, right now,
with a link I can actually buy from.**

<p align="center">Chanel <i>Soft Candy</i> — <b>$50</b><br>↓<br>e.l.f. <i>Joyful</i> — <b>$7</b><br>↓<br><b>86% cheaper</b></p>

Finding that price by hand means trawling website after website — and one
candidate is rarely enough; you want a few similar shades side by side. So the
app returns several close twins, each with a live, page-verified price and a
link you can buy from.

## Why agents?

A static database or a conventional scraper can't solve this:

- **Query-time retrieval beats catalog maintenance.** There is no need to keep
  9,000+ products' prices and availability fresh
  when an agent with a search tool can price just the 3–4 candidates the user
  actually asked about, live, at the moment they ask. Discontinued shades
  surface naturally as "not found" instead of lingering as stale rows.
- **The evidence needs judgment.** Is this a store or a blog quoting a 2023
  price? Is "Ruby Woo Retro Matte" the same product as "Ruby Woo"? Is the page
  showing "sold out"? These are language-understanding calls, not regexes.
- **Verification requires reading pages.** A second agent actually loads each
  candidate product page (via an MCP fetch tool) and confirms the brand and
  shade appear on it — turning a search claim into page-verified evidence.
- **Failure needs adaptation.** When a long catalog product name returns
  nothing, the agent is re-run with a shorter brand + shade query — the kind
  of adjustment a fixed pipeline can't improvise.

**Why two agents and not one?** The immediate reason is that Gemini 2.5 won't
combine the built-in `google_search` tool with a function-calling tool like MCP
`fetch` in one request (a limit lifted in Gemini 3) — but I'd keep the split anyway:

- **Search returns *claims*; fetch returns *proof*.** They're different jobs
  with different failure modes — search casts wide, fetch commits to one URL —
  so each agent carries one focused instruction and one tool.
- **Verification can overrule search.** A page that contradicts the snippet has
  its price cleared. One combined agent would blur "what was claimed" with
  "what was confirmed" — the exact distinction the reliability story rests on.
- **Each stage can be traced and tested independently.**

What stays *deterministic* is everything that should be: ΔE76 color matching,
retailer exclusions, conflict resolution, and the final "cheapest in-stock
wins" decision. Agents gather and verify evidence; code makes the call.

## Architecture

Two Gemini agents wrapped in deterministic orchestration:

1. **Stage 1 — search agent** (`gemini-2.5-flash` + `google_search`): finds
   offers for a candidate and must return *grounded* evidence — the quoted
   snippet it saw, the source domain, and a source classification
   (ecommerce / blog / other). It is forbidden from stating prices from
   training memory.
2. **Stage 2 — fetch agent** (`gemini-2.5-flash` + MCP `fetch` tool): loads
   each offer's product page and verifies the brand and shade actually appear
   on it.

```mermaid
flowchart LR
    U(["User picks<br/>a shade"]) --> TIE["<b>Color match</b><br/><i>ΔE76 · ~9k shades</i>"]
    TIE -- "closest twins" --> S1
    subgraph LOOP["&nbsp;for each candidate twin&nbsp;"]
        direction LR
        S1["<b>Search agent</b><br/><i>Gemini + google_search</i>"]
        S1 -- "claims" --> FLT["<b>Evidence filters</b><br/><i>trust rules · URLs</i>"]
        FLT -- "pages worth reading" --> S2["<b>Verify agent</b><br/><i>Gemini + MCP fetch</i>"]
        S2 -- "page-verified offers" --> CR["<b>Conflict resolution</b><br/><i>dedupe · sanity checks</i>"]
    end
    CR --> WIN(["Cheapest in-stock<br/>twin wins"])

    classDef agent fill:#ede9fe,stroke:#7c3aed,color:#312e81
    classDef det fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef outcome fill:#fef3c7,stroke:#d97706,color:#78350f
    class S1,S2 agent
    class TIE,FLT,CR det
    class WIN outcome
```

<sub>**Purple** = the two Gemini agent invocations · **green** = deterministic
Python. The edge labels are the story: *claims* from search harden into
*page-verified offers*, and only code makes the final call. The buy link the
user clicks opens the exact page the verify agent read.</sub>

Key design choices:

- **Evidence-grounded offers.** Prices found on review sites (e.g. Temptalia)
  still count as grounded price signals, but the UI links them to a Google
  Shopping search ("find") rather than pretending the blog is a store ("buy").
- **Retry on empty.** If the first search returns nothing — or the model gives
  up on the long catalog product name — the candidate is retried once with a
  shorter brand + shade query before a "not found" verdict is accepted.
- **Sequential by default.** Concurrent fetches from one IP trip retailer
  bot-walls and returned thinner results in testing. Set
  `CANDIDATE_CONCURRENCY=2` (env var) to trade reliability for speed.
- **`robots.txt` is deliberately bypassed on page verification.** The MCP
  fetch server runs with `--ignore-robots-txt` because major retailers
  disallow all automated agents, which would make the verify stage impossible.
  The tradeoff is scoped: each fetch is user-initiated, reads a single product
  page the user is about to open anyway, and runs sequentially — this is a
  price check, not crawling at scale.
- **Live catalog.** ~9k products loaded at startup from the color-finder app's
  Supabase (`lipstick-data` table, publishable key), with ΔE76 matching ported
  from that repo's `lipstick-utils.js`. The shade you pick is the *anchor*;
  the pipeline prices it plus its 3 closest catalog twins (one per product
  line) — capped because each candidate is a paid ~20s agent run. If Supabase is unreachable the UI falls back to
  four curated, live-verified shades.

## Demo

<p align="center"><sub>The run below: <b>Chanel 124 "Soft Candy" ($50)</b> → agents search the web and page-verify each price live → winner is <b>e.l.f. "Joyful" ($7)</b>, in stock — <b>86% cheaper</b>. Along the way one candidate's page says it's <i>discontinued</i>, so it drops out, and pricier matches rank below the winner.</sub></p>

<p align="center">
  <img src="assets/demo_agent_product_finder.gif" width="720" alt="Full run of Find Your Lipstick Twin: pick a shade, the two agents search and page-verify prices live, and the cheapest in-stock twin surfaces first">
</p>


### A run, screen by screen

A different run from the GIF above — anchor **Chanel 104**, winner a
page-verified **$13 NYX** on Ulta:

| | | |
|:--:|:--:|:--:|
| <img src="assets/front_end_1_chanel.png" width="240" alt="Landing page"><br>**1 · Drop your shade**<br><sub>The lipstick you're obsessed with.</sub> | <img src="assets/front_end_2_chanel.png" width="240" alt="Brand and shade picker"><br>**2 · Brand + exact shade**<br><sub>Chanel 104 · matte-only or any format.</sub> | <img src="assets/front_end_3_chanel.png" width="240" alt="Live search progress"><br>**3 · The agent works live**<br><sub>Searches the web, reads real pages · ~1–2 min.</sub> |
| <img src="assets/front_end_4_chanel.png" width="240" alt="Ranked results"><br>**4 · Ranked by price**<br><sub>Closest matches, cheapest in-stock twin first.</sub> | <img src="assets/front_end_5_chanel.png" width="240" alt="Twin detail with buy link"><br>**5 · Twin detail + buy**<br><sub>Close match, live price, tap through to the store.</sub> | <img src="assets/front_end_6_to_results.png" width="240" alt="Ulta product page for the verified NYX twin at $13.00"><br>**6 · Buy on the real store**<br><sub>The verified page the fetch agent read — $13 NYX on Ulta.</sub> |

<sub>About the badges in the screenshots: every candidate is classified by its
measured color distance to your shade, then labeled with a friendly name
instead of a number: **identical twin**, **separated at birth**, **fraternal
twin**, or **doppelgänger**, from closest to furthest.</sub>

### Run it yourself

**With Docker (easiest — one container, backend + UI + fetch server).** You only
need Docker and a Gemini API key; the container prompts for the key on start:

```bash
docker build -f docker/Dockerfile -t lipstick-twin .
docker run -it -p 8000:8000 lipstick-twin     # asks for your Gemini API key
```

Then open http://localhost:8000. Full instructions (including the
non-interactive `-e GOOGLE_API_KEY=…` form) are in [docker/README.md](docker/README.md).

**Or run it locally** (Python 3.11+):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # ADK, google-genai, MCP fetch server, FastAPI
echo "GOOGLE_API_KEY=your-key-here" > .env
```

The `mcp-server-fetch` package (installed above) is the MCP `fetch` server the
verify stage runs over stdio — no separate install needed.

**Run:**

```bash
cd src/backend
python -m uvicorn main:app --port 8000
```

Open http://localhost:8000. Requires `GOOGLE_API_KEY` in the repo-root `.env`
(paid/pay-as-you-go tier recommended; a full 4-candidate query costs a few
cents and takes 1–2 minutes).

The server caches identical queries for 6 hours, so repeat runs — and video
retakes — are instant.

To let remote testers in, tunnel the port, e.g.:

```bash
cloudflared tunnel --url http://localhost:8000   # or: ngrok http 8000
```

## The build

| Layer | What / how |
|---|---|
| Agents | Two `LlmAgent`s on **Google ADK**, model `gemini-2.5-flash`, run via `InMemoryRunner` |
| Tools | ADK built-in `google_search` (grounded search); **MCP** `fetch` server over stdio for page verification |
| Orchestration | Plain Python (`src/backend/pipeline.py`): candidate loop, retries, URL resolution, conflict resolution, winner pick |
| Color matching | ΔE76 in Python, ported from the color-finder app's `lipstick-utils.js`; catalog from **Supabase** (~9k rows) |
| Backend | **FastAPI** + uvicorn (`src/backend/main.py`): a single long-running `POST /api/find-dupes` request, 6h in-process cache, static-hosts the UI |
| Frontend | Self-contained `.dc.html` components + vanilla JS — no build step |
| Observability | Per-candidate JSONL agent traces + full run outcomes in `src/research/traces/`; usage/UI events in `usage.jsonl` ([details](ARCHITECTURE.md#observability)) |
| Process | Prototyped in a Jupyter notebook (`src/research/agent_dupe_price_finder.ipynb`), then extracted into the pipeline module |

For the repo layout and how a request flows through it, see
[ARCHITECTURE.md](ARCHITECTURE.md).

## How I built it

I started the project in a Jupyter notebook, hardening it by running real
queries and reading the traces. Once the pipeline was robust enough, I moved it
into a FastAPI backend and continued the same loop — run, read the trace, fix —
now against real testers. Almost every design decision below came from watching
the agent confidently return something *wrong* and asking why. A few examples:

- **The two-agent split was forced, then became the point.** Gemini rejects
  combining built-in `google_search` with the MCP `fetch` tool in one agent
  (`INVALID_ARGUMENT`). Splitting them, coordinated by Python, fixed it *and*
  gave me the clean two-stage pipeline the rest of the design leans on.
- **Killing phantom "best picks."** A trace showed a confident `$8.49 BEST
  PICK` sourced from a Walmart search page for a *different* product. Now the
  fetch agent re-reads the product page and matches **brand + shade tokens** —
  a wrong-product price is cleared, so confidence follows verified identity,
  not "did the fetch succeed."
- **Stopping the fetch agent from drowning in tokens.** Early traces showed
  ~130k characters of raw CSS and JavaScript per page (`raw=true` plus
  pagination). The instruction now forbids both: one
  `fetch(url, max_length=20000)` on the simplified text, returning structured
  `{title, brand, shade, price, in_stock}`.
- **Not deduping away a legitimately cheaper version.** The $16 Ruby Woo
  travel mini and the $25 full size were collapsed as "duplicates," and a
  null-price snippet outranked the mini. Conflict resolution now groups offers
  by URL path (the SKU) *before* dedup and ranks by
  `(has_price, url_quality, confidence)`, so the cheaper real option can win.
- **Excluding retailers I learned to distrust — then letting Walmart back in
  by page type.** Target and Walmart kept producing believable-but-wrong
  prices, but a blanket ban also discarded a legitimate in-stock L'Oréal at
  **$15.94**. Now a resolved `walmart.com/ip/…` product page is kept and
  always fetch-verified; search/browse pages and the B2B storefront are
  dropped before they consume a fetch call.

An `/ip/` URL isn't an airtight guarantee either — Walmart sometimes serves a
listing of several products under a single `walmart.com/ip/…` link that the page-type
gate waves through. The quarantine rule — when one store reports several
conflicting prices for one product, drop them all — is the backstop for exactly that. In the
Chanel run dissected [below](#a-query-end-to-end), one such Dior "product" page
yielded four different prices: different items on the listing, none confirmed to be the shade being priced:

```jsonc
// all four tagged to ONE URL — walmart.com/ip/Dior-Addict-Lip-Tint/1401958943 —
// which is actually a listing of several Dior items, not one product page:
{ "retailer": "walmart.com", "price": 40.88, "confidence": "high", "_dropped_reason": "quarantined" }
{ "retailer": "walmart.com", "price": 42.36, "confidence": "high", "_dropped_reason": "quarantined" }
{ "retailer": "walmart.com", "price": 44.11, "confidence": "high", "_dropped_reason": "quarantined" }
{ "retailer": "walmart.com", "price": 62.07, "confidence": "high", "_dropped_reason": "quarantined" }
// one store, prices that don't agree → all four quarantined; the page-verified
// Ulta offer ($42, "brand + shade confirmed on page") survived instead
```

Each fix landed with unit tests for the deterministic parts (25 tests across
conflict resolution and identity verification), so the reliability work is
locked in rather than re-discovered.

## How well it works

**Across 19 real queries, verification rejected 13 incorrect prices that would
otherwise have ranked** — roughly 1 in 9 prices the search agent returned was
wrong, and opening the page is what caught them. All numbers below come straight
from the run traces in `src/research/traces/` (19 live queries, 79 candidate
twins, 167 offers) — a development sample, not a benchmark.

### A query, end to end

One real run — anchor **Chanel 124 "Soft Candy"** ($50 matte), 7 closest twins
*(traced before the candidate cap dropped to today's 4)*:

```
7 color twins  →  19 offers surfaced by the search agent
               →  fetch agent rejects 1   (Ulta's page was shade "156 Dance",
                                            not Soft Candy — wrong product)
               →  resolution quarantines 4 (the four-price Dior listing shown
                                            in full in "How I built it")
                  + sets aside 1 whose page says discontinued
               →  14 offers kept, 10 with a live price
               →  cheapest in-stock twin:  e.l.f. "Joyful"  $7.00 @ walmart.com  (ΔE 4.8)
```

A $50 Chanel resolved to a page-verified **$7 dupe — 86% cheaper** — on a link that
opens the actual product page.

### Verification earns its place

Where those 13 rejections came from (out of 116 offers carrying a price):

- **3 were the wrong product**, caught only because the fetch agent opened the
  page — e.g. for L'Oréal "Caramel Latte 799", Ulta's confidently-returned page
  was actually shade "Fairest Nude". These snippets arrived stamped
  `confidence: high`; loading the page is the only thing that caught them.
- **10 were duplicate or self-contradicting listings** from a single retailer —
  the clearest being the Dior listing above — dropped as untrustworthy rather than
  gambled on.

Here's one of the wrong-product rejects in full, verbatim from this run's trace —
a search hit that looked solid until the page was actually read:

```jsonc
// Stage 1 — search agent proposes an Ulta offer for the Chanel shade:
{ "retailer": "ulta.com", "source_type": "ecommerce",
  "product_title": "CHANEL LE ROUGE DUO ULTRA TENUE Ultrawear Liquid Lip Colour" }

// Stage 2 — fetch agent opens the page (capped at 20k chars) and reads it:
fetch(url="…ulta.com/p/le-rouge-duo-ultra-tenue…", max_length=20000)
  → page:      "CHANEL - 156 Dance LE ROUGE DUO ULTRA TENUE …"
  → extracted: { "brand": "CHANEL", "shade": "156 Dance", "price": 53.00 }

// verify_product — asked for "124 Soft Candy", the page is "156 Dance":
_verify: "rejected: shade mismatch (page shade: '156 Dance')"   // price cleared → can't win
```

This is a **lower bound**: eBay/Target and Walmart search/browse offers are dropped
up front, before a run's outcome file is even written, so they never reach this count.

### Latency and cache

- A live query takes a **median of 83s** (mean 92s, range 23–200s) — about **~22s
  per candidate twin**, dominated by the sequential per-page fetches.
- Repeat queries return from the 6-hour outcome cache **instantly**. In this sample
  8 of 27 calls were cache hits — those were re-running the same shade while
  capturing screenshots, which is exactly what the cache is for.

## What I learned

A few things that will carry to the next agent I build:

- **Separate search from verification.** The most useful structural decision was
  making retrieval and confirmation two different jobs: the search agent finds
  candidate offers and returns *evidence*; the fetch agent visits the page and
  *verifies* it. The pipeline isn't `LLM → answer`, it's
  `search → evidence → verify → decide`. Keeping those roles apart is what made
  every reliability fix possible — you can only correct a claim you've kept
  separate from its proof.
- **Grounding is evidence, not an answer.** A lot of grounding demos stop at
  `Gemini + google_search → answer`. That framing was the source of my worst
  false positives, because a grounded snippet is still just a *claim*. Treating
  search output as evidence that has to be independently verified on the page —
  rather than as the answer itself — is a more honest use of grounding, and it's
  what killed the phantom prices.
- **Let deterministic code make the decisions.** Strikingly little is left to the
  model. Python owns retries, filtering, dedup, conflict resolution, ranking,
  caching, and the final cheapest-price call. The LLM does only what models are
  good at: reading pages, matching brand and shade, interpreting messy text.
  That gather-with-the-model / decide-with-code split is where most production
  systems seem to land, and it's what makes the system testable. You can unit-
  test the deterministic parts.
- **Observability is most of the work.** The highest-leverage thing I built
  wasn't an agent. It was the JSONL tracing. Almost every fix above started with
  reading a trace. Once each run recorded exactly what it searched, fetched,
  verified, and dropped, iteration happened fast.
- **Accuracy and latency pull against each other.** Almost every reliability
  lever — more candidates, retries, sequential fetches, a second agent opening
  each page — makes the answer better and the user wait longer. The usage log
  made the cost concrete: testers who waited too long churned (`abandoned_wait`
  / `left_during_wait`). So the caps (4 candidates, one retry, the 6-hour cache)
  are deliberate bets on how much correctness is worth how much patience.

## License

Released under the [MIT License](LICENSE). The catalog data comes from my
[Lipstick Color Finder](https://github.com/ConstanzaSchibber/lipstick_color_extraction)
project; retailer prices and pages belong to their respective owners.
