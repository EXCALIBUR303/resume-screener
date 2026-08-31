from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_is_up(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_does_not_need_a_database(client: TestClient) -> None:
    """Liveness must not depend on Postgres, or a DB blip restarts the app."""
    assert client.get("/healthz").status_code == 200


def test_request_id_is_returned(client: TestClient) -> None:
    assert client.get("/healthz").headers.get("x-request-id")


def test_supplied_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/healthz", headers={"x-request-id": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


def test_hostile_request_id_is_replaced(client: TestClient) -> None:
    """A caller must not be able to inject newlines or JSON into log output."""
    hostile = 'evil"\n{"level":"critical","event":"forged"}'
    returned = client.get("/healthz", headers={"x-request-id": hostile}).headers["x-request-id"]
    assert returned != hostile
    assert "\n" not in returned


def test_cors_is_an_allowlist_never_a_wildcard(settings) -> None:
    """A wildcard origin with credentials is invalid, and is the classic way a
    browser app hands its own API to any site the user happens to visit."""
    assert "*" not in settings.cors_origins
    assert all(o.startswith(("http://", "https://")) for o in settings.cors_origins)
