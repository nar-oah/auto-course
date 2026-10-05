from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from typing import Any

import httpx

from .models import StudyInfo


RETRY_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) "
    "Gecko/20100101 Firefox/145.0"
)


def json_object(response: httpx.Response | None) -> dict[str, Any]:
    if response is None:
        return {}
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class StudyClient:
    """One HTTP client and cookie jar for the whole domain task."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.sleep = sleep
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "User-Agent": USER_AGENT,
                "Origin": self.base_url,
                "Connection": "keep-alive",
            },
            timeout=15,
            follow_redirects=True,
            transport=transport,
        )

    def __enter__(self) -> "StudyClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.client.close()

    @staticmethod
    def _retry_after(response: httpx.Response | None) -> float | None:
        if response is None:
            return None
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                return None

    def request(self, method: str, local: str, **kwargs: Any) -> httpx.Response | None:
        # Match the old Session's three retries, including POST requests.
        for attempt in range(4):
            response = None
            try:
                response = self.client.request(method, local, **kwargs)
            except httpx.RequestError:
                pass
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
            if attempt == 3:
                return None
            delay = self._retry_after(response)
            if delay is None:
                delay = 0 if attempt == 0 else 2**attempt
            if delay:
                self.sleep(delay)
        return None

    def get_captcha(self, local: str) -> httpx.Response | None:
        return self.request("GET", local, headers={"Accept": "image/webp,*/*"})

    def get_page(self, local: str = "/") -> httpx.Response | None:
        return self.request(
            "GET",
            local,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,zh-HK;q=0.7,en-US;q=0.6,en;q=0.5",
                "Upgrade-Insecure-Requests": "1",
            },
        )

    def post_login(self, username: str, password: str, code: str) -> httpx.Response | None:
        return self.request(
            "POST",
            "/user/login",
            data={"username": username, "password": password, "code": code, "redirect": ""},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/user/login",
            },
        )

    def post_online(self) -> httpx.Response | None:
        return self.request(
            "POST",
            "/user/online",
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )

    def post_study(self, info: StudyInfo, code: str = "") -> httpx.Response | None:
        payload = {
            "nodeId": info.video_id,
            "studyId": str(info.study_id),
            "studyTime": str(info.time),
        }
        if code:
            payload["code"] = code
        return self.request(
            "POST",
            "/user/node/study",
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
