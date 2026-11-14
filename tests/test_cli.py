import pytest

from cellblock import cli
from cellblock_sdk.client import ApiError


def test_api_error_is_reported_as_a_message_not_a_traceback(monkeypatch):
    def not_found(*_args, **_kwargs):
        raise ApiError(404, "sandbox not found")

    monkeypatch.setattr(cli, "_client", lambda: object())
    monkeypatch.setattr(cli.Sandbox, "connect", not_found)
    with pytest.raises(SystemExit) as exc:
        cli.main(["exec", "sbx_nope", "true"])
    assert "sandbox not found" in str(exc.value.code)
