FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder
WORKDIR /app
ENV UV_LINK_MODE=copy

COPY . .
RUN uv sync --locked --no-dev --no-editable --package api

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/api /app/api

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/api
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
