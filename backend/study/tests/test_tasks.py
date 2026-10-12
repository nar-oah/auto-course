import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from auto_course_study import tasks
from auto_course_study.cancellation import StudyStopped


CREDENTIALS = {"username": "student", "password": "password"}


class TaskTests(unittest.TestCase):
    def test_start_fans_out_to_domain_queue_without_exposing_credentials(self) -> None:
        with patch.object(tasks, "discover_domains", return_value=["one.example", "two.example"]), patch.object(tasks.run_domain, "apply_async") as enqueue:
            self.assertIsNone(tasks.start.run(CREDENTIALS))
        self.assertEqual(enqueue.call_count, 2)
        for call, domain in zip(enqueue.call_args_list, ["one.example", "two.example"]):
            self.assertEqual(call.kwargs["args"], [domain, CREDENTIALS])
            self.assertEqual(call.kwargs["queue"], "study")
            self.assertNotIn("password", call.kwargs["argsrepr"])
            self.assertNotIn("student", call.kwargs["argsrepr"])

    def test_domain_worker_keeps_one_context_managed_client(self) -> None:
        client = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=False)
        with patch.object(tasks, "StudyClient", return_value=context) as constructor, patch.object(tasks, "CourseBot") as bot:
            tasks.run_domain.run("one.example", CREDENTIALS)
        self.assertEqual(constructor.call_args.args, ("https://one.example",))
        self.assertTrue(callable(constructor.call_args.kwargs["check"]))
        self.assertTrue(callable(constructor.call_args.kwargs["sleep"]))
        self.assertIs(bot.call_args.args[0], client)
        self.assertEqual(bot.call_args.args[1].model_dump(), CREDENTIALS)
        bot.return_value.run.assert_called_once_with()
        context.__exit__.assert_called_once_with(None, None, None)

    def test_stopped_domain_closes_client_and_returns_without_error(self) -> None:
        client = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=False)
        with patch.object(tasks, "StudyClient", return_value=context), patch.object(tasks, "CourseBot") as bot:
            bot.return_value.run.side_effect = StudyStopped
            self.assertIsNone(tasks.run_domain.run("one.example", CREDENTIALS))
        self.assertIs(context.__exit__.call_args.args[0], StudyStopped)

    def test_stop_during_discovery_does_not_enqueue_domains(self) -> None:
        with patch.object(tasks, "discover_domains", side_effect=StudyStopped), patch.object(tasks.run_domain, "apply_async") as enqueue:
            self.assertIsNone(tasks.start.run(CREDENTIALS))
        enqueue.assert_not_called()

    def test_stop_during_fanout_does_not_enqueue_remaining_domains(self) -> None:
        with patch.object(tasks, "TaskCancellation") as cancellation, patch.object(tasks, "discover_domains", return_value=["one.example", "two.example"]), patch.object(tasks.run_domain, "apply_async") as enqueue:
            cancellation.return_value.__enter__.return_value.check.side_effect = [None, StudyStopped]
            self.assertIsNone(tasks.start.run(CREDENTIALS))
        self.assertEqual(enqueue.call_count, 1)

    def test_task_credentials_are_validated_before_discovery(self) -> None:
        with patch.object(tasks, "discover_domains") as discover:
            with self.assertRaises(ValidationError):
                tasks.start.run({"username": "student"})
        discover.assert_not_called()

    def test_celery_uses_json_broker_and_no_results(self) -> None:
        self.assertEqual(tasks.app.conf.task_default_queue, "study")
        self.assertEqual(tasks.app.conf.task_serializer, "json")
        self.assertEqual(tasks.app.conf.accept_content, ["json"])
        self.assertTrue(tasks.app.conf.task_ignore_result)
        self.assertFalse(tasks.app.conf.task_store_errors_even_if_ignored)
        self.assertIsNone(tasks.app.conf.result_backend)
        self.assertTrue(tasks.start.ignore_result)
        self.assertTrue(tasks.run_domain.ignore_result)


if __name__ == "__main__":
    unittest.main()
