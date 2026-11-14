import pytest

from cellblock.errors import BadRequest
from cellblock.paths import normalize


@pytest.mark.parametrize("raw,expected", [
    ("a.txt", "/workspace/a.txt"),
    ("/workspace/x/./y", "/workspace/x/y"),
    ("/workspace", "/workspace"),
])
def test_accepts_paths_inside_workspace(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize("raw", ["../etc/passwd", "a/../../etc", "/etc/passwd", "/workspace/../etc", "", "a\x00b", "/workspacex/y"])
def test_rejects_escapes(raw):
    with pytest.raises(BadRequest):
        normalize(raw)
