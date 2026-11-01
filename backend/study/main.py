import logging
import os

from celery import Celery

from auto import Config, CourseBot, User
from urls import get_urls


celery_app = Celery(
    "study",
    broker=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
)
celery_app.conf.update(
    task_default_queue="study",
    task_routes={"study.*": {"queue": "study"}},
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    result_backend=None,
)


@celery_app.task(name="study.start", ignore_result=True)
def start(credentials: dict[str, str]) -> None:
    for domain in get_urls():
        run_domain.apply_async(
            args=[domain, credentials],
            queue="study",
            argsrepr=f"({domain!r}, <credentials>)",
        )


@celery_app.task(name="study.run_domain", ignore_result=True)
def run_domain(domain: str, credentials: dict[str, str]) -> None:
    user = User(**credentials)
    task_id = run_domain.request.id
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
