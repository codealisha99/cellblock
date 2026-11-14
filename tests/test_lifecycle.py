import time

import pytest

from cellblock.backends.base import SandboxSpec
from cellblock.errors import Conflict, NotFound, QuotaExceeded
from cellblock.service import Service, collect
from cellblock.store import Store

pytestmark = pytest.mark.docker


def _spec(**over):
    base = dict(image="python:3.12-slim", cpu=1.0, memory_mb=256, pids=128, network=False, workspace_mb=64)
    return SandboxSpec(**{**base, **over})


def test_ttl_reaper_kills_expired_sandbox(service, key, backend):
    info = service.create_sandbox(key, _spec(), ttl_s=1)
    time.sleep(1.2)
    assert service.reap_expired() == [info["id"]]
    assert info["id"] not in backend.list_managed()
    with pytest.raises(NotFound):
        service.get(key, info["id"])


def test_pause_blocks_exec_and_resume_restores(service, key):
    sid = service.create_sandbox(key, _spec(), ttl_s=600)["id"]
    service.pause(key, sid)
    with pytest.raises(Conflict):
        service.exec_stream(key, sid, ["true"], 5)
    service.resume(key, sid)
    assert collect(service.exec_stream(key, sid, ["echo", "back"], 10))["stdout"] == "back\n"


def test_snapshot_restore_preserves_files(service, key):
    src = service.create_sandbox(key, _spec(), ttl_s=600)["id"]
    service.write_file(key, src, "proj/main.py", b"print('hi')")
    service.write_file(key, src, "data.bin", bytes(range(200)))
    template = service.snapshot(key, src, "baseline")
    dst = service.create_sandbox(key, _spec(), ttl_s=600, snapshot_id=template["id"])["id"]
    assert service.read_file(key, dst, "proj/main.py") == b"print('hi')"
    assert service.read_file(key, dst, "data.bin") == bytes(range(200))
    other = service.create_key("other")
    with pytest.raises(NotFound):
        service.create_sandbox(other, _spec(), ttl_s=600, snapshot_id=template["id"])


def test_quota_per_key(service, settings, key):
    limit = settings.max_sandboxes_per_key
    for _ in range(limit):
        service.create_sandbox(key, _spec(), ttl_s=600)
    with pytest.raises(QuotaExceeded):
        service.create_sandbox(key, _spec(), ttl_s=600)
    other = service.create_key("other")
    service.create_sandbox(other, _spec(), ttl_s=600)  # another key is unaffected


def test_orphan_reconciliation_after_restart(settings, service, key, backend):
    kept = service.create_sandbox(key, _spec(), ttl_s=600)["id"]
    orphan = service.create_sandbox(key, _spec(), ttl_s=600)["id"]
    lost = service.create_sandbox(key, _spec(), ttl_s=600)["id"]
    service.store.delete("sandboxes", orphan)              # crash between container start and DB write
    backend.client.containers.get(f"cellblock-{lost}").kill()  # container died while control plane was down

    restarted = Service(settings, Store(settings.db_path), backend)  # "restart"
    report = restarted.reconcile()

    assert orphan in report["orphans_removed"] and orphan not in backend.list_managed()
    assert lost in report["lost"] and restarted.store.get("sandboxes", lost)["status"] == "lost"
    assert kept in backend.list_managed()
    assert restarted.get(key, kept)["status"] == "running"


def test_warm_pool_claims_prestarted_container(settings, backend, key):
    settings = settings.__class__(**{**settings.__dict__, "pool_size": 1})
    svc = Service(settings, Store(settings.db_path), backend)
    svc.refill_pool()
    pooled = svc.store.find("sandboxes", status="pooled")[0]["id"]
    warm = svc.create_sandbox(key, svc.default_spec, ttl_s=600)
    assert warm["id"] == pooled and warm["status"] == "running"
    assert svc.pool_ready() == 0
    assert collect(svc.exec_stream(key, warm["id"], ["echo", "warm"], 10))["stdout"] == "warm\n"
    svc.refill_pool()
    spare = svc.store.find("sandboxes", status="pooled")[0]["id"]
    svc.shutdown()
    assert spare not in backend.list_managed()  # unclaimed pool containers never outlive the control plane
    assert pooled in backend.list_managed()     # claimed sandboxes are owned by a client and survive it
