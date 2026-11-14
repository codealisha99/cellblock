"""Shared fixtures. Every test gets its own SQLite file and Docker instance label, so
cleanup can only ever touch containers this test created."""

import uuid

import pytest
from fastapi.testclient import TestClient

from cellblock.api import create_app
from cellblock.backends.docker import DockerBackend
from cellblock.config import Settings
from cellblock.service import Service
from cellblock.store import Store


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(home=tmp_path, pool_size=0, instance=f"t{uuid.uuid4().hex[:8]}", reaper_interval_s=0.2)


@pytest.fixture
def backend(settings) -> DockerBackend:
    b = DockerBackend(instance=settings.instance)
    yield b
    for sandbox_id in list(b.list_managed()):
        b.destroy(sandbox_id)


@pytest.fixture
def service(settings, backend) -> Service:
    svc = Service(settings, Store(settings.db_path), backend)
    yield svc
    for row in svc.store.find("sandboxes"):
        backend.destroy(row["id"])


@pytest.fixture
def key(service) -> str:
    return service.create_key("test")


@pytest.fixture
def api(settings, backend):
    app = create_app(settings, backend)
    with TestClient(app) as client:
        yield client, app.state.service
