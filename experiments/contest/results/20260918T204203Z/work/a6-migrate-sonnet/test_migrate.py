"""Hidden tests for A6 — SQLite → JSONL migration."""
import json
import os
import sqlite3

import pytest

from solution import migrate


def make_db(path, rows=None, with_table=True):
    con = sqlite3.connect(path)
    if with_table:
        con.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, ts TEXT, payload TEXT)")
        for r in rows or []:
            con.execute("INSERT INTO events(id, ts, payload) VALUES (?, ?, ?)", r)
    else:
        con.execute("CREATE TABLE other(x)")
    con.commit()
    con.close()


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def test_basic(tmp_path):
    db = tmp_path / "e.db"
    out = tmp_path / "out.jsonl"
    make_db(db, [(1, "2026-01-01", "a"), (2, "2026-01-02", "b"), (3, "2026-01-03", "c")])
    assert migrate(str(db), str(out)) is True
    lines = read_lines(out)
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"id": 1, "ts": "2026-01-01", "payload": "a"}
    assert json.loads(lines[2])["id"] == 3


def test_idempotent_noop(tmp_path):
    db = tmp_path / "e.db"
    out = tmp_path / "out.jsonl"
    make_db(db, [(1, "t", "p")])
    assert migrate(str(db), str(out)) is True
    first = out.read_bytes()
    assert migrate(str(db), str(out)) is False
    assert out.read_bytes() == first


def test_rewrites_when_db_changed(tmp_path):
    db = tmp_path / "e.db"
    out = tmp_path / "out.jsonl"
    make_db(db, [(1, "t", "p")])
    migrate(str(db), str(out))
    con = sqlite3.connect(db)
    con.execute("INSERT INTO events VALUES (2, 't2', 'p2')")
    con.commit()
    con.close()
    assert migrate(str(db), str(out)) is True
    assert len(read_lines(out)) == 2


def test_empty_table(tmp_path):
    db = tmp_path / "e.db"
    out = tmp_path / "out.jsonl"
    make_db(db, [])
    assert migrate(str(db), str(out)) is True
    assert read_lines(out) == []
    assert migrate(str(db), str(out)) is False


def test_unicode_no_ascii_escapes(tmp_path):
    db = tmp_path / "e.db"
    out = tmp_path / "out.jsonl"
    make_db(db, [(1, "2026", "привет мир")])
    migrate(str(db), str(out))
    raw = out.read_text(encoding="utf-8")
    assert "привет мир" in raw
    assert json.loads(raw) == {"id": 1, "ts": "2026", "payload": "привет мир"}


def test_missing_table_rollback_existing_out(tmp_path):
    db = tmp_path / "e.db"
    out = tmp_path / "out.jsonl"
    make_db(db, with_table=False)
    out.write_text("sentinel", encoding="utf-8")
    with pytest.raises(RuntimeError):
        migrate(str(db), str(out))
    assert out.read_text(encoding="utf-8") == "sentinel"


def test_missing_table_no_out_created(tmp_path):
    db = tmp_path / "e.db"
    out = tmp_path / "out.jsonl"
    make_db(db, with_table=False)
    with pytest.raises(RuntimeError):
        migrate(str(db), str(out))
    assert not out.exists()


def test_missing_db_no_out_created(tmp_path):
    out = tmp_path / "out.jsonl"
    with pytest.raises(Exception):
        migrate(str(tmp_path / "nope.db"), str(out))
    assert not out.exists()


def test_no_tmp_files_left_on_failure(tmp_path):
    db = tmp_path / "e.db"
    out = tmp_path / "out.jsonl"
    make_db(db, with_table=False)
    with pytest.raises(RuntimeError):
        migrate(str(db), str(out))
    leftovers = [p for p in tmp_path.iterdir() if p.name != "e.db"]
    assert leftovers == []


def test_order_by_id_not_insert_order(tmp_path):
    db = tmp_path / "e.db"
    out = tmp_path / "out.jsonl"
    make_db(db, [(3, "t3", "c"), (1, "t1", "a"), (2, "t2", "b")])
    migrate(str(db), str(out))
    ids = [json.loads(line)["id"] for line in read_lines(out)]
    assert ids == [1, 2, 3]


def test_trailing_newline_format(tmp_path):
    db = tmp_path / "e.db"
    out = tmp_path / "out.jsonl"
    make_db(db, [(1, "t", "p")])
    migrate(str(db), str(out))
    raw = out.read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r" not in raw


def test_existing_wrong_content_overwritten(tmp_path):
    db = tmp_path / "e.db"
    out = tmp_path / "out.jsonl"
    make_db(db, [(1, "t", "p")])
    out.write_text("garbage\n", encoding="utf-8")
    assert migrate(str(db), str(out)) is True
    assert json.loads(read_lines(out)[0])["id"] == 1
