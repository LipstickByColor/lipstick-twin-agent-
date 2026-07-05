# Find Your Lipstick Twin — one-container demo (FastAPI backend + static UI).
#
# The backend static-hosts the web app and spawns the MCP `fetch` server as a
# subprocess using this same Python interpreter, so a single image runs the
# whole thing. Judges only need Docker and a Gemini (Google AI Studio) API key.
#
# Build from the REPO ROOT (build context = repo root):
#   docker build -f docker/Dockerfile -t lipstick-twin .
#   docker run -it -p 8000:8000 lipstick-twin      # prompts for the API key
#
# then open http://localhost:8000
FROM python:3.11-slim

# uvicorn logs go to stderr per-line; keep Python unbuffered so they show live.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first so the layer caches across code edits.
COPY requirements.txt .
RUN pip install -r requirements.txt

# App code, mirroring the repo's src/ layout so the pipeline's relative paths
# (../web, ../research/traces) resolve the same as they do locally. Secrets
# (.env), the research notebook, assets, and prior traces are left out.
COPY src/backend/ ./src/backend/
COPY src/web/ ./src/web/

# The pipeline writes JSONL traces to ../research/traces relative to the
# backend; create it so the first run doesn't have to.
RUN mkdir -p /app/src/research/traces

EXPOSE 8000

# uvicorn resolves `main:app` from the backend dir; pipeline paths are anchored
# to __file__, so this is the right working directory.
WORKDIR /app/src/backend

COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
