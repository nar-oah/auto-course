from auto_course_contracts.cancellation import STOP_KEY_PREFIX, STOP_TTL_SECONDS
from redis import Redis

from .broker import app


def stop_account(username: str) -> None:
    active = app.control.inspect().active() or {}
    task_ids = set()
    for tasks in active.values():
        for task in tasks:
            name = task.get("name")
            if name not in {"study.start", "study.run_domain"}:
                continue
            args = task.get("args", [])
            index = 0 if name == "study.start" else 1
            if not isinstance(args, (list, tuple)) or len(args) <= index:
                continue
            credentials = args[index]
            if isinstance(credentials, dict) and credentials.get("username") == username:
                task_ids.add(task["id"])

    if not task_ids:
        return

    # Signal task IDs, never the account itself, so later submissions can run.
    with Redis.from_url(app.conf.broker_url) as redis:
        with redis.pipeline() as pipeline:
            for task_id in task_ids:
                pipeline.setex(f"{STOP_KEY_PREFIX}{task_id}", STOP_TTL_SECONDS, "1")
            pipeline.execute()
