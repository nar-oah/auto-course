import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from auto_course_contracts import Credentials
import httpx

from auto_course_study.bot import CourseBot
from auto_course_study.http import StudyClient
from auto_course_study.models import Config, Course, StudyInfo, Video


CREDENTIALS = Credentials(username="student", password="password")
VIDEO = Video("Video", "video1", Course("1", "Course"), 100, 50, .5)
LONG_VIDEO = Video("Long video", "video1", VIDEO.course, 600, 0, 0)


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class BotTests(unittest.TestCase):
    def make_bot(
        self, handler: object, *, config: Config = Config(), clock: VirtualClock | None = None
    ) -> tuple[CourseBot, StudyClient]:
        client = StudyClient("https://courses.example", transport=httpx.MockTransport(handler), sleep=Mock())
        self.addCleanup(client.client.close)
        bot = CourseBot(
            client, CREDENTIALS, config=config,
            ocr=Mock(classification=Mock(return_value="abcd")),
            sleep=Mock(side_effect=clock.sleep if clock else None),
        )
        return bot, client

    def test_original_remaining_time_algorithm_and_study_id(self) -> None:
        payloads = []
        clock = VirtualClock()
        submitted_at = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(parse_qs(request.content.decode()))
            submitted_at.append(clock.now)
            return httpx.Response(200, json={"status": True, "studyId": 9})

        bot, _ = self.make_bot(handler, clock=clock)
        with patch.object(bot, "_send_online_pings"):
            self.assertTrue(bot.update_study_progress(VIDEO))
        self.assertEqual([payload["studyTime"] for payload in payloads], [["1"], ["31"], ["46"]])
        self.assertEqual([payload["studyId"] for payload in payloads], [["0"], ["9"], ["9"]])
        self.assertEqual([call.args[0] for call in bot.sleep.call_args_list], [0, 30, 15])
        self.assertEqual(submitted_at, [0, 30, 45])

    def test_completed_video_needs_no_progress_request_or_heartbeat(self) -> None:
        requests = []
        bot, _ = self.make_bot(lambda request: requests.append(request) or httpx.Response(200))
        complete = Video("Complete", "complete", VIDEO.course, 100, 100, 1)
        with patch.object(bot, "_send_online_pings") as heartbeat:
            self.assertTrue(bot.update_study_progress(complete))
        self.assertEqual(requests, [])
        heartbeat.assert_not_called()

    def test_captcha_resubmission_uses_returned_study_id(self) -> None:
        payloads = []
        clock = VirtualClock()
        submitted_at = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/service/code/aa":
                return httpx.Response(200, content=b"captcha")
            payload = parse_qs(request.content.decode())
            payloads.append(payload)
            submitted_at.append(clock.now)
            if len(payloads) == 1:
                return httpx.Response(200, json={"need_code": 1})
            return httpx.Response(200, json={"status": True, "studyId": "8"})

        bot, _ = self.make_bot(handler, clock=clock)
        with patch.object(bot, "_send_online_pings"):
            self.assertTrue(bot.update_study_progress(VIDEO))
        self.assertEqual(payloads[1]["code"], ["abcd"])
        self.assertEqual(payloads[1]["studyTime"], ["1"])
        self.assertEqual(payloads[2]["studyId"], ["8"])
        self.assertEqual([payload["studyTime"] for payload in payloads], [["1"], ["1"], ["31"], ["46"]])
        self.assertEqual(submitted_at, [0, 0, 30, 45])

    def test_relogin_waits_for_next_progress_and_preserves_cookie_and_study_id(self) -> None:
        payloads = []
        clock = VirtualClock()
        submitted_at = []
        login_at = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/service/code":
                return httpx.Response(200, content=b"captcha")
            if request.url.path == "/user/login":
                login_at.append(clock.now)
                return httpx.Response(200, json={"status": True}, headers={"Set-Cookie": "session=renewed; Path=/"})
            payloads.append(parse_qs(request.content.decode()))
            submitted_at.append(clock.now)
            if len(payloads) == 2:
                return httpx.Response(200, json={"offline": 1})
            if len(payloads) == 3:
                self.assertEqual(request.headers["Cookie"], "session=renewed")
            return httpx.Response(200, json={"status": True, "studyId": 7})

        bot, _ = self.make_bot(handler, clock=clock)
        with patch.object(bot, "_send_online_pings"):
            self.assertTrue(bot.update_study_progress(VIDEO))
        self.assertEqual(login_at, [30])
        self.assertEqual(submitted_at, [0, 30, 60])
        self.assertEqual([payload["studyId"] for payload in payloads], [["0"], ["7"], ["7"]])
        self.assertEqual([payload["studyTime"] for payload in payloads], [["1"], ["31"], ["46"]])

    def test_failed_midway_submission_preserves_the_scheduled_wait(self) -> None:
        clock = VirtualClock()
        submissions = []
        responses = iter([{"status": True, "studyId": 7}, {}, {"status": True, "studyId": 7}])

        def handler(request: httpx.Request) -> httpx.Response:
            submissions.append((clock.now, parse_qs(request.content.decode())))
            return httpx.Response(200, json=next(responses))

        bot, _ = self.make_bot(handler, clock=clock)
        with patch.object(bot, "_send_online_pings"):
            self.assertTrue(bot.update_study_progress(VIDEO))
        self.assertEqual([when for when, _ in submissions], [0, 30, 60])
        self.assertEqual([payload["studyTime"] for _, payload in submissions], [["1"], ["31"], ["46"]])
        self.assertEqual([payload["studyId"] for _, payload in submissions], [["0"], ["7"], ["7"]])
        self.assertEqual([call.args[0] for call in bot.sleep.call_args_list], [0, 30, 30])

    def test_repeated_offline_challenges_stop_after_five_failed_submissions(self) -> None:
        submitted_at = []
        clock = VirtualClock()

        def handler(request: httpx.Request) -> httpx.Response:
            submitted_at.append(clock.now)
            return httpx.Response(200, json={"offline": 1})

        bot, _ = self.make_bot(handler, clock=clock)
        with patch.object(bot, "login", return_value=True) as login, patch.object(bot, "_send_online_pings"):
            self.assertFalse(bot.update_study_progress(LONG_VIDEO))
        self.assertEqual(login.call_count, 5)
        self.assertEqual(submitted_at, [0, 30, 60, 90, 120])

    def test_uncertain_submissions_wait_and_advance_before_recovery(self) -> None:
        outcomes = [
            (200, {}), (200, None), (200, {"status": True}),
            (200, {"status": True, "studyId": -1}),
            (200, {"status": True, "studyId": True}),
            (200, {"status": True, "studyId": 1.5}),
            (200, {"status": True, "studyId": 1.0}),
            (200, {"status": True, "studyId": None}),
            (200, {"status": True, "studyId": []}),
            (200, {"status": True, "studyId": {"value": 1}}),
            (200, {"status": True, "studyId": "invalid"}),
            (200, {"status": True, "studyId": "-1"}),
            (200, {"status": True, "studyId": "1.0"}),
            (200, {"status": True, "studyId": ""}),
            (503, {"status": True, "studyId": 99}),
            (None, None),
        ]
        for status, body in outcomes:
            with self.subTest(status=status, body=body):
                clock = VirtualClock()
                submissions = []

                def handler(request: httpx.Request) -> httpx.Response:
                    submissions.append((clock.now, parse_qs(request.content.decode())))
                    if len(submissions) == 1:
                        if status is None:
                            raise httpx.ConnectError("unavailable", request=request)
                        if body is None:
                            return httpx.Response(status)
                        return httpx.Response(status, json=body)
                    return httpx.Response(200, json={"status": True, "studyId": 5})

                bot, _ = self.make_bot(handler, clock=clock)
                with patch.object(bot, "_send_online_pings"):
                    self.assertTrue(bot.update_study_progress(VIDEO))
                self.assertEqual([when for when, _ in submissions], [0, 30, 45])
                self.assertEqual([payload["studyTime"] for _, payload in submissions], [["1"], ["31"], ["46"]])
                self.assertEqual([payload["studyId"] for _, payload in submissions], [["0"], ["0"], ["5"]])

    def test_nonnegative_study_ids_do_not_consume_the_failure_budget(self) -> None:
        for study_id in (0, "0", 7, "007"):
            with self.subTest(study_id=study_id):
                clock = VirtualClock()
                submissions = []

                def handler(request: httpx.Request) -> httpx.Response:
                    submissions.append((clock.now, parse_qs(request.content.decode())))
                    return httpx.Response(200, json={"status": True, "studyId": study_id})

                bot, _ = self.make_bot(handler, config=Config(max_failures=1), clock=clock)
                with patch.object(bot, "_send_online_pings"):
                    self.assertTrue(bot.update_study_progress(VIDEO))
                self.assertEqual([when for when, _ in submissions], [0, 30, 45])
                self.assertEqual([payload["studyTime"] for _, payload in submissions], [["1"], ["31"], ["46"]])
                self.assertEqual(
                    [payload["studyId"] for _, payload in submissions],
                    [["0"], [str(int(study_id))], [str(int(study_id))]],
                )

    def test_successful_zero_study_id_replaces_the_previous_id(self) -> None:
        submissions = []
        responses = iter([
            {"status": True, "studyId": 7},
            {"status": True, "studyId": "0"},
            {"status": True, "studyId": 0},
        ])

        def handler(request: httpx.Request) -> httpx.Response:
            submissions.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json=next(responses))

        bot, _ = self.make_bot(handler, config=Config(max_failures=1))
        with patch.object(bot, "_send_online_pings"):
            self.assertTrue(bot.update_study_progress(VIDEO))
        self.assertEqual([payload["studyId"] for payload in submissions], [["0"], ["7"], ["0"]])

    def test_failure_budget_accumulates_across_successes_and_retains_study_id(self) -> None:
        clock = VirtualClock()
        submissions = []
        responses = iter([{}, {"status": True, "studyId": 5}, {}, {"status": True, "studyId": 5}, {}])

        def handler(request: httpx.Request) -> httpx.Response:
            submissions.append((clock.now, parse_qs(request.content.decode())))
            return httpx.Response(200, json=next(responses))

        bot, _ = self.make_bot(handler, config=Config(max_failures=3), clock=clock)
        with patch.object(bot, "_send_online_pings"):
            self.assertFalse(bot.update_study_progress(LONG_VIDEO))
        self.assertEqual([when for when, _ in submissions], [0, 30, 60, 90, 120])
        self.assertEqual([payload["studyTime"] for _, payload in submissions], [["1"], ["31"], ["61"], ["91"], ["121"]])
        self.assertEqual([payload["studyId"] for _, payload in submissions], [["0"], ["0"], ["5"], ["5"], ["5"]])

    def test_failed_final_submission_is_not_repeated(self) -> None:
        for final in ({}, {"status": True}, {"offline": 1}):
            with self.subTest(final=final):
                clock = VirtualClock()
                submissions = []

                def handler(request: httpx.Request) -> httpx.Response:
                    submissions.append((clock.now, parse_qs(request.content.decode())))
                    response = final if len(submissions) == 3 else {"status": True, "studyId": 5}
                    return httpx.Response(200, json=response)

                bot, _ = self.make_bot(handler, clock=clock)
                with patch.object(bot, "login", return_value=True), patch.object(bot, "_send_online_pings"):
                    self.assertFalse(bot.update_study_progress(VIDEO))
                self.assertEqual([when for when, _ in submissions], [0, 30, 45])
                self.assertEqual([payload["studyTime"] for _, payload in submissions], [["1"], ["31"], ["46"]])

    def test_failed_recovery_caps_final_progress_and_stops_before_next_video(self) -> None:
        for midway in ({}, {"offline": 1}, {"need_code": 1}):
            with self.subTest(midway=midway):
                clock = VirtualClock()
                submissions = []

                def handler(request: httpx.Request) -> httpx.Response:
                    if request.url.path == "/service/code/aa":
                        return httpx.Response(200, content=b"captcha")
                    payload = parse_qs(request.content.decode())
                    submissions.append((clock.now, payload))
                    if payload["studyTime"] == ["1"]:
                        body = {"status": True, "studyId": 7}
                    elif payload["studyTime"] == ["31"]:
                        body = midway
                    else:
                        body = {}
                    return httpx.Response(200, json=body)

                bot, _ = self.make_bot(handler, config=Config(max_failures=3), clock=clock)
                with (
                    patch.object(bot, "login", return_value=True),
                    patch.object(bot, "_send_online_pings"),
                    patch.object(bot.video_list, "get_videos", return_value=iter([
                        VIDEO, Video("Next", "video2", VIDEO.course, 100, 50, .5),
                    ])),
                ):
                    bot.run()
                expected_at = [0, 30, 30, 60] if midway == {"need_code": 1} else [0, 30, 60]
                expected_times = [["1"], ["31"], ["31"], ["46"]] if midway == {"need_code": 1} else [["1"], ["31"], ["46"]]
                self.assertEqual([when for when, _ in submissions], expected_at)
                self.assertEqual([payload["studyTime"] for _, payload in submissions], expected_times)
                self.assertTrue(all(payload["nodeId"] == [VIDEO.id] for _, payload in submissions))
                self.assertEqual([payload["studyTime"] for _, payload in submissions].count(["46"]), 1)

    def test_repeated_captcha_challenge_allows_only_one_resubmission_each_interval(self) -> None:
        clock = VirtualClock()
        submissions = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/service/code/aa":
                return httpx.Response(200, content=b"captcha")
            submissions.append((clock.now, parse_qs(request.content.decode())))
            return httpx.Response(200, json={"need_code": 1})

        bot, _ = self.make_bot(handler, config=Config(max_failures=2), clock=clock)
        with patch.object(bot, "_send_online_pings"):
            self.assertFalse(bot.update_study_progress(LONG_VIDEO))
        self.assertEqual([when for when, _ in submissions], [0, 0, 30, 30])
        self.assertEqual([payload["studyTime"] for _, payload in submissions], [["1"], ["1"], ["31"], ["31"]])
        self.assertEqual([payload.get("code") for _, payload in submissions], [None, ["abcd"], None, ["abcd"]])

    def test_captcha_resubmission_offline_logs_in_without_third_progress_request(self) -> None:
        submissions = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/service/code/aa":
                return httpx.Response(200, content=b"captcha")
            submissions.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"need_code": 1} if len(submissions) == 1 else {"offline": 1})

        bot, _ = self.make_bot(handler)
        with patch.object(bot, "login", return_value=True) as login:
            self.assertIsNone(bot._submit_progress(StudyInfo("v", 7, 31)))
        login.assert_called_once_with()
        self.assertEqual(len(submissions), 2)
        self.assertEqual([payload["studyId"] for payload in submissions], [["7"], ["7"]])

    def test_unconfirmed_captcha_resubmission_keeps_study_id_and_waits(self) -> None:
        clock = VirtualClock()
        submissions = []
        responses = iter([
            {"status": True, "studyId": 7}, {"need_code": 1},
            {"status": True, "studyId": -1}, {"status": True, "studyId": 9},
        ])

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/service/code/aa":
                return httpx.Response(200, content=b"captcha")
            submissions.append((clock.now, parse_qs(request.content.decode())))
            return httpx.Response(200, json=next(responses))

        bot, _ = self.make_bot(handler, clock=clock)
        with patch.object(bot, "_send_online_pings"):
            self.assertTrue(bot.update_study_progress(VIDEO))
        self.assertEqual([when for when, _ in submissions], [0, 30, 30, 60])
        self.assertEqual([payload["studyTime"] for _, payload in submissions], [["1"], ["31"], ["31"], ["46"]])
        self.assertEqual([payload["studyId"] for _, payload in submissions], [["0"], ["7"], ["7"], ["7"]])

    def test_final_captcha_submission_is_not_repeated(self) -> None:
        for final, successful in (
            ({"status": True, "studyId": "8"}, True),
            ({"status": True, "studyId": 0}, True),
            ({"status": True, "studyId": "0"}, True),
            ({"status": True}, False), ({"status": False}, False),
        ):
            with self.subTest(final=final):
                clock = VirtualClock()
                submissions = []
                responses = iter([
                    {"status": True, "studyId": 7}, {"status": True, "studyId": 7},
                    {"need_code": 1}, final,
                ])

                def handler(request: httpx.Request) -> httpx.Response:
                    if request.url.path == "/service/code/aa":
                        return httpx.Response(200, content=b"captcha")
                    submissions.append((clock.now, parse_qs(request.content.decode())))
                    return httpx.Response(200, json=next(responses))

                bot, _ = self.make_bot(handler, clock=clock)
                with patch.object(bot, "_send_online_pings"):
                    self.assertIs(bot.update_study_progress(VIDEO), successful)
                self.assertEqual([when for when, _ in submissions], [0, 30, 45, 45])
                self.assertEqual([payload["studyTime"] for _, payload in submissions], [["1"], ["31"], ["46"], ["46"]])
                self.assertEqual(submissions[-1][1]["code"], ["abcd"])

    def test_unreadable_captcha_terminates_without_resubmission(self) -> None:
        study_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/service/code/aa":
                return httpx.Response(200, content=b"captcha")
            study_requests.append(request)
            return httpx.Response(200, json={"need_code": 1})

        bot, _ = self.make_bot(handler)
        bot.ocr.classification.return_value = ""
        with patch.object(bot, "_send_online_pings"):
            self.assertFalse(bot.update_study_progress(VIDEO))
        self.assertEqual(len(study_requests), 1)

    def test_fatal_progress_failure_stops_domain_without_consuming_next_video(self) -> None:
        for status, body in ((200, {"status": False}), (200, {"offline": 1}), (403, {"status": True, "studyId": 9}), (429, {})):
            with self.subTest(status=status, body=body):
                submissions = []
                consumed = []

                def handler(request: httpx.Request) -> httpx.Response:
                    submissions.append(request)
                    return httpx.Response(status, json=body, headers={"Retry-After": "120"} if status == 429 else {})

                def videos():
                    consumed.append(VIDEO.id)
                    yield VIDEO
                    consumed.append("video2")
                    yield Video("Next", "video2", VIDEO.course, 100, 50, .5)

                bot, _ = self.make_bot(handler)
                with patch.object(bot, "login", side_effect=[True, False]), patch.object(bot, "_send_online_pings"), patch.object(bot.video_list, "get_videos", return_value=videos()):
                    bot.run()
                self.assertEqual(len(submissions), 1)
                self.assertEqual(consumed, [VIDEO.id])

    def test_uncertain_progress_budget_stops_domain_before_next_video(self) -> None:
        clock = VirtualClock()
        submitted_at = []
        consumed = []

        def handler(request: httpx.Request) -> httpx.Response:
            submitted_at.append(clock.now)
            return httpx.Response(200, json={})

        def videos():
            consumed.append(LONG_VIDEO.id)
            yield LONG_VIDEO
            consumed.append("video2")
            yield Video("Next", "video2", VIDEO.course, 100, 50, .5)

        bot, _ = self.make_bot(handler, config=Config(max_failures=2), clock=clock)
        with patch.object(bot, "login", return_value=True), patch.object(bot, "_send_online_pings"), patch.object(bot.video_list, "get_videos", return_value=videos()):
            bot.run()
        self.assertEqual(submitted_at, [0, 30])
        self.assertEqual(consumed, [LONG_VIDEO.id])

    def test_login_accepts_boolean_and_integer_success_without_arbitrary_truthy_data(self) -> None:
        for status, value, expected in (
            (200, True, True), (200, 1, True),
            (200, False, False), (200, 0, False), (200, -1, False), (200, 2, False),
            (200, 1.0, False), (200, "true", False), (200, "1", False),
            (200, [1], False), (200, {"success": True}, False), (200, None, False),
            (403, True, False), (403, 1, False),
        ):
            with self.subTest(status=status, value=value):
                bot, _ = self.make_bot(lambda request: httpx.Response(status, json={"status": value}))
                with patch.object(bot, "recognize_captcha", return_value="abcd"):
                    self.assertIs(bot.login(), expected)

    def test_nonboolean_progress_status_does_not_confirm_a_study_id(self) -> None:
        for status in (1, "true", "1", [True], {"success": True}):
            with self.subTest(status=status):
                bot, _ = self.make_bot(lambda request: httpx.Response(200, json={"status": status, "studyId": 7}))
                self.assertIsNone(bot._submit_progress(StudyInfo(VIDEO.id, 0, 1)))

    def test_online_ping_is_immediate_then_every_120_seconds(self) -> None:
        client = Mock()
        bot = CourseBot(client, CREDENTIALS)
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        bot._send_online_pings(stop)
        self.assertEqual(client.post_online.call_count, 2)
        self.assertEqual([call.kwargs for call in client.post_online.call_args_list], [{"stop": stop}, {"stop": stop}])
        self.assertEqual([call.args for call in stop.wait.call_args_list], [(120,), (120,)])

    def test_heartbeat_stops_and_joins_on_progress_exception(self) -> None:
        bot, _ = self.make_bot(lambda request: httpx.Response(200))
        heartbeats = []
        started = threading.Event()

        def heartbeat(stop: threading.Event) -> None:
            heartbeats.append((stop, threading.current_thread()))
            started.set()
            stop.wait()

        def fail(info: StudyInfo) -> None:
            self.assertTrue(started.wait(timeout=1))
            raise ValueError("invalid progress")

        with patch.object(bot, "_send_online_pings", side_effect=heartbeat), patch.object(bot, "_submit_progress", side_effect=fail):
            with self.assertRaisesRegex(ValueError, "invalid progress"):
                bot.update_study_progress(VIDEO)
        stop, thread = heartbeats[0]
        self.assertTrue(stop.is_set())
        self.assertFalse(thread.is_alive())

    def test_heartbeat_stops_and_joins_on_terminal_progress_results(self) -> None:
        for failure in ("rejected", "offline", "budget", "captcha", "final"):
            with self.subTest(failure=failure):
                heartbeats = []
                started = threading.Event()
                study_calls = 0

                def heartbeat(stop: threading.Event) -> None:
                    heartbeats.append((stop, threading.current_thread()))
                    started.set()
                    stop.wait()

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal study_calls
                    if request.url.path == "/service/code/aa":
                        return httpx.Response(200, content=b"captcha")
                    study_calls += 1
                    if failure == "rejected":
                        body = {"status": False}
                    elif failure == "offline":
                        body = {"offline": 1}
                    elif failure == "captcha":
                        body = {"need_code": 1}
                    elif failure == "final" and study_calls < 3:
                        body = {"status": True, "studyId": 7}
                    else:
                        body = {}
                    return httpx.Response(200, json=body)

                bot, _ = self.make_bot(handler, config=Config(max_failures=2))
                bot.ocr.classification.return_value = ""
                bot.sleep.side_effect = lambda seconds: self.assertTrue(started.wait(timeout=1))
                with patch.object(bot, "_send_online_pings", side_effect=heartbeat), patch.object(bot, "login", return_value=False):
                    self.assertFalse(bot.update_study_progress(VIDEO))
                stop, thread = heartbeats[0]
                self.assertTrue(stop.is_set())
                self.assertFalse(thread.is_alive())

    def test_each_video_owns_its_heartbeat_event(self) -> None:
        bot, _ = self.make_bot(lambda request: httpx.Response(200, json={"status": True, "studyId": 1}))
        events = []

        def heartbeat(stop: threading.Event) -> None:
            events.append(stop)
            stop.wait()

        with patch.object(bot, "_send_online_pings", side_effect=heartbeat):
            bot.update_study_progress(VIDEO)
            bot.update_study_progress(VIDEO)
        self.assertEqual(len(events), 2)
        self.assertIsNot(events[0], events[1])
        self.assertTrue(all(event.is_set() for event in events))

    def test_failed_login_does_not_fetch_courses(self) -> None:
        bot, _ = self.make_bot(lambda request: httpx.Response(200, json={"status": False}))
        with patch.object(bot.video_list, "get_videos") as videos:
            bot.run()
        videos.assert_not_called()

    def test_full_workflow_uses_login_session_for_catalog_heartbeat_and_study(self) -> None:
        seen = []
        online_started = threading.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            seen.append(path)
            if path == "/service/code":
                return httpx.Response(200, content=b"captcha", headers={"Set-Cookie": "captcha=initial; Path=/"})
            if path == "/user/login":
                self.assertIn("captcha=initial", request.headers["Cookie"])
                data = parse_qs(request.content.decode())
                self.assertEqual(data["username"], ["student"])
                self.assertEqual(data["password"], ["password"])
                return httpx.Response(200, json={"status": True}, headers={"Set-Cookie": "session=logged-in; Path=/"})
            self.assertIn("session=logged-in", request.headers["Cookie"])
            if path == "/user/":
                return httpx.Response(200, text='<div class="total">1 / 1</div>')
            if path == "/user/index":
                return httpx.Response(200, text='<div class="name"><a href="?courseId=1">Course</a></div>')
            if path == "/user/study_record.json":
                return httpx.Response(200, json={"list": [{"id": "video1", "progress": .5, "duration": 50, "videoDuration": "00:01:40"}]})
            if path == "/user/online":
                online_started.set()
                return httpx.Response(200, json={"status": True})
            if path == "/user/node/study":
                return httpx.Response(200, json={"status": True, "studyId": 7})
            self.fail(f"Unexpected request: {path}")

        bot, _ = self.make_bot(handler)
        bot.sleep = lambda seconds: self.assertTrue(online_started.wait(timeout=1))
        bot.run()
        self.assertIn("/user/online", seen)
        self.assertEqual(seen.count("/user/node/study"), 3)
        self.assertEqual(seen.count("/service/code"), 1)


if __name__ == "__main__":
    unittest.main()
