import os
from typing import Any

from auto_course_contracts import Credentials
from celery import Celery

from .bot import CourseBot
from .cancellation import StudyStopped, TaskCancellation
from .discovery import DISCOVERY_URL, discover_domains
from .http import StudyClient


app = Celery("study", broker=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"))
app.conf.update(
    task_default_queue="study",
    task_routes={"study.*": {"queue": "study"}},
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    result_backend=None,
)


@app.task(name="study.start", ignore_result=True)
def start(credentials: dict[str, Any]) -> None:
    validated = Credentials.model_validate(credentials).model_dump(mode="json")
    with TaskCancellation(start.request.id, app.conf.broker_url) as cancellation:
        try:
            with StudyClient(
                DISCOVERY_URL, sleep=cancellation.sleep, check=cancellation.check
            ) as client:
                domains = discover_domains(client)
            for domain in domains:
                cancellation.check()
                run_domain.apply_async(
                    args=[domain, validated],
                    queue="study",
                    argsrepr=f"({domain!r}, <credentials>)",
                )
        except StudyStopped:
            return


@app.task(name="study.run_domain", ignore_result=True)
def run_domain(domain: str, credentials: dict[str, Any]) -> None:
    validated = Credentials.model_validate(credentials)
    with TaskCancellation(run_domain.request.id, app.conf.broker_url) as cancellation:
        try:
            with StudyClient(
                f"https://{domain}", sleep=cancellation.sleep, check=cancellation.check
            ) as client:
                CourseBot(client, validated, sleep=cancellation.sleep).run()
        except StudyStopped:
            return
