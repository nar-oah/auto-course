from collections.abc import Iterator
import re
from typing import Any

from bs4 import BeautifulSoup

from .http import StudyClient, json_object
from .models import Course, Video
from .trace import get_trace


class VideoList:
    def __init__(self, client: StudyClient, threshold: float) -> None:
        self.client = client
        self.threshold = threshold
        self.trace = get_trace(client)

    @staticmethod
    def _get_seconds(hms: str) -> int:
        hours, minutes, seconds = map(int, hms.split(":"))
        return hours * 3600 + minutes * 60 + seconds

    def _get_pages(self) -> int:
        response = self.client.request("GET", "/user/")
        if response is None:
            self.trace.anomaly("course_pagination_unavailable", "catalog")
            return 0
        total = BeautifulSoup(response.text, "html.parser").find("div", class_="total")
        if total is not None and (match := re.search(r"/\s*(\d+)", total.get_text())):
            self.trace.event("course_pages", pages=int(match.group(1)))
            return int(match.group(1))
        self.trace.event("course_pagination_missing")
        return 0

    @staticmethod
    def _handle_courses(html: str) -> Iterator[Course]:
        soup = BeautifulSoup(html, "html.parser")
        for item in soup.find_all("div", class_="name"):
            link = item.find("a")
            if link is None:
                continue
            href = link.get("href")
            if isinstance(href, str) and (match := re.search(r"courseId=(\d+)", href)):
                yield Course(id=match.group(1), name=link.get_text().strip())

    def _get_video_items(self, course: Course) -> Iterator[dict[str, Any]]:
        path = f"/user/study_record.json?courseId={course.id}&page={{page}}"
        first_page = json_object(self.client.request("GET", path.format(page=1)))
        page_info = first_page.get("pageInfo", {})
        try:
            page_count = max(1, int(page_info.get("pageCount", 1))) if isinstance(page_info, dict) else 1
        except (TypeError, ValueError, OverflowError):
            page_count = 1
        # Reuse the first response; the old implementation fetched page 1 twice.
        for page in range(1, page_count + 1):
            data = first_page if page == 1 else json_object(self.client.request("GET", path.format(page=page)))
            items = data.get("list", [])
            if isinstance(items, list):
                yield from (item for item in items if isinstance(item, dict))

    def get_videos(self) -> Iterator[Video]:
        self.trace.event("catalog_start")
        for page in range(1, self._get_pages() + 1):
            response = self.client.request("GET", f"/user/index?page={page}")
            if response is None:
                self.trace.anomaly("course_page_unavailable", "catalog", page=page)
                continue
            for course in self._handle_courses(response.text):
                self.trace.event("course_discovered", course=self.trace.token("course", course.id))
                for item in self._get_video_items(course):
                    try:
                        progress = float(item.get("progress", 1))
                        if not progress < self.threshold:
                            continue
                        total = self._get_seconds(item.get("videoDuration", "00:00:00"))
                        watched = int(item.get("duration", 0))
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        self.trace.event("video_item_skipped", reason="invalid_duration_or_progress")
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
