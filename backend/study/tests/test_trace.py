from contextlib import contextmanager
import json
import logging
import os
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from auto_course_contracts import Credentials
import httpx

from auto_course_study import tasks, trace
from auto_course_study.bot import CourseBot
from auto_course_study.http import StudyClient, json_object
from auto_course_study.models import Config, Course, StudyInfo, Video


DOMAIN = "courses.example"
TASK_ID = "trace-task-123"
CREDENTIALS = {"username": "private-student", "password": "private-password"}
CAPTCHA = "private-captcha"
COURSE = Course("private-course-id", "Private course title")
VIDEO = Video("Private video title", "private-video-id", COURSE, 100, 50, .5)
NEXT_VIDEO = Video("Private next title", "private-next-id", COURSE, 100, 50, .5)
LONG_VIDEO = Video("Private long title", VIDEO.id, COURSE, 600, 0, 0)


@contextmanager
def captured_trace(enabled: str | None = "1"):
    """Capture only diagnostic records; never configure application logging."""
    records = []
    logger = logging.getLogger("auto_course_study.trace")
    handler = logging.Handler()
    handler.emit = records.append
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with patch.dict(os.environ):
            if enabled is None:
                os.environ.pop("STUDY_TRACE", None)
            else:
                os.environ["STUDY_TRACE"] = enabled
            yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def payloads(records):
    return [json.loads(record.getMessage()) for record in records]


class TraceTests(unittest.TestCase):
    def run_task(
        self,
        progress_handler,
        *,
        enabled="1",
        videos=(VIDEO,),
        config=Config(),
        login_status=True,
        heartbeat=False,
    ):
        requests = []
        study_waits = Mock()
        http_waits = Mock()
        clients = []

        def handler(request):
            requests.append((
                request.method,
                str(request.url),
                request.content,
                tuple(sorted(request.headers.items())),
            ))
            if request.url.path == "/service/code":
                return httpx.Response(200, content=b"private-captcha-image")
            if request.url.path == "/user/login":
                return httpx.Response(
                    200, json={"status": login_status},
                    headers={"Set-Cookie": "session=private-cookie; Path=/"},
                )
            if request.url.path == "/service/code/aa":
                return httpx.Response(200, content=b"private-captcha-image")
            return progress_handler(request)

        def client_factory(base_url):
            client = StudyClient(
                base_url, transport=httpx.MockTransport(handler), sleep=http_waits,
            )
            clients.append(client)
            return client

        def bot_factory(client, credentials):
            bot = CourseBot(
                client, credentials, config=config,
                ocr=Mock(classification=Mock(return_value=CAPTCHA)), sleep=study_waits,
            )
            bot.video_list.get_videos = Mock(return_value=iter(videos))
            if not heartbeat:
                bot._send_online_pings = Mock()
            return bot

        error = None
        with (
            captured_trace(enabled) as records,
            patch.object(tasks, "StudyClient", side_effect=client_factory),
            patch.object(tasks, "CourseBot", side_effect=bot_factory),
            patch("auto_course_study.bot.time.time", return_value=10),
        ):
            tasks.run_domain.push_request(id=TASK_ID)
            try:
                result = tasks.run_domain.run(DOMAIN, CREDENTIALS)
            except Exception as exc:
                error = exc
                result = None
            finally:
                tasks.run_domain.pop_request()
        self.assertTrue(all(client.client.is_closed for client in clients))
        return {
            "logs": payloads(records), "requests": requests, "result": result,
            "error": error,
            "study_waits": [call.args[0] for call in study_waits.call_args_list],
            "http_waits": [call.args[0] for call in http_waits.call_args_list],
        }

    def only_log(self, run):
        self.assertIsNone(run["error"])
        self.assertEqual(len(run["logs"]), 1)
        log = run["logs"][0]
        self.assertEqual(log["type"], "study_trace")
        self.assertEqual(log["task_id"], TASK_ID)
        self.assertEqual(log["domain"], DOMAIN)
        self.assertTrue(log["summary"]["started_at"].endswith("Z"))
        self.assertTrue(log["summary"]["ended_at"].endswith("Z"))
        self.assertGreaterEqual(log["summary"]["duration_seconds"], 0)
        return log

    def test_default_off_and_only_literal_one_enables_trace(self):
        for value in (None, "0", "true", "yes", ""):
            with self.subTest(value=value):
                run = self.run_task(
                    lambda request: httpx.Response(200, json={"status": False}),
                    enabled=value,
                )
                self.assertIsNone(run["error"])
                self.assertEqual(run["logs"], [])

    def test_normal_completion_emits_one_summary_without_request_events(self):
        run = self.run_task(
            lambda request: httpx.Response(200, json={"status": True, "studyId": 7}),
            videos=(VIDEO, NEXT_VIDEO),
        )
        log = self.only_log(run)
        self.assertNotIn("events", log)
        self.assertIsNone(log["summary"]["first_anomaly"])
        self.assertEqual(run["study_waits"], [0, 30, 15, 0, 30, 15])

    def test_uncaught_exception_is_preserved_and_flushes_diagnostics(self):
        def fail(request):
            raise ValueError("private-password private-cookie unexpected failure")

        run = self.run_task(fail)
        self.assertIsInstance(run["error"], ValueError)
        self.assertEqual(len(run["logs"]), 1)
        log = run["logs"][0]
        self.assertIsNotNone(log["summary"]["first_anomaly"])
        self.assertTrue(log["events"])
        serialized = json.dumps(log)
        self.assertNotIn("private-password", serialized)
        self.assertNotIn("private-cookie", serialized)

    def test_trace_on_preserves_requests_waits_and_returns_for_recovery_paths(self):
        cases = (
            ({"status": True, "studyId": 7},),
            ({"need_code": 1}, {"status": True, "studyId": "7"}),
            ({"status": True, "studyId": 7}, {"offline": 1}),
            ({}, {"status": True, "studyId": 7}),
            ({"status": False},),
            ({"status": True, "studyId": 7}, {"status": True, "studyId": 7}, {}),
        )
        for responses in cases:
            with self.subTest(responses=responses):
                runs = []
                for enabled in (None, "1"):
                    pending = list(responses)

                    def handler(request):
                        body = pending.pop(0) if pending else {"status": True, "studyId": 7}
                        return httpx.Response(200, json=body)

                    runs.append(self.run_task(handler, enabled=enabled))
                before, after = runs
                for key in ("requests", "study_waits", "http_waits", "result", "error"):
                    self.assertEqual(before[key], after[key], key)

    def test_bounded_events_keep_first_anomaly_and_task_statistics(self):
        with captured_trace() as records:
            with trace.task_trace(DOMAIN, TASK_ID) as diagnostic:
                diagnostic.anomaly("first_failure", "progress")
                for index in range(400):
                    diagnostic.event("synthetic_event", index=index)
                self.assertEqual(diagnostic.events.maxlen, 256)
                self.assertLessEqual(len(diagnostic.events), 256)
                diagnostic.outcome("task", "completed", "success")
        log = payloads(records)[0]
        self.assertEqual(len(log["events"]), 256)
        self.assertGreater(log["summary"]["events_total"], 400)
        self.assertGreater(log["summary"]["events_dropped"], 0)
        self.assertEqual(log["summary"]["first_anomaly"]["reason"], "first_failure")


if __name__ == "__main__":
    unittest.main()
