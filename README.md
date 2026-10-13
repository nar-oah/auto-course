# auto-course

`backend/api` accepts account credentials at `POST /start` and publishes
`study.start` to Redis through Celery. It returns an empty `202 Accepted` response.
`backend/study` discovers learning domains from links with an `h3` title containing
`课程`, deduplicates them in page order, and publishes one `study.run_domain` task
per domain. Each domain task uses one HTTPX client for
captcha recognition, login, course/video discovery, online heartbeats and progress
submission. Only the API exposes HTTP; study runs as a Celery worker.

`backend/contracts` contains only the credentials model shared by the two
services. Course, video and progress models belong to study. There is no task
result backend, task status API, polling or user task log storage.

## Backend

Provide the same `CELERY_BROKER_URL` to both services. The default is
`redis://redis:6379/0`; production Redis must be reachable on the existing backend
network. `compose.prod.yml` keeps the existing API/study services and external
networks. Its `.env` can override the broker address without exposing study.

```sh
cd backend
uv sync --all-packages
export CELERY_BROKER_URL=redis://127.0.0.1:6379/0
uv run --all-packages uvicorn auto_course_api.main:app --host 127.0.0.1 --port 8000
```

In another terminal, start the worker:

```sh
cd backend
export CELERY_BROKER_URL=redis://127.0.0.1:6379/0
uv run --all-packages celery -A auto_course_study.tasks:app worker -Q study --loglevel=info
```

Run the backend tests without contacting learning domains or Redis:

```sh
cd backend
uv run --all-packages pytest
```

The API supports browser CORS preflight. `CORS_ALLOW_ORIGINS` can restrict access
to a comma-separated list of frontend origins; its default is `*`. The production
proxy removes `/auto-course` before forwarding requests to FastAPI.

## Frontend

`frontend/app` contains the form and the generated-schema API client. Web passes
browser fetch; Desktop passes Tauri HTTP plugin fetch. Credentials remain only in
component memory, and a successful submission displays only a receipt message.

```sh
cd frontend
pnpm install
pnpm api:generate
pnpm build:web
pnpm check
pnpm --filter @auto-course/desktop build
```

Build Web before running the workspace check on a fresh checkout: Wrangler's
generated types include the built Worker entry point. `build:web` also builds the
shared App package.

The production generator reads
`https://aws.naroah.top/auto-course/openapi.json`, and the client calls
`https://aws.naroah.top/auto-course/start`. While the production schema is not
available, generate the same type file from a running local API without changing
the production script:

```sh
cd frontend
pnpm exec openapi-typescript http://127.0.0.1:8000/openapi.json -o app/src/lib/api/schema.d.ts
```
