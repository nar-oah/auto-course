from collections.abc import Iterator
from functools import wraps
import math
import re
from typing import Any

from bs4 import BeautifulSoup

from .http import StudyClient, json_object
from .models import Course, Video
from .trace import get_trace


def _diagnostic(function):
    """Extra inspection must never affect catalog iteration or exceptions."""
    @wraps(function)
    def call(self, *args, **kwargs):
        try:
            if self.trace.enabled:
                return function(self, *args, **kwargs)
        except Exception as error:
            self._catalog_issue("catalog_diagnostic_failed", exception_type=type(error).__name__)
    return call


class VideoList:
    def __init__(self, client: StudyClient, threshold: float) -> None:
        self.client = client
        self.threshold = threshold
        self.trace = get_trace(client)

    def _catalog_issue(self, reason: str, **fields: Any) -> None:
        try:
            self.trace.event("catalog_issue", reason=reason, **fields)
            self.trace.anomaly(reason, "catalog", **fields)
        except Exception:
            pass

    @_diagnostic
    def _trace_course_page(self, response, page: int) -> None:
        self.trace.event("course_page_response", page=page, response=self.trace.response(response))
        if response.status_code >= 400:
            self._catalog_issue("course_page_http_error", page=page)
        if not response.text.strip():
            self._catalog_issue("course_page_empty", page=page)

    @_diagnostic
    def _trace_course_structure(self, soup, page: int) -> None:
        items = soup.find_all("div", class_="name")
        valid = 0
        for item in items:
            link = item.find("a")
            href = None if link is None else link.get("href")
            valid += int(isinstance(href, str) and re.search(r"courseId=(\d+)", href) is not None)
        self.trace.event("course_page_structure", page=page, entries=len(items), valid_course_links=valid)
        if not items:
            self._catalog_issue("course_page_unrecognized", page=page)
        elif valid != len(items):
            self._catalog_issue("course_page_invalid_course_links", page=page, skipped=len(items) - valid)

    @_diagnostic
    def _trace_video_page(self, response, data: dict[str, Any], course: Course, page: int) -> None:
        fields = {"course": self.trace.token("course", course.id), "page": page}
        details = self.trace.response(response)
        self.trace.event("video_page_response", **fields, response=details)
        if response is None:
            self._catalog_issue("video_page_unavailable", **fields)
            return
        if response.status_code >= 400:
            self._catalog_issue("video_page_http_error", **fields)
        if not details.get("json_parsed"):
            self._catalog_issue("video_response_not_json", **fields)
            return
        if not details.get("json_object"):
            self._catalog_issue("video_response_not_object", **fields)
            return
        failed = (
            data.get("status") is False or data.get("success") is False
            or (type(data.get("status")) is int and data["status"] == 0)
            or data.get("offline") in (True, 1)
            or any(data.get(key) not in (None, False, 0, "", "0") for key in (
                "error", "error_code", "errorCode", "errcode", "errno", "resultCode",
            ))
            or (isinstance(data.get("status"), str) and data["status"].lower() in {
                "false", "failure", "failed", "error", "denied", "rejected", "unauthorized", "forbidden",
            })
        )
        if failed:
            self._catalog_issue("video_response_business_error", **fields, response=details)
        elif data.get("status") is not True and data.get("success") is not True and any(
            data.get(key) not in (None, "", False, 0) for key in (
                "message", "msg", "error_message", "errorMessage", "errorMsg", "errmsg", "detail",
            )
        ):
            self._catalog_issue("video_response_ambiguous_message", **fields, response=details)
        if "list" not in data:
            self._catalog_issue("video_list_missing", **fields)
        elif not isinstance(data["list"], list):
            self._catalog_issue("video_list_invalid_type", **fields, value_type=type(data["list"]).__name__)
        else:
            invalid = sum(not isinstance(item, dict) for item in data["list"])
            self.trace.event("video_page_structure", **fields, entries=len(data["list"]), invalid_entries=invalid)
            if invalid:
                self._catalog_issue("video_list_invalid_items", **fields, skipped=invalid)
        if page == 1:
            page_info = data.get("pageInfo")
            if not isinstance(page_info, dict) or "pageCount" not in page_info:
                self._catalog_issue("video_pagination_missing", **fields)
            else:
                value = page_info["pageCount"]
                try:
                    count = int(value)
                    valid = not isinstance(value, bool) and (
                        count >= 1 or (count == 0 and data.get("list") == [])
                    ) and (
                        not isinstance(value, float) or value == count
                    )
                except (TypeError, ValueError, OverflowError):
                    valid = False
                if not valid:
                    self._catalog_issue("video_pagination_invalid", **fields, value_type=type(value).__name__)

    @_diagnostic
    def _trace_progress(self, item: dict[str, Any], progress: float) -> None:
        if "progress" not in item:
            self._catalog_issue("video_progress_missing")
        elif not math.isfinite(progress):
            self._catalog_issue("video_progress_nonfinite")

    @staticmethod
    def _get_seconds(hms: str) -> int:
        hours, minutes, seconds = map(int, hms.split(":"))
        return hours * 3600 + minutes * 60 + seconds

    def _get_pages(self) -> int:
        response = self.client.request("GET", "/user/")
        if response is None:
            self._catalog_issue("course_pagination_unavailable")
            return 0
        self._trace_pagination_response(response)
        try:
            total = BeautifulSoup(response.text, "html.parser").find("div", class_="total")
            if total is not None and (match := re.search(r"/\s*(\d+)", total.get_text())):
                self.trace.event("course_pages", pages=int(match.group(1)))
                return int(match.group(1))
        except Exception as error:
            self._catalog_issue("course_pagination_parse_exception", exception_type=type(error).__name__)
            raise
        self.trace.event("course_pagination_missing")
        self._catalog_issue("course_pagination_missing")
        return 0

    @_diagnostic
    def _trace_pagination_response(self, response) -> None:
        self.trace.event("course_pagination_response", response=self.trace.response(response))
        if response.status_code >= 400:
            self._catalog_issue("course_pagination_http_error")

    @staticmethod
    def _handle_courses(html: str, *, inspect=None) -> Iterator[Course]:
        soup = BeautifulSoup(html, "html.parser")
        if inspect is not None:
            inspect(soup)
        for item in soup.find_all("div", class_="name"):
            link = item.find("a")
            if link is None:
                continue
            href = link.get("href")
            if isinstance(href, str) and (match := re.search(r"courseId=(\d+)", href)):
                yield Course(id=match.group(1), name=link.get_text().strip())

    def _get_video_items(self, course: Course) -> Iterator[dict[str, Any]]:
        path = f"/user/study_record.json?courseId={course.id}&page={{page}}"
        first_response = self.client.request("GET", path.format(page=1))
        first_page = json_object(first_response)
        page_info = first_page.get("pageInfo", {})
        try:
            page_count = max(1, int(page_info.get("pageCount", 1))) if isinstance(page_info, dict) else 1
        except (TypeError, ValueError, OverflowError):
            page_count = 1
        # Reuse the first response; the old implementation fetched page 1 twice.
        for page in range(1, page_count + 1):
            response = first_response if page == 1 else self.client.request("GET", path.format(page=page))
            data = first_page if page == 1 else json_object(response)
            self._trace_video_page(response, data, course, page)
            items = data.get("list", [])
            if isinstance(items, list):
                yield from (item for item in items if isinstance(item, dict))

    def get_videos(self) -> Iterator[Video]:
        self.trace.event("catalog_start")
        for page in range(1, self._get_pages() + 1):
            response = self.client.request("GET", f"/user/index?page={page}")
            if response is None:
                self._catalog_issue("course_page_unavailable", page=page)
                continue
            self._trace_course_page(response, page)
            for course in self._handle_courses(
                response.text, inspect=lambda soup: self._trace_course_structure(soup, page),
            ):
                self.trace.event("course_discovered", course=self.trace.token("course", course.id))
                for item in self._get_video_items(course):
                    try:
                        progress = float(item.get("progress", 1))
                        self._trace_progress(item, progress)
                        if not progress < self.threshold:
                            continue
                        total = self._get_seconds(item.get("videoDuration", "00:00:00"))
                        watched = int(item.get("duration", 0))
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        self.trace.event("video_item_skipped", reason="invalid_duration_or_progress")
                        self._catalog_issue("video_item_invalid_duration_or_progress")
                        continue
                    self.trace.event(
                        "pending_video_discovered", video=self.trace.token("video", str(item.get("id", ""))),
                        course=self.trace.token("course", course.id),
                    )
                    yield Video(
                        name=str(item.get("name", "")),
                        id=str(item.get("id", "")),
                        course=course,
                        total_duration=total,
                        watched_duration=watched,
                        progress=progress,
                    )
        self.trace.event("catalog_complete", counts_are_final=True)
