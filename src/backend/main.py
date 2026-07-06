"""FastAPI wrapper for the dupe price-finder pipeline + static host for the web app.

Run:  ~/.pyenv/versions/3.11.9/bin/python -m uvicorn main:app --port 8000
from the src/backend/ directory, then open http://localhost:8000
"""
import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline import TRACE_DIR, run_tie_set

# Route app logs to stderr (never block-buffered, unlike stdout under Docker /
# a PaaS), unbuffered per line. Bare-message format keeps the pipeline's
# indented narration readable; hosted platforms timestamp lines themselves.
logging.basicConfig(level=logging.INFO, format="%(message)s")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
USAGE_LOG = TRACE_DIR / "usage.jsonl"


def log_usage(event: str, record: dict) -> None:
    """Append one usage event next to the agent traces. Never let logging
    break a request."""
    try:
        line = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "event": event, **record}
        with USAGE_LOG.open("a") as f:
            f.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass

app = FastAPI(title="Lipstick Twin — dupe price finder")


class Candidate(BaseModel):
    brand: str
    product: str
    shade: str
    delta_e: float
    model_config = {"extra": "allow"}  # hex, format, etc. pass through to the response


class DupeRequest(BaseModel):
    tie_set: list[Candidate] = Field(..., min_length=1, max_length=7)  # anchor + up to 6 dupes
    country: str = "US"
    strategy: Literal["cheapest", "closest"] = "cheapest"
    # What the user actually typed/picked in the UI — logged for later analysis,
    # never fed to the pipeline.
    client: dict | None = None


# Prices are stable for hours; cache full outcomes so repeat demo runs (and
# video retakes) are instant and free.
_CACHE_TTL_S = 6 * 3600
_cache: dict[str, tuple[float, dict]] = {}


def _outcome_summary(outcome: dict) -> dict:
    rec = outcome.get("recommendation")
    ret = outcome.get("retailer") or {}
    return {
        "winner": {"brand": rec.get("brand"), "shade": rec.get("shade"),
                   "price": rec.get("_best_price"), "retailer": ret.get("retailer")} if rec else None,
        "tradeoff": outcome.get("tradeoff"),
        "prices_found": {
            f"{c.get('brand')} / {c.get('shade')}": sum(1 for p in c.get("prices", []) if p.get("price") is not None)
            for c in outcome.get("all_candidates", [])
        },
        "statuses": {
            f"{c.get('brand')} / {c.get('shade')}": c["status"]
            for c in outcome.get("all_candidates", []) if c.get("status")
        },
    }


@app.on_event("startup")
async def _seed_cache() -> None:
    """Replay a saved outcome: SEED_OUTCOME=<path to an *_outcome.json> pre-warms
    the cache so the identical UI query returns that run's real result instantly
    (screenshots, video retakes) instead of re-pricing live."""
    seed = os.environ.get("SEED_OUTCOME")
    if not seed:
        return
    try:
        data = json.loads(Path(seed).read_text())
        request = {k: v for k, v in data["request"].items() if k != "client"}
        key = json.dumps(request, sort_keys=True, default=str)
        _cache[key] = (time.time(), data["outcome"])
        logging.info("cache seeded from %s", seed)
    except Exception as e:
        logging.warning("SEED_OUTCOME failed (%r) — starting with an empty cache", e)


@app.post("/api/find-dupes")
async def find_dupes(req: DupeRequest) -> dict:
    # Cache on the pipeline inputs only — the client blob varies per user and
    # must not fragment the cache.
    key = json.dumps(req.model_dump(exclude={"client"}), sort_keys=True, default=str)
    base = {
        "client": req.client,
        "tie_set": [f"{c.brand} / {c.shade}" for c in req.tie_set],
        "country": req.country,
    }
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        log_usage("find_dupes", {**base, "cache_hit": True, **_outcome_summary(hit[1])})
        return hit[1]

    t0 = time.time()
    try:
        outcome = await run_tie_set(
            [c.model_dump() for c in req.tie_set],
            country=req.country,
            strategy=req.strategy,
        )
    except Exception as e:
        log_usage("find_dupes", {**base, "cache_hit": False, "duration_s": round(time.time() - t0, 1), "error": repr(e)})
        raise
    _cache[key] = (time.time(), outcome)

    # Persist the full result (every price, URL, page title) alongside the
    # per-candidate agent traces — the cache is in-memory only.
    outcome_file = None
    try:
        # %f (microseconds) prevents two requests finishing in the same second
        # from overwriting each other's outcome file.
        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = TRACE_DIR / f"{ts_str}_outcome.json"
        path.write_text(json.dumps({"request": req.model_dump(), "outcome": outcome},
                                   indent=2, default=str))
        outcome_file = path.name
    except Exception:
        pass

    log_usage("find_dupes", {**base, "cache_hit": False, "duration_s": round(time.time() - t0, 1),
                             "outcome_file": outcome_file, **_outcome_summary(outcome)})
    return outcome


@app.post("/api/log")
async def log_event(request: Request, payload: dict = Body(...)) -> dict:
    """Lightweight beacon for UI events worth learning from (e.g. searches
    that matched nothing in the catalog)."""
    event = str(payload.pop("event", "ui"))[:40]
    slim = {k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:300])
            for k, v in list(payload.items())[:10]}
    slim["ua"] = (request.headers.get("user-agent") or "")[:120]
    log_usage(event, slim)
    return {"ok": True}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "Find Your Lipstick Twin.dc.html")


app.mount("/", StaticFiles(directory=WEB_DIR), name="static")
