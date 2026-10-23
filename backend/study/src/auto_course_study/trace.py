"""Temporary, opt-in, in-memory diagnostics. Never used to decide study behavior.

Only this module emits logs. Identifiers are keyed per task; bodies, headers,
credentials, captcha text, titles and arbitrary server messages are never logged.
"""

from collections import Counter, deque
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import logging
import math
import os
import threading
import time
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)
CAPACITY = 256
_PATHS = frozenset({
    "/", "/user/", "/user/index", "/user/login", "/user/online",
    "/user/node/study", "/user/study_record.json", "/service/code",
    "/service/code/aa",
})
_BUSINESS_WORDS = frozenset({
    "true", "false", "success", "failure", "failed", "error", "ok",
    "denied", "rejected", "offline", "unauthorized", "forbidden",
})


def _guard(default=None):
    """Diagnostics, including a broken log handler, cannot break a task."""
    def decorate(function):
        @wraps(function)
        def call(self, *args, **kwargs):
            if not self.enabled:
                return default
            try:
                return function(self, *args, **kwargs)
            except Exception:
                return default
        return call
    return decorate


class StudyTrace:
    def __init__(self, domain: str, task_id: str | None, *, enabled: bool = True):
        self.enabled = enabled
        self.domain = (urlsplit("//" + domain).hostname or "unknown")[:253]
        self.task_id = str(task_id)[:128] if task_id is not None else None
        self.events = deque(maxlen=CAPACITY)
        self.lock = threading.RLock()
        self.local = threading.local()
        self.salt = os.urandom(16) if enabled else b""
        self.started = time.monotonic()
        self.started_at = self._utc()
        self.counts = Counter()
        self.http = Counter()
        self.request_windows = deque(maxlen=32)
        self.active_requests = {}
        self.request_sequence = 0
        self.progress_sequence = 0
        self.video_sequence = 0
        self.video = None
        self.previous_video_end = None
        self.previous_progress = None
        self.first_anomaly = None
        self.last_progress = None
        self.last_heartbeat = None
        self.final_stage = "task"
        self.reason = "returned_without_confirmed_outcome"
        self.result = "early_return"
        self.secrets = set()
        self.finished = False
        self.event("task_start")

    @staticmethod
    def _utc():
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @_guard(0.0)
    def clock(self):
        return time.monotonic()

    @_guard(0.0)
    def elapsed(self, start):
        return max(0.0, time.monotonic() - start)

    @_guard("redacted")
    def token(self, kind, value):
        # No identifier map that could grow with the number of videos/requests.
        if kind == "study" and isinstance(value, (int, str)) and str(value).isdecimal():
            value = str(int(value))
        digest = hashlib.blake2b(str(value).encode(), key=self.salt, digest_size=8).hexdigest()
        return f"{kind}:{digest}"

    @_guard()
    def protect(self, *values):
        with self.lock:
            self.secrets.update(v for v in values if isinstance(v, str) and v)

    def _clean(self, value, depth=0):
        if depth > 6:
            return "[depth limit]"
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return round(value, 6) if math.isfinite(value) else str(value)
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[redacted]")
            return value[:160]
        if isinstance(value, dict):
            return {str(k)[:64]: self._clean(v, depth + 1) for k, v in list(value.items())[:48]}
        if isinstance(value, (tuple, list)):
            return [self._clean(v, depth + 1) for v in value[:32]]
        return {"type": type(value).__name__[:48]}

    def _record(self, name, fields):
        self.counts[name] += 1
        self.counts["events_total"] += 1
        record = {
            "event": name, "seq": self.counts["events_total"], "at": self._utc(),
            "elapsed_seconds": round(time.monotonic() - self.started, 6),
            "video": self.video, **getattr(self.local, "context", {}),
            **self._clean(fields),
        }
        self.events.append(record)
        return record

    @_guard()
    def event(self, name, **fields):
        with self.lock:
            return self._record(name, fields)

    @_guard()
    def anomaly(self, reason, stage, **fields):
        with self.lock:
            record = self._record("anomaly", {"reason": reason, "stage": stage, **fields})
            if self.first_anomaly is None:
                self.first_anomaly = record

    @_guard()
    def outcome(self, stage, reason, result):
        with self.lock:
            self.final_stage, self.reason, self.result = stage, reason, result
            self._record("task_outcome", {"stage": stage, "reason": reason, "result": result})

    def context(self, **fields):
        if not self.enabled:
            return nullcontext()
        return self._context(fields)

    @contextmanager
    def _context(self, fields):
        previous = getattr(self.local, "context", {})
        try:
            self.local.context = {**previous, **self._clean(fields)}
        except Exception:
            pass
        try:
            yield
        finally:
            try:
                self.local.context = previous
            except Exception:
                pass

    @_guard()
    def video_start(self, video_id, total_duration, watched_duration, progress, finish_time):
        with self.lock:
            self.video_sequence += 1
            self.video = self.token("video", video_id)
            self.video_started = time.monotonic()
            self._record("video_start", {
                "video_ordinal": self.video_sequence, "total_duration": total_duration,
                "watched_duration": watched_duration, "raw_progress": progress,
                "finish_time": finish_time,
                "since_previous_video_end": None if self.previous_video_end is None else self.elapsed(self.previous_video_end),
            })

    @_guard()
    def video_end(self, result, reason):
        with self.lock:
            self.previous_video_end = time.monotonic()
            self._record("video_end", {"video_ordinal": self.video_sequence, "result": result,
                                      "reason": reason, "duration_seconds": self.elapsed(self.video_started)})

    @_guard({})
    def study_id(self, value, *, present=True):
        valid = (isinstance(value, int) and not isinstance(value, bool) and value >= 0) or (
            isinstance(value, str) and value.isdecimal()
        )
        return {"present": present, "type": type(value).__name__, "valid": valid,
                "ref": self.token("study", value) if valid else None,
                "zero": valid and str(value).strip("0") == ""}

    def _business_value(self, value):
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return self._clean(value)
        if isinstance(value, str) and len(value) <= 32:
            if value.lower() in _BUSINESS_WORDS or value.isdecimal():
                return self._clean(value)
        # Arbitrary server strings can contain names, credentials or captcha text.
        return {"type": type(value).__name__, "ref": self.token("business", value)}

    @_guard({})
    def response(self, response):
        if response is None:
            return {"received": False, "json_parsed": False}
        fields = {"received": True}
        if isinstance(response, dict):
            data = response
        else:
            fields["http_status"] = response.status_code
            try:
                data = response.json()
            except ValueError:
                return {**fields, "json_parsed": False}
        fields["json_parsed"] = True
        fields["json_object"] = isinstance(data, dict)
        if not isinstance(data, dict):
            return fields
        for name in ("status", "need_code", "offline", "error_code", "errorCode", "errcode", "errno", "resultCode"):
            if name in data:
                fields[name] = self._business_value(data[name])
        # A generic 'code' can be an echoed captcha. Only expose numeric codes.
        if isinstance(data.get("code"), int) and not isinstance(data["code"], bool):
            fields["code"] = data["code"]
        fields["studyId"] = self.study_id(data.get("studyId"), present="studyId" in data)
        return fields

    @_guard()
    def progress_start(self, info, kind="normal", next_time=None, failures=None, actual_wait=None, cause=None):
        with self.lock:
            self.progress_sequence += 1
            now = time.monotonic()
            video = self.token("video", info.video_id)
            previous = self.previous_progress
            fields = {
                "submission": self.progress_sequence, "video": video, "kind": kind,
                "studyTime": info.time, "studyId": self.study_id(info.study_id),
                "next_time": next_time, "failures": failures, "actual_wait": actual_wait,
                "cause": cause, "video_changed": previous is not None and previous[1] != video,
                "since_previous_submission": None if previous is None else now - previous[0],
                "repeated_study_time": previous is not None and previous[1:] == (video, info.time),
            }
            self.previous_progress = (now, video, info.time)
            self._record("progress_start", fields)
            return {**fields, "started": now}

    @_guard()
    def progress_end(self, token, response=None, data=None, decision="unknown", confirmed=None):
        if token is None:
            return
        with self.lock:
            self.last_progress = self._record("progress_end", {
                **{k: v for k, v in token.items() if k != "started"},
                "duration_seconds": self.elapsed(token["started"]),
                "response": self.response(response if response is not None else data),
                "decision": decision, "confirmed": confirmed,
            })

    @staticmethod
    def _path(local):
        path = urlsplit(str(local)).path
        return path if path in _PATHS else "/[redacted]"

    @_guard()
    def http_start(self, method, local):
        with self.lock:
            self.request_sequence += 1
            now = time.monotonic()
            path = self._path(local)
            token = {"request": self.request_sequence, "method": method if method in {"GET", "POST", "PUT", "DELETE", "HEAD", "PATCH", "OPTIONS"} else "OTHER",
                     "path": path, "started": now, "started_at": self._utc(), "video": self.video,
                     **getattr(self.local, "context", {}), "overlaps": []}
            for active in self.active_requests.values():
                if {path, active["path"]} == {"/user/online", "/user/node/study"}:
                    self.http["heartbeat_progress_overlaps"] += 1
                    overlap = {"request": active["request"], "video": active["video"], "same_video": active["video"] == token["video"]}
                    token["overlaps"].append(overlap)
                    if len(active["overlaps"]) < 16:
                        active["overlaps"].append({"request": token["request"], "video": token["video"], "same_video": overlap["same_video"]})
            self.active_requests[token["request"]] = token
            self.http["attempts"] += 1
            self.http[f"path:{path}"] += 1
            minute = int((now - self.started) // 60)
            if not self.request_windows or self.request_windows[-1]["minute"] != minute:
                self.request_windows.append({"minute": minute, "started_at": token["started_at"], "attempts": 0, "errors": 0, "http_429": 0, "http_5xx": 0})
            self.request_windows[-1]["attempts"] += 1
            self._record("http_start", {k: v for k, v in token.items() if k != "started"})
            return token

    @_guard()
    def http_end(self, token, response=None, error=None, disposition="returned"):
        if token is None:
            return
        with self.lock:
            self.active_requests.pop(token["request"], None)
            status = response.status_code if response is not None else None
            self.http[f"status:{status}"] += 1
            if error is not None:
                self.http[f"exception:{type(error).__name__}"] += 1
            window = self.request_windows[-1]
            window["errors"] += int(error is not None)
            window["http_429"] += int(status == 429)
            window["http_5xx"] += int(status is not None and status >= 500)
            history = [] if response is None else [r.status_code for r in response.history]
            self.http["redirects"] += len(history)
            fields = {
                **{k: v for k, v in token.items() if k != "started"},
                "duration_seconds": self.elapsed(token["started"]), "http_status": status,
                "response": self.response(response), "disposition": disposition,
                "exception_type": None if error is None else type(error).__name__,
                "timeout_type": type(error).__name__ if error is not None and "Timeout" in type(error).__name__ else None,
                "redirect_count": len(history), "redirect_statuses": history[:16],
                "final_path": None if response is None else self._path(response.url),
                "redirect_paths": [] if response is None else [self._path(r.url) for r in response.history[:16]],
                "retry_after_present": response is not None and "Retry-After" in response.headers,
            }
            self.http["json_parsed" if fields["response"].get("json_parsed") else "json_unparsed"] += 1
            record = self._record("http_end", fields)
            if token["path"] == "/user/online":
                self.last_heartbeat = record
            if error is not None or (status is not None and status >= 400):
                self.anomaly("http_exception" if error is not None else "http_error_status", "http",
                             request=token["request"], http_status=status,
                             exception_type=None if error is None else type(error).__name__)

    @_guard()
    def retry(self, attempt, reason, delay, actual_wait=None, interrupted=False):
        with self.lock:
            self.http["retries"] += 1
            self._record("http_retry", {"retry": attempt, "reason": reason, "planned_wait": delay,
                                       "actual_wait": actual_wait, "interrupted": interrupted})

    @_guard()
    def finish(self, celery_return="returned"):
        with self.lock:
            if self.finished:
                return
            self.finished = True
            self._record("task_end", {"stage": self.final_stage, "reason": self.reason, "result": self.result})
            summary = {
                "started_at": self.started_at, "ended_at": self._utc(),
                "duration_seconds": self.elapsed(self.started), "final_stage": self.final_stage,
                "reason": self.reason, "result": self.result, "celery_return": celery_return,
                "first_anomaly": self.first_anomaly, "counts": dict(self.counts),
                "http": dict(self.http), "request_windows": list(self.request_windows),
                "last_progress": self.last_progress, "last_heartbeat": self.last_heartbeat,
                "events_total": self.counts["events_total"], "events_retained": len(self.events),
                "events_dropped": max(0, self.counts["events_total"] - len(self.events)),
                "courses_discovered": self.counts["course_discovered"],
                "pending_videos_discovered": self.counts["pending_video_discovered"],
                "catalog_complete": self.counts["catalog_complete"] > 0,
            }
            payload = {"type": "study_trace", "task_id": self.task_id, "domain": self.domain, "summary": summary}
            abnormal = self.first_anomaly is not None or self.result not in {"completed", "empty_video_list"}
            if abnormal:
                payload["events"] = list(self.events)
            output = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            logger.log(logging.WARNING if abnormal else logging.INFO, output)


_DISABLED = StudyTrace("unknown", None, enabled=False)
_CURRENT = ContextVar("study_trace", default=_DISABLED)


def get_trace(client=None):
    try:
        trace = getattr(client, "trace", None) if client is not None else None
        return trace if isinstance(trace, StudyTrace) else _CURRENT.get()
    except Exception:
        return _DISABLED


@contextmanager
def task_trace(domain, task_id):
    trace = _DISABLED
    context_token = None
    try:
        if os.getenv("STUDY_TRACE") == "1":
            trace = StudyTrace(domain, task_id)
        context_token = _CURRENT.set(trace)
    except Exception:
        pass
    celery_return = "returned"
    try:
        yield trace
    except BaseException as error:
        celery_return = "raised"
        trace.anomaly("uncaught_exception", "task", exception_type=type(error).__name__)
        trace.outcome("task", "uncaught_exception", "exception")
        raise
    finally:
        trace.finish(celery_return)
        if context_token is not None:
            try:
                _CURRENT.reset(context_token)
            except Exception:
                pass
