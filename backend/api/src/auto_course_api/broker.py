import os

from celery import Celery

app = Celery("auto_course_api", broker=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"))
app.conf.update(
    task_default_queue="study",
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    result_backend=None,
)
