import logging
import os

from celery import Celery

from auto import Config, CourseBot, User
from urls import get_urls


celery_app = Celery(
    "study",
    broker=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
)
celery_app.conf.update(
    task_default_queue="study",
    task_routes={"study.*": {"queue": "study"}},
)


@celery_app.task(name="study.start", ignore_result=True)
def start(credentials: dict[str, str]) -> None:
    for domain in get_urls():
        run_bot.apply_async(args=[domain, credentials], queue="study")


@celery_app.task(name="study.run_bot", bind=True, ignore_result=True)
def run_bot(self, domain: str, credentials: dict[str, str]) -> None:
    user = User(**credentials)
    task_id = self.request.id
    logger = logging.getLogger(f"{task_id}_{domain}")
    logger.setLevel(logging.INFO)

    logger.info(f"--- 任务启动 [ID:{task_id}] ---")
    logger.info(f"目标: {domain} | 用户: {user.username}")

    try:
        bot = CourseBot(Config(), domain, user, logger)
        bot.run()
    except Exception as e:
        logger.error(f"进程发生严重错误: {e}", exc_info=True)
    finally:
        logger.info("--- 任务结束 ---")
