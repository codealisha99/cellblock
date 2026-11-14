"""Path policy for the file API. Lexical checks here; the backend re-checks the
symlink-resolved path inside the container before touching anything."""

import posixpath

from cellblock.errors import BadRequest

WORKSPACE = "/workspace"


def normalize(raw: str) -> str:
    """Return an absolute path under /workspace, or raise BadRequest.

    Relative paths are taken relative to /workspace. Any `..` segment is
    rejected outright rather than resolved, so `a/../../etc` never gets a chance.
    """
    if not raw or "\x00" in raw:
        raise BadRequest("invalid path")
    if ".." in raw.split("/"):
        raise BadRequest("path must not contain '..'")
    absolute = raw if raw.startswith("/") else f"{WORKSPACE}/{raw}"
    norm = posixpath.normpath(absolute)
    return assert_inside(norm)


def assert_inside(path: str) -> str:
    """Raise BadRequest unless `path` is /workspace or below it."""
    if path != WORKSPACE and not path.startswith(WORKSPACE + "/"):
        raise BadRequest("path must be inside /workspace")
    return path
