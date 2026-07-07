"""Lipstick dupe price-finder pipeline, extracted from agent/agent_dupe_price_finder.ipynb.

Two-agent ADK pipeline (google_search agent + MCP fetch agent) orchestrated in
Python: run_tie_set(tie_set, country, strategy) -> outcome dict.
"""
import asyncio
import base64
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

from google.genai import types
from google.genai.errors import ClientError
from google.adk.agents import LlmAgent
from google.adk.models.google_llm import Gemini
from google.adk.runners import InMemoryRunner
from google.adk.tools import google_search
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

import os

log = logging.getLogger("pipeline")

MODEL = "gemini-2.5-flash"

# How many candidates to price concurrently. Concurrent runs finish faster but
# concurrent fetches from one IP trip retailer bot-walls more often and came
# back with noticeably thinner results in testing — default to sequential,
# which is the notebook's proven mode.
CANDIDATE_CONCURRENCY = int(os.environ.get("CANDIDATE_CONCURRENCY", "1"))

_COUNTRY_HINTS = {
    "US": "buy price USD",
    "UK": "buy price GBP",
    "PT": "comprar preço EUR",
}

# ── Trace logging ─────────────────────────────────────────────────────────────

TRACE_DIR = Path(__file__).resolve().parent.parent / "research" / "traces"
TRACE_DIR.mkdir(parents=True, exist_ok=True)


def _event_to_records(event, candidate_label: str) -> list[dict]:
    """Flatten one ADK event into a list of JSON-serializable trace records."""
    ts     = datetime.now(timezone.utc).isoformat()
    author = getattr(event, "author", "unknown")
    role   = getattr(event.content, "role", None) if event.content else None
    base   = {"ts": ts, "candidate": candidate_label, "author": author, "role": role}

    records = []

    # Grounding metadata: the authoritative (domain, title, uri) triples that
    # google_search actually used. Captured independently of the model's JSON so
    # we can recover product URLs the model failed to transcribe into its answer.
    gm = getattr(event, "grounding_metadata", None)
    if gm and getattr(gm, "grounding_chunks", None):
        chunks = []
        for ch in gm.grounding_chunks:
            web = getattr(ch, "web", None)
            if web and (web.uri or web.domain or web.title):
                chunks.append({"domain": web.domain, "title": web.title, "uri": web.uri})
        if chunks:
            records.append({**base, "type": "grounding", "name": "google_search", "content": chunks})

    if not (event.content and event.content.parts):
        return records

    for part in event.content.parts:

        if getattr(part, "text", None):
            records.append({**base, "type": "text", "name": None, "content": part.text})

        elif getattr(part, "function_call", None):
            fc = part.function_call
            records.append({
                **base, "type": "tool_call", "name": fc.name,
                "content": dict(fc.args or {}),
            })

        elif getattr(part, "function_response", None):
            fr   = part.function_response
            resp = fr.response
            # MCP fetch returns full page HTML — truncate so traces stay readable
            if isinstance(resp, dict):
                resp = {
                    k: (v[:1200] + " …[truncated]" if isinstance(v, str) and len(v) > 1200 else v)
                    for k, v in resp.items()
                }
            records.append({
                **base, "type": "tool_response", "name": fr.name,
                "content": resp,
            })

    return records


class RunTracer:
    """Collects ADK events for one candidate run and writes a JSONL trace file."""

    def __init__(self, candidate_label: str):
        # %f (microseconds) keeps names collision-free when concurrent requests
        # trace the same candidate in the same second, while staying sortable.
        ts_str    = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe      = candidate_label.replace(" ", "_").replace("/", "-")
        self.path = TRACE_DIR / f"{ts_str}_{safe}.jsonl"
        self._records: list[dict] = []
        self.grounding: list[dict] = []
        self.label = candidate_label

    def record(self, event) -> None:
        recs = _event_to_records(event, self.label)
        self._records.extend(recs)
        for r in recs:
            if r["type"] == "grounding":
                self.grounding.extend(r["content"])

    def flush(self) -> Path:
        with self.path.open("w") as f:
            for r in self._records:
                f.write(json.dumps(r, default=str) + "\n")
        return self.path


# ── URL utilities ─────────────────────────────────────────────────────────────

_SEARCH_PARAM_RE = re.compile(
    r'[?&](q|query|search|searchTerm|keyword|keywords|term|terms)=',
    re.IGNORECASE,
)


def classify_url(url: str | None) -> str:
    """
    Classify a URL returned by the agent.

    Returns:
        'retailer_link' — direct product page (path present, no search params).
        'vertex_link'   — Google grounding redirect (destination unknown until followed).
        'hallucinated'  — root domain only, or a search/category page.
        'missing'       — null or empty.
    """
    if not url:
        return "missing"
    if "vertexaisearch.cloud.google.com/grounding-api-redirect/" in url:
        return "vertex_link"
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path:
        return "hallucinated"
    if _SEARCH_PARAM_RE.search(url):
        return "hallucinated"
    return "retailer_link"


def _recover_blocked_url(url: str) -> str:
    """
    Recover the real product URL from a retailer anti-bot wall.

    Some retailers (Walmart) redirect automated clients to a bot wall such as
    ``https://www.walmart.com/blocked?url=<base64 of the real path>&uuid=...``.
    The genuine product path is base64-encoded in the ``url`` query param, so we
    can reconstruct a durable product link even though the page itself refused to
    load. Non-blocked URLs are returned unchanged.
    """
    parsed = urlparse(url)
    if parsed.path.rstrip("/") != "/blocked":
        return url
    enc = (parse_qs(parsed.query).get("url") or [None])[0]
    if not enc:
        return url
    pad = enc + "=" * (-len(enc) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            path = decoder(pad).decode("utf-8", "replace")
        except Exception:
            continue
        if path.startswith("/"):
            return f"{parsed.scheme}://{parsed.netloc}{path}"
    return url


def resolve_vertex_link(url: str, timeout: int = 5, attempts: int = 3) -> str | None:
    """Follow a Vertex grounding redirect and return the final destination URL.

    The redirect service 403s under burst load and recovers on a short-backoff
    retry — losing the link loses the retailer, and it's disproportionately the
    major retailers (ulta, sephora, macys) that arrive as redirects rather than
    direct URLs. A 404 is a model-mangled token that no retry can fix, so it
    fails immediately."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    )
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _recover_blocked_url(resp.url)
        except urllib.error.HTTPError as e:
            if e.code == 404 or i == attempts - 1:
                log.warning(f"    vertex redirect HTTP {e.code}: {e.reason}")
                return None
        except Exception as e:
            if i == attempts - 1:
                log.warning(f"    vertex redirect error: {type(e).__name__}: {e}")
                return None
        time.sleep(1 + i)  # runs in a worker thread, never on the event loop
    return None


# ── Agents ────────────────────────────────────────────────────────────────────

# google_search is a Gemini built-in tool. Built-in tools cannot be combined
# with function-calling tools (like MCP) in one request, so search and fetch
# live in separate agents coordinated by Python orchestration.
SEARCH_INSTRUCTION = """
You are a lipstick price comparison specialist. Find current online retail prices.

## RULES
- NEVER state a price from training memory. Prices change daily — always search first.
- Include every retailer where a price is shown or a product URL is available.
- NEVER construct or guess a URL. Only use URLs that appear in the search results.
  A root domain like "https://www.ulta.com" is not a product URL — return null instead.
  If you are not 100% certain the URL appeared verbatim in a search result, use null.

## Steps
1. Search for the product using the query provided.
2. Read each result — Google Shopping results show the price directly,
   e.g. "MAC Lipstick Ruby Woo — $23.00 · In stock".
3. Collect every retailer name, price, stock status, and URL from the search.

## Output format
Return ONLY a JSON array — no prose, no markdown fences, no explanation:
[
  {"retailer": "sephora.com", "price": 28.0, "currency": "USD",
   "in_stock": true, "url": "https://...", "confidence": "high", "source_type": "ecommerce",
   "product_title": "MAC Matte Lipstick - Ruby Woo", "evidence": "MAC Matte Lipstick Ruby Woo — $28.00 · In stock · Sephora"}
]
- retailer: the domain of the site where you saw the evidence (e.g. "sephora.com").
  NEVER null when evidence is quoted — if the price appears on a blog or review
  site instead of a store, still use that site's domain, with confidence "medium" at most.
- source_type: REQUIRED — classify the site the evidence came from:
  "ecommerce" = an online store where this product can be purchased (sephora.com, ulta.com, brand sites),
  "blog" = a review/editorial/swatch site that mentions the price but sells nothing (e.g. temptalia.com),
  "other" = anything else (forums, videos, news).
- price: numeric value, or null if not visible in snippet
- url: exact URL from the search result, null if not present
- confidence: REQUIRED — always include. "high" = price clearly shown, "medium" = inferred, "low" = uncertain
- product_title: the listing title exactly as it appears in the search result, null if not shown
- evidence: the exact snippet text (25 words max) where you saw this retailer and price, null if none.
  If you cannot quote real evidence for an entry, do not include that entry.

## Error cases
If no Shopping results or product listings are found, return a single-element array
with a status field instead of an empty array:
[{"retailer": null, "price": null, "currency": null, "in_stock": false,
  "url": null, "confidence": "none", "status": "not_found"}]

Status values:
- "not_found"    — no results found for this product
- "discontinued" — search results indicate the product has been discontinued or delisted

For normal results, omit the status field.
"""

search_agent = LlmAgent(
    name="lipstick_search",
    model=Gemini(model=MODEL),
    instruction=SEARCH_INSTRUCTION,
    tools=[google_search],
)

mcp_fetch_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,          # same interpreter as this server
            # --ignore-robots-txt: retailers disallow all bots, which would kill
            # the verify stage. Acceptable here because fetches are user-initiated,
            # one product page at a time, sequential — not crawling (see README).
            args=["-m", "mcp_server_fetch", "--ignore-robots-txt"],
        ),
        timeout=30,
    )
)

FETCH_INSTRUCTION = """
You are a product identity + price extractor. Given a product page URL and the product
we are looking for, fetch the page ONCE and extract both what the page is and its price.

## Fetch rules (important — controls cost and reliability)
- Call the fetch tool EXACTLY ONCE for the URL.
- Do NOT pass raw=true. The default simplified text is what you want — raw HTML is
  mostly CSS/JS noise with no price.
- Do NOT paginate with start_index. Instead request enough text in one call by passing
  a large max_length (for example max_length=20000).
- The simplified text begins with the page <title>, which normally names the brand and
  shade — use it to identify the product.

## Steps
1. fetch(url, max_length=20000)   # one call, simplified text
2. From that text read: the product title, brand, shade / color name, price, stock status.

## Output format
Return ONLY a JSON object — no prose, no markdown fences:
{"title": "...", "brand": "...", "shade": "...", "price": 28.0, "currency": "USD", "in_stock": true}
- title:    the product page title, verbatim (usually the first line), or null
- brand:    brand shown on the page, or null
- shade:    shade / color name shown on the page, or null
- price:    numeric value, or null if not found on the page
- currency: "USD", "GBP", "EUR", etc.
- in_stock: true / false / null if unknown

Never invent values. If the page failed to load or shows an error, return all fields null.
"""

fetch_agent = LlmAgent(
    name="lipstick_fetcher",
    model=Gemini(model=MODEL),
    instruction=FETCH_INSTRUCTION,
    tools=[mcp_fetch_toolset],
)


# ── optimize() — deterministic price ranker ───────────────────────────────────

def optimize(priced_candidates: list[dict], strategy: str = "cheapest") -> dict:
    """
    Deterministically pick the best candidate from a priced tie set.

    Args:
        priced_candidates: Each dict has brand/product/shade/delta_e plus a "prices"
            list of {retailer, price, currency, in_stock, url, confidence} dicts.
        strategy: "cheapest" (default) — lowest price among in-stock options.
                  "closest"           — smallest delta_e (closest color match).

    Returns:
        {
            "recommendation": chosen candidate dict (with _best_price/_best_retailer attached),
            "retailer":       the cheapest in-stock retailer entry for the recommendation,
            "alternative":    alternative candidate when a meaningful price tradeoff exists,
            "tradeoff":       True when cheapest ≠ closest AND price gap exceeds 15%,
        }
    """
    def _best_in_stock(candidate: dict) -> tuple[float | None, dict | None]:
        options = [
            p for p in candidate.get("prices", [])
            if p.get("in_stock") and p.get("price") is not None
        ]
        if not options:
            return None, None
        # A blog/review price (source_type != ecommerce) proves the price is real
        # but isn't a place to buy — only use it when no store carries a price.
        stores = [p for p in options if (p.get("source_type") or "ecommerce") == "ecommerce"]
        best = min(stores or options, key=lambda p: p["price"])
        return best["price"], best

    scored = []
    for c in priced_candidates:
        val, retailer = _best_in_stock(c)
        if val is not None:
            scored.append({**c, "_best_price": val, "_best_retailer": retailer})

    if not scored:
        return {
            "recommendation": None, "retailer": None,
            "alternative": None, "tradeoff": False,
            "note": "No in-stock candidates found.",
        }

    by_price = min(scored, key=lambda c: c["_best_price"])
    by_color = min(scored, key=lambda c: c.get("delta_e", float("inf")))

    chosen = by_price if strategy == "cheapest" else by_color

    # Tradeoff: cheapest shade ≠ closest-color shade AND gap exceeds 15%
    different = by_price.get("shade") != by_color.get("shade")
    gap = (
        (by_color["_best_price"] - by_price["_best_price"]) / by_color["_best_price"]
        if by_color["_best_price"] > 0 else 0
    )
    tradeoff = different and gap > 0.15

    alternative = None
    if tradeoff:
        alternative = by_color if strategy == "cheapest" else by_price

    return {
        "recommendation": chosen,
        "retailer":       chosen["_best_retailer"],
        "alternative":    alternative,
        "tradeoff":       tradeoff,
    }


# ── Orchestration ─────────────────────────────────────────────────────────────

# Retailers excluded from results entirely — their listings are unreliable for a
# specific shade:
#   target.com  — search pages return unrelated products when the shade isn't listed.
#   business.walmart.com — Walmart's B2B storefront: bulk/again listings, not
#                 consumer retail pricing.
#   ebay        — third-party resale listings: prices/stock don't reflect retail and
#                 authenticity is unverifiable.
#
# Consumer Walmart (walmart.com) is NOT hard-excluded. It was, because grounding
# often surfaced Walmart *search/browse* pages whose quoted price belonged to a
# different variant (confident false positives). We now keep it only when it
# resolves to a genuine product page (walmart.com/ip/…) and force those through
# the fetch-verify stage — see _is_walmart_consumer / _walmart_is_pdp and the
# Walmart gate in price_one_candidate.
_EXCLUDED_RETAILERS = {"target.com", "target", "ulta beauty at target", "ebay"}
_EXCLUDED_DOMAINS = {"business.walmart.com", "ebay.com"}


def _retailer_key(s: str | None) -> str:
    """Loose retailer identity: 'Ulta Beauty' ~ 'ulta.com' ~ 'ulta' ~ 'm.ulta.com'.

    Lowercase, strip www/m prefixes and everything from a common TLD on, then
    drop non-alphanumerics ('e.l.f. cosmetics' → 'elfcosmetics')."""
    s = (s or "").lower().strip()
    s = re.sub(r"^(www|m)\.", "", s)
    s = re.sub(r"\.(com|net|org|shop|store|co|us|uk|ca|kr|fr|de)\b.*$", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _same_retailer(a: str | None, b: str | None) -> bool:
    ka, kb = _retailer_key(a), _retailer_key(b)
    return bool(ka) and bool(kb) and (ka in kb or kb in ka)


def _is_excluded(p: dict) -> bool:
    """True if this price entry belongs to an excluded retailer (by name or URL host)."""
    retailer = (p.get("retailer") or "").lower()
    if any(excl in retailer for excl in _EXCLUDED_RETAILERS):
        return True
    host = urlparse(p.get("url") or "").netloc.lower().removeprefix("www.")
    return host in _EXCLUDED_DOMAINS or retailer.removeprefix("www.") in _EXCLUDED_DOMAINS


def _is_walmart_consumer(p: dict) -> bool:
    """True for a consumer walmart.com listing (not the business.walmart.com B2B
    storefront, which _is_excluded already drops). URL host wins; falls back to
    the retailer name when the entry has no URL yet."""
    host = urlparse(p.get("url") or "").netloc.lower().removeprefix("www.")
    if host:
        return host == "walmart.com"
    retailer = (p.get("retailer") or "").lower().removeprefix("www.")
    return retailer in ("walmart", "walmart.com")


def _walmart_is_pdp(p: dict) -> bool:
    """A genuine Walmart product page: walmart.com/ip/…. Search/browse/category
    pages (the historical false-positive source) and URL-less entries are not."""
    parsed = urlparse(p.get("url") or "")
    if parsed.netloc.lower().removeprefix("www.") != "walmart.com":
        return False
    return parsed.path.lstrip("/").startswith("ip/")


_FETCH_TIMEOUT_S = 45  # max seconds to wait for one MCP fetch call

# At most this many vertex-redirect resolutions in flight across ALL candidates:
# the redirect service rate-limits bursts with 403s, and each 403 costs a
# retailer link (see resolve_vertex_link).
_VERTEX_RESOLVE_SEM = asyncio.Semaphore(3)

# Gemini's front end closes idle keep-alive connections, and httpx reusing a
# dead socket surfaces as "Server disconnected without sending a response".
# These are transport faults, not model errors: one retry on a fresh
# connection succeeds, and re-running one agent turn costs far less than
# losing the whole tie-set run that contains it.
_TRANSIENT_HTTP_ERRORS = (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError)


async def _run_agent(agent, message: str, tracer: RunTracer) -> str:
    """Run one agent turn, record events to tracer, return full text response."""
    async def _attempt() -> str:
        runner  = InMemoryRunner(agent=agent, app_name="price_finder")
        session = await runner.session_service.create_session(
            app_name="price_finder", user_id="u1"
        )
        text = ""
        async for event in runner.run_async(
            user_id="u1",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
        ):
            tracer.record(event)
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        text += part.text
        return text

    try:
        return await _attempt()
    except _TRANSIENT_HTTP_ERRORS as e:
        log.warning(f"    transient connection error — retrying turn: {e}")
        await asyncio.sleep(2)
        return await _attempt()


def _parse_list(text: str) -> list[dict]:
    clean = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    try:
        parsed = json.loads(clean)
        if isinstance(parsed, list):
            return [p for p in parsed if isinstance(p, dict)]
    except (json.JSONDecodeError, ValueError):
        if text.strip():
            log.warning(f"    JSON parse failed: {text[:200]!r}")
    return []


def _parse_obj(text: str) -> dict:
    clean = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    try:
        parsed = json.loads(clean)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return {}


def _backfill_urls_from_grounding(prices: list[dict], grounding: list[dict]) -> int:
    """
    Recover product URLs the model dropped, using google_search grounding chunks.

    google_search grounds every result with an authoritative (domain, title, uri)
    triple, but the model sometimes returns a retailer with ``url: null`` even
    though it clearly had the link (e.g. "ulta.com" with no URL). For each such
    entry we match a grounding chunk by domain and fill in its uri, and keep the
    chunk title as a durable identity anchor for links that may later expire.

    Returns the number of entries backfilled. Mutates `prices` in place.
    """
    if not grounding:
        return 0
    by_domain: dict[str, dict] = {}
    for ch in grounding:
        # Newer API responses leave `domain` unset and put the bare domain
        # string in `title` — accept either as the domain key.
        dom = (ch.get("domain") or ch.get("title") or "").lower().removeprefix("www.")
        if dom and dom not in by_domain:
            by_domain[dom] = ch

    n = 0
    for entry in prices:
        if entry.get("url") or entry.get("status"):
            continue
        retailer = (entry.get("retailer") or "").lower().removeprefix("www.").strip()
        if not retailer:
            continue
        ch = by_domain.get(retailer)
        if not ch:  # loose match on brand key: "ulta.com" ~ "Ulta Beauty" ~ "ulta"
            rkey = re.split(r"[.\s]+", retailer, maxsplit=1)[0]
            ch = next((c for d, c in by_domain.items() if d.split(".")[0] == rkey), None)
        if ch and ch.get("uri"):
            entry["url"] = ch["uri"]
            title = ch.get("title") or ""
            # Only keep the chunk title as an identity anchor when it's a real
            # page title, not the bare domain standing in for one.
            if title and not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", title.lower()) \
                    and not entry.get("product_title"):
                entry["product_title"] = title
            n += 1
    return n


def _resolve_conflicts(prices: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Groups entries by canonical entity (domain from URL, falling back to retailer
    name), resolves conflicts, returns (kept, dropped).

    Status sentinels ("not_found" / "discontinued") are kept only when there are
    no real offers. Mixed with offers they move to dropped — a discontinued shade
    can still be in stock at some stores, so real prices must never be erased by
    a sentinel; the caller lifts the status to candidate level instead.

    Resolution order within a conflict group:
      0. Verified products        — fetch-confirmed pages (brand+shade matched).
      1. Exact-duplicate fast path — same price -> keep first.
      2. URL quality tiebreaker   — retailer_link > vertex_link > hallucinated.
      3. Confidence tiebreaker    — high > medium > low.
      4. Quarantine               — top entries still tied with different prices
                                    -> drop all in group.
    """
    sentinels = [p for p in prices if p.get("status")]
    offers    = [p for p in prices if not p.get("status")]
    if sentinels and not offers:
        return sentinels, []
    # A sentinel alongside real offers is contradictory — the offers win. Park
    # the sentinel in dropped (never kept) so its status stays visible to the
    # caller without erasing real prices.
    dropped: list[dict] = [{**p, "_dropped_reason": "sentinel_with_offers"} for p in sentinels]

    prices = [p for p in offers if not _is_excluded(p)]

    def _brand_key(netloc: str) -> str:
        parts = netloc.split(".")
        return parts[-2] if len(parts) >= 2 else parts[0]

    def _fuzzy_match(a: str, b: str) -> bool:
        """True if one name is a word-boundary prefix of the other (min 3 chars)."""
        if a == b:
            return True
        short, long = (a, b) if len(a) <= len(b) else (b, a)
        if len(short) < 3:
            return False
        return long.startswith(short) and (len(long) == len(short) or long[len(short)] == " ")

    groups: dict[str, list[dict]] = {}
    name_entries: list[dict] = []

    for p in prices:
        url = p.get("url") or ""
        if url and "vertexaisearch" not in url:
            netloc = urlparse(url).netloc.lower().removeprefix("www.")
            if netloc:
                groups.setdefault(_brand_key(netloc), []).append(p)
                continue
        name_entries.append(p)

    for i, p in enumerate(name_entries):
        retailer = (p.get("retailer") or "").lower().strip()
        if not retailer:
            # Evidence-only offer with no retailer name. Each one is its own
            # source — pooling them under one "unknown" store made unrelated
            # reference prices look like a conflict and quarantine each other.
            groups.setdefault(f"_anon_{i}", []).append(p)
            continue
        if re.match(r"^[\w-]+\.\w+$", retailer):
            retailer = retailer.split(".")[0]
        matched = next((b for b in groups if _fuzzy_match(retailer, b)), None)
        groups.setdefault(matched or retailer, []).append(p)

    _URL_Q = {"retailer_link": 2, "vertex_link": 1, "hallucinated": 0, "missing": -1}
    _CONF  = {"high": 2, "medium": 1, "low": 0}

    kept: list[dict] = []

    for group in groups.values():
        if len(group) == 1:
            kept.append(group[0])
            continue

        # Fetch-confirmed real product pages take priority. Different product pages
        # (distinct URL path / SKU) are different VERSIONS of the same shade — e.g.
        # MAC Ruby Woo full-size vs travel mini — so keep each with its product info
        # instead of quarantining them as a price conflict. Same-SKU duplicates are
        # collapsed, and unverified snippets for the domain are superseded.
        verified = [p for p in group if p.get("_verified")]
        if verified:
            by_sku: dict[str, list[dict]] = {}
            for p in verified:
                sku = urlparse(p.get("url") or "").path.rstrip("/")
                by_sku.setdefault(sku, []).append(p)
            for sku_group in by_sku.values():
                best = max(sku_group, key=lambda p: (
                    _CONF.get(p.get("confidence", ""), 0), p.get("price") is not None))
                kept.append(best)
                dropped.extend({**p, "_dropped_reason": "lower_quality"}
                               for p in sku_group if p is not best)
            dropped.extend({**p, "_dropped_reason": "superseded"}
                           for p in group if not p.get("_verified"))
            continue

        price_values = {p.get("price") for p in group if p.get("price") is not None}
        if len(price_values) <= 1:
            # 0 or 1 distinct real price in this group. Keep the single best entry,
            # preferring one that actually carries the price — a null-price snippet
            # must never out-survive the entry that found the real price — then break
            # ties on URL quality and confidence.
            def _keep_score(p: dict) -> tuple[int, int, int]:
                return (
                    1 if p.get("price") is not None else 0,
                    _URL_Q.get(classify_url(p.get("url")), -1),
                    _CONF.get(p.get("confidence", ""), 0),
                )
            ranked = sorted(group, key=_keep_score, reverse=True)
            kept.append(ranked[0])
            dropped.extend({**p, "_dropped_reason": "lower_quality"} for p in ranked[1:])
            continue

        def _score(p: dict) -> tuple[int, int]:
            return (_URL_Q.get(classify_url(p.get("url")), -1), _CONF.get(p.get("confidence", ""), 0))

        ranked    = sorted(group, key=_score, reverse=True)
        top_score = _score(ranked[0])
        top_tier  = [p for p in ranked if _score(p) == top_score]

        if len(top_tier) == 1:
            kept.append(top_tier[0])
            dropped.extend({**p, "_dropped_reason": "lower_quality"} for p in ranked[1:])
        else:
            top_prices = {p.get("price") for p in top_tier if p.get("price") is not None}
            if len(top_prices) <= 1:
                kept.append(top_tier[0])
                dropped.extend({**p, "_dropped_reason": "lower_quality"} for p in ranked[1:])
            else:
                dropped.extend({**p, "_dropped_reason": "quarantined"} for p in group)

    return kept, dropped


_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")


def _norm(s: str | None) -> str:
    """Lowercase, strip punctuation, collapse whitespace. 'M·A·C' -> 'm a c'."""
    return re.sub(r"\s+", " ", _PUNCT_RE.sub(" ", (s or "").lower())).strip()


# Words that mark a listing as a multi-product bundle rather than the single
# shade being priced — "MAC Ultimate Trick Mini Lipstick Set USD $171 Value"
# is a 12-piece gift set whose marketing *value* is $171, not a $26 lipstick.
# "mini" is deliberately absent: a travel mini of the same shade is fine.
_MULTI_ITEM_RE = re.compile(
    r"\b(set|sets|kit|kits|duo|trio|quad|bundle|bundles|collection|vault|palette|gift)\b"
    r"|\bvalue\b"
)


def _is_multi_item_title(title: str | None) -> bool:
    return bool(_MULTI_ITEM_RE.search(_norm(title)))


def verify_product(candidate: dict, fetched: dict) -> tuple[str, str]:
    """
    Confirm a fetched page is the SAME brand + shade as the candidate.

    Returns (verdict, reason):
      'high'   — brand and shade both confirmed on the page.
      'medium' — page loaded and nothing contradicts it, but not fully confirmed.
      'reject' — page is a different product (wrong brand or wrong shade).
      'low'    — no page identity available to verify at all.

    Size / format differences (e.g. travel mini vs full size) are NOT a mismatch:
    matching is on brand + shade tokens only.
    """
    title = fetched.get("title") or ""
    hay = _norm(f"{title} {fetched.get('brand') or ''} {fetched.get('shade') or ''}")
    if not hay:
        return "low", "no page identity to verify"

    # A bundle page can legitimately contain the brand and shade tokens and
    # still be the wrong product to price — reject before the token match.
    if _is_multi_item_title(title):
        return "reject", f"multi-item listing, not a single shade (page: {title[:50]!r})"

    brand = _norm(candidate.get("brand"))
    shade = _norm(candidate.get("shade"))
    hay_tokens = set(hay.split())

    # Catalog brands carry corporate suffixes pages drop ('mac cosmetics' vs a
    # page titled 'MAC Trick Lipstick') — also accept the brand with those
    # suffixes stripped, as a phrase, so real pages aren't false-rejected.
    brand_core = re.sub(r"\b(cosmetics|beauty|makeup|professional|labs)\b", " ", brand)
    brand_core = re.sub(r"\s+", " ", brand_core).strip()

    def _phrase_in(needle: str) -> bool:
        return bool(needle) and (needle in hay or needle.replace(" ", "") in hay.replace(" ", ""))

    brand_ok = _phrase_in(brand) or _phrase_in(brand_core)
    shade_ok = bool(shade) and all(tok in hay_tokens for tok in shade.split())

    if brand_ok and shade_ok:
        return "high", "brand + shade confirmed on page"
    if not brand_ok and (fetched.get("brand") or title):
        return "reject", f"brand mismatch (page: {title[:50]!r})"
    if not shade_ok and fetched.get("shade"):
        return "reject", f"shade mismatch (page shade: {fetched.get('shade')!r})"
    return "medium", "partial identity match"


async def price_one_candidate(candidate: dict, country: str = "US") -> dict:
    """
    Two-stage price lookup. See the notebook for the full stage-by-stage rationale:
      Stage 1  — search_agent (google_search): Shopping snippets → prices + URLs.
      Stage 1a — recover URLs the model dropped from grounding metadata,
                 then resolve vertex redirect links to real retailer URLs.
      Stage 1.5 — targeted search for entries with a retailer name but no URL.
      Stage 2  — fetch_agent (MCP fetch): fetch null-price URLs, verify
                 low/medium-confidence snippet prices against the live page.
      Post     — conflict resolution by retailer domain.
    """
    brand, product, shade = candidate["brand"], candidate["product"], candidate["shade"]
    hint   = _COUNTRY_HINTS.get(country)
    if hint is None:
        log.warning(f"    warning: country {country!r} not in _COUNTRY_HINTS — defaulting to USD")
        hint = "buy price USD"
    label  = f"{brand}/{shade}"
    tracer = RunTracer(label)

    # Primary: retailer-anchored query to guide Shopping panel toward known stores
    search_msg = (
        f"Find current online retail prices for: {brand} {product} in shade \"{shade}\".\n"
        f"Country: {country}. Search for: {brand} {product} {shade} {hint} ulta walmart sephora cvs\n"
        f"Return all retailers and prices as a JSON array."
    )
    # Fallback: shorter query used only if primary returns zero entries
    fallback_msg = (
        f"Find current online retail prices for: {brand} {shade} lipstick.\n"
        f"Country: {country}. Search for: {brand} {shade} lipstick {hint}\n"
        f"Return all retailers and prices as a JSON array."
    )

    # ── Stage 1: search ───────────────────────────────────────────────────────
    raw_search = ""
    try:
        raw_search = await _run_agent(search_agent, search_msg, tracer)
    # 429 retry is deliberately Stage-1-only. A rate-limited primary search means
    # zero results (fatal), so waiting out the retryDelay is worth it. Stages 1.5
    # and 2 fail soft — a missing URL or an unverified snippet price — so they
    # fail fast instead of making an interactive user wait ~60s per retry.
    except ClientError as e:
        if e.status_code == 429:
            retry_s = 62
            try:
                details = e.response_json.get("error", {}).get("details", [])
                for d in details:
                    if d.get("@type", "").endswith("RetryInfo"):
                        retry_s = int(d.get("retryDelay", "60s").rstrip("s")) + 2
            except Exception:
                pass
            log.warning(f"    429 — waiting {retry_s}s...")
            await asyncio.sleep(retry_s)
            try:
                raw_search = await _run_agent(search_agent, search_msg, tracer)
            except Exception as e2:
                log.error(f"    retry failed: {e2}")
        else:
            log.error(f"    search error: {e}")

    prices = _parse_list(raw_search)
    # Drop excluded retailers up front so we never spend resolve/fetch calls on them.
    prices = [p for p in prices if not _is_excluded(p)]

    # Fallback: retry with the short query if the primary found nothing usable —
    # truly empty OR only sentinel entries ("not_found" / "discontinued"). The
    # primary query carries the full verbose product name, which is often exactly
    # why the model bails; the short query frequently succeeds where it failed.
    sentinel_only = bool(prices) and all(p.get("status") for p in prices)
    if not prices or sentinel_only:
        log.info(f"    {'model bailed' if sentinel_only else 'no results'} — retrying with short query...")
        try:
            raw_fallback = await _run_agent(search_agent, fallback_msg, tracer)
            fallback_prices = [p for p in _parse_list(raw_fallback) if not _is_excluded(p)]
            if any(not p.get("status") for p in fallback_prices):
                # Real offers refute a "not_found" bail, but not a "discontinued"
                # one — the retry's hits are often marketplaces/resale stock of a
                # genuinely retired shade. Carry that sentinel along so
                # _resolve_conflicts parks it and the candidate keeps its status
                # next to the offers instead of losing it.
                carried = [p for p in prices if p.get("status") == "discontinued"][:1]
                prices = fallback_prices + carried
            elif not prices:
                prices = fallback_prices  # still nothing; a sentinel beats an empty list
        except Exception as e:
            log.error(f"    fallback error: {e}")

    # ── Stage 1a/1b: resolve redirects, backfill from grounding metadata ─────
    # An unresolvable redirect is a dead link for the user too — drop it so the
    # entry re-enters the no-URL flows (metadata backfill and targeted search
    # below, and the UI's Google Shopping "find" fallback built from
    # product_title/brand+shade).
    async def _resolve_vertex_entries() -> None:
        # urlopen is blocking — run resolutions in threads so the event loop
        # stays free, but only a few at a time (shared across candidates): a
        # full burst is what trips the redirect service's 403 rate limit.
        entries = [e for e in prices if classify_url(e.get("url")) == "vertex_link"]

        async def _res(e: dict) -> str | None:
            async with _VERTEX_RESOLVE_SEM:
                return await asyncio.to_thread(resolve_vertex_link, e["url"])

        results = await asyncio.gather(*(_res(e) for e in entries))
        for entry, resolved in zip(entries, results):
            entry["url"] = resolved
            if not resolved:
                log.warning(f"    vertex link unresolved — dropped: {entry.get('retailer', '?')}")

    # Model-transcribed redirect tokens are often mangled (404) — resolve/drop
    # them FIRST so the metadata backfill can refill those entries with the
    # exact URIs from grounding chunks, then resolve the backfilled links too.
    await _resolve_vertex_entries()
    n_backfilled = _backfill_urls_from_grounding(prices, getattr(tracer, "grounding", []))
    if n_backfilled:
        log.info(f"    recovered {n_backfilled} URL(s) from grounding metadata")
        await _resolve_vertex_entries()

    # ── Stage 1.5: targeted search for no-URL entries ─────────────────────────
    no_url = [
        p for p in prices
        if not p.get("url") and p.get("retailer") and "status" not in p
    ]
    if no_url:
        log.info(f"    {len(no_url)} no-URL retailer(s) — firing targeted searches...")
    for entry in no_url:
        retailer = entry["retailer"]
        targeted_msg = (
            f"Find the exact product page URL for: {brand} {product} shade \"{shade}\" at {retailer}.\n"
            f"Country: {country}. Search for: {brand} {product} {shade} {retailer} {hint}\n"
            f"Return ONLY a JSON array with one entry for {retailer}."
        )
        try:
            raw_targeted = await _run_agent(search_agent, targeted_msg, tracer)
            for tp in _parse_list(raw_targeted):
                if not tp.get("url"):
                    continue
                url = tp["url"]
                if classify_url(url) == "vertex_link":
                    url = await asyncio.to_thread(resolve_vertex_link, url)
                    if not url:  # dead redirect — try the next entry instead
                        continue
                # The model often ignores "one entry for {retailer}" and returns
                # whatever it found — stapling a stranger's URL onto this entry
                # mislabels the link AND the snippet price next to it. The URL
                # host is what the user clicks, so it alone decides (the model's
                # own retailer label often parrots the one we asked for).
                host = urlparse(url).netloc
                if not _same_retailer(host, retailer):
                    log.info(f"    targeted search returned {host!r} for {retailer} — skipped")
                    continue
                entry["url"] = url
                if entry.get("price") is None and tp.get("price") is not None:
                    entry["price"]      = tp["price"]
                    entry["currency"]   = tp.get("currency", entry.get("currency"))
                    entry["confidence"] = tp.get("confidence", "medium")
                break
        except Exception as e:
            log.warning(f"    targeted search error ({retailer}): {e}")

    # ── Walmart gate: keep consumer Walmart only as a genuine product page ────
    # Redirect resolution (1a/1b) and the targeted search (1.5) have now had their
    # chance to land a real walmart.com/ip/ URL. Drop any consumer Walmart entry
    # that still isn't a product page — those are the search/browse pages whose
    # price belongs to a different variant. Survivors are force-verified below.
    walmart_kept = []
    for p in prices:
        if _is_walmart_consumer(p) and not _walmart_is_pdp(p):
            log.info(f"    walmart dropped — not a product page: {p.get('url') or 'no url'}")
            continue
        walmart_kept.append(p)
    prices = walmart_kept

    # A snippet titled like a bundle is pricing a different product — a gift
    # set's marketing "value", not this shade. Void the snippet price (a voided
    # entry never reaches optimize() or the UI); entries that kept a URL fall
    # into the Stage 2 fetch below, where the page itself can re-price them and
    # verify_product has the final say.
    for p in prices:
        if p.get("price") is not None and "status" not in p \
                and _is_multi_item_title(p.get("product_title")):
            log.info(f"    {p.get('retailer', '?')}: multi-item title — snippet price "
                     f"${p['price']} voided ({(p.get('product_title') or '')[:60]!r})")
            p["price"] = None
            p["confidence"] = "low"
            p["_verify"] = "multi-item title — snippet price voided"

    # ── Stage 2: fetch to get missing prices + verify uncertain ones ──────────
    # Walmart product pages are always fetched, even at high confidence: Walmart's
    # documented failure mode is confident-but-wrong snippets, so the page itself
    # is the only trustworthy check.
    fetch_entries = [
        p for p in prices
        if p.get("url") and "status" not in p
        and classify_url(p.get("url")) != "hallucinated"
        and (
            p.get("price") is None
            or p.get("confidence") in ("low", "medium", None)
            or _is_walmart_consumer(p)
        )
    ]
    if fetch_entries:
        log.info(f"    fetching {len(fetch_entries)} URL(s)...")

    for entry in fetch_entries:
        fetch_msg = (
            f"Fetch this product page and extract the price for "
            f"{brand} {product} in shade \"{shade}\":\n{entry['url']}"
        )
        try:
            raw_fetch = await asyncio.wait_for(
                _run_agent(fetch_agent, fetch_msg, tracer),
                timeout=_FETCH_TIMEOUT_S,
            )
            fetched = _parse_obj(raw_fetch)
            verdict, reason = verify_product(candidate, fetched)
            if verdict == "reject":
                # Fetched page is a different product (wrong brand/shade). Discard its
                # price entirely so a mismatched page can never win in optimize().
                entry["price"]      = None
                entry["in_stock"]   = False
                entry["confidence"] = "low"
                entry["_verify"]    = f"rejected: {reason}"
                log.info(f"    {entry.get('retailer', '?')}: identity check rejected — {reason}")
            elif fetched.get("price") is not None:
                # Price confirmed on a page that matches. Confidence follows IDENTITY,
                # not merely fetch success: "high" only when brand+shade both matched.
                entry["price"]      = fetched["price"]
                entry["currency"]   = fetched.get("currency", entry.get("currency", "USD"))
                entry["in_stock"]   = fetched.get("in_stock", entry.get("in_stock"))
                entry["confidence"]    = verdict         # "high" | "medium" | "low"
                entry["_verify"]       = reason
                entry["_verified"]     = verdict == "high"
                entry["product_title"] = fetched.get("title") or entry.get("product_title")
            elif fetched.get("in_stock") is False:
                # Page confirmed unavailable — clear the snippet price so this
                # entry won't be selected by optimize()
                entry["price"]      = None
                entry["in_stock"]   = False
                entry["confidence"] = "high"
                log.info(f"    {entry.get('retailer', '?')}: page says unavailable — snippet price cleared")
        except asyncio.TimeoutError:
            log.warning(f"    fetch timeout ({entry.get('retailer', '?')})")
        except Exception as e:
            log.warning(f"    fetch error ({entry.get('retailer', '?')}): {e}")

    # ── Post-process ──────────────────────────────────────────────────────────
    prices, price_conflicts = _resolve_conflicts(prices)

    # Candidate-level status. An all-sentinel result carries it in `prices`; a
    # mixed result keeps the offers and parks the sentinel in price_conflicts.
    # "not_found" is meaningless once offers exist, but "discontinued" is not —
    # a retired shade often stays in stock at some stores, so it survives
    # alongside real prices.
    status = next((p["status"] for p in prices if p.get("status")), None) \
        or next((p["status"] for p in price_conflicts if p.get("status") == "discontinued"), None)

    tracer.flush()

    return {**candidate, "prices": prices, "price_conflicts": price_conflicts, "status": status}


async def run_tie_set(
    tie_set: list[dict],
    country: str = "US",
    strategy: str = "cheapest",
) -> dict:
    """
    Price all candidates (small concurrent fan-out), then run optimize().

    Args:
        tie_set: List of {"brand", "product", "shade", "delta_e"} dicts.
                 Extra keys (hex, format, ...) pass through untouched.
        country: Two-letter country code (default "US").
        strategy: "cheapest" (default) or "closest" — passed to optimize().
    """
    log.info(f"Pricing {len(tie_set)} candidates (country={country})...")
    sem = asyncio.Semaphore(CANDIDATE_CONCURRENCY)

    async def _one(c: dict) -> dict:
        async with sem:
            log.info(f"  → {c['brand']} / {c['shade']}")
            try:
                return await price_one_candidate(c, country)
            except Exception as e:
                # One candidate dying must not cancel the gather: that voids the
                # other candidates' already-paid searches and 500s the request.
                # An error candidate renders like any 0-price result in the UI.
                log.error(f"    {c['brand']} / {c['shade']} failed: {e!r}")
                return {**c, "prices": [], "price_conflicts": [], "status": "error"}

    priced = list(await asyncio.gather(*(_one(c) for c in tie_set)))
    for result in priced:
        found  = [p for p in result.get("prices", []) if p.get("price") is not None]
        status = result.get("status")
        label  = f"{result['brand']} / {result['shade']}"
        if found:
            note = f" (status: {status})" if status else ""
            log.info(f"    {label}: {len(found)} price(s){note}: {[p.get('retailer') for p in found]}")
        else:
            log.info(f"    {label}: status {status}" if status else f"    {label}: 0 prices")

    outcome = optimize(priced, strategy=strategy)
    outcome["all_candidates"] = priced
    return outcome
