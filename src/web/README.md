# `src/web/` — no-build frontend

The single-page UI for the dupe price-finder. The root [`README.md`](../README.md)
covers *why* the product works this way; this file covers *what the files are* and
*how the page talks to the backend*. It's served statically by
[`../backend/`](../backend/) at `/` — there is no build step for the app itself.

- **`Find Your Lipstick Twin.dc.html`** — the whole app: landing → brand/shade
  search → agent progress → results. On mount it pulls the ~9k-row lipstick catalog
  from Supabase, computes ΔE76 in CIELAB **client-side** to build the tie set (anchor
  + its closest twins), and `POST`s that to `/api/find-dupes`. It renders the
  cheapest page-verified dupe that comes back, and beacons UI events to `/api/log`.
- **`Lipstick.dc.html`** — a small standalone lipstick-tube component (a `color`
  prop drives the CSS). An illustration asset, independent of the main app.
- **`support.js`** — the generated **dc-runtime** that renders these files.
  *Do not edit* — it's built from `dc-runtime/src/*.ts` (`cd dc-runtime && bun run build`).

## Format

These are `.dc.html` files: an `<x-dc>` template with `{{ ... }}` bindings plus a
`<script type="text/x-dc" data-dc-script>` block whose `class Component extends
DCLogic` holds the state and logic. `support.js` parses both and renders with React,
so the host page must provide `window.React` / `window.ReactDOM`. All app state
(screen, catalog, search, results) lives in the `Component` in the main file.

## How it talks to the backend

| Call | When | Purpose |
|---|---|---|
| `GET` Supabase `lipstick-data` | on mount | Load the catalog (paged REST, publishable key). Falls back to a curated in-file list if unreachable. |
| `POST /api/find-dupes` | shade picked | Send the tie set; wait 1–3 min for page-verified prices. Aborts at 4 min. |
| `POST /api/log` | throughout | Fire-and-forget usage beacon (e.g. searches matching nothing). |

The ΔE color math and the catalog live entirely in the browser; the backend only
prices the tie set it's handed. See [`../backend/README.md`](../backend/README.md)
for the API contract and pipeline.

## Run it

Not standalone — launch the backend and open the page it serves:

```bash
cd ../backend
~/.pyenv/versions/3.11.9/bin/python -m uvicorn main:app --port 8000
# then open http://localhost:8000
```
