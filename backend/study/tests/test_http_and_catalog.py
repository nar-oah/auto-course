from collections import Counter
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs

import httpx

from auto_course_study.catalog import VideoList
from auto_course_study.discovery import discover_domains
from auto_course_study.http import StudyClient, json_object
from auto_course_study.models import StudyInfo


class HttpTests(unittest.TestCase):
    def test_retries_exhaust_after_four_requests_with_positive_backoff(self) -> None:
        handler = Mock(return_value=httpx.Response(503))
        sleep = Mock()
        with StudyClient("https://courses.example", transport=httpx.MockTransport(handler), sleep=sleep) as client:
            self.assertIsNone(client.request("POST", "/user/online"))
            self.assertEqual(handler.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])

    def test_retry_after_and_transport_errors(self) -> None:
        responses = [
            httpx.ConnectError("connection failed"),
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, json={"status": True}),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        sleep = Mock()
        with StudyClient("https://courses.example", transport=httpx.MockTransport(handler), sleep=sleep) as client:
            self.assertEqual(json_object(client.post_online()), {"status": True})
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 3])

    def test_retry_after_never_causes_an_immediate_retry(self) -> None:
        for retry_after in ("0", "-3", "Mon, 01 Jan 2001 00:00:00 GMT"):
            with self.subTest(retry_after=retry_after):
                handler = Mock(side_effect=[
                    httpx.Response(429, headers={"Retry-After": retry_after}),
                    httpx.Response(200),
                ])
                sleep = Mock()
                with StudyClient("https://courses.example", transport=httpx.MockTransport(handler), sleep=sleep) as client:
                    self.assertEqual(client.post_online().status_code, 200)
                self.assertEqual(handler.call_count, 2)
                sleep.assert_called_once_with(1)

    def test_invalid_retry_after_uses_backoff(self) -> None:
        for retry_after in ("invalid", "nan", "inf"):
            with self.subTest(retry_after=retry_after):
                handler = Mock(return_value=httpx.Response(503, headers={"Retry-After": retry_after}))
                sleep = Mock()
                with StudyClient("https://courses.example", transport=httpx.MockTransport(handler), sleep=sleep) as client:
                    self.assertIsNone(client.post_online())
                self.assertEqual(handler.call_count, 4)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])

    def test_study_posts_once_and_preserves_failure_response(self) -> None:
        info = StudyInfo(video_id="video-1", study_id=42, time=31)
        for status in (200, 400, 403, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                response = httpx.Response(status, json={"status": status == 200}, headers={"Retry-After": "0"})
                handler = Mock(return_value=response)
                sleep = Mock()
                with StudyClient("https://courses.example", transport=httpx.MockTransport(handler), sleep=sleep) as client:
                    self.assertIs(client.post_study(info, "captcha"), response)
                handler.assert_called_once()
                request = handler.call_args.args[0]
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.url.path, "/user/node/study")
                self.assertEqual(parse_qs(request.content.decode()), {
                    "nodeId": ["video-1"], "studyId": ["42"],
                    "studyTime": ["31"], "code": ["captcha"],
                })
                sleep.assert_not_called()

    def test_study_transport_error_is_not_retried(self) -> None:
        handler = Mock(side_effect=httpx.ConnectError("connection failed"))
        sleep = Mock()
        with StudyClient("https://courses.example", transport=httpx.MockTransport(handler), sleep=sleep) as client:
            self.assertIsNone(client.post_study(StudyInfo("video-1", 0, 1)))
        handler.assert_called_once()
        sleep.assert_not_called()

    def test_study_redirect_does_not_replay_the_request(self) -> None:
        for status in (302, 307, 308):
            with self.subTest(status=status):
                response = httpx.Response(status, headers={"Location": "/user/node/study"})
                handler = Mock(side_effect=[response, httpx.Response(200)])
                sleep = Mock()
                with StudyClient("https://courses.example", transport=httpx.MockTransport(handler), sleep=sleep) as client:
                    self.assertIs(client.post_study(StudyInfo("video-1", 0, 1)), response)
                handler.assert_called_once()
                sleep.assert_not_called()

    def test_study_without_captcha_omits_code(self) -> None:
        handler = Mock(return_value=httpx.Response(200))
        with StudyClient("https://courses.example", transport=httpx.MockTransport(handler)) as client:
            client.post_study(StudyInfo("video-1", 0, 1))
        self.assertEqual(parse_qs(handler.call_args.args[0].content.decode()), {
            "nodeId": ["video-1"], "studyId": ["0"], "studyTime": ["1"],
        })

    def test_redirects_keep_cookie_and_close_client(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(302, headers={"Location": "/user/", "Set-Cookie": "session=active; Path=/"})
            self.assertEqual(request.headers["Cookie"], "session=active")
            return httpx.Response(200, text="user page")

        with StudyClient("https://courses.example", transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(client.get_page().text, "user page")
            self.assertEqual(client.client.timeout.read, 15)
            self.assertEqual(client.client.headers["Origin"], "https://courses.example")
        self.assertTrue(client.client.is_closed)

    def test_invalid_json_is_not_a_success_response(self) -> None:
        self.assertEqual(json_object(httpx.Response(200, text="login required")), {})
        self.assertEqual(json_object(httpx.Response(200, json=[1, 2])), {})
        self.assertEqual(json_object(None), {})


class CatalogTests(unittest.TestCase):
    def test_discovery_selects_course_links_and_deduplicates_domains(self) -> None:
        html = """
        <div class="list set_index5" data-num="1">
          <a href="https://news.example"><div><h3>通知公告</h3></div></a>
          <a href="https://first.example/path"><div><h3>在线课程测评考试平台</h3></div></a>
          <a href="//second.example/path"><div><h3>劳动课程测评考试平台</h3></div></a>
          <a href="https://other.example"><div><h3>数字图书馆</h3></div></a>
          <a href="https://third.example"><div><h3>公益课程平台</h3></div></a>
          <a href="https://first.example/other"><div><h3>其他课程</h3></div></a>
          <a href="https://plain.example">课程</a>
          <a><div><h3>课程</h3></div></a>
        </div>
        """
        with StudyClient("https://www.canvard.net.cn", transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))) as client:
            self.assertEqual(discover_domains(client), ["first.example", "second.example", "third.example"])

    def test_all_course_and_video_pages_without_fetching_page_one_twice(self) -> None:
        seen = Counter()

        def handler(request: httpx.Request) -> httpx.Response:
            seen[str(request.url)] += 1
            path = request.url.path
            if path == "/user/":
                return httpx.Response(200, text='<div class="total">1 / 2</div>')
            if path == "/user/index":
                course_id = "10" if request.url.params["page"] == "1" else "20"
                return httpx.Response(200, text=f'<div class="name"><a href="/user/course?courseId={course_id}"> Course {course_id} </a></div>')
            if path == "/user/study_record.json":
                course_id = request.url.params["courseId"]
                page = request.url.params["page"]
                if course_id == "10" and page == "1":
                    return httpx.Response(200, json={
                        "pageInfo": {"pageCount": 2},
                        "list": [
                            {"id": "v1", "name": "First", "videoDuration": "01:02:03", "duration": 12, "progress": .90},
                            {"id": "finished", "progress": .91},
                            {"id": "unknown"},
                            {"id": "bad", "progress": .1, "videoDuration": "broken"},
                            {"id": "nan", "progress": "nan"},
                        ],
                    })
                return httpx.Response(200, json={
                    "pageInfo": {"pageCount": 1},
                    "list": [{"id": f"v{course_id}-{page}", "progress": .1, "videoDuration": "00:00:50", "duration": "3"}],
                })
            self.fail(f"Unexpected endpoint: {request.url}")

        with StudyClient("https://courses.example", transport=httpx.MockTransport(handler)) as client:
            videos = list(VideoList(client, .91).get_videos())
        self.assertEqual([video.id for video in videos], ["v1", "v10-2", "v20-1"])
        self.assertEqual(videos[0].total_duration, 3723)
        self.assertEqual(videos[0].watched_duration, 12)
        self.assertEqual(videos[0].course.name, "Course 10")
        self.assertTrue(all(count == 1 for count in seen.values()))
        self.assertIn("https://courses.example/user/index?page=2", seen)

    def test_missing_course_pagination_and_invalid_video_json_are_skipped(self) -> None:
        with StudyClient("https://courses.example", transport=httpx.MockTransport(lambda request: httpx.Response(200, text="login page"))) as client:
            self.assertEqual(list(VideoList(client, .91).get_videos()), [])

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/user/":
                return httpx.Response(200, text='<div class="total">1 / 1</div>')
            if request.url.path == "/user/index":
                return httpx.Response(200, text='<div class="name"><a href="?courseId=1">Course</a></div>')
            return httpx.Response(200, text="not JSON")

        with StudyClient("https://courses.example", transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(list(VideoList(client, .91).get_videos()), [])


if __name__ == "__main__":
    unittest.main()
