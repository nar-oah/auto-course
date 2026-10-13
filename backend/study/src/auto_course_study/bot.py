from collections.abc import Callable
import math
import threading
import time
from typing import Protocol

from auto_course_contracts import Credentials

from .catalog import VideoList
from .http import StudyClient, json_object
from .models import Config, StudyInfo, Video


class CaptchaRecognizer(Protocol):
    def classification(self, image: bytes) -> str: ...


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
        if response is None:
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
        return bool(json_object(response).get("status", False))

    def _send_online_pings(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.client.post_online()
            stop.wait(self.config.online_interval)

    @staticmethod
    def _study_id(response: object) -> int | None:
        if not isinstance(response, dict) or response.get("status") is not True:
            return None
        study_id = response.get("studyId")
        if isinstance(study_id, bool):
            return None
        if isinstance(study_id, int):
            return study_id
        if isinstance(study_id, str) and study_id.isdecimal():
            return int(study_id)
        return None

    def _submit_progress(self, info: StudyInfo) -> int | None:
        data = json_object(self.client.post_study(info))
        study_id = self._study_id(data)
        if study_id is not None:
            return study_id
        if data.get("need_code", 0) == 1:
            code = self.recognize_captcha("/service/code/aa")
            if code:
                return self._study_id(json_object(self.client.post_study(info, code)))
        elif data.get("offline", 0) == 1 and self.login():
            return self._study_id(json_object(self.client.post_study(info)))
        # One recovery per submission bounds repeated captcha/offline challenges.
        return None

    def update_study_progress(self, video: Video) -> None:
        finish_time = math.ceil(
            (video.total_duration - video.watched_duration) * self.config.finish_progress
        )
        if finish_time <= 1:
            return
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
            while failures < self.config.max_failures:
                self.sleep(next_time)
                study_time += next_time
                info = StudyInfo(video.id, study_id, study_time)
                next_id = self._submit_progress(info)
                if next_id is None:
                    failures += 1
                    # Retry this payload, including the final progress submission.
                    next_time = 0
                    continue
                study_id = next_id
                if study_time >= finish_time:
                    break
                next_time = min(self.config.study_interval, finish_time - study_time)
        finally:
            stop.set()
            # Do not close/reuse the domain Client while a heartbeat is in flight.
            online_thread.join()

    def run(self) -> None:
        if not self.login():
            return
        for video in self.video_list.get_videos():
            self.update_study_progress(video)
