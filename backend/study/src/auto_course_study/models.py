from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    study_interval: int = 30
    online_interval: int = 120
    finish_progress: float = 0.91
    max_failures: int = 5


@dataclass(frozen=True)
class Course:
    id: str
    name: str


@dataclass(frozen=True)
class Video:
    name: str
    id: str
    course: Course
    total_duration: int
    watched_duration: int
    progress: float


@dataclass(frozen=True)
class StudyInfo:
    video_id: str
    study_id: int
    time: int
