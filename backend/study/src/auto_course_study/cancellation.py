import time

from auto_course_contracts.cancellation import STOP_KEY_PREFIX
from redis import Redis


class StudyStopped(Exception):
    """Unwind the task so its heartbeat and HTTP client are closed."""


class TaskCancellation:
    def __init__(self, task_id: str | None, broker_url: str) -> None:
        self.key = f"{STOP_KEY_PREFIX}{task_id}" if task_id else None
        self.redis = Redis.from_url(broker_url) if task_id else None

    def __enter__(self) -> "TaskCancellation":
        return self

    def __exit__(self, *args: object) -> None:
        if self.redis is not None:
            try:
                self.redis.delete(self.key)
            finally:
                self.redis.close()

    def check(self) -> None:
        if self.redis is not None and self.redis.exists(self.key):
            raise StudyStopped

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))
