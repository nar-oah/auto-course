from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
from threading import Event
import time
from typing import Any

import httpx

from .models import StudyInfo
from .trace import get_trace


RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 60
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
        # Keep the task's collector available to the existing heartbeat thread.
        self.trace = get_trace()
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
            delay = float(value)
            return max(0.0, delay) if math.isfinite(delay) else None
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                return max(0.0, delay) if math.isfinite(delay) else None
            except (ValueError, TypeError, OverflowError):
                return None

    def request(
        self,
        method: str,
        local: str,
        *,
        retry: bool = True,
        stop: Event | None = None,
        **kwargs: Any,
    ) -> httpx.Response | None:
        # Progress submissions are retried by the study loop after its wait.
        for attempt in range(4 if retry else 1):
            if stop is not None and stop.is_set():
                self.trace.event("http_skipped", reason="stop_set", attempt=attempt + 1)
                return None
            response = None
            request_error = None
            token = self.trace.http_start(method, local)
            try:
                response = self.client.request(method, local, **kwargs)
            except httpx.RequestError as error:
                request_error = error
            except BaseException as error:
                self.trace.http_end(token, error=error, disposition="exception_propagated")
                raise
            else:
                if not retry or response.status_code not in RETRY_STATUSES:
                    self.trace.http_end(
                        token,
                        response=response,
                        disposition="returned" if retry else "returned_without_retry",
                    )
                    return response
            if not retry or attempt == 3:
                self.trace.http_end(
                    token,
                    response=response,
                    error=request_error,
                    disposition="retries_exhausted" if retry else "failed_without_retry",
                )
                return None
            delay = self._retry_after(response)
            retry_after = delay
            if delay is None:
                delay = 2**attempt
            elif delay > MAX_RETRY_AFTER:
                # End this request instead of retrying before the server permits it.
                self.trace.http_end(
                    token, response=response, error=request_error, disposition="retry_after_limit"
                )
                self.trace.event(
                    "http_retry_after_limit", attempt=attempt + 1, retry_after=delay,
                    maximum_wait=MAX_RETRY_AFTER,
                )
                return None
            delay = max(1.0, delay)
            self.trace.http_end(
                token, response=response, error=request_error, disposition="retry_scheduled"
            )
            reason = (
                type(request_error).__name__
                if request_error is not None
                else f"http_{response.status_code}"
            )
            self.trace.event(
                "http_retry_wait_start", attempt=attempt + 1, reason=reason,
                planned_wait=delay, retry_after=retry_after,
            )
            wait_started = self.trace.clock()
            try:
                if stop is None:
                    self.sleep(delay)
                elif stop.wait(delay):
                    self.trace.retry(
                        attempt + 1, reason, delay,
                        actual_wait=self.trace.elapsed(wait_started), interrupted=True,
                    )
                    return None
            except BaseException as error:
                self.trace.retry(
                    attempt + 1, reason, delay,
                    actual_wait=self.trace.elapsed(wait_started), interrupted=True,
                )
                self.trace.event("http_retry_wait_exception", error_type=type(error).__name__)
                raise
            self.trace.retry(
                attempt + 1, reason, delay, actual_wait=self.trace.elapsed(wait_started)
            )
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

    def post_online(self, stop: Event | None = None) -> httpx.Response | None:
        return self.request(
            "POST",
            "/user/online",
            stop=stop,
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
            retry=False,
            follow_redirects=False,
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
