import threading
from unittest.mock import Mock, patch

import httpx
import pytest

from auto_course_contracts import Credentials
from auto_course_contracts.cancellation import STOP_KEY_PREFIX
from auto_course_study.bot import CourseBot
from auto_course_study.cancellation import StudyStopped, TaskCancellation
from auto_course_study.http import StudyClient
from auto_course_study.models import Course, Video


def test_task_stop_interrupts_sleep_and_removes_its_signal():
    with patch("auto_course_study.cancellation.Redis") as redis, patch("auto_course_study.cancellation.time.sleep") as sleep:
        connection = redis.from_url.return_value
        connection.exists.side_effect = [False, True]
        with pytest.raises(StudyStopped):
            with TaskCancellation("task-one", "redis://localhost/0") as cancellation:
                cancellation.sleep(60)
        sleep.assert_called_once()
        assert sleep.call_args.args[0] <= 0.5
        connection.delete.assert_called_once_with(f"{STOP_KEY_PREFIX}task-one")
        connection.close.assert_called_once_with()


def test_stopped_http_request_is_not_sent_and_client_is_closed():
    send = Mock(return_value=httpx.Response(200))
    with pytest.raises(StudyStopped):
        with StudyClient("https://one.example", transport=httpx.MockTransport(send), check=Mock(side_effect=StudyStopped)) as client:
            client.get_page()
    send.assert_not_called()
    assert client.client.is_closed


def test_cancelled_heartbeat_exits_without_a_thread_error():
    client = Mock()
    client.post_online.side_effect = StudyStopped
    bot = CourseBot(client, Credentials(username="student", password="secret"))
    bot._send_online_pings(threading.Event())
    client.post_online.assert_called_once_with()


def test_stop_during_progress_joins_heartbeat():
    client = Mock()
    bot = CourseBot(client, Credentials(username="student", password="secret"), sleep=Mock(side_effect=StudyStopped))
    with patch("auto_course_study.bot.threading.Thread") as thread:
        with pytest.raises(StudyStopped):
            bot.update_study_progress(Video("Video", "video", Course("course", "Course"), 120, 0, 0))
    thread.return_value.start.assert_called_once_with()
    thread.return_value.join.assert_called_once_with()
    assert thread.call_args.kwargs["args"][0].is_set()
