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
    full_bytes: int = 0,
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
        full_bytes=full_bytes,
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
    store.save_message_record(_make_record(record_id="b", session_id="S", stage="before", tokens=100, byte_size=1000))
    store.save_message_record(_make_record(record_id="a", session_id="S", stage="after", tokens=20, byte_size=200))
    sessions = store.list_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == "S"
    assert s["request_count"] == 1
    assert s["total_tokens_before"] == 100
    assert s["total_tokens_after"] == 20
    assert s["total_bytes_before"] == 1000
    assert s["total_bytes_after"] == 200
    # savings_percent is byte-based (honest metric), not token-based.
    assert s["savings_percent"] == 80.0


def test_list_sessions_prefers_full_bytes_over_byte_size(store):
    """full_bytes (full-context size) is used for the ratio when present,
    falling back to byte_size (delta payload) for legacy rows."""
    store.save_message_record(_make_record(
        record_id="b", session_id="S", stage="before",
        byte_size=1000, full_bytes=10000,
    ))
    store.save_message_record(_make_record(
        record_id="a", session_id="S", stage="after",
        byte_size=200, full_bytes=4000,
    ))
    s = store.list_sessions()[0]
    assert s["total_bytes_before"] == 10000
    assert s["total_bytes_after"] == 4000
    # (10000 - 4000) / 10000 = 60%
    assert s["savings_percent"] == 60.0


def test_list_sessions_falls_back_to_byte_size_when_full_zero(store):
    """Legacy rows (full_bytes == 0) aggregate on byte_size — the ratio the
    dashboard showed before full-context capture existed."""
    store.save_message_record(_make_record(
        record_id="b", session_id="S", stage="before", byte_size=1000, full_bytes=0,
    ))
    store.save_message_record(_make_record(
        record_id="a", session_id="S", stage="after", byte_size=200, full_bytes=0,
    ))
    s = store.list_sessions()[0]
    assert s["total_bytes_before"] == 1000
    assert s["total_bytes_after"] == 200
    assert s["savings_percent"] == 80.0


def test_list_sessions_zero_tokens_does_not_div_by_zero(store):
    """A zero-byte snapshot must not divide by zero; savings stays 0.0."""
    store.save_message_record(_make_record(record_id="b", session_id="S", stage="before", tokens=0, byte_size=0))
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
    rec = _make_record(full_bytes=4321)
    d = rec.to_dict()
    rec2 = CompressedMessageRecord.from_dict(d)
    assert rec2.record_id == rec.record_id
    assert rec2.session_id == rec.session_id
    assert rec2.stage == rec.stage
    assert rec2.messages_json == rec.messages_json
    assert rec2.tokens == rec.tokens
    assert rec2.created_at == rec.created_at
    assert rec2.byte_size == rec.byte_size
    assert rec2.full_bytes == rec.full_bytes


def test_tools_fields_roundtrip_via_storage(store):
    """tools_json / tools_byte_size / tools_hash survive a save → read cycle."""
    tools_json = '[{"type":"function","function":{"name":"echo","description":"hi"}}]'
    rec = CompressedMessageRecord(
        record_id="rt",
        request_id="req-rt",
        session_id="sess-rt",
        stage="before",
        model="gpt-4o",
        provider="openai",
        api_style="openai",
        messages_json='[{"role":"user","content":"hi"}]',
        tokens=5,
        messages_hash="h-rt",
        created_at=_dt.datetime(2025, 8, 23, 10, 0, 0),
        byte_size=24,
        tools_json=tools_json,
        tools_byte_size=len(tools_json.encode("utf-8")),
        tools_hash="hash-rt",
    )
    store.save_message_record(rec)
    got = store.list_records_by_request("req-rt")
    assert len(got) == 1
    assert got[0].tools_json == tools_json
    assert got[0].tools_byte_size == len(tools_json.encode("utf-8"))
    assert got[0].tools_hash == "hash-rt"


def test_full_bytes_fields_roundtrip_via_storage(store):
    """full_bytes survives a save → read cycle."""
    rec = _make_record(full_bytes=12345)
    store.save_message_record(rec)
    got = store.list_records_by_request("req-1")
    assert len(got) == 1
    assert got[0].full_bytes == 12345


def test_full_bytes_defaults_to_zero(store):
    """Records built without full_bytes keep the 0 default (legacy parity)."""
    rec = _make_record()
    assert rec.full_bytes == 0
    store.save_message_record(rec)
    assert store.list_records_by_request("req-1")[0].full_bytes == 0


def test_tools_fields_default_to_empty(store):
    """Legacy records (no tools columns) round-trip with empty defaults."""
    rec = _make_record()  # uses defaults — no tools_json/byte_size/hash
    assert rec.tools_json == ""
    assert rec.tools_byte_size == 0
    assert rec.tools_hash == ""
    store.save_message_record(rec)
    got = store.list_records_by_request("req-1")
    assert len(got) == 1
    assert got[0].tools_json == ""
    assert got[0].tools_byte_size == 0
    assert got[0].tools_hash == ""


def test_legacy_row_without_tools_columns_still_readable(tmp_path):
    """A SQLite database created before the migration must still load:
    every legacy row reads back with tools fields defaulted to empty.
    """
    import sqlite3

    db_path = tmp_path / "legacy.db"
    # Build a hand-crafted legacy schema (no tools columns) and seed a row.
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE compressed_messages (
                record_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                model TEXT NOT NULL,
                provider TEXT NOT NULL,
                api_style TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                tokens INTEGER NOT NULL,
                messages_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                byte_size INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            """INSERT INTO compressed_messages
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "legacy-1", "req-legacy", "sess-legacy", "before",
                "gpt-4o", "openai", "openai",
                '[{"role":"user","content":"hi"}]',
                5, "h-legacy", "2025-08-23T10:00:00Z", 24,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Opening the legacy database with our SQLiteStorage must run the
    # ALTER TABLE migration and load the legacy row with empty tool fields.
    s = SQLiteStorage(str(db_path))
    try:
        recs = s.list_records_by_request("req-legacy")
        assert len(recs) == 1
        assert recs[0].record_id == "legacy-1"
        assert recs[0].tools_json == ""
        assert recs[0].tools_byte_size == 0
        assert recs[0].tools_hash == ""
        # The migration has now added the columns.
        cur = s._get_conn().cursor()
        cur.execute("PRAGMA table_info(compressed_messages)")
        cols = {row[1] for row in cur.fetchall()}
        assert "tools_json" in cols
        assert "tools_byte_size" in cols
        assert "tools_hash" in cols
        assert "full_bytes" in cols
    finally:
        s.close()
