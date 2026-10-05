from collections.abc import Iterator
import re
from typing import Any

from bs4 import BeautifulSoup

from .http import StudyClient, json_object
from .models import Course, Video


class VideoList:
    def __init__(self, client: StudyClient, threshold: float) -> None:
        self.client = client
        self.threshold = threshold

    @staticmethod
    def _get_seconds(hms: str) -> int:
        hours, minutes, seconds = map(int, hms.split(":"))
        return hours * 3600 + minutes * 60 + seconds

    def _get_pages(self) -> int:
        response = self.client.request("GET", "/user/")
        if response is None:
            return 0
        total = BeautifulSoup(response.text, "html.parser").find("div", class_="total")
        if total is not None and (match := re.search(r"/\s*(\d+)", total.get_text())):
            return int(match.group(1))
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
        for page in range(1, self._get_pages() + 1):
            response = self.client.request("GET", f"/user/index?page={page}")
            if response is None:
                continue
            for course in self._handle_courses(response.text):
                for item in self._get_video_items(course):
                    try:
                        progress = float(item.get("progress", 1))
                        if progress >= self.threshold:
                            continue
                        total = self._get_seconds(item.get("videoDuration", "00:00:00"))
                        watched = int(item.get("duration", 0))
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        continue
                    yield Video(
                        name=str(item.get("name", "")),
                        id=str(item.get("id", "")),
                        course=course,
                        total_duration=total,
                        watched_duration=watched,
                        progress=progress,
                    )
