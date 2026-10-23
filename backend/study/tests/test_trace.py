from contextlib import contextmanager
import json
import logging
import os
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from auto_course_study import tasks, trace
from auto_course_study.bot import CourseBot
from auto_course_study.catalog import VideoList
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
    """Reassemble diagnostic payloads independently of chunk delivery order."""
    results = []
    chunks = {}
    for record in records:
        payload = json.loads(record.getMessage())
        if payload["type"] != "study_trace_chunk":
            results.append(payload)
            continue
        if payload["encoding"] != "json_fragment":
            raise AssertionError("Unknown diagnostic chunk encoding")
        key = (payload["task_id"], payload["domain"])
        group = chunks.setdefault(key, {"count": payload["chunk_count"], "parts": {}})
        if group["count"] != payload["chunk_count"]:
            raise AssertionError("Inconsistent diagnostic chunk count")
        index = payload["chunk_index"]
        if not 1 <= index <= group["count"] or index in group["parts"]:
            raise AssertionError("Invalid or duplicate diagnostic chunk index")
        group["parts"][index] = payload["data"]
    for group in chunks.values():
        if len(group["parts"]) != group["count"]:
            raise AssertionError("Missing diagnostic chunk")
        results.append(json.loads("".join(
            group["parts"][index] for index in range(1, group["count"] + 1)
        )))
    return results


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
        credentials=CREDENTIALS,
        ocr_result=CAPTCHA,
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
                ocr=Mock(classification=Mock(return_value=ocr_result)), sleep=study_waits,
            )
            bot.video_list.get_videos = Mock(return_value=iter(videos))
            if callable(heartbeat):
                bot._send_online_pings = lambda stop: heartbeat(bot, stop)
            elif not heartbeat:
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
                result = tasks.run_domain.run(DOMAIN, credentials)
            except Exception as exc:
                error = exc
                result = None
            finally:
                tasks.run_domain.pop_request()
        self.assertTrue(all(client.client.is_closed for client in clients))
        return {
            "logs": payloads(records), "requests": requests, "result": result,
            "records": records,
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
        self.assertEqual(log["summary"]["result"], "completed")
        self.assertEqual(log["summary"]["reason"], "traversal_completed")
        self.assertEqual(log["summary"]["celery_return"], "returned")
        self.assertEqual(run["study_waits"], [0, 30, 15, 0, 30, 15])

    def test_business_rejection_and_early_returns_are_distinct_from_celery_success(self):
        cases = (
            ("rejected", ({"status": False},), (VIDEO, NEXT_VIDEO), Config(), True, "progress_rejected", "business_rejected"),
            ("unconfirmed_boundary", ({"status": True, "studyId": 7}, {"status": True, "studyId": 7}, {}), (VIDEO, NEXT_VIDEO), Config(), True, "progress_unconfirmed", "boundary_unconfirmed"),
            ("budget", ({}, {}), (LONG_VIDEO, NEXT_VIDEO), Config(max_failures=2), True, "failure_budget_exhausted", "failure_budget_exhausted"),
            ("login", (), (VIDEO,), Config(), False, "login_failed", "initial_login_failed"),
            ("empty", (), (), Config(), True, "empty_video_list", "empty_video_list"),
        )
        for name, responses, videos, config, login_status, result, reason in cases:
            with self.subTest(name=name):
                pending = list(responses)

                def handler(request):
                    return httpx.Response(200, json=pending.pop(0))

                log = self.only_log(self.run_task(
                    handler, videos=videos, config=config, login_status=login_status,
                ))
                summary = log["summary"]
                self.assertEqual(summary["result"], result)
                self.assertEqual(summary["reason"], reason)
                self.assertEqual(summary["celery_return"], "returned")
                if name == "empty":
                    self.assertTrue(log["events"])
                    self.assertIn("task_start", [event["event"] for event in log["events"]])
                    self.assertIsNone(summary["last_progress"])
                else:
                    self.assertTrue(log["events"])
                    self.assertIsNotNone(summary["first_anomaly"])
                if responses:
                    self.assertFalse(summary["last_progress"]["confirmed"])
                    self.assertEqual(summary["last_progress"]["response"]["http_status"], 200)
                    self.assertEqual(summary["counts"]["video_start"], 1)
                if name == "rejected":
                    self.assertFalse(summary["last_progress"]["response"]["status"])
                    self.assertEqual(summary["last_progress"]["decision"], "exit_business_rejected")

    def test_video_transition_records_immediate_next_start_and_last_response(self):
        submissions = 0

        def handler(request):
            nonlocal submissions
            submissions += 1
            return httpx.Response(200, json={
                "status": submissions < 4, "studyId": 7,
            })

        log = self.only_log(self.run_task(handler, videos=(VIDEO, NEXT_VIDEO)))
        starts = [event for event in log["events"] if event["event"] == "video_start"]
        ends = [event for event in log["events"] if event["event"] == "video_end"]
        progress = [event for event in log["events"] if event["event"] == "progress_start"]
        self.assertEqual([event["video_ordinal"] for event in starts], [1, 2])
        self.assertNotEqual(starts[0]["video"], starts[1]["video"])
        self.assertLess(ends[0]["seq"], starts[1]["seq"])
        self.assertLess(starts[1]["seq"], progress[3]["seq"])
        self.assertGreaterEqual(starts[1]["since_previous_video_end"], 0)
        self.assertEqual(ends[0]["reason"], "boundary_confirmed")
        self.assertEqual(starts[0]["total_duration"], 100)
        self.assertEqual(starts[0]["watched_duration"], 50)
        self.assertEqual(starts[0]["raw_progress"], .5)
        self.assertEqual(starts[0]["finish_time"], 46)
        self.assertEqual([event["studyTime"] for event in progress], [1, 31, 46, 1])
        self.assertEqual([event["submission"] for event in progress], [1, 2, 3, 4])
        self.assertEqual([event["video_changed"] for event in progress], [False, False, False, True])
        self.assertEqual(log["summary"]["last_progress"]["video"], starts[1]["video"])
        for private in (VIDEO.id, NEXT_VIDEO.id, COURSE.id, VIDEO.name, COURSE.name):
            self.assertNotIn(private, json.dumps(log))

    def test_captcha_duplicate_and_relogin_recovery_have_explicit_causes(self):
        pending = [
            {"need_code": 1}, {"status": True, "studyId": "7"},
            {"offline": 1}, {"status": False, "errorCode": 409},
        ]
        log = self.only_log(self.run_task(
            lambda request: httpx.Response(200, json=pending.pop(0)),
        ))
        starts = [event for event in log["events"] if event["event"] == "progress_start"]
        ends = [event for event in log["events"] if event["event"] == "progress_end"]
        self.assertEqual([event["studyTime"] for event in starts], [1, 1, 31, 46])
        self.assertEqual([event["kind"] for event in starts], ["normal", "captcha", "normal", "normal"])
        self.assertEqual([event["cause"] for event in starts], ["initial", "captcha", "scheduled", "relogin"])
        self.assertEqual([event["repeated_study_time"] for event in starts], [False, True, False, False])
        self.assertEqual([event["decision"] for event in ends], ["captcha_supplement", "confirmed", "restore_login", "exit_business_rejected"])
        self.assertTrue(ends[1]["confirmed"])
        self.assertEqual(starts[2]["studyId"]["ref"], ends[1]["response"]["studyId"]["ref"])
        self.assertEqual(starts[3]["failures"], 1)
        self.assertGreaterEqual(starts[3]["actual_wait"], 0)
        logins = [event for event in log["events"] if event["event"] == "login_result"]
        self.assertEqual([event["login_phase"] for event in logins], ["initial", "recovery"])
        self.assertTrue(all(event["accepted"] for event in logins))
        self.assertEqual(log["summary"]["last_progress"]["response"]["errorCode"], 409)
        for secret in (*CREDENTIALS.values(), CAPTCHA, "private-cookie"):
            self.assertNotIn(secret, json.dumps(log))

    def test_heartbeat_progress_overlap_is_attributed_to_the_same_video(self):
        heartbeat_started = threading.Event()
        progress_started = threading.Event()

        def handler(request):
            if request.url.path == "/user/online":
                heartbeat_started.set()
                self.assertTrue(progress_started.wait(timeout=2))
                return httpx.Response(200, json={"status": True})
            self.assertEqual(request.url.path, "/user/node/study")
            self.assertTrue(heartbeat_started.wait(timeout=2))
            progress_started.set()
            return httpx.Response(200, json={"status": False})

        log = self.only_log(self.run_task(handler, heartbeat=True))
        summary = log["summary"]
        self.assertEqual(summary["http"]["heartbeat_progress_overlaps"], 1)
        self.assertEqual(summary["last_heartbeat"]["video"], summary["last_progress"]["video"])
        self.assertTrue(summary["last_heartbeat"]["overlaps"][0]["same_video"])
        names = [event["event"] for event in log["events"]]
        for event in ("heartbeat_thread_started", "heartbeat_thread_stop_requested", "heartbeat_thread_exit", "heartbeat_thread_joined"):
            self.assertIn(event, names)
        self.assertLess(names.index("heartbeat_thread_exit"), names.index("heartbeat_thread_joined"))

    def test_last_heartbeat_can_be_distinguished_from_progress_on_the_next_video(self):
        heartbeat_started = threading.Event()
        heartbeat_calls = []
        study_calls = []

        def heartbeat(bot, stop):
            heartbeat_calls.append(stop)
            if len(heartbeat_calls) == 1:
                CourseBot._send_online_pings(bot, stop)
            else:
                # Hold the second thread before its first ping, simulating a
                # scheduler that lets the study thread reach rejection first.
                stop.wait(timeout=2)

        def handler(request):
            if request.url.path == "/user/online":
                heartbeat_started.set()
                return httpx.Response(200, json={"status": True})
            self.assertTrue(heartbeat_started.wait(timeout=2))
            study_calls.append(request)
            return httpx.Response(200, json={"status": len(study_calls) < 4, "studyId": 7})

        log = self.only_log(self.run_task(handler, videos=(VIDEO, NEXT_VIDEO), heartbeat=heartbeat))
        videos = [event["video"] for event in log["events"] if event["event"] == "video_start"]
        self.assertEqual(log["summary"]["last_heartbeat"]["video"], videos[0])
        self.assertEqual(log["summary"]["last_progress"]["video"], videos[1])
        self.assertNotEqual(videos[0], videos[1])

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

    def test_concurrent_buffer_writes_preserve_sequence_and_counts(self):
        with captured_trace() as records:
            with trace.task_trace(DOMAIN, TASK_ID) as diagnostic:
                diagnostic.anomaly("dump_for_test", "heartbeat")

                def emit():
                    for index in range(100):
                        diagnostic.event("thread_event", index=index)

                workers = [threading.Thread(target=emit) for _ in range(4)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())
                diagnostic.outcome("task", "completed", "completed")
        log = payloads(records)[0]
        self.assertEqual(log["summary"]["counts"]["thread_event"], 400)
        sequences = [event["seq"] for event in log["events"]]
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertEqual(len(sequences), 256)

    def test_emitter_failure_does_not_change_a_task_result(self):
        with patch.object(trace.logger, "log", side_effect=RuntimeError("broken handler")):
            run = self.run_task(
                lambda request: httpx.Response(200, json={"status": True, "studyId": 7}),
            )
        self.assertIsNone(run["error"])
        self.assertIsNone(run["result"])
        self.assertEqual(run["study_waits"], [0, 30, 15])

    def test_collection_failure_does_not_change_requests_or_waits(self):
        def handler(request):
            return httpx.Response(200, json={"status": True, "studyId": 7})

        before = self.run_task(handler, enabled=None)
        with patch.object(trace.StudyTrace, "_record", side_effect=RuntimeError("broken collector")):
            after = self.run_task(handler)
        for key in ("requests", "study_waits", "http_waits", "result", "error"):
            self.assertEqual(before[key], after[key], key)

    def test_short_numeric_credentials_cannot_corrupt_structured_json(self):
        log = self.only_log(self.run_task(
            lambda request: httpx.Response(200, json={"status": False, "errorCode": "7"}),
            credentials={"username": "1", "password": "7"},
        ))
        self.assertEqual(log["type"], "study_trace")
        self.assertEqual(log["summary"]["last_progress"]["studyTime"], 1)
        self.assertEqual(log["summary"]["last_progress"]["response"]["http_status"], 200)
        self.assertFalse(log["summary"]["last_progress"]["response"]["status"])
        self.assertEqual(log["summary"]["last_progress"]["response"]["errorCode"], "[redacted]")
        video = next(event for event in log["events"] if event["event"] == "video_start")["video"]
        self.assertEqual(log["summary"]["last_progress"]["video"], video)
        self.assertTrue(all(
            event["started_at"].endswith("Z") and "[redacted]" not in event["started_at"]
            for event in log["events"] if "started_at" in event
        ))

    def test_catalog_counts_are_final_only_after_lazy_video_traversal(self):
        def handler(request):
            if request.url.path == "/user/":
                return httpx.Response(200, text='<div class="total">1 / 1</div>')
            if request.url.path == "/user/index":
                return httpx.Response(200, text='<div class="name"><a href="/user/course?courseId=123">Private course</a></div>')
            self.assertEqual(request.url.path, "/user/study_record.json")
            return httpx.Response(200, json={
                "pageInfo": {"pageCount": 1},
                "list": [
                    {"id": "video-a", "name": "Private title", "progress": .5, "videoDuration": "00:01:00", "duration": 10},
                    {"id": "video-b", "progress": 0, "videoDuration": "00:02:00"},
                    {"id": "finished", "progress": 1},
                ],
            })

        for complete in (False, True):
            with self.subTest(complete=complete):
                with captured_trace() as records:
                    with trace.task_trace(DOMAIN, TASK_ID) as diagnostic:
                        with StudyClient("https://" + DOMAIN, transport=httpx.MockTransport(handler)) as client:
                            videos = VideoList(client, .91).get_videos()
                            self.assertEqual(next(videos).id, "video-a")
                            if complete:
                                self.assertEqual([video.id for video in videos], ["video-b"])
                            else:
                                videos.close()
                        diagnostic.outcome("video_list", "traversal_completed", "completed")
                summary = payloads(records)[0]["summary"]
                self.assertEqual(summary["courses_discovered"], 1)
                self.assertEqual(summary["pending_videos_discovered"], 2 if complete else 1)
                self.assertEqual(summary["catalog_complete"], complete)

    def test_http_diagnostics_never_include_credentials_headers_bodies_or_query(self):
        secrets = (
            "private-query", "private-authorization", "private-header-cookie",
            "private-request-body", "private-response-body", "private-study-id",
            CREDENTIALS["username"], CREDENTIALS["password"], CAPTCHA,
        )
        response = httpx.Response(200, json={
            "status": False, "studyId": "private-study-id", "need_code": 1,
            "offline": 0, "code": CAPTCHA, "errorCode": CREDENTIALS["password"],
            "message": "private-response-body " + CREDENTIALS["username"],
        })
        handler = Mock(return_value=response)
        with captured_trace() as records:
            with trace.task_trace(DOMAIN, TASK_ID) as diagnostic:
                with StudyClient(
                    "https://" + DOMAIN, transport=httpx.MockTransport(handler),
                ) as client:
                    actual = client.request(
                        "POST", "/private-request-body?token=private-query", retry=False,
                        headers={"Authorization": "private-authorization", "Cookie": "private-header-cookie"},
                        data={"username": CREDENTIALS["username"], "password": CREDENTIALS["password"], "code": CAPTCHA},
                    )
                    self.assertIs(actual, response)
                    self.assertFalse(json_object(actual)["status"])
                diagnostic.anomaly("dump_for_test", "http")
                diagnostic.outcome("task", "completed", "completed")
        serialized = json.dumps(payloads(records))
        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, serialized)
        self.assertIn("/[redacted]", serialized)

    def test_http_retries_redirects_and_json_failures_are_retained(self):
        seen = []
        responses = [
            httpx.ReadTimeout("private-timeout-message"),
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(503, headers={"Retry-After": "invalid"}),
            httpx.Response(200, json={"status": True}),
        ]

        def handler(request):
            seen.append(request.url.path)
            if request.url.path == "/":
                return httpx.Response(302, headers={"Location": "/user/?token=private-redirect-query"})
            if request.url.path == "/user/":
                return httpx.Response(200, text="private-response-body")
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        sleep = Mock()
        with captured_trace() as records:
            with trace.task_trace(DOMAIN, TASK_ID) as diagnostic:
                with StudyClient(
                    "https://" + DOMAIN, transport=httpx.MockTransport(handler), sleep=sleep,
                ) as client:
                    self.assertEqual(json_object(client.post_online()), {"status": True})
                    self.assertEqual(json_object(client.get_page()), {})
                diagnostic.anomaly("dump_for_test", "http")
                diagnostic.outcome("task", "completed", "completed")
        self.assertEqual(seen, ["/user/online"] * 4 + ["/", "/user/"])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 3, 4])
        log = payloads(records)[0]
        self.assertEqual(log["summary"]["http"]["retries"], 3)
        self.assertEqual(log["summary"]["http"]["redirects"], 1)
        http_events = [event for event in log["events"] if event["event"] == "http_end"]
        self.assertEqual([event["http_status"] for event in http_events], [None, 429, 503, 200, 200])
        self.assertEqual(http_events[0]["exception_type"], "ReadTimeout")
        self.assertEqual(http_events[0]["timeout_type"], "ReadTimeout")
        self.assertEqual(http_events[-1]["redirect_statuses"], [302])
        self.assertFalse(http_events[-1]["response"]["json_parsed"])
        self.assertEqual(log["summary"]["request_windows"][0]["attempts"], 5)
        self.assertEqual(log["summary"]["request_windows"][0]["http_429"], 1)
        self.assertEqual(log["summary"]["request_windows"][0]["http_5xx"], 1)
        self.assertNotIn("private-timeout-message", json.dumps(log))
        self.assertNotIn("private-redirect-query", json.dumps(log))

    def test_http_no_retry_retry_after_limit_and_exhaustion_have_distinct_outcomes(self):
        responses = [
            httpx.Response(429, json={"status": False}, headers={"Retry-After": "60"}),
            httpx.Response(429, headers={"Retry-After": "3600"}),
            *[httpx.Response(503) for _ in range(4)],
        ]
        handler = Mock(side_effect=responses)
        sleep = Mock()
        with captured_trace() as records:
            with trace.task_trace(DOMAIN, TASK_ID) as diagnostic:
                with StudyClient(
                    "https://" + DOMAIN, transport=httpx.MockTransport(handler), sleep=sleep,
                ) as client:
                    self.assertIs(client.post_study(StudyInfo(VIDEO.id, 0, 1)), responses[0])
                    self.assertIsNone(client.post_online())
                    self.assertIsNone(client.get_page())
                diagnostic.outcome("task", "completed", "completed")
        self.assertEqual(handler.call_count, 6)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])
        log = payloads(records)[0]
        ends = [event for event in log["events"] if event["event"] == "http_end"]
        self.assertEqual([event["disposition"] for event in ends], [
            "returned_without_retry", "retry_after_limit", "retry_scheduled",
            "retry_scheduled", "retry_scheduled", "retries_exhausted",
        ])
        limit = next(event for event in log["events"] if event["event"] == "http_retry_after_limit")
        self.assertEqual(limit["retry_after"], 3600)
        self.assertFalse(ends[0]["response"]["status"])

    def test_concurrent_login_and_heartbeat_retries_keep_their_call_identity(self):
        first_attempts = threading.Barrier(2)
        attempts = {"/user/login": 0, "/user/online": 0}
        errors = []
        results = []

        def handler(request):
            path = request.url.path
            attempts[path] += 1
            if attempts[path] == 1:
                first_attempts.wait(timeout=2)
                return httpx.Response(503 if path == "/user/login" else 429)
            return httpx.Response(200, json={"status": True})

        with captured_trace() as records:
            with trace.task_trace(DOMAIN, TASK_ID) as diagnostic:
                with StudyClient(
                    "https://" + DOMAIN, transport=httpx.MockTransport(handler), sleep=Mock(),
                ) as client:
                    def request(kind):
                        try:
                            with diagnostic.context(request_kind=kind):
                                result = client.post_online() if kind == "heartbeat" else client.post_login(
                                    CREDENTIALS["username"], CREDENTIALS["password"], CAPTCHA,
                                )
                                results.append(result.status_code)
                        except Exception as error:
                            errors.append(error)

                    workers = [threading.Thread(target=request, args=(kind,)) for kind in ("login", "heartbeat")]
                    for worker in workers:
                        worker.start()
                    for worker in workers:
                        worker.join(timeout=3)
                        self.assertFalse(worker.is_alive())
                diagnostic.outcome("task", "completed", "completed")
        self.assertEqual(errors, [])
        self.assertEqual(results, [200, 200])
        log = payloads(records)[0]
        starts = [event for event in log["events"] if event["event"] == "http_start"]
        self.assertEqual(len({event["request"] for event in starts}), 4)
        self.assertEqual(len({event["call_id"] for event in starts}), 2)
        for path in attempts:
            path_events = [event for event in starts if event["path"] == path]
            self.assertEqual([event["attempt"] for event in path_events], [1, 2])
            self.assertEqual(len({event["call_id"] for event in path_events}), 1)
            retry = next(event for event in log["events"] if event["event"] == "http_retry" and event["path"] == path)
            self.assertEqual(retry["call_id"], path_events[0]["call_id"])
            self.assertEqual(retry["request"], path_events[0]["request"])
            self.assertEqual(retry["attempt"], 1)
            self.assertEqual(retry["retry"], 1)
        self.assertEqual(log["summary"]["http"]["calls"], 2)

    def test_first_failure_and_last_study_response_survive_buffer_eviction(self):
        with captured_trace() as records:
            with trace.task_trace(DOMAIN, TASK_ID) as diagnostic:
                diagnostic.video_start(VIDEO.id, 100, 50, .5, 46)
                first = diagnostic.progress_start(StudyInfo(VIDEO.id, 0, 1))
                request = diagnostic.http_start("POST", "/user/node/study")
                rejected = httpx.Response(200, json={"status": False, "errorCode": 409})
                diagnostic.http_end(request, rejected)
                diagnostic.progress_end(first, rejected, decision="exit_business_rejected", confirmed=False)
                diagnostic.anomaly("business_rejected", "progress")
                # A later response and many events must not replace evidence at
                # the first anomaly or evict the final study response snapshot.
                second = diagnostic.progress_start(StudyInfo(VIDEO.id, 0, 31))
                request = diagnostic.http_start("POST", "/user/node/study")
                accepted = httpx.Response(200, json={"status": True, "studyId": 7})
                diagnostic.http_end(request, accepted)
                diagnostic.progress_end(second, accepted, decision="confirmed", confirmed=True)
                for index in range(400):
                    diagnostic.event("later_heartbeat_event", index=index)
                diagnostic.outcome("task", "completed", "completed")
        log = payloads(records)[0]
        self.assertFalse(any(event["event"] == "progress_end" for event in log["events"]))
        first = log["summary"]["first_anomaly"]
        self.assertFalse(first["last_study_request"]["response"]["status"])
        self.assertEqual(first["last_progress"]["decision"], "exit_business_rejected")
        self.assertTrue(log["summary"]["last_study_request"]["response"]["status"])
        self.assertEqual(log["summary"]["last_progress"]["decision"], "confirmed")
        self.assertEqual(log["summary"]["last_study_request"]["final_path"], "/user/node/study")

    def test_bare_mock_response_keeps_http_and_redirect_diagnostics(self):
        with captured_trace() as records:
            with trace.task_trace(DOMAIN, TASK_ID) as diagnostic:
                request = diagnostic.http_start("GET", "/user/")
                diagnostic.http_end(request, httpx.Response(
                    200, json={"status": False}, history=[httpx.Response(302)],
                ))
                diagnostic.anomaly("dump_for_test", "http")
                diagnostic.outcome("task", "completed", "completed")
        event = next(event for event in payloads(records)[0]["events"] if event["event"] == "http_end")
        self.assertEqual(event["http_status"], 200)
        self.assertEqual(event["final_path"], "/user/")
        self.assertEqual(event["redirect_paths"], ["/[redacted]"])
        self.assertEqual(event["redirect_statuses"], [302])

    def test_recent_ocr_secret_storage_is_bounded_and_credentials_remain_protected(self):
        with captured_trace():
            with trace.task_trace(DOMAIN, TASK_ID) as diagnostic:
                diagnostic.protect(*CREDENTIALS.values())
                for index in range(100):
                    diagnostic.protect(f"private-ocr-{index}")
                self.assertEqual(diagnostic.secrets.maxlen, 16)
                self.assertEqual(len(diagnostic.secrets), 16)
                self.assertEqual(len(diagnostic.credential_secrets), 2)
                registry = repr((diagnostic.credential_secrets, diagnostic.secrets))
                for secret in (*CREDENTIALS.values(), "private-ocr-0", "private-ocr-99"):
                    self.assertNotIn(secret, registry)
                self.assertEqual(diagnostic.response({"errorCode": CREDENTIALS["password"]})["errorCode"], "[redacted]")
                self.assertEqual(diagnostic.response({"errorCode": "private-ocr-99"})["errorCode"], "[redacted]")
                diagnostic.outcome("task", "completed", "completed")

    def test_numeric_ocr_echo_in_business_fields_is_masked(self):
        for recognized, echoed in (("628491", 628491), ("001234", 1234)):
            with self.subTest(recognized=recognized):
                log = self.only_log(self.run_task(
                    lambda request: httpx.Response(200, json={"status": False, "code": echoed, "errorCode": recognized}),
                    ocr_result=recognized,
                ))
                response = log["summary"]["last_study_request"]["response"]
                self.assertEqual(response["code"], "[redacted]")
                self.assertEqual(response["errorCode"], "[redacted]")
                self.assertNotIn(json.dumps(recognized), json.dumps(log))

    def test_video_prepare_and_progress_parse_exceptions_keep_the_precise_exit_stage(self):
        invalid = Video("Invalid", VIDEO.id, COURSE, float("nan"), 0, 0)
        run = self.run_task(lambda request: self.fail("No study request expected"), videos=(invalid,))
        self.assertIsInstance(run["error"], ValueError)
        summary = run["logs"][0]["summary"]
        self.assertEqual(summary["final_stage"], "video_prepare")
        self.assertEqual(summary["reason"], "finish_time_exception")
        self.assertEqual(summary["counts"]["video_end"], 1)
        with patch.object(CourseBot, "_progress_data", side_effect=TypeError("private parse error")):
            run = self.run_task(lambda request: httpx.Response(200, json={"status": True, "studyId": 7}))
        self.assertIsInstance(run["error"], TypeError)
        summary = run["logs"][0]["summary"]
        self.assertEqual(summary["final_stage"], "progress")
        self.assertEqual(summary["reason"], "progress_parse_exception")
        self.assertEqual(summary["last_progress"]["decision"], "exception")
        self.assertEqual(summary["last_study_request"]["response"]["status"], True)


if __name__ == "__main__":
    unittest.main()
