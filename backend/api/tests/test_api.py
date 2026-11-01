import json
import os
import subprocess
import sys
from functools import partial
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from kombu import Connection, Producer

from auto_course_api.broker import app as broker
from auto_course_api.main import app
from auto_course_contracts import Credentials

client = TestClient(app)


def test_start_publishes_credentials_and_returns_empty_accepted_response():
    with patch.object(broker, "send_task") as publish:
        response = client.post("/start", json={"username": "student", "password": "secret"})

    assert response.status_code == 202
    assert response.content == b""
    publish.assert_called_once_with(
        "study.start",
        args=[{"username": "student", "password": "secret"}],
        queue="study",
        ignore_result=True,
        argsrepr="(<credentials>,)",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"username": "student"},
        {"username": "student", "password": ""},
        {"username": "", "password": "secret"},
        {"username": "student", "password": "secret", "task_id": "unused"},
    ],
)
def test_invalid_request_does_not_publish(payload):
    with patch.object(broker, "send_task") as publish:
        assert client.post("/start", json=payload).status_code == 422
    publish.assert_not_called()


def test_openapi_contains_only_start_and_shared_credentials():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert set(schema["paths"]) == {"/start"}
    operation = schema["paths"]["/start"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Credentials"
    }
    assert "content" not in operation["responses"]["202"]
    credentials = schema["components"]["schemas"]["Credentials"]
    assert set(credentials["properties"]) == {"username", "password"}


def test_broker_has_no_result_storage_and_password_is_not_in_model_repr():
    assert broker.conf.task_default_queue == "study"
    assert broker.conf.task_serializer == "json"
    assert broker.conf.accept_content == ["json"]
    assert broker.conf.result_backend is None
    assert broker.conf.task_ignore_result is True
    assert broker.conf.task_store_errors_even_if_ignored is False
    assert "secret" not in repr(Credentials(username="student", password="secret"))


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (None, "redis://redis:6379/0"),
        ("redis://broker.example:6380/7", "redis://broker.example:6380/7"),
    ],
)
def test_broker_default_and_environment_override(override, expected):
    environment = os.environ.copy()
    environment.pop("CELERY_BROKER_URL", None)
    if override is not None:
        environment["CELERY_BROKER_URL"] = override
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import json; from auto_course_api.broker import app; "
            "print(json.dumps(app.conf.broker_url))",
        ],
        env=environment,
        text=True,
    )
    assert json.loads(output) == expected


def test_browser_cors_preflight():
    response = client.options(
        "/start",
        headers={
            "Origin": "https://auto-course.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_browser_cors_uses_configured_origins():
    environment = os.environ.copy()
    environment["CORS_ALLOW_ORIGINS"] = "https://first.example,https://second.example"
    program = """
import json
from fastapi.testclient import TestClient
from auto_course_api.main import app

client = TestClient(app)
results = []
for origin in ("https://first.example", "https://second.example", "https://other.example"):
    response = client.options(
        "/start",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    results.append([response.status_code, response.headers.get("access-control-allow-origin")])
print(json.dumps(results))
"""
    output = subprocess.check_output(
        [sys.executable, "-c", program], env=environment, text=True
    )
    assert json.loads(output) == [
        [200, "https://first.example"],
        [200, "https://second.example"],
        [400, None],
    ]


def test_start_publishes_compatible_json_message_to_study_queue():
    credentials = {"username": "student", "password": "secret"}
    with Connection("memory://") as connection:
        queue = connection.SimpleQueue("study")
        try:
            queue.clear()
            publish = partial(broker.send_task, producer=Producer(connection))
            with patch.object(broker, "send_task", wraps=publish):
                response = client.post("/start", json=credentials)

            assert response.status_code == 202
            assert response.content == b""
            message = queue.get(block=False)
            assert message.content_type == "application/json"
            assert message.headers["task"] == "study.start"
            assert message.headers["argsrepr"] == "(<credentials>,)"
            assert message.headers["ignore_result"] is True
            assert message.delivery_info["routing_key"] == "study"
            args, kwargs, _ = message.payload
            assert args == [credentials]
            assert kwargs == {}
            message.ack()
        finally:
            queue.close()
