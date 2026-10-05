from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

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
    assert broker.conf.result_backend is None
    assert broker.conf.task_ignore_result is True
    assert broker.conf.task_store_errors_even_if_ignored is False
    assert "secret" not in repr(Credentials(username="student", password="secret"))


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
