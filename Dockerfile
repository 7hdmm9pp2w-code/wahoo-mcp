# wahoo-mcp as a remote MCP server (Streamable HTTP on port 8000)

FROM ghcr.io/astral-sh/uv:0.8-python3.13-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Dependencies first, so code changes do not reinstall them
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.13-slim-bookworm
# Same interpreter path as the build stage, so the venv works unchanged
RUN useradd --create-home --uid 1000 app && mkdir /data && chown app /data
WORKDIR /app
COPY --from=build --chown=app /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    WAHOO_TOKEN_FILE=/data/token.json \
    WAHOO_HTTP_HOST=0.0.0.0 \
    WAHOO_HTTP_PORT=8000
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "-m", "src.http_app"]
