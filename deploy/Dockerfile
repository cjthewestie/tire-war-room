# War-room app image (FastAPI + sim + detector + safety + agent + OR-Tools + web/dist).
# Build context = repo root:   docker build -f deploy/Dockerfile -t warroom-app:latest .
# Runs on the DGX Spark (linux/arm64) and on a Mac via Docker Desktop (linux/arm64).
# Everything the app needs at runtime is baked in at build time, so it runs with NO internet:
# web/dist, the Python venv, and the fastembed multilingual embedding model for the SOP KB.

# ---- web build ----
FROM node:22-bookworm-slim AS web
WORKDIR /src/web
RUN corepack enable pnpm
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ---- python deps ----
FROM python:3.12-slim-bookworm AS py
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# .python-version is a bare "3.12" (no platform) so uv works on the Mac and the Spark; here we pin the image's 3.12.
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python3.12
COPY warroom/ warroom/
RUN uv sync --frozen --no-dev --python /usr/local/bin/python3.12
# Pre-fetch the SOP embedding model so ingest/search never touches the network at runtime.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed HF_HOME=/opt/hf
RUN /opt/venv/bin/python -c "from fastembed import TextEmbedding; \
TextEmbedding(model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', cache_dir='/opt/fastembed')"

# ---- runtime ----
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends curl tini && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 10001 --home /app warroom
WORKDIR /app
COPY --from=py /opt/venv /opt/venv
COPY --from=py --chown=10001 /opt/fastembed /opt/fastembed
COPY --from=py /app/warroom /app/warroom
COPY --from=web /src/web/dist /app/web/dist
COPY bench/ /app/bench/
COPY data/ /app/data/
RUN mkdir -p /app/data/runtime && chown -R warroom /app/data
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 DO_NOT_TRACK=1 \
    HOST=0.0.0.0 PORT=8000
USER warroom
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "warroom.api"]
