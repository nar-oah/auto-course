FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder
WORKDIR /app
ENV UV_LINK_MODE=copy

COPY . .
RUN uv sync --locked --no-dev --no-editable --package study

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/study /app/study

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/study

CMD ["celery", "-A", "auto_course_study.tasks:app", "worker", "-Q", "study", "--loglevel=info"]
