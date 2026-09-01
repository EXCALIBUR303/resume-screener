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


def test_local_mode_claims_network_isolation(settings) -> None:
    from screener_api.settings import Settings

    local = Settings(
        app_env="dev",
        postgres_password="x",
        app_kek="x",
        jwt_secret="x",
        deployment_mode="local",
        postgres_socket_dir="/var/run/postgresql",
    )
    assert local.worker_parse_is_network_isolated


def test_cloud_mode_does_not_claim_network_isolation() -> None:
    """Managed Postgres is TCP-only, so worker-parse cannot run with
    network_mode: none. The guarantee is genuinely weaker in cloud mode and the
    code must not pretend otherwise — ADR-0008 and ADR-0016."""
    from screener_api.settings import Settings

    cloud = Settings(
        app_env="dev",
        postgres_password="x",
        app_kek="x",
        jwt_secret="x",
        deployment_mode="cloud",
        postgres_socket_dir="",
    )
    assert not cloud.worker_parse_is_network_isolated


def test_local_mode_without_a_socket_does_not_claim_isolation() -> None:
    """Setting the mode is not enough: without the socket the worker is on a
    network whatever the label says."""
    from screener_api.settings import Settings

    mislabelled = Settings(
        app_env="dev",
        postgres_password="x",
        app_kek="x",
        jwt_secret="x",
        deployment_mode="local",
        postgres_socket_dir="",
    )
    assert not mislabelled.worker_parse_is_network_isolated
