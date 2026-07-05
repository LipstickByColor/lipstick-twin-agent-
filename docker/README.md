# Run the demo in Docker

Everything — the FastAPI backend, the web UI, and the MCP page-fetch server —
runs in **one container**. You only need Docker installed and a **Gemini
(Google AI Studio) API key**. Get a free key at
<https://aistudio.google.com/apikey>.

A full 4-candidate query costs a few cents on a pay-as-you-go key and takes
1–2 minutes; repeat queries are served instantly from a 6-hour cache.

Run all commands **from the repo root** (the build context is the whole repo).

## 1. Build

```bash
docker build -f docker/Dockerfile -t lipstick-twin .
```

## 2. Run

**Interactive — the container asks for your key (recommended):**

```bash
docker run -it -p 8000:8000 lipstick-twin
```

You'll be prompted:

```
Enter your Gemini (Google AI Studio) API key: ****
```

**Non-interactive — pass the key on the command line:**

```bash
docker run -p 8000:8000 -e GOOGLE_API_KEY=your-key-here lipstick-twin
```

Either way, open **<http://localhost:8000>** and pick a shade.

**Or use Docker Compose** (one command, builds too). Compose can't prompt, so
put your key in a `.env` file at the repo root (`GOOGLE_API_KEY=your-key-here`);
compose loads it into the container automatically:

```bash
docker compose -f docker/compose.yaml up --build
```

Open <http://localhost:8000>; stop with `Ctrl-C` then
`docker compose -f docker/compose.yaml down`.

## Notes

- The key lives only in the running container's environment — it is never baked
  into the image (`.env` is excluded via `.dockerignore`).
- To keep the JSONL run traces after the container stops, mount a volume:
  `-v "$PWD/src/research/traces:/app/src/research/traces"`.
- Stop with `Ctrl-C` (interactive) or `docker stop <container>`.
