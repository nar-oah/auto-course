import os

from auto_course_contracts import Credentials
from celery import Celery
from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware


celery_app = Celery(
    "auto_course_api",
    broker=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
)
celery_app.conf.update(
    task_default_queue="study",
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    result_backend=None,
)

app = FastAPI(title="Auto Course", root_path="/auto-course")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)


@app.post("/start", status_code=status.HTTP_202_ACCEPTED, response_class=Response)
def start(credentials: Credentials) -> Response:
    celery_app.send_task(
        "study.start",
        args=[credentials.model_dump(mode="json")],
        queue="study",
        ignore_result=True,
        argsrepr="(<credentials>,)",
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)
