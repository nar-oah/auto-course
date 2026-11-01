import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from kombu import Connection, Producer


STUDY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDY_DIR))

import main


CREDENTIALS = {"username": "student", "password": "secret"}


class TaskContractTests(unittest.TestCase):
    def test_registered_task_names_queue_json_and_no_results(self):
        app = main.celery_app
        self.assertEqual(app.main, "study")
        self.assertIs(app.tasks["study.start"], main.start._get_current_object())
        self.assertIs(app.tasks["study.run_domain"], main.run_domain._get_current_object())
        self.assertEqual(app.conf.task_default_queue, "study")
        self.assertEqual(app.conf.task_routes, {"study.*": {"queue": "study"}})
        self.assertEqual(app.conf.task_serializer, "json")
        self.assertEqual(app.conf.accept_content, ["json"])
        self.assertTrue(app.conf.task_ignore_result)
        self.assertFalse(app.conf.task_store_errors_even_if_ignored)
        self.assertIsNone(app.conf.result_backend)
        self.assertTrue(main.start.ignore_result)
        self.assertTrue(main.run_domain.ignore_result)

    def test_broker_default_and_environment_override(self):
        program = (
            "import json, sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "import main; "
            "print(json.dumps(main.celery_app.conf.broker_url))"
        )
        for override, expected in (
            (None, "redis://redis:6379/0"),
            ("redis://broker.example:6380/7", "redis://broker.example:6380/7"),
        ):
            with self.subTest(override=override):
                environment = os.environ.copy()
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                environment.pop("CELERY_BROKER_URL", None)
                if override is not None:
                    environment["CELERY_BROKER_URL"] = override
                output = subprocess.check_output(
                    [sys.executable, "-c", program, str(STUDY_DIR)],
                    cwd=STUDY_DIR,
                    env=environment,
                    text=True,
                )
                self.assertEqual(json.loads(output), expected)

    def test_start_fans_out_one_independent_task_per_domain(self):
        domains = ["first.example", "second.example", "third.example"]
        with patch.object(main, "get_urls", return_value=iter(domains)), patch.object(
            main.run_domain, "apply_async"
        ) as publish, patch.object(main, "CourseBot") as bot:
            result = main.start.apply(args=[CREDENTIALS], throw=True)

        self.assertTrue(result.successful())
        self.assertIsNone(result.result)
        bot.assert_not_called()
        self.assertEqual(publish.call_count, len(domains))
        for call, domain in zip(publish.call_args_list, domains):
            self.assertEqual(call.args, ())
            self.assertEqual(call.kwargs["args"], [domain, CREDENTIALS])
            self.assertEqual(call.kwargs["queue"], "study")
            self.assertNotIn("kwargs", call.kwargs)
            self.assertEqual(call.kwargs["argsrepr"], f"({domain!r}, <credentials>)")
            self.assertNotIn(CREDENTIALS["username"], call.kwargs["argsrepr"])
            self.assertNotIn(CREDENTIALS["password"], call.kwargs["argsrepr"])

    def test_start_without_domains_returns_without_publishing(self):
        with patch.object(main, "get_urls", return_value=[]), patch.object(
            main.run_domain, "apply_async"
        ) as publish:
            result = main.start.apply(args=[CREDENTIALS], throw=True)
        self.assertTrue(result.successful())
        self.assertIsNone(result.result)
        publish.assert_not_called()

    def test_api_json_message_is_accepted_by_registered_start_task(self):
        with Connection("memory://") as connection:
            queue = connection.SimpleQueue("study")
            try:
                queue.clear()
                main.celery_app.send_task(
                    "study.start",
                    args=[CREDENTIALS],
                    queue="study",
                    ignore_result=True,
                    argsrepr="(<credentials>,)",
                    producer=Producer(connection),
                )
                message = queue.get(block=False)
                self.assertEqual(message.content_type, "application/json")
                self.assertEqual(message.headers["task"], "study.start")
                self.assertEqual(message.headers["argsrepr"], "(<credentials>,)")
                self.assertEqual(message.delivery_info["routing_key"], "study")
                args, kwargs, _ = message.payload
                self.assertEqual(args, [CREDENTIALS])
                self.assertEqual(kwargs, {})
                with patch.object(main, "get_urls", return_value=["first.example"]), patch.object(
                    main.run_domain, "apply_async"
                ) as publish:
                    result = main.celery_app.tasks[message.headers["task"]].apply(
                        args=args, kwargs=kwargs, task_id=message.headers["id"], throw=True
                    )
                self.assertTrue(result.successful())
                self.assertIsNone(result.result)
                self.assertEqual(publish.call_args.kwargs["args"], ["first.example", CREDENTIALS])
                message.ack()
            finally:
                queue.close()

    def test_domain_task_has_compatible_json_wire_arguments(self):
        with Connection("memory://") as connection:
            queue = connection.SimpleQueue("study")
            try:
                queue.clear()
                main.run_domain.apply_async(
                    args=["first.example", CREDENTIALS],
                    queue="study",
                    argsrepr="('first.example', <credentials>)",
                    producer=Producer(connection),
                )
                message = queue.get(block=False)
                self.assertEqual(message.content_type, "application/json")
                self.assertEqual(message.headers["task"], "study.run_domain")
                self.assertEqual(message.headers["argsrepr"], "('first.example', <credentials>)")
                self.assertEqual(message.delivery_info["routing_key"], "study")
                args, kwargs, _ = message.payload
                self.assertEqual(args, ["first.example", CREDENTIALS])
                self.assertEqual(kwargs, {})
                with patch.object(main, "CourseBot") as bot:
                    result = main.celery_app.tasks[message.headers["task"]].apply(
                        args=args, kwargs=kwargs, task_id=message.headers["id"], throw=True
                    )
                self.assertTrue(result.successful())
                self.assertIsNone(result.result)
                bot.return_value.run.assert_called_once_with()
                message.ack()
            finally:
                queue.close()

    def test_domain_uses_legacy_config_user_bot_and_standard_logs(self):
        with patch.object(main, "CourseBot") as bot, self.assertLogs(
            "domain-task-id_first.example", level="INFO"
        ) as logs:
            result = main.run_domain.apply(
                args=["first.example", CREDENTIALS], task_id="domain-task-id", throw=True
            )
        self.assertTrue(result.successful())
        self.assertIsNone(result.result)
        config, domain, user, logger = bot.call_args.args
        self.assertIs(type(config), main.Config)
        self.assertEqual(config, main.Config())
        self.assertEqual(domain, "first.example")
        self.assertIs(type(user), main.User)
        self.assertEqual(user, main.User(**CREDENTIALS))
        self.assertEqual(logger.name, "domain-task-id_first.example")
        bot.assert_called_once()
        bot.return_value.run.assert_called_once_with()
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [
                "--- 任务启动 [ID:domain-task-id] ---",
                "目标: first.example | 用户: student",
                "--- 任务结束 ---",
            ],
        )

    def test_domain_constructor_and_run_exceptions_keep_legacy_logging(self):
        for failure_at in ("constructor", "run"):
            with self.subTest(failure_at=failure_at):
                failure = RuntimeError("legacy failure")
                bot = Mock()
                if failure_at == "constructor":
                    bot.side_effect = failure
                else:
                    bot.return_value.run.side_effect = failure
                with patch.object(main, "CourseBot", bot), self.assertLogs(
                    "failed-domain_first.example", level="INFO"
                ) as logs:
                    result = main.run_domain.apply(
                        args=["first.example", CREDENTIALS], task_id="failed-domain", throw=True
                    )
                self.assertTrue(result.successful())
                self.assertIsNone(result.result)
                errors = [record for record in logs.records if record.levelname == "ERROR"]
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0].getMessage(), "进程发生严重错误: legacy failure")
                self.assertIs(errors[0].exc_info[1], failure)
                self.assertEqual(logs.records[-1].getMessage(), "--- 任务结束 ---")

    def test_one_platform_failure_does_not_stop_another_platform_task(self):
        domains = ["first.example", "second.example"]
        with patch.object(main, "get_urls", return_value=domains), patch.object(
            main.run_domain, "apply_async"
        ) as publish:
            main.start.apply(args=[CREDENTIALS], throw=True)
        first_bot = Mock()
        first_bot.run.side_effect = RuntimeError("first platform failed")
        second_bot = Mock()
        with patch.object(main, "CourseBot", side_effect=[first_bot, second_bot]) as factory:
            for index, call in enumerate(publish.call_args_list):
                with self.assertLogs(f"platform-{index}_{domains[index]}", level="INFO"):
                    result = main.run_domain.apply(
                        args=call.kwargs["args"], task_id=f"platform-{index}", throw=True
                    )
                self.assertTrue(result.successful())
                self.assertIsNone(result.result)
        self.assertEqual([call.args[1] for call in factory.call_args_list], domains)
        first_bot.run.assert_called_once_with()
        second_bot.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
