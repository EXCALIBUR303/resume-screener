from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from screener_api.main import create_app
from screener_api.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="dev",
        postgres_password="test-password",
        app_kek="test-kek",
        jwt_secret="test-jwt",
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))
