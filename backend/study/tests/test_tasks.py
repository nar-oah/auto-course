import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from auto_course_contracts import Credentials
import httpx
from pydantic import ValidationError

from auto_course_study import tasks
from auto_course_study.bot import CourseBot
from auto_course_study.http import StudyClient
from auto_course_study.models import Course, Video


CREDENTIALS = {"username": "student", "password": "password"}
VIDEO = Video("Video", "video1", Course("1", "Course"), 100, 50, .5)
NEXT_VIDEO = Video("Next", "video2", VIDEO.course, 100, 50, .5)


class TaskTests(unittest.TestCase):
    def test_start_fans_out_to_domain_queue_without_exposing_credentials(self) -> None:
        with patch.object(tasks, "discover_domains", return_value=["one.example", "two.example"]), patch.object(tasks.run_domain, "apply_async") as enqueue:
            self.assertIsNone(tasks.start.run(CREDENTIALS))
        self.assertEqual(enqueue.call_count, 2)
        for call, domain in zip(enqueue.call_args_list, ["one.example", "two.example"]):
            self.assertEqual(call.kwargs["args"], [domain, CREDENTIALS])
            self.assertEqual(call.kwargs["queue"], "study")
            self.assertNotIn("password", call.kwargs["argsrepr"])
            self.assertNotIn("student", call.kwargs["argsrepr"])

    def test_domain_worker_keeps_one_context_managed_client(self) -> None:
        client = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=False)
        with patch.object(tasks, "StudyClient", return_value=context) as constructor, patch.object(tasks, "CourseBot") as bot:
            tasks.run_domain.run("one.example", CREDENTIALS)
        constructor.assert_called_once_with("https://one.example")
        self.assertIs(bot.call_args.args[0], client)
        self.assertEqual(bot.call_args.args[1].model_dump(), CREDENTIALS)
        bot.return_value.run.assert_called_once_with()
        context.__exit__.assert_called_once_with(None, None, None)

    def make_domain_bot(self, handler: object) -> tuple[CourseBot, StudyClient]:
        client = StudyClient("https://one.example", transport=httpx.MockTransport(handler))
        self.addCleanup(client.client.close)
        bot = CourseBot(
            client,
            Credentials.model_validate(CREDENTIALS),
            ocr=Mock(classification=Mock(return_value="abcd")),
            sleep=Mock(),
        )
        return bot, client

    def test_failed_initial_login_closes_domain_session(self) -> None:
        paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/service/code":
                return httpx.Response(200, content=b"captcha")
            return httpx.Response(200, json={"status": False})

        bot, client = self.make_domain_bot(handler)
        with (
            patch.object(tasks, "StudyClient", return_value=client),
            patch.object(tasks, "CourseBot", return_value=bot),
            patch.object(bot.video_list, "get_videos") as get_videos,
        ):
            tasks.run_domain.run("one.example", CREDENTIALS)
        self.assertEqual(paths, ["/service/code", "/user/login"])
        get_videos.assert_not_called()
        self.assertTrue(client.client.is_closed)

    def test_rejection_and_failed_final_progress_stop_domain_and_close_session(self) -> None:
        for fail_at, failure in ((1, {"status": False}), (3, {"status": False}), (3, {})):
            with self.subTest(fail_at=fail_at, failure=failure):
                payloads = []

                def handler(request: httpx.Request) -> httpx.Response:
                    self.assertEqual(request.url.path, "/user/node/study")
                    payloads.append(parse_qs(request.content.decode()))
                    if len(payloads) == fail_at:
                        return httpx.Response(200, json=failure)
                    return httpx.Response(200, json={"status": True, "studyId": 7})

                bot, client = self.make_domain_bot(handler)
                with (
                    patch.object(tasks, "StudyClient", return_value=client),
                    patch.object(tasks, "CourseBot", return_value=bot),
                    patch.object(bot, "login", return_value=True),
                    patch.object(bot, "_send_online_pings"),
                    patch.object(
                        bot.video_list, "get_videos", return_value=iter([VIDEO, NEXT_VIDEO])
                    ),
                ):
                    tasks.run_domain.run("one.example", CREDENTIALS)
                self.assertEqual(len(payloads), fail_at)
                self.assertTrue(all(payload["nodeId"] == [VIDEO.id] for payload in payloads))
                self.assertTrue(client.client.is_closed)

    def test_progress_exception_joins_heartbeat_before_closing_domain_session(self) -> None:
        started = threading.Event()
        heartbeats = []
        cleanup_order = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/user/node/study")
            self.assertTrue(started.wait(timeout=1))
            raise ValueError("unexpected progress error")

        bot, client = self.make_domain_bot(handler)
        original_close = client.client.close

        def heartbeat(stop: threading.Event) -> None:
            heartbeats.append((stop, threading.current_thread()))
            started.set()
            stop.wait()
            cleanup_order.append("heartbeat stopped")

        def close() -> None:
            stop, thread = heartbeats[0]
            self.assertTrue(stop.is_set())
            self.assertFalse(thread.is_alive())
            cleanup_order.append("client closed")
            original_close()

        with (
            patch.object(tasks, "StudyClient", return_value=client),
            patch.object(tasks, "CourseBot", return_value=bot),
            patch.object(bot, "login", return_value=True),
            patch.object(bot, "_send_online_pings", side_effect=heartbeat),
            patch.object(bot.video_list, "get_videos", return_value=iter([VIDEO, NEXT_VIDEO])),
            patch.object(client.client, "close", side_effect=close),
        ):
            with self.assertRaisesRegex(ValueError, "unexpected progress error"):
                tasks.run_domain.run("one.example", CREDENTIALS)
        self.assertEqual(cleanup_order, ["heartbeat stopped", "client closed"])
        self.assertTrue(client.client.is_closed)

    def test_http_heartbeat_wait_is_interruptible_before_domain_session_closes(self) -> None:
        for retry_after, expected_wait in (("3600", 120), ("60", 60), (None, 1)):
            with self.subTest(retry_after=retry_after):
                waiting = threading.Event()
                heartbeats = []
                online_requests = []
                study_requests = []
                waits = []
                cleanup_order = []

                def handler(request: httpx.Request) -> httpx.Response:
                    if request.url.path == "/user/online":
                        online_requests.append(request)
                        headers = {"Retry-After": retry_after} if retry_after is not None else {}
                        return httpx.Response(503, headers=headers)
                    self.assertEqual(request.url.path, "/user/node/study")
                    self.assertTrue(waiting.wait(timeout=1))
                    study_requests.append(parse_qs(request.content.decode()))
                    return httpx.Response(200, json={"status": False})

                bot, client = self.make_domain_bot(handler)
                client.sleep = Mock()
                original_online = client.post_online
                original_heartbeat = bot._send_online_pings
                original_close = client.client.close

                def online(*, stop: threading.Event) -> httpx.Response | None:
                    original_wait = stop.wait

                    def wait(timeout: float) -> bool:
                        waits.append(timeout)
                        waiting.set()
                        return original_wait(timeout)

                    stop.wait = Mock(side_effect=wait)
                    return original_online(stop=stop)

                def heartbeat(stop: threading.Event) -> None:
                    heartbeats.append((stop, threading.current_thread()))
                    original_heartbeat(stop)
                    cleanup_order.append("heartbeat stopped")

                def close() -> None:
                    stop, thread = heartbeats[0]
                    self.assertTrue(stop.is_set())
                    self.assertFalse(thread.is_alive())
                    cleanup_order.append("client closed")
                    original_close()

                with (
                    patch.object(tasks, "StudyClient", return_value=client),
                    patch.object(tasks, "CourseBot", return_value=bot),
                    patch.object(bot, "login", return_value=True),
                    patch.object(bot, "_send_online_pings", side_effect=heartbeat),
                    patch.object(bot.video_list, "get_videos", return_value=iter([VIDEO, NEXT_VIDEO])),
                    patch.object(client, "post_online", side_effect=online),
                    patch.object(client.client, "close", side_effect=close),
                ):
                    tasks.run_domain.run("one.example", CREDENTIALS)
                self.assertEqual(waits[0], expected_wait)
                self.assertEqual(len(online_requests), 1)
                self.assertEqual(len(study_requests), 1)
                self.assertEqual(study_requests[0]["nodeId"], [VIDEO.id])
                client.sleep.assert_not_called()
                self.assertEqual(cleanup_order, ["heartbeat stopped", "client closed"])
                self.assertTrue(client.client.is_closed)

    def test_task_credentials_are_validated_before_discovery(self) -> None:
        with patch.object(tasks, "discover_domains") as discover:
            with self.assertRaises(ValidationError):
                tasks.start.run({"username": "student"})
        discover.assert_not_called()

    def test_celery_uses_json_broker_and_no_results(self) -> None:
        self.assertEqual(tasks.app.conf.task_default_queue, "study")
        self.assertEqual(tasks.app.conf.task_serializer, "json")
        self.assertEqual(tasks.app.conf.accept_content, ["json"])
        self.assertTrue(tasks.app.conf.task_ignore_result)
        self.assertFalse(tasks.app.conf.task_store_errors_even_if_ignored)
        self.assertIsNone(tasks.app.conf.result_backend)
        self.assertTrue(tasks.start.ignore_result)
        self.assertTrue(tasks.run_domain.ignore_result)


if __name__ == "__main__":
    unittest.main()
