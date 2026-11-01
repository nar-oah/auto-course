import logging
from typing import Optional
from dataclasses import dataclass
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from requests import Response
from urllib3.util.retry import Retry


@dataclass
class StudyInfo:
    video_id: str
    study_id: int
    time: int


class Call:
    def __init__(self, url: str) -> None:
        self.url = url
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["POST", "GET", "OPTIONS"],
            )
        )
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) Gecko/20100101 Firefox/145.0",
                "Origin": url,
                "Connection": "keep-alive",
            }
        )

    def request(self, method: str, local: str, **kwargs) -> Optional[Response]:
        try:
            return self.session.request(method, self.url + local, timeout=15, **kwargs)
        except RequestException as e:
            logging.error(f"重试次数耗尽: {e}")
            return None

    def mod_referer(self, node_id: str) -> None:
        self.session.headers["Referer"] = f"{self.url}/user/node?nodeId={node_id}"

    def get_captcha(self, local: str) -> Optional[Response]:
        return self.request("get", local, headers={"Accept": "image/webp,*/*"})

    def get_page(self, local: str = "/") -> Optional[Response]:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,zh-HK;q=0.7,en-US;q=0.6,en;q=0.5",
            "Upgrade-Insecure-Requests": "1",
        }
        return self.request("get", local, headers=headers)

    def post_login(self, username: str, password: str, code: str) -> Optional[Response]:
        local: str = "/user/login"
        payload = {
            "username": username,
            "password": password,
            "code": code,
            "redirect": "",
        }
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.url + local,
        }
        return self.request("post", local, data=payload, headers=headers)

    def post_online(self) -> Optional[Response]:
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        return self.request("post", "/user/online", headers=headers)

    def post_study(self, info: StudyInfo, code: str = "") -> Optional[Response]:
        payload = {
            "nodeId": info.video_id,
            "studyId": str(info.study_id),
            "studyTime": str(info.time),
        }
        if code != "":
            payload["code"] = code
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }
        return self.request("post", "/user/node/study", data=payload, headers=headers)
