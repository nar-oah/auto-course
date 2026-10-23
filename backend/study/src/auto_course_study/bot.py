from collections.abc import Callable
import math
import threading
import time
from typing import Protocol

from auto_course_contracts import Credentials
import httpx

from .catalog import VideoList
from .http import StudyClient, json_object
from .models import Config, StudyInfo, Video
from .trace import get_trace


class CaptchaRecognizer(Protocol):
    def classification(self, image: bytes) -> str: ...


class StudyAborted(RuntimeError):
    """The domain cannot safely continue submitting progress."""


class CourseBot:
    def __init__(
        self,
        client: StudyClient,
        credentials: Credentials,
        *,
        config: Config = Config(),
        ocr: CaptchaRecognizer | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.credentials = credentials
        self.config = config
        self.ocr = ocr
        self.sleep = sleep
        self.video_list = VideoList(client, config.finish_progress)
        self.trace = get_trace(client)
        self._trace_login_phase = "initial"
        self._trace_video_id: str | None = None
        self._trace_progress_context: dict[str, object] = {}
        self._trace_retry_cause = "initial"
        self._trace_abort_result = ("progress_rejected", "progress_aborted")

    def recognize_captcha(self, path: str) -> str | None:
        started = self.trace.clock()
        self.trace.event("captcha_start", purpose="progress" if path == "/service/code/aa" else "login")
        try:
            response = self.client.get_captcha(f"{path}?r={time.time()}")
            if response is None or not response.is_success:
                self.trace.event("captcha_result", acquired=False, ocr_result=False, duration=self.trace.elapsed(started), response=self.trace.response(response))
                return None
            if self.ocr is None:
                # Loading ONNX/OCR is only needed inside the domain worker.
                import ddddocr

                self.ocr = ddddocr.DdddOcr(show_ad=False)
            code = self.ocr.classification(response.content)
            self.trace.event("captcha_result", acquired=True, ocr_result=isinstance(code, str) and bool(code), duration=self.trace.elapsed(started))
            return code if isinstance(code, str) else None
        except Exception as exc:
            self.trace.event("captcha_exception", exception_type=type(exc).__name__, duration=self.trace.elapsed(started))
            raise

    def login(self) -> bool:
        self.trace.event("login_start", login_phase=self._trace_login_phase)
        with self.trace.context(login_phase=self._trace_login_phase, request_kind="login"):
            code = self.recognize_captcha("/service/code")
            if not code:
                self.trace.event("login_result", login_phase=self._trace_login_phase, accepted=False, reason="captcha_unavailable")
                return False
            response = self.client.post_login(
                self.credentials.username, self.credentials.password, code
            )
            if response is None or not response.is_success:
                self.trace.event("login_result", login_phase=self._trace_login_phase, accepted=False, reason="http_unconfirmed", response=self.trace.response(response))
                return False
            status = json_object(response).get("status")
            # Preserve the old login's integer success flag without accepting
            # arbitrary truthy strings, numbers or containers as authentication.
            accepted = isinstance(status, (bool, int)) and status == 1
            self.trace.event("login_result", login_phase=self._trace_login_phase, accepted=accepted, reason="confirmed" if accepted else "business_unconfirmed", response=self.trace.response(response))
            return accepted

    def _send_online_pings(self, stop: threading.Event) -> None:
        self.trace.event("heartbeat_thread_started")
        exit_reason = "stop_requested"
        try:
            while not stop.is_set():
                started = self.trace.clock()
                self.trace.event("heartbeat_start")
                with self.trace.context(request_kind="heartbeat", heartbeat_video=self.trace.token("video", self._trace_video_id)):
                    response = self.client.post_online(stop=stop)
                self.trace.event("heartbeat_result", duration=self.trace.elapsed(started), response=self.trace.response(response))
                stop.wait(self.config.online_interval)
        except Exception as exc:
            exit_reason = "exception"
            self.trace.anomaly("heartbeat_exception", "heartbeat", exception_type=type(exc).__name__)
            raise
        finally:
            self.trace.event("heartbeat_thread_exit", reason=exit_reason)

    @staticmethod
    def _study_id(response: object) -> int | None:
        if not isinstance(response, dict) or response.get("status") is not True:
            return None
        study_id = response.get("studyId")
        if isinstance(study_id, bool):
            return None
        if isinstance(study_id, int) and study_id >= 0:
            return study_id
        if isinstance(study_id, str) and study_id.isdecimal():
            try:
                return int(study_id)
            except ValueError:
                return None
        return None

    @staticmethod
    def _progress_data(response: httpx.Response | None) -> dict[str, object]:
        if response is None:
            return {}
        if 400 <= response.status_code < 500:
            raise StudyAborted("Progress submission was rejected")
        return json_object(response) if response.is_success else {}

    def _submit_progress(self, info: StudyInfo) -> int | None:
        self._trace_abort_result = ("progress_rejected", "progress_aborted")
        submission = self.trace.progress_start(info, kind="normal", cause=self._trace_retry_cause, **self._trace_progress_context)
        with self.trace.context(request_kind="progress", progress_id=submission, submission_kind="normal"):
            response = self.client.post_study(info)
        try:
            data = self._progress_data(response)
        except StudyAborted:
            self.trace.progress_end(submission, response=response, decision="exit_http_rejected", confirmed=False)
            self._trace_abort_result = ("progress_rejected", "http_rejected")
            self.trace.anomaly("http_rejected", "progress")
            self.trace.outcome("progress", "http_rejected", "progress_rejected")
            raise
        if data.get("status") is not True and data.get("need_code", 0) == 1:
            self.trace.progress_end(submission, response=response, data=data, decision="captcha_supplement", confirmed=False)
            code = self.recognize_captcha("/service/code/aa")
            if not code:
                self._trace_abort_result = ("progress_unconfirmed", "captcha_unavailable")
                self.trace.anomaly("captcha_unavailable", "progress_captcha")
                self.trace.outcome("progress_captcha", "captcha_unavailable", "progress_unconfirmed")
                raise StudyAborted("Unable to recover the progress captcha")
            # A captcha permits one supplement of this payload. Its response must
            # confirm a studyId before the loop can treat it as accepted.
            submission = self.trace.progress_start(info, kind="captcha", next_time=self._trace_progress_context.get("next_time"), failures=self._trace_progress_context.get("failures"), actual_wait=0, cause="captcha")
            with self.trace.context(request_kind="progress", progress_id=submission, submission_kind="captcha"):
                response = self.client.post_study(info, code)
            try:
                data = self._progress_data(response)
            except StudyAborted:
                self.trace.progress_end(submission, response=response, decision="exit_http_rejected", confirmed=False)
                self._trace_abort_result = ("progress_rejected", "captcha_http_rejected")
                self.trace.anomaly("captcha_http_rejected", "progress_captcha")
                self.trace.outcome("progress_captcha", "captcha_http_rejected", "progress_rejected")
                raise
        if data.get("status") is True:
            study_id = self._study_id(data)
            self.trace.progress_end(submission, response=response, data=data, decision="confirmed" if study_id is not None else "retry_invalid_study_id", confirmed=study_id is not None)
            self._trace_retry_cause = "scheduled" if study_id is not None else "invalid_study_id"
            if study_id is None:
                self.trace.anomaly("invalid_study_id", "progress")
            return study_id
        if data.get("offline", 0) == 1:
            self.trace.progress_end(submission, response=response, data=data, decision="restore_login", confirmed=False)
            self._trace_login_phase = "recovery"
            try:
                logged_in = self.login()
            finally:
                self._trace_login_phase = "initial"
            if not logged_in:
                self._trace_abort_result = ("login_failed", "recovery_login_failed")
                self.trace.anomaly("recovery_login_failed", "recovery_login")
                self.trace.outcome("recovery_login", "recovery_login_failed", "login_failed")
                raise StudyAborted("Unable to restore the login session")
            # Login restores cookies, not confirmation of the previous progress.
            # The next submission belongs to the next timed loop iteration.
            self._trace_retry_cause = "relogin"
            self.trace.anomaly("progress_offline", "progress")
            return None
        if data.get("need_code", 0) == 1:
            self.trace.progress_end(submission, response=response, data=data, decision="retry_captcha_unconfirmed", confirmed=False)
            self._trace_retry_cause = "captcha_unconfirmed"
            self.trace.anomaly("captcha_unconfirmed", "progress_captcha")
            return None
        if data.get("status") is False:
            self.trace.progress_end(submission, response=response, data=data, decision="exit_business_rejected", confirmed=False)
            self._trace_abort_result = ("progress_rejected", "business_rejected")
            self.trace.anomaly("business_rejected", "progress")
            self.trace.outcome("progress", "business_rejected", "progress_rejected")
            raise StudyAborted("Progress submission was rejected")
        self.trace.progress_end(submission, response=response, data=data, decision="retry_unconfirmed", confirmed=False)
        self._trace_retry_cause = "unconfirmed"
        self.trace.anomaly("progress_unconfirmed", "progress")
        return None

    def update_study_progress(self, video: Video) -> bool:
        finish_time = math.ceil(
            (video.total_duration - video.watched_duration) * self.config.finish_progress
        )
        if finish_time <= 1:
            return True
        stop = threading.Event()
        online_thread = threading.Thread(
            target=self._send_online_pings, args=(stop,), daemon=True
        )
        online_thread.start()
        try:
            study_id = 0
            study_time = 1
            next_time = 0
            failures = 0
            while study_time < finish_time and failures < self.config.max_failures:
                self.sleep(next_time)
                # Keep the scheduled wait without submitting past the target.
                study_time = min(study_time + next_time, finish_time)
                info = StudyInfo(video.id, study_id, study_time)
                try:
                    next_id = self._submit_progress(info)
                except StudyAborted:
                    return False
                if next_id is None:
                    failures += 1
                else:
                    study_id = next_id
                # Like the original loop, submit the boundary once, then exit.
                # An unconfirmed final submission must also stop the domain.
                if study_time >= finish_time:
                    return next_id is not None
                # Preserve the scheduled wait on failure/relogin, as before.
                # The initial zero wait must never carry into an uncertain retry.
                if next_id is not None or next_time == 0:
                    next_time = min(
                        self.config.study_interval, finish_time - study_time
                    )
            return False
        finally:
            stop.set()
            # Do not close/reuse the domain Client while a heartbeat is in flight.
            online_thread.join()

    def run(self) -> None:
        if not self.login():
            return
        for video in self.video_list.get_videos():
            if not self.update_study_progress(video):
                return
