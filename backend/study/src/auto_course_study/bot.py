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

    def recognize_captcha(self, path: str) -> str | None:
        response = self.client.get_captcha(f"{path}?r={time.time()}")
        if response is None or not response.is_success:
            return None
        if self.ocr is None:
            # Loading ONNX/OCR is only needed inside the domain worker.
            import ddddocr

            self.ocr = ddddocr.DdddOcr(show_ad=False)
        code = self.ocr.classification(response.content)
        return code if isinstance(code, str) else None

    def login(self) -> bool:
        code = self.recognize_captcha("/service/code")
        if not code:
            return False
        response = self.client.post_login(
            self.credentials.username, self.credentials.password, code
        )
        if response is None or not response.is_success:
            return False
        status = json_object(response).get("status")
        # Preserve the old login's integer success flag without accepting
        # arbitrary truthy strings, numbers or containers as authentication.
        return isinstance(status, (bool, int)) and status == 1

    def _send_online_pings(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.client.post_online(stop=stop)
            stop.wait(self.config.online_interval)

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
        data = self._progress_data(self.client.post_study(info))
        if data.get("status") is not True and data.get("need_code", 0) == 1:
            code = self.recognize_captcha("/service/code/aa")
            if not code:
                raise StudyAborted("Unable to recover the progress captcha")
            # A captcha permits one supplement of this payload. Its response must
            # confirm a studyId before the loop can treat it as accepted.
            data = self._progress_data(self.client.post_study(info, code))
        if data.get("status") is True:
            return self._study_id(data)
        if data.get("offline", 0) == 1:
            if not self.login():
                raise StudyAborted("Unable to restore the login session")
            # Login restores cookies, not confirmation of the previous progress.
            # The next submission belongs to the next timed loop iteration.
            return None
        if data.get("need_code", 0) == 1:
            return None
        if data.get("status") is False:
            raise StudyAborted("Progress submission was rejected")
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
