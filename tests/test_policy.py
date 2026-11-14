"""Policy tests that need no Docker: image allowlist, legacy pool rows, and timeout classification."""

from dataclasses import replace

from fastapi.testclient import TestClient

from cellblock.api import create_app
from cellblock.config import Settings
from cellblock.errors import ImageNotAllowed
from cellblock.service import Service, timed_out_by_limit
from cellblock.store import Store


class FakeBackend:
    def __init__(self) -> None:
        self.live: dict[str, str] = {}
        self.destroyed: list[str] = []

    def list_managed(self) -> dict[str, str]:
        return dict(self.live)

    def destroy(self, sandbox_id: str) -> None:
        self.destroyed.append(sandbox_id)
        self.live.pop(sandbox_id, None)


def _settings(tmp_path) -> Settings:
    return Settings(home=tmp_path, pool_size=0, reaper_interval_s=60)


def test_exit_124_from_the_program_is_not_a_timeout():
    assert timed_out_by_limit(124, elapsed_s=0.2, timeout_s=30) is False
    assert timed_out_by_limit(124, elapsed_s=30.1, timeout_s=30) is True


def test_timeout_wrapper_sigkill_counts_as_timeout_only_at_the_limit():
    assert timed_out_by_limit(137, elapsed_s=30.5, timeout_s=30) is True
    assert timed_out_by_limit(137, elapsed_s=1.0, timeout_s=30) is False  # OOM or kill from the API
    assert timed_out_by_limit(0, elapsed_s=31, timeout_s=30) is False


def test_image_outside_allowlist_is_rejected_before_any_docker_call(tmp_path):
    settings = _settings(tmp_path)
    svc = Service(settings, Store(settings.db_path), FakeBackend())
    key = svc.create_key("t")
    spec = replace(svc.default_spec, image="alpine:3.20")
    try:
        svc.create_sandbox(key, spec, ttl_s=60)
    except ImageNotAllowed as exc:
        assert "alpine:3.20" in str(exc)
    else:
        raise AssertionError("image outside the allowlist was accepted")


def test_api_returns_422_for_disallowed_image(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, FakeBackend())) as client:
        key = client.app.state.service.create_key("t")
        resp = client.post("/v1/sandboxes", json={"image": "alpine:3.20"},
                           headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 422
    assert "not allowed" in resp.json()["error"]


def test_reconcile_removes_legacy_pool_rows(tmp_path):
    settings = _settings(tmp_path)
    store, backend = Store(settings.db_path), FakeBackend()
    svc = Service(settings, store, backend)
    legacy = "sbx_legacypool0000000000000000"
    backend.live[legacy] = "running"
    # Older builds left warm-pool containers as "running" rows with no owner.
    store.insert("sandboxes", svc._row(legacy, None, "running", svc.default_spec, None))

    report = svc.reconcile()

    assert report["legacy_pool_removed"] == [legacy]
    assert backend.destroyed == [legacy] and legacy not in backend.live
    assert store.get("sandboxes", legacy)["status"] == "deleted"
    assert svc.pool_ready() == 0
