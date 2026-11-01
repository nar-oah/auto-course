from json import JSONDecodeError
import logging
import math
import time
import threading
import time
from typing import Iterator, Optional
import ddddocr
from requests import Response
from dataclasses import dataclass
from list import Video, VideoList
from response import Call, StudyInfo


@dataclass(frozen=True)
class Config:
    STUDY_INTERVAL: int = 30
    ONLINE_INTERVAL: int = 120
    FINISH_PROGRESS: float = 0.91


@dataclass
class User:
    username: str
    password: str


class CourseBot:
    def __init__(self, config: Config, url: str, user: User, logger: logging.Logger):
        self.config = config
        self.base_url = f"https://{url}".rstrip("/")
        self.username = user.username
        self.password = user.password
        self.response = Call(url=self.base_url)
        self.video_list = VideoList(self.response, Config.FINISH_PROGRESS)
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.stop_online_thread = threading.Event()
        self.logger = logger

    def recognize_captcha(self, captcha_path: str) -> Optional[str]:
        local = f"{captcha_path}?r={time.time()}"
        response = self.response.get_captcha(local)
        if response is not None:
            code = self.ocr.classification(response.content)
            if isinstance(code, str):
                self.logger.info(f"验证码识别成功: {code}")
                return code
        return None

    def login(self) -> bool:
        self.logger.info("开始执行登录流程...")
        code = self.recognize_captcha("/service/code")
        if not code:
            self.logger.error("无法获取或识别验证码，登录失败。")
            return False
        response = self.response.post_login(self.username, self.password, code)
        if response is not None:
            data: dict = response.json()
            return data.get("status", False)
        return False

    def _send_online_ping(self):
        while not self.stop_online_thread.is_set():
            self.response.post_online()
            self.stop_online_thread.wait(Config.ONLINE_INTERVAL)

    def get_study_id(self, response: Response | None) -> Optional[int | str | bool]:
        if response is not None:
            try:
                content: dict = response.json()
                if content.get("status", False) is True:
                    return content.get("studyId", None)
                elif content.get("need_code", 0) == 1:
                    return self.recognize_captcha(captcha_path="/service/code/aa")
                elif content.get("offline", 0) == 1:
                    return self.login()
            except JSONDecodeError:
                self.logger.error(response)
                return None
        return None

    def update_study_progress(self, video: Video):
        online_thread = threading.Thread(target=self._send_online_ping, daemon=True)
        self.stop_online_thread.clear()
        online_thread.start()

        study_id: int = 0
        study_time: int = 1
        next_time: int = 0
        remaining_time: int = video.total_duration - video.watched_duration
        finish_time: int = math.ceil(remaining_time * Config.FINISH_PROGRESS)
        retry = 0
        while study_time < finish_time and retry < 5:
            time.sleep(next_time)
            study_time += next_time
            study_info = StudyInfo(video.id, study_id, study_time)
            response = self.response.post_study(study_info)
            match self.get_study_id(response):
                case None:
                    retry += 1
                case bool(is_login):
                    retry += 0 if is_login else 1
                case str(code):
                    self.response.post_study(study_info, code)
                case int(id):
                    study_id = id
                    next_time = min(Config.STUDY_INTERVAL, finish_time - study_time)
                    self.logger.info(f"{video.name} | {study_time}s/{finish_time}s")
        self.stop_online_thread.set()
        online_thread.join(timeout=5)
        self.logger.info(f"课程《{video.name}》处理完毕！")

    def run(self):
        self.login()
        self.logger.info("=" * 10 + " 获取待学习视频列表 " + "=" * 10)
        videos: Iterator[Video] = self.video_list.get_videos()
        self.logger.info("=" * 10 + " 开始自动学习任务 " + "=" * 10)
        for i, video in enumerate(videos):
            self.logger.info(f"[{i + 1}]《{video.name}》({video.id})")
            self.update_study_progress(video)
        self.logger.info("=" * 10 + " 所有任务已处理完毕 " + "=" * 10)
