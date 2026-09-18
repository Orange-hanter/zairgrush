```python
import json
import os
import sqlite3
import tempfile


def migrate(db_path: str, out_path: str) -> bool:
    """
    Мигрирует таблицу events(id, ts, payload) из SQLite базы db_path
    в JSONL файл out_path.

    Возвращает True, если файл был (пере)записан, False если содержимое
    не изменилось (идемпотентный no-op).

    При любой ошибке (нет таблицы events, битая база, отсутствующий файл БД
    и т.п.) бросает RuntimeError, существующий out_path не изменяется,
    временные файлы не остаются.
    """
    if not os.path.exists(db_path):
        raise RuntimeError(f"Database file not found: {db_path}")

    conn = None
    try:
        try:
            conn = sqlite3.connect(db_path)
        except sqlite3.Error as e:
            raise RuntimeError(f"Cannot open database: {e}") from e

        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                ("events",),
            )
            if cur.fetchone() is None:
                raise RuntimeError("Table 'events' does not exist")

            cur.execute("SELECT id, ts, payload FROM events ORDER BY id ASC")
            rows = cur.fetchall()
        except sqlite3.Error as e:
            raise RuntimeError(f"Database error while reading events: {e}") from e
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    lines = []
    for row_id, ts, payload in rows:
        obj = {"id": row_id, "ts": ts, "payload": payload}
        lines.append(json.dumps(obj, ensure_ascii=False))

    if lines:
        new_content = ("\n".join(lines) + "\n").encode("utf-8")
    else:
        new_content = b""

    if os.path.exists(out_path):
        try:
            with open(out_path, "rb") as f:
                existing = f.read()
        except OSError as e:
            raise RuntimeError(f"Cannot read existing out_path: {e}") from e

        if existing == new_content:
            return False

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."

    fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".tmp_migrate_", suffix=".jsonl")
    try:
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(new_content)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            raise

        os.replace(tmp_path, out_path)
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        if isinstance(e, RuntimeError):
            raise
        raise RuntimeError(f"Failed to write output file: {e}") from e

    return True
```