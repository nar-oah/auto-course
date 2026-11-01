"""Offline regression coverage for the copied legacy study flow."""

import json
import logging
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, call, patch

STUDY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDY_DIR))

import requests

import auto
import list as video_module
import response as response_module
import urls


def response(data=None, *, text=None, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = (text if text is not None else json.dumps(data)).encode()
    result.encoding = "utf-8"
    return result


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        forbidden = patch(
            "requests.adapters.HTTPAdapter.send",
            side_effect=AssertionError("A regression test attempted real HTTP"),
        )
        forbidden.start()
        self.addCleanup(forbidden.stop)


class RequestTests(OfflineTestCase):
    def test_retry_policy_and_session_cookie_are_preserved(self):
        client = response_module.Call("https://platform.example")
        self.addCleanup(client.session.close)
        adapter = client.session.get_adapter(client.url)
        self.assertIs(adapter, client.session.get_adapter("http://platform.example"))
        retry = adapter.max_retries
        self.assertEqual(retry.total, 3)
        self.assertEqual(retry.backoff_factor, 1)
        self.assertEqual(retry.status_forcelist, [429, 500, 502, 503, 504])
        self.assertEqual(set(retry.allowed_methods), {"POST", "GET", "OPTIONS"})
        self.assertEqual(client.session.headers["Origin"], client.url)
        self.assertEqual(client.session.headers["Connection"], "keep-alive")
        self.assertIn("Firefox/145.0", client.session.headers["User-Agent"])
        prepared = []

        def send(request, **kwargs):
            prepared.append((request, kwargs))
            result = response({"status": True})
            result.request = request
            headers = Message()
            if len(prepared) == 1:
                headers["Set-Cookie"] = "session=legacy-cookie; Path=/; Secure"
            result.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
            return result

        with patch.object(adapter, "send", side_effect=send):
            client.get_captcha("/service/code?r=1")
            client.post_login("student", "password", "4321")
            client.post_online()
        self.assertEqual(client.session.cookies.get("session"), "legacy-cookie")
        self.assertEqual([item[1]["timeout"] for item in prepared], [15, 15, 15])
        self.assertEqual(prepared[1][0].headers["Cookie"], "session=legacy-cookie")
        self.assertEqual(prepared[2][0].headers["Cookie"], "session=legacy-cookie")

    def test_legacy_request_paths_headers_and_payloads(self):
        client = response_module.Call("https://platform.example")
        self.addCleanup(client.session.close)
        with patch.object(client.session, "request", return_value=response({})) as request:
            client.get_captcha("/service/code?r=1")
            client.post_login("student", "password", "4321")
            client.post_online()
            client.post_study(response_module.StudyInfo("video", 7, 31))
            client.post_study(response_module.StudyInfo("video", 7, 31), "1234")
        self.assertEqual(
            [(item.args[0], item.args[1]) for item in request.call_args_list],
            [("get", "https://platform.example/service/code?r=1"),
             ("post", "https://platform.example/user/login"),
             ("post", "https://platform.example/user/online"),
             ("post", "https://platform.example/user/node/study"),
             ("post", "https://platform.example/user/node/study")],
        )
        self.assertTrue(all(item.kwargs["timeout"] == 15 for item in request.call_args_list))
        self.assertEqual(request.call_args_list[0].kwargs["headers"], {"Accept": "image/webp,*/*"})
        self.assertEqual(request.call_args_list[1].kwargs["data"], {
            "username": "student", "password": "password", "code": "4321", "redirect": "",
        })
        self.assertEqual(request.call_args_list[1].kwargs["headers"]["Referer"], client.url + "/user/login")
        self.assertEqual(request.call_args_list[3].kwargs["data"], {
            "nodeId": "video", "studyId": "7", "studyTime": "31",
        })
        self.assertEqual(request.call_args_list[4].kwargs["data"]["code"], "1234")

    def test_only_request_exceptions_are_converted_to_none(self):
        client = response_module.Call("https://platform.example")
        self.addCleanup(client.session.close)
        with patch.object(client.session, "request", side_effect=requests.RequestException("offline")):
            with self.assertLogs(level=logging.ERROR):
                self.assertIsNone(client.request("get", "/user/"))
        with patch.object(client.session, "request", side_effect=ValueError("unexpected")):
            with self.assertRaisesRegex(ValueError, "unexpected"):
                client.request("get", "/user/")


class VideoListTests(OfflineTestCase):
    def test_course_and_video_pages_remain_lazy_and_keep_first_page_refetch(self):
        client = Mock()
        client.request.side_effect = [
            response(text='<div class="total">1 / 1</div>'),
            response(text='<div class="name"><a href="/user/course?courseId=12"> Course </a></div>'),
            response({"pageInfo": {"pageCount": 2}}),
            response({"list": [
                {"id": "v1", "name": "Video", "videoDuration": "01:02:03", "duration": "5", "progress": "0.2"},
                {"id": "threshold", "progress": 0.91},
                {"id": "missing"},
            ]}),
            response({"list": [{"id": "v2", "progress": 0.9}]}),
        ]
        videos = video_module.VideoList(client, 0.91).get_videos()
        self.assertEqual(client.request.call_args_list, [call("get", "/user/")])
        items = list(videos)
        self.assertEqual(client.request.call_args_list, [
            call("get", "/user/"),
            call("get", "/user/index?page=1"),
            call("get", "/user/study_record.json?courseId=12&page=1"),
            call("get", "/user/study_record.json?courseId=12&page=1"),
            call("get", "/user/study_record.json?courseId=12&page=2"),
        ])
        self.assertEqual([item.id for item in items], ["v1", "v2"])
        self.assertEqual(items[0].course, video_module.Course("12", "Course"))
        self.assertEqual((items[0].total_duration, items[0].watched_duration), (3723, 5))
        self.assertEqual((items[1].name, items[1].total_duration, items[1].watched_duration), ("", 0, 0))

    def test_missing_course_page_count_yields_no_videos(self):
        client = Mock()
        client.request.return_value = response(text="<html></html>")
        with self.assertLogs(level=logging.ERROR):
            videos = video_module.VideoList(client, 0.91).get_videos()
        self.assertEqual(list(videos), [])
        client.request.assert_called_once_with("get", "/user/")

    def test_failed_pages_are_skipped_without_retrying_in_video_list(self):
        first, third = response({}), response({})
        client = Mock()
        client.request.side_effect = [first, None, third]
        videos = video_module.VideoList(client, 0.91)
        self.assertEqual(list(videos._get_responses(3, "/page/{page}")), [first, third])
        self.assertEqual(client.request.call_args_list, [
            call("get", "/page/1"), call("get", "/page/2"), call("get", "/page/3"),
        ])


class CourseBotTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        ocr = patch.object(auto.ddddocr, "DdddOcr")
        self.ocr_factory = ocr.start()
        self.addCleanup(ocr.stop)
        self.bot = auto.CourseBot(
            auto.Config(STUDY_INTERVAL=1, ONLINE_INTERVAL=2, FINISH_PROGRESS=0.1),
            "platform.example/", auto.User("student", "password"), Mock(),
        )
        self.addCleanup(self.bot.response.session.close)
        self.bot.response = Mock()

    def video(self, duration=40, watched=0):
        return video_module.Video("Video", "v1", video_module.Course("c1", "Course"), duration, watched, 0)

    def progress(self, outcomes, duration=40, watched=0):
        self.bot.stop_online_thread = Mock()
        with patch.object(auto.threading, "Thread") as thread_factory:
            with patch.object(auto.time, "sleep") as sleep:
                with patch.object(self.bot, "get_study_id", side_effect=outcomes):
                    self.bot.update_study_progress(self.video(duration, watched))
        thread_factory.assert_called_once_with(target=self.bot._send_online_ping, daemon=True)
        thread_factory.return_value.start.assert_called_once_with()
        thread_factory.return_value.join.assert_called_once_with(timeout=5)
        self.bot.stop_online_thread.clear.assert_called_once_with()
        self.bot.stop_online_thread.set.assert_called_once_with()
        return self.bot.response.post_study.call_args_list, sleep.call_args_list

    def test_captcha_ocr_and_login_use_legacy_endpoints(self):
        self.ocr_factory.assert_called_once_with(show_ad=False)
        self.bot.ocr.classification.return_value = "4321"
        self.bot.response.get_captcha.return_value = response(text="captcha-image")
        self.bot.response.post_login.return_value = response({"status": True})
        with patch.object(auto.time, "time", return_value=123):
            self.assertIs(self.bot.login(), True)
        self.bot.response.get_captcha.assert_called_once_with("/service/code?r=123")
        self.bot.ocr.classification.assert_called_once_with(b"captcha-image")
        self.bot.response.post_login.assert_called_once_with("student", "password", "4321")

    def test_missing_captcha_skips_login(self):
        self.bot.response.get_captcha.return_value = None
        self.assertFalse(self.bot.login())
        self.bot.response.post_login.assert_not_called()

    def test_get_study_id_keeps_status_priority_and_strict_boolean_check(self):
        self.assertIsNone(self.bot.get_study_id(None))
        self.assertEqual(self.bot.get_study_id(response({"status": True, "studyId": 7, "need_code": 1})), 7)
        self.assertIsNone(self.bot.get_study_id(response({"status": 1, "studyId": 7})))
        self.assertEqual(self.bot.get_study_id(response({"status": True, "studyId": "string-id"})), "string-id")
        with patch.object(self.bot, "recognize_captcha", return_value="1234") as captcha:
            with patch.object(self.bot, "login", return_value=True) as login:
                self.assertEqual(self.bot.get_study_id(response({"need_code": 1, "offline": 1})), "1234")
                captcha.assert_called_once_with(captcha_path="/service/code/aa")
                login.assert_not_called()
                self.assertIs(self.bot.get_study_id(response({"offline": 1})), True)
                login.assert_called_once_with()

    def test_get_study_id_only_catches_json_decode_errors(self):
        self.assertIsNone(self.bot.get_study_id(response(text="not-json")))
        self.bot.logger.error.assert_called_once()
        invalid = Mock()
        invalid.json.side_effect = ValueError("unexpected")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            self.bot.get_study_id(invalid)

    def test_none_retries_exactly_five_times_without_advancing_time(self):
        calls, sleeps = self.progress([None] * 5)
        self.assertEqual(calls, [call(response_module.StudyInfo("v1", 0, 1))] * 5)
        self.assertEqual(sleeps, [call(0)] * 5)

    def test_success_does_not_reset_accumulated_retries(self):
        calls, sleeps = self.progress([None, None, 11, None, None, 11, None], duration=1000)
        self.assertEqual([item.args[0].time for item in calls], [1, 1, 1, 31, 61, 91, 121])
        self.assertEqual([item.args[0].study_id for item in calls], [0, 0, 0, 11, 11, 11, 11])
        self.assertEqual(sleeps, [call(0)] * 3 + [call(30)] * 4)

    def test_boolean_login_results_are_not_study_ids(self):
        calls, sleeps = self.progress([True, False, False, False, False, False])
        self.assertEqual(calls, [call(response_module.StudyInfo("v1", 0, 1))] * 6)
        self.assertEqual(sleeps, [call(0)] * 6)

    def test_captcha_resubmits_same_progress_then_uses_legacy_intervals(self):
        calls, sleeps = self.progress(["2468", 9, 9, 9])
        self.assertEqual(calls, [
            call(response_module.StudyInfo("v1", 0, 1)),
            call(response_module.StudyInfo("v1", 0, 1), "2468"),
            call(response_module.StudyInfo("v1", 0, 1)),
            call(response_module.StudyInfo("v1", 9, 31)),
            call(response_module.StudyInfo("v1", 9, 37)),
        ])
        self.assertIs(calls[0].args[0], calls[1].args[0])
        self.assertEqual(sleeps, [call(0), call(0), call(30), call(6)])

    def test_finish_time_uses_remaining_duration_and_starts_heartbeat_for_empty_work(self):
        calls, sleeps = self.progress([], duration=41, watched=40)
        self.assertEqual(calls, [])
        self.assertEqual(sleeps, [])

    def test_heartbeat_posts_before_waiting_for_class_interval(self):
        self.bot.stop_online_thread = Mock()
        self.bot.stop_online_thread.is_set.side_effect = [False, False, True]
        order = Mock()
        order.attach_mock(self.bot.response.post_online, "post")
        order.attach_mock(self.bot.stop_online_thread.wait, "wait")
        self.bot._send_online_ping()
        self.assertEqual(order.mock_calls, [call.post(), call.wait(120), call.post(), call.wait(120)])

    def test_unexpected_study_error_keeps_original_exception_behavior(self):
        self.bot.stop_online_thread = Mock()
        self.bot.response.post_study.side_effect = RuntimeError("failed")
        with patch.object(auto.threading, "Thread") as thread_factory:
            with patch.object(auto.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    self.bot.update_study_progress(self.video())
        self.bot.stop_online_thread.set.assert_not_called()
        thread_factory.return_value.join.assert_not_called()

    def test_run_continues_after_failed_login_and_preserves_video_order(self):
        videos = [self.video(), self.video(duration=60)]
        self.bot.video_list = Mock()
        self.bot.video_list.get_videos.return_value = iter(videos)
        with patch.object(self.bot, "login", return_value=False) as login:
            with patch.object(self.bot, "update_study_progress") as update:
                self.bot.run()
        login.assert_called_once_with()
        self.bot.video_list.get_videos.assert_called_once_with()
        self.assertEqual(update.call_args_list, [call(videos[0]), call(videos[1])])


class PlatformTests(OfflineTestCase):
    def test_only_first_two_selected_links_are_returned_without_filtering(self):
        html = '''<a href="https://outside.example">Outside</a>
        <div class="list set_index5" data-num="1">
          <a href="https://first.example/path">First</a>
          <a href="/relative/path">Relative</a>
          <a href="https://third.example">Third</a>
        </div>'''
        with patch.object(urls, "Call") as factory:
            factory.return_value.get_page.return_value = response(text=html)
            result = urls.get_urls()
            self.assertEqual(list(result), ["first.example", ""])
        factory.assert_called_once_with("https://www.canvard.net.cn")
        factory.return_value.get_page.assert_called_once_with()

    def test_missing_platform_response_returns_empty_list(self):
        with patch.object(urls, "Call") as factory:
            factory.return_value.get_page.return_value = None
            self.assertEqual(urls.get_urls(), [])


if __name__ == "__main__":
    unittest.main()
