from unittest.mock import patch

import pytest

from auto_course_api import stopping
from auto_course_contracts.cancellation import STOP_KEY_PREFIX, STOP_TTL_SECONDS


def task(task_id, username, name="study.run_domain"):
    credentials = {"username": username, "password": "secret"}
    args = [credentials] if name == "study.start" else ["one.example", credentials]
    return {"id": task_id, "name": name, "args": args}


def test_stop_signals_all_running_tasks_for_only_the_exact_account():
    active = {
        "worker-one": [task("start", "student", "study.start"), task("one", "student")],
        "worker-two": [
            task("two", "student"),
            task("other", "student2"),
            task("unrelated", "student", "other.task"),
        ],
    }
    with patch.object(stopping.app.control, "inspect") as inspect, patch.object(stopping, "Redis") as redis:
        inspect.return_value.active.return_value = active
        assert stopping.stop_account("student") is None
        pipeline = redis.from_url.return_value.__enter__.return_value.pipeline.return_value.__enter__.return_value
        assert {call.args for call in pipeline.setex.call_args_list} == {
            (f"{STOP_KEY_PREFIX}{task_id}", STOP_TTL_SECONDS, "1")
            for task_id in {"start", "one", "two"}
        }
        pipeline.execute.assert_called_once_with()


@pytest.mark.parametrize("active", [None, {}, {"worker": []}, {"worker": [task("other", "someone-else")]}])
def test_stop_does_nothing_when_account_has_no_running_tasks(active):
    with patch.object(stopping.app.control, "inspect") as inspect, patch.object(stopping, "Redis") as redis:
        inspect.return_value.active.return_value = active
        assert stopping.stop_account("student") is None
    redis.from_url.assert_not_called()
