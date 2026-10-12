import os

from auto_course_contracts import Credentials, StopRequest
from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware

from .broker import app as celery_app
from .stopping import stop_account

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


@app.post("/stop", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def stop(request: StopRequest) -> Response:
    stop_account(request.username)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
