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


def test_openapi_contains_start_stop_and_shared_requests():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert set(schema["paths"]) == {"/start", "/stop"}
    operation = schema["paths"]["/start"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Credentials"
    }
    assert "content" not in operation["responses"]["202"]
    credentials = schema["components"]["schemas"]["Credentials"]
    assert set(credentials["properties"]) == {"username", "password"}
    stop = schema["paths"]["/stop"]["post"]
    assert stop["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/StopRequest"
    }
    assert "content" not in stop["responses"]["204"]
    assert set(schema["components"]["schemas"]["StopRequest"]["properties"]) == {"username"}


def test_stop_requires_only_username_and_returns_no_task_status():
    with patch("auto_course_api.main.stop_account") as stop:
        response = client.post("/stop", json={"username": "student"})
    assert response.status_code == 204
    assert response.content == b""
    stop.assert_called_once_with("student")


@pytest.mark.parametrize("payload", [{}, {"username": ""}, {"username": "student", "password": "secret"}])
def test_invalid_stop_request_does_not_stop_tasks(payload):
    with patch("auto_course_api.main.stop_account") as stop:
        assert client.post("/stop", json=payload).status_code == 422
    stop.assert_not_called()


def test_broker_has_no_result_storage_and_password_is_not_in_model_repr():
    assert broker.conf.result_backend is None
    assert broker.conf.task_ignore_result is True
    assert broker.conf.task_store_errors_even_if_ignored is False
    assert "secret" not in repr(Credentials(username="student", password="secret"))


@pytest.mark.parametrize("path", ["/start", "/stop"])
def test_browser_cors_preflight(path):
    response = client.options(
        path,
        headers={
            "Origin": "https://auto-course.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
