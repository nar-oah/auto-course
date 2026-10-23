import json
import unittest
from unittest.mock import Mock, patch

import httpx

from auto_course_study import trace
from auto_course_study import catalog
from auto_course_study.catalog import VideoList
from auto_course_study.http import StudyClient
from test_trace import captured_trace, payloads


PAGINATION = '<div class="total">1 / 1</div>'
COURSES = '<div class="name"><a href="?courseId=123">Private course title</a></div>'


class CatalogTraceTests(unittest.TestCase):
    def run_catalog(self, responses, *, enabled="1"):
        requests = []
        sleep = Mock()
        videos = []
        error = None

        def handler(request):
            requests.append((request.method, str(request.url), request.content))
            if request.url.path in responses:
                value = responses[request.url.path]
                return value(request) if callable(value) else value
            if request.url.path == "/user/":
                return httpx.Response(200, text=PAGINATION)
            if request.url.path == "/user/index":
                return httpx.Response(200, text=COURSES)
            self.fail(f"Unexpected mock endpoint: {request.url.path}")

        with captured_trace(enabled) as records:
            try:
                with trace.task_trace("courses.example", "catalog-trace-task") as diagnostic:
                    with StudyClient(
                        "https://courses.example", transport=httpx.MockTransport(handler), sleep=sleep,
                    ) as client:
                        videos = list(VideoList(client, .91).get_videos())
                    diagnostic.outcome("catalog", "empty_video_list" if not videos else "completed",
                                       "empty_video_list" if not videos else "completed")
            except Exception as raised:
                error = raised
        logs = payloads(records)
        return videos, error, requests, sleep.call_args_list, logs

    def assert_trace_preserves_behavior(self, responses):
        disabled = self.run_catalog(responses, enabled=None)
        enabled = self.run_catalog(responses)
        self.assertEqual(disabled[:4], enabled[:4])
        self.assertEqual(disabled[4], [])
        return enabled[4][0]

    def assert_empty_issue(self, responses, reason):
        payload = self.assert_trace_preserves_behavior(responses)
        self.assertEqual(payload["summary"]["result"], "empty_video_list")
        self.assertFalse(payload["summary"]["catalog_reliable"])
        self.assertEqual(payload["summary"]["empty_video_list_reason"], "catalog_incomplete_or_invalid")
        self.assertGreater(payload["summary"]["catalog_issue_counts"].get(reason, 0), 0)
        self.assertIn("events", payload)
        self.assertEqual(payload["events"][-1]["event"], "task_end")
        return payload

    def test_valid_empty_lists_and_completed_videos_are_trustworthy(self):
        for count, items in (
            (0, []), (1, []), (1, [{"id": "done", "progress": .91}]),
            (1, [{"id": "done", "progress": 1}]),
        ):
            with self.subTest(page_count=count, items=items):
                payload = self.assert_trace_preserves_behavior({
                    "/user/study_record.json": httpx.Response(200, json={
                        "pageInfo": {"pageCount": count}, "list": items,
                    }),
                })
                self.assertTrue(payload["summary"]["catalog_reliable"])
                self.assertEqual(payload["summary"]["empty_video_list_reason"], "no_pending_videos")
                self.assertEqual(payload["summary"]["catalog_issue_counts"], {})
                self.assertIn("events", payload)

    def test_missing_pagination_is_uncertain_and_does_not_add_requests(self):
        for html in ("", "login required", '<div class="total">unknown</div>'):
            with self.subTest(html=html):
                payload = self.assert_empty_issue(
                    {"/user/": httpx.Response(200, text=html)}, "course_pagination_missing",
                )
                self.assertEqual(payload["summary"]["http"]["calls"], 1)

    def test_failed_course_fetches_keep_existing_retry_and_exit_behavior(self):
        for path, reason in (
            ("/user/", "course_pagination_unavailable"),
            ("/user/index", "course_page_unavailable"),
        ):
            with self.subTest(path=path):
                payload = self.assert_empty_issue({path: httpx.Response(503)}, reason)
                self.assertEqual(payload["summary"]["http"]["retries"], 3)

    def test_unrecognized_empty_and_malformed_course_pages_are_uncertain(self):
        for html, reason in (
            ("", "course_page_empty"),
            ("<html>login required</html>", "course_page_unrecognized"),
            ('<div class="name">Missing link</div>', "course_page_invalid_course_links"),
            ('<div class="name"><a href="?wrong=1">Name</a></div>', "course_page_invalid_course_links"),
        ):
            with self.subTest(reason=reason, html=html):
                self.assert_empty_issue({"/user/index": httpx.Response(200, text=html)}, reason)

    def test_video_response_and_list_shapes_are_diagnosed_without_changing_fallbacks(self):
        for response, reason in (
            (httpx.Response(200, text="login required"), "video_response_not_json"),
            (httpx.Response(200, json=[]), "video_response_not_object"),
            (httpx.Response(200, json={}), "video_list_missing"),
            (httpx.Response(200, json={"list": None}), "video_list_invalid_type"),
            (httpx.Response(200, json={"list": {}}), "video_list_invalid_type"),
            (httpx.Response(200, json={"list": [None, 1, "private item"]}), "video_list_invalid_items"),
            (httpx.Response(503), "video_page_unavailable"),
        ):
            with self.subTest(reason=reason):
                self.assert_empty_issue({"/user/study_record.json": response}, reason)

    def test_invalid_video_pagination_remains_on_the_original_single_page_fallback(self):
        for page_info, reason in (
            (None, "video_pagination_missing"), ([], "video_pagination_missing"),
            ({}, "video_pagination_missing"), ({"pageCount": None}, "video_pagination_invalid"),
            ({"pageCount": "invalid"}, "video_pagination_invalid"),
            ({"pageCount": -1}, "video_pagination_invalid"),
            ({"pageCount": True}, "video_pagination_invalid"),
        ):
            with self.subTest(page_info=page_info):
                payload = self.assert_empty_issue({
                    "/user/study_record.json": httpx.Response(200, json={"pageInfo": page_info, "list": []}),
                }, reason)
                self.assertEqual(payload["summary"]["http"]["calls"], 3)

    def test_business_errors_and_unknown_messages_cannot_be_reported_as_no_pending_videos(self):
        cases = [({"status": False}, "video_response_business_error"),
                 ({"errorCode": 403}, "video_response_business_error"),
                 ({"error": "private error text"}, "video_response_business_error")]
        cases.extend(({field: "private error text"}, "video_response_ambiguous_message") for field in (
            "message", "msg", "error_message", "errorMessage", "errorMsg", "errmsg", "detail",
        ))
        for fields, reason in cases:
            with self.subTest(fields=fields):
                payload = self.assert_empty_issue({
                    "/user/study_record.json": httpx.Response(200, json={
                        "pageInfo": {"pageCount": 1}, "list": [], **fields,
                    }),
                }, reason)
                self.assertNotIn("private error text", json.dumps(payload))
                self.assertNotIn("Private course title", json.dumps(payload))

    def test_skipped_and_unknown_progress_items_keep_the_same_business_result(self):
        for item, reason in (
            ({}, "video_progress_missing"),
            ({"progress": "nan"}, "video_progress_nonfinite"),
            ({"progress": "inf"}, "video_progress_nonfinite"),
            ({"progress": "private-invalid"}, "video_item_invalid_duration_or_progress"),
            ({"progress": .1, "videoDuration": "broken"}, "video_item_invalid_duration_or_progress"),
        ):
            with self.subTest(item=item):
                self.assert_empty_issue({
                    "/user/study_record.json": httpx.Response(200, json={
                        "pageInfo": {"pageCount": 1}, "list": [item],
                    }),
                }, reason)

    def test_multiple_pages_and_pending_videos_keep_identical_request_order_and_results(self):
        def video_page(request):
            page = request.url.params["page"]
            return httpx.Response(200, json={
                "pageInfo": {"pageCount": 2},
                "list": [{"id": f"video-{page}", "progress": .1, "videoDuration": "00:00:50", "duration": 3}],
            })

        payload = self.assert_trace_preserves_behavior({"/user/study_record.json": video_page})
        self.assertEqual(payload["summary"]["pending_videos_discovered"], 2)
        self.assertTrue(payload["summary"]["catalog_reliable"])
        self.assertEqual(payload["summary"]["http"]["calls"], 4)

    def test_original_parser_exception_type_and_requests_are_preserved(self):
        runs = []
        for enabled in (None, "1"):
            with patch("auto_course_study.catalog.BeautifulSoup", side_effect=RuntimeError("private parser text")):
                runs.append(self.run_catalog({}, enabled=enabled))
        self.assertEqual(runs[0][0], runs[1][0])
        self.assertIs(type(runs[0][1]), RuntimeError)
        self.assertIs(type(runs[1][1]), RuntimeError)
        self.assertEqual(runs[0][2:4], runs[1][2:4])
        payload = runs[1][4][0]
        self.assertEqual(payload["summary"]["result"], "exception")
        self.assertEqual(payload["summary"]["first_anomaly"]["exception_type"], "RuntimeError")
        self.assertIn("course_pagination_parse_exception", payload["summary"]["catalog_issue_counts"])
        self.assertNotIn("private parser text", json.dumps(payload))

    def test_course_parser_one_time_failure_is_not_consumed_by_diagnostics(self):
        parser = catalog.BeautifulSoup
        runs = []
        for enabled in (None, "1"):
            calls = []

            def parse_once(html, *args, **kwargs):
                calls.append(html)
                if len(calls) == 2:
                    raise RuntimeError("private one-time parsing failure")
                return parser(html, *args, **kwargs)

            with patch("auto_course_study.catalog.BeautifulSoup", side_effect=parse_once):
                runs.append(self.run_catalog({}, enabled=enabled))
            self.assertEqual(len(calls), 2)
        self.assertIs(type(runs[0][1]), RuntimeError)
        self.assertIs(type(runs[1][1]), RuntimeError)
        self.assertEqual(runs[0][2:4], runs[1][2:4])
        self.assertEqual(runs[1][4][0]["summary"]["result"], "exception")

    def test_broken_diagnostic_inspection_does_not_interrupt_catalog_business(self):
        responses = {"/user/study_record.json": httpx.Response(200, json={
            "pageInfo": {"pageCount": 1},
            "list": [{"id": "video-1", "progress": .1, "videoDuration": "00:00:50"}],
        })}
        expected = self.run_catalog(responses, enabled=None)
        with patch.object(trace.StudyTrace, "response", side_effect=RuntimeError("broken diagnostic")):
            actual = self.run_catalog(responses)
        self.assertEqual(expected[:4], actual[:4])
        self.assertIn("catalog_diagnostic_failed", actual[4][0]["summary"]["catalog_issue_counts"])
