import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from auto_course_contracts import Credentials
import httpx

from auto_course_study.bot import CourseBot
from auto_course_study.http import StudyClient
from auto_course_study.models import Course, StudyInfo, Video


CREDENTIALS = Credentials(username="student", password="password")
VIDEO = Video("Video", "video1", Course("1", "Course"), 100, 50, .5)


class BotTests(unittest.TestCase):
    def make_bot(self, handler: object) -> tuple[CourseBot, StudyClient]:
        client = StudyClient("https://courses.example", transport=httpx.MockTransport(handler))
        self.addCleanup(client.client.close)
        bot = CourseBot(client, CREDENTIALS, ocr=Mock(classification=Mock(return_value="abcd")), sleep=Mock())
        return bot, client

    def test_original_remaining_time_algorithm_and_study_id(self) -> None:
        payloads = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"status": True, "studyId": 9})

        bot, _ = self.make_bot(handler)
        with patch.object(bot, "_send_online_pings"):
            bot.update_study_progress(VIDEO)
        self.assertEqual([payload["studyTime"] for payload in payloads], [["1"], ["31"], ["46"]])
        self.assertEqual([payload["studyId"] for payload in payloads], [["0"], ["9"], ["9"]])
        self.assertEqual([call.args[0] for call in bot.sleep.call_args_list], [0, 30, 15])

    def test_captcha_resubmission_uses_returned_study_id(self) -> None:
        payloads = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/service/code/aa":
                return httpx.Response(200, content=b"captcha")
            payload = parse_qs(request.content.decode())
            payloads.append(payload)
            if len(payloads) == 1:
                return httpx.Response(200, json={"need_code": 1})
            return httpx.Response(200, json={"status": True, "studyId": 8})

        bot, _ = self.make_bot(handler)
        with patch.object(bot, "_send_online_pings"):
            bot.update_study_progress(VIDEO)
        self.assertEqual(payloads[1]["code"], ["abcd"])
        self.assertEqual(payloads[1]["studyTime"], ["1"])
        self.assertEqual(payloads[2]["studyId"], ["8"])

    def test_relogin_preserves_domain_cookie_for_progress(self) -> None:
        study_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal study_calls
            if request.url.path == "/service/code":
                return httpx.Response(200, content=b"captcha")
            if request.url.path == "/user/login":
                return httpx.Response(200, json={"status": True}, headers={"Set-Cookie": "session=renewed; Path=/"})
            study_calls += 1
            if study_calls == 1:
                return httpx.Response(200, json={"offline": 1})
            self.assertEqual(request.headers["Cookie"], "session=renewed")
            return httpx.Response(200, json={"status": True, "studyId": 7})

        bot, _ = self.make_bot(handler)
        self.assertEqual(bot._submit_progress(StudyInfo("v", 0, 1)), 7)
        self.assertEqual(study_calls, 2)

    def test_repeated_offline_challenges_stop_after_five_failed_submissions(self) -> None:
        bot, _ = self.make_bot(lambda request: httpx.Response(200, json={"offline": 1}))
        with patch.object(bot, "login", return_value=True) as login, patch.object(bot, "_send_online_pings"):
            bot.update_study_progress(VIDEO)
        self.assertEqual(login.call_count, 5)
        self.assertEqual(bot.sleep.call_count, 5)
        self.assertTrue(all(call.args == (0,) for call in bot.sleep.call_args_list))

    def test_failed_final_submission_retries_same_time_and_failure_budget_is_cumulative(self) -> None:
        bot, _ = self.make_bot(lambda request: httpx.Response(200))
        outcomes = [None, 5, None, 5, None, 5]
        with patch.object(bot, "_send_online_pings"), patch.object(bot, "_submit_progress", side_effect=outcomes) as submit:
            bot.update_study_progress(VIDEO)
        self.assertEqual([call.args[0].time for call in submit.call_args_list], [1, 1, 31, 31, 46, 46])

        bot.sleep.reset_mock()
        with patch.object(bot, "_send_online_pings"), patch.object(bot, "_submit_progress", side_effect=[None, None, 5, None, None, 5, None]) as submit:
            bot.update_study_progress(VIDEO)
        self.assertEqual(submit.call_count, 7)
        self.assertEqual(submit.call_args.args[0].time, 46)

    def test_online_ping_is_immediate_then_every_120_seconds(self) -> None:
        client = Mock()
        bot = CourseBot(client, CREDENTIALS)
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        bot._send_online_pings(stop)
        self.assertEqual(client.post_online.call_count, 2)
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
