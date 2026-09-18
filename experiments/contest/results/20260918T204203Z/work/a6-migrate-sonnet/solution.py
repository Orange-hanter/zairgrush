import json
import os
import sqlite3
import tempfile


def migrate(db_path: str, out_path: str) -> bool:
    """
    Migrate the `events` table from an SQLite database at `db_path`
    into a JSONL file at `out_path`.

    Returns True if `out_path` was (re)written, False if it already
    contained exactly the expected content (idempotent no-op).

    Raises RuntimeError on any failure (missing db file, missing
    `events` table, corrupted db, etc.) and leaves `out_path`
    untouched in that case.
    """
    if not os.path.exists(db_path):
        raise RuntimeError(f"Database file not found: {db_path}")

    rows = None
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
            )
            if cur.fetchone() is None:
                raise RuntimeError("Table 'events' does not exist")

            cur.execute("SELECT id, ts, payload FROM events ORDER BY id ASC")
            rows = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise RuntimeError(f"Failed to read database: {e}") from e

    if rows is None:
        raise RuntimeError("Failed to read database: unknown error")

    lines = []
    for row_id, ts, payload in rows:
        obj = {"id": row_id, "ts": ts, "payload": payload}
        lines.append(json.dumps(obj, ensure_ascii=False))

    if lines:
        content = ("\n".join(lines) + "\n").encode("utf-8")
    else:
        content = b""

    if os.path.exists(out_path):
        try:
            with open(out_path, "rb") as f:
                existing = f.read()
        except OSError as e:
            raise RuntimeError(f"Failed to read existing output file: {e}") from e

        if existing == content:
            return False

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."

    fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".migrate_tmp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    return True
