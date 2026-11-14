import pytest

from cellblock.backends.base import SandboxSpec
from cellblock.errors import BadRequest, NotFound
from cellblock.service import collect

pytestmark = pytest.mark.docker


@pytest.fixture
def sb(service, key):
    spec = SandboxSpec(image="python:3.12-slim", cpu=1.0, memory_mb=256, pids=128, network=False, workspace_mb=64)
    return service.create_sandbox(key, spec, ttl_s=600)["id"]


def test_write_read_list_roundtrip_with_binary(service, key, sb):
    blob = bytes(range(256)) * 4
    service.write_file(key, sb, "data/nested/blob.bin", blob)
    assert service.read_file(key, sb, "data/nested/blob.bin") == blob
    listing = service.list_files(key, sb, "data/nested")
    assert listing == [{"name": "blob.bin", "type": "file", "size": len(blob)}]


def test_missing_and_directory_reads(service, key, sb):
    with pytest.raises(NotFound):
        service.read_file(key, sb, "nope.txt")
    service.write_file(key, sb, "d/x", b"1")
    with pytest.raises(BadRequest):
        service.read_file(key, sb, "d")


@pytest.mark.parametrize("raw", ["../../etc/passwd", "/etc/passwd", "/workspace/../etc/x", "a/../../b"])
def test_traversal_is_rejected(service, key, sb, raw):
    with pytest.raises(BadRequest):
        service.read_file(key, sb, raw)
    with pytest.raises(BadRequest):
        service.write_file(key, sb, raw, b"x")


def test_symlink_escape_is_rejected(service, key, sb):
    collect(service.exec_stream(key, sb, ["sh", "-c", "ln -s /etc /workspace/escape"], 10))
    with pytest.raises(BadRequest):
        service.read_file(key, sb, "escape/passwd")
    with pytest.raises(BadRequest):
        service.write_file(key, sb, "escape/pwned", b"x")
    assert collect(service.exec_stream(key, sb, ["sh", "-c", "ls /etc/pwned"], 10))["exit_code"] != 0
