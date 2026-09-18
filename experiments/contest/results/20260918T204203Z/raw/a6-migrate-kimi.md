 ```python
import json
import os
import sqlite3
import tempfile


def migrate(db_path: str, out_path: str) -> bool:
    if not os.path.exists(db_path):
        raise RuntimeError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        try:
            rows = cur.execute(
                "SELECT id, ts, payload FROM events ORDER BY id ASC"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                raise RuntimeError("Table 'events' not found") from exc
            raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    lines = []
    for row in rows:
        obj = {"id": row[0], "ts": row[1], "payload": row[2]}
        lines.append((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    expected = b"".join(lines)

    if os.path.exists(out_path):
        with open(out_path, "rb") as f:
            if f.read() == expected:
                return False

    out_dir = os.path.dirname(out_path) or "."
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=out_dir, prefix=".migrate_", suffix=".tmp", delete=False, mode="wb"
        ) as tmp:
            tmp_path = tmp.name
            tmp.write(expected)
        os.replace(tmp_path, out_path)
        return True
    except Exception:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise
```