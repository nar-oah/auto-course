from itertools import chain
import logging
import re
from dataclasses import dataclass
from typing import Iterator, List, Optional
from requests import Response
from bs4 import BeautifulSoup
from response import Call


@dataclass
class Course:
    id: str
    name: str


@dataclass
class Video:
    name: str
    id: str
    course: Course
    total_duration: int
    watched_duration: int
    progress: float


class VideoList:
    def __init__(self, response: Call, threshold: float) -> None:
        self.response: Call = response
        self.threshold: float = threshold
        self.user: str = "/user/"
        self.course: str = f"/user/index?page={{page}}"
        self.video: str = f"/user/study_record.json?courseId={{id}}&page={{page}}"

    def _get_seconds(self, hms: str) -> int:
        parts = list(map(int, hms.split(":")))
        h, m, s = parts
        return h * 3600 + m * 60 + s

    def _get_pages(self) -> Optional[int]:
        response = self.response.request("get", self.user)
        if response is not None:
            soup = BeautifulSoup(response.text, "html.parser")
            total_div = soup.find("div", class_="total")
            if total_div and (match := re.search(r"/\s*(\d+)", total_div.text)):
                return int(match.group(1))
        return None

    def _handle_courses(self, html: str) -> List[Course]:
        soup = BeautifulSoup(html, "html.parser")
        return [
            Course(id=m.group(1), name=link.text.strip())
            for item in soup.find_all("div", class_="name")
            if (link := item.find("a"))
            and isinstance(href := link.get("href"), str)
            and (m := re.search(r"courseId=(\d+)", href))
        ]

    def _get_responses(self, page_count: int, local: str) -> Iterator[Response]:
        for page_num in range(1, page_count + 1):
            response = self.response.request("get", local.format(page=page_num))
            if response is not None:
                yield response
            else:
                continue

    def _handle_videos(self, course: Course) -> Iterator[List[dict]]:
        url = self.video.replace("{id}", str(course.id))
        response = self.response.request("get", url.format(page=1))
        if response is not None:
            data = response.json()
            page_count = data.get("pageInfo", {}).get("pageCount", 1)
            yield from (
                response.json().get("list", [])
                for response in self._get_responses(page_count, url)
                if response.json()
            )

    def get_videos(self) -> Iterator[Video]:
        match self._get_pages():
            case int(pages):
                logging.info("开始获取课程列表...")
            case None:
                logging.error("无法解析课程总页数，请检查Cookie或页面结构。")
                pages: int = 0
        responses: Iterator[Response] = self._get_responses(pages, self.course)
        course_lists: Iterator[List[Course]] = (
            self._handle_courses(response.text) for response in responses
        )
        return (
            Video(
                name=item.get("name", ""),
                id=item.get("id", ""),
                course=course,
                total_duration=self._get_seconds(item.get("videoDuration", "00:00:00")),
                watched_duration=int(item.get("duration", 0)),
                progress=float(item.get("progress", 0)),
            )
            for course in chain.from_iterable(course_lists)
            for item in chain.from_iterable(self._handle_videos(course))
            if float(item.get("progress", 1)) < self.threshold
        )
