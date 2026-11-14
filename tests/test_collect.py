from cellblock.service import collect


def _events(chunks):
    yield {"type": "started", "exec_id": "ex_1"}
    for kind, data in chunks:
        yield {"type": kind, "data": data}
    yield {"type": "exit", "exit_code": 0, "timed_out": False}


def test_collect_joins_every_chunk_in_order():
    chunks = [("stdout", f"{i},") for i in range(20_000)] + [("stderr", "warn\n")]
    out = collect(_events(chunks))
    assert out["stdout"] == "".join(f"{i}," for i in range(20_000))
    assert out["stderr"] == "warn\n"
    assert (out["exec_id"], out["exit_code"], out["truncated"]) == ("ex_1", 0, False)


def test_collect_caps_each_stream_and_flags_truncation():
    out = collect(_events([("stdout", "a" * 6), ("stdout", "b" * 6), ("stderr", "c" * 3)]), limit=8)
    assert out["stdout"] == "aaaaaabb"
    assert out["stderr"] == "ccc"
    assert out["truncated"] is True
