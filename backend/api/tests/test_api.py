import asyncio
import importlib.util
import json
import os
from functools import partial
from pathlib import Path
import unittest
from unittest.mock import patch

from kombu import Connection, Producer


def load_api():
    spec = importlib.util.spec_from_file_location(
        "api_main", Path(__file__).resolve().parents[1] / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = load_api()
CREDENTIALS = {"username": "student", "password": "secret"}


def request(application=api.app, *, method="POST", payload=None, headers=None):
    async def run():
        events = []

        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode()}

        async def send(event):
            events.append(event)

        await application(
            {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": method, "scheme": "http", "path": "/start",
                "root_path": "", "query_string": b"",
                "headers": [(b"content-type", b"application/json"), *(headers or [])],
            },
            receive, send,
        )
        return events[0]["status"], dict(events[0]["headers"]), b"".join(
            event.get("body", b"") for event in events[1:]
        )

    return asyncio.run(run())


class ApiTests(unittest.TestCase):
    def test_start_publishes_credentials_and_returns_empty_accepted_response(self):
        with patch.object(api.celery_app, "send_task") as publish:
            status, _, body = request(payload=CREDENTIALS)
        self.assertEqual((status, body), (202, b""))
        publish.assert_called_once_with(
            "study.start", args=[CREDENTIALS], queue="study",
            ignore_result=True, argsrepr="(<credentials>,)",
        )

    def test_invalid_request_does_not_publish(self):
        for payload in (
            {}, {"username": "student"}, {"username": "student", "password": ""},
            {"username": "", "password": "secret"}, {**CREDENTIALS, "task_id": "unused"},
        ):
            with self.subTest(payload=payload), patch.object(api.celery_app, "send_task") as publish:
                self.assertEqual(request(payload=payload)[0], 422)
                publish.assert_not_called()

    def test_openapi_and_credentials_contract(self):
        schema = api.app.openapi()
        self.assertEqual(set(schema["paths"]), {"/start"})
        operation = schema["paths"]["/start"]["post"]
        self.assertEqual(operation["requestBody"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/Credentials"})
        self.assertNotIn("content", operation["responses"]["202"])
        self.assertEqual(set(schema["components"]["schemas"]["Credentials"]["properties"]),
                         {"username", "password"})
        self.assertNotIn("secret", repr(api.Credentials(**CREDENTIALS)))

    def test_broker_configuration_and_environment_override(self):
        for override, expected in ((None, "redis://redis:6379/0"),
                                   ("redis://broker.example:6380/7", "redis://broker.example:6380/7")):
            with self.subTest(override=override), patch.dict(os.environ):
                os.environ.pop("CELERY_BROKER_URL", None)
                if override is not None:
                    os.environ["CELERY_BROKER_URL"] = override
                broker = load_api().celery_app
                try:
                    self.assertEqual(broker.conf.broker_url, expected)
                    self.assertEqual(broker.conf.task_default_queue, "study")
                    self.assertEqual(broker.conf.task_serializer, "json")
                    self.assertEqual(broker.conf.accept_content, ["json"])
                    self.assertIsNone(broker.conf.result_backend)
                    self.assertTrue(broker.conf.task_ignore_result)
                    self.assertFalse(broker.conf.task_store_errors_even_if_ignored)
                finally:
                    broker.close()

    def test_cors_default_and_configured_origins(self):
        for origins, allowed in ((None, "*"), ("https://first.example,https://second.example",
                                              "https://second.example")):
            with self.subTest(origins=origins), patch.dict(os.environ):
                os.environ.pop("CORS_ALLOW_ORIGINS", None)
                if origins is not None:
                    os.environ["CORS_ALLOW_ORIGINS"] = origins
                configured = load_api()
                try:
                    for origin in ("https://second.example", "https://other.example"):
                        status, headers, _ = request(configured.app, method="OPTIONS", headers=[
                            (b"origin", origin.encode()),
                            (b"access-control-request-method", b"POST"),
                            (b"access-control-request-headers", b"content-type"),
                        ])
                        accepted = origins is None or origin == allowed
                        self.assertEqual(status, 200 if accepted else 400)
                        self.assertEqual(headers.get(b"access-control-allow-origin"),
                                         allowed.encode() if accepted else None)
                finally:
                    configured.celery_app.close()

    def test_start_publishes_compatible_json_message_to_study_queue(self):
        with Connection("memory://") as connection:
            queue = connection.SimpleQueue("study")
            try:
                queue.clear()
                publish = partial(api.celery_app.send_task, producer=Producer(connection))
                with patch.object(api.celery_app, "send_task", wraps=publish):
                    self.assertEqual(request(payload=CREDENTIALS)[0], 202)
                message = queue.get(block=False)
                self.assertEqual(message.content_type, "application/json")
                self.assertEqual(message.headers["task"], "study.start")
                self.assertEqual(message.headers["argsrepr"], "(<credentials>,)")
                self.assertTrue(message.headers["ignore_result"])
                self.assertEqual(message.delivery_info["routing_key"], "study")
                self.assertEqual(message.payload[:2], [[CREDENTIALS], {}])
                message.ack()
            finally:
                queue.close()


if __name__ == "__main__":
    unittest.main()
