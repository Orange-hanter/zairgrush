```python
import json
import os
import sqlite3
import tempfile


def migrate(db_path: str, out_path: str) -> bool:
    """
    Одноразовая миграция таблицы events из SQLite в JSONL.

    Возвращает True, если out_path был (пере)записан, False - если
    существующий out_path уже соответствует ожидаемому содержимому
    (идемпотентный no-op).

    При любой ошибке (нет БД, нет таблицы events, битая БД и т.п.)
    существующий out_path остаётся неизменным, временные файлы удаляются,
    исключение распространяется наружу.
    """
    # --- 1. Читаем данные из БД, не трогая out_path ---
    if not os.path.isfile(db_path):
        raise RuntimeError(f"database file not found: {db_path}")

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as e:
        raise RuntimeError(f"failed to open database: {e}") from e

    try:
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                ("events",),
            )
            if cur.fetchone() is None:
                raise RuntimeError("table 'events' does not exist")

            cur = conn.execute("SELECT id, ts, payload FROM events ORDER BY id ASC")
            lines = []
            for row in cur:
                obj = {"id": row[0], "ts": row[1], "payload": row[2]}
                lines.append(json.dumps(obj, ensure_ascii=False))

            if lines:
                content = ("\n".join(lines) + "\n").encode("utf-8")
            else:
                content = b""
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"failed to read table 'events': {e}") from e
    finally:
        conn.close()

    # --- 2. Сравнение с существующим содержимым ---
    if os.path.exists(out_path):
        try:
            with open(out_path, "rb") as f:
                existing = f.read()
            if existing == content:
                return False
        except OSError:
            # если не удалось прочитать существующий файл - продолжаем
            # попытку записи (перезапишем корректно)
            pass

    # --- 3. Атомарная запись через временный файл в том же каталоге ---
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_migrate_", dir=out_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, out_path)
        tmp_path = None
        return True
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
```