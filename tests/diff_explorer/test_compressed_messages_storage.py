"""Unit tests for the 4 new ``Storage`` methods (save_message_record / etc).

Covers both SQLiteStorage and JSONLStorage; the contracts are identical so
the same shape of assertions is used for both. We use ``tmp_path`` (pytest
fixture) so each test gets a fresh on-disk file.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from headroom.storage.compressed_messages import CompressedMessageRecord
from headroom.storage.jsonl import JSONLStorage
from headroom.storage.sqlite import SQLiteStorage


def _make_record(
    *,
    record_id: str = "r1",
    request_id: str = "req-1",
    session_id: str = "sess-1",
    stage: str = "before",
    model: str = "gpt-4o",
    tokens: int = 10,
    byte_size: int | None = None,
    messages_json: str = '[{"role":"user","content":"hi"}]',
    created_at: _dt.datetime | None = None,
) -> CompressedMessageRecord:
    return CompressedMessageRecord(
        record_id=record_id,
        request_id=request_id,
        session_id=session_id,
        stage=stage,
        model=model,
        provider="openai",
        api_style="openai",
        messages_json=messages_json,
        tokens=tokens,
        messages_hash="h1",
        created_at=created_at or _dt.datetime(2025, 8, 23, 10, 0, 0),
        byte_size=byte_size if byte_size is not None else len(messages_json.encode()),
    )


@pytest.fixture(params=["sqlite", "jsonl"])
def store(request, tmp_path):
    """Yield a fresh Storage instance per test (both backends)."""
    if request.param == "sqlite":
        s = SQLiteStorage(str(tmp_path / "test.db"))
    else:
        s = JSONLStorage(str(tmp_path / "test.jsonl"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# save_message_record + read
# ---------------------------------------------------------------------------
def test_save_and_list_by_request(store):
    rec = _make_record()
    store.save_message_record(rec)
    got = store.list_records_by_request("req-1")
    assert len(got) == 1
    assert got[0].record_id == "r1"
    assert got[0].stage == "before"
    assert got[0].session_id == "sess-1"


def test_save_two_stages_returns_two_records(store):
    store.save_message_record(_make_record(record_id="b", stage="before", tokens=100))
    store.save_message_record(_make_record(record_id="a", stage="after", tokens=20))
    got = store.list_records_by_request("req-1")
    assert len(got) == 2
    # Order: created_at ASC, then stage ASC — both records share ts
    # so stage ordering wins; "after" < "before" alphabetically.
    assert got[0].stage == "after"
    assert got[1].stage == "before"


def test_save_upserts_on_record_id(store):
    """Same record_id → content is replaced, not duplicated."""
    store.save_message_record(_make_record(record_id="r1", tokens=10))
    store.save_message_record(_make_record(record_id="r1", tokens=99, messages_json='[{"role":"user","content":"UPD"}]'))
    got = store.list_records_by_request("req-1")
    assert len(got) == 1
    assert got[0].tokens == 99
    assert "UPD" in got[0].messages_json


# ---------------------------------------------------------------------------
# list_records_by_session
# ---------------------------------------------------------------------------
def test_list_records_by_session_returns_all_stages(store):
    store.save_message_record(_make_record(record_id="b", request_id="r1", session_id="S", stage="before"))
    store.save_message_record(_make_record(record_id="a", request_id="r1", session_id="S", stage="after"))
    store.save_message_record(_make_record(record_id="b2", request_id="r2", session_id="S", stage="before"))
    got = store.list_records_by_session("S")
    assert len(got) == 3


def test_list_records_by_session_unknown_returns_empty(store):
    assert store.list_records_by_session("nope") == []


def test_list_records_by_session_pagination(store):
    for i in range(5):
        store.save_message_record(_make_record(
            record_id=f"r{i}", request_id=f"req-{i}", session_id="S",
            created_at=_dt.datetime(2025, 8, 23, 10, 0, i),
        ))
    first_2 = store.list_records_by_session("S", limit=2, offset=0)
    next_2 = store.list_records_by_session("S", limit=2, offset=2)
    assert len(first_2) == 2
    assert len(next_2) == 2
    assert {r.record_id for r in first_2}.isdisjoint({r.record_id for r in next_2})


# ---------------------------------------------------------------------------
# list_records_by_request
# ---------------------------------------------------------------------------
def test_list_records_by_request_unknown_returns_empty(store):
    assert store.list_records_by_request("nope") == []


# ---------------------------------------------------------------------------
# list_sessions — aggregation
# ---------------------------------------------------------------------------
def test_list_sessions_empty(store):
    assert store.list_sessions() == []


def test_list_sessions_aggregates_two_stage(store):
    store.save_message_record(_make_record(record_id="b", session_id="S", stage="before", tokens=100))
    store.save_message_record(_make_record(record_id="a", session_id="S", stage="after", tokens=20))
    sessions = store.list_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == "S"
    assert s["request_count"] == 1
    assert s["total_tokens_before"] == 100
    assert s["total_tokens_after"] == 20
    assert s["savings_percent"] == 80.0


def test_list_sessions_zero_tokens_does_not_div_by_zero(store):
    store.save_message_record(_make_record(record_id="b", session_id="S", stage="before", tokens=0))
    sessions = store.list_sessions()
    assert sessions[0]["savings_percent"] == 0.0


def test_list_sessions_required_fields(store):
    store.save_message_record(_make_record(session_id="S"))
    s = store.list_sessions()[0]
    required = {"session_id", "request_count", "first_seen", "last_seen",
                "total_tokens_before", "total_tokens_after", "savings_percent"}
    assert required <= set(s.keys())


def test_list_sessions_orders_by_last_seen_desc(store):
    store.save_message_record(_make_record(record_id="a", session_id="A",
                                           created_at=_dt.datetime(2025, 1, 1)))
    store.save_message_record(_make_record(record_id="b", session_id="B",
                                           created_at=_dt.datetime(2025, 6, 1)))
    sessions = store.list_sessions()
    assert sessions[0]["session_id"] == "B"
    assert sessions[1]["session_id"] == "A"


def test_list_sessions_pagination(store):
    for i in range(3):
        store.save_message_record(_make_record(
            record_id=f"r{i}", session_id=f"S{i}",
            created_at=_dt.datetime(2025, 1, 1 + i),
        ))
    first_2 = store.list_sessions(limit=2)
    assert len(first_2) == 2


# ---------------------------------------------------------------------------
# to_dict / from_dict roundtrip
# ---------------------------------------------------------------------------
def test_to_from_dict_roundtrip():
    rec = _make_record()
    d = rec.to_dict()
    rec2 = CompressedMessageRecord.from_dict(d)
    assert rec2.record_id == rec.record_id
    assert rec2.session_id == rec.session_id
    assert rec2.stage == rec.stage
    assert rec2.messages_json == rec.messages_json
    assert rec2.tokens == rec.tokens
    assert rec2.created_at == rec.created_at
    assert rec2.byte_size == rec.byte_size
