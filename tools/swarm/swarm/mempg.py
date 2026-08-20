# ruff: noqa: SLF001
# SLF001 — долг декомпозиции memory.py: тесты патчат `memory.<имя>`,
# поэтому подмодули обращаются к shim'у по имени (`memory.pg` и т.п.).
# Снятие долга = публичные имена в memstore с синхронной правкой тестов.
"""PG-производный индекс памяти (FTS, вектор, sync/reindex)."""

from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import memory  # noqa: E402
import obs  # noqa: E402

log = obs.get_logger("memory")


def _cfg_db(config: dict[str, Any]) -> str:
    return str(config.get("memory_db") or memory.DEFAULT_DB)


def pg(config: dict[str, Any], sql: str,
       sql_vars: dict[str, str] | None = None,
       stdin: str | None = None) -> tuple[bool, str]:
    """Единственная дверь к psql. Данные — ТОЛЬКО через -v (psql сам
    экранирует `:'name'`) или через переданный поток (COPY): значение,
    склеенное в текст SQL, — это инъекция, и мутационный аудит обязан
    ловить такую правку.
    """
    if memory._state["failures"] >= memory.BREAKER:
        return False, "предохранитель открыт: PG недоступен"
    cmd = ["psql", _cfg_db(config), "--no-psqlrc", "-X", "-q",
           "-v", "ON_ERROR_STOP=1", "-At"]
    for key, value in (sql_vars or {}).items():
        cmd += ["-v", f"{key}={value}"]
    cmd += ["-f", "-"]
    payload = sql if stdin is None else f"{sql}\n{stdin}"
    try:
        r = memory.subprocess.run(cmd, input=payload, capture_output=True,
                           text=True, timeout=memory.PG_TIMEOUT, check=False)
    except (OSError, memory.subprocess.SubprocessError) as e:
        memory._state["failures"] += 1
        return False, f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        memory._state["failures"] += 1
        return False, (r.stderr or "").strip()[:300]
    memory._state["failures"] = 0
    return True, r.stdout.strip()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS lessons (
  id text PRIMARY KEY,
  repo text NOT NULL, stand text NOT NULL,
  ts timestamptz NOT NULL, run_id text, swarm_sha text, goal text,
  source text NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('useful','dead_end','corrected')),
  correction text, task_id text, title text,
  body text NOT NULL,
  anchors jsonb NOT NULL DEFAULT '[]',
  anchors_ok boolean, validated_ts timestamptz,
  count integer NOT NULL DEFAULT 1,
  tags jsonb NOT NULL DEFAULT '[]',
  tombstone boolean NOT NULL DEFAULT false,
  tsv tsvector GENERATED ALWAYS AS
      (to_tsvector('russian', coalesce(title,'') || ' ' || body)) STORED
);
CREATE INDEX IF NOT EXISTS lessons_tsv  ON lessons USING gin (tsv);
CREATE INDEX IF NOT EXISTS lessons_repo ON lessons (repo) WHERE NOT tombstone;
INSERT INTO meta (key, value) VALUES ('schema_version', '1'::jsonb)
  ON CONFLICT (key) DO NOTHING;
"""


def ensure_schema(config: dict[str, Any]) -> bool:
    result: tuple[bool, str] = memory.pg(config, SCHEMA_SQL)
    return result[0]


_ROW_FIELDS = ("id", "repo", "stand", "ts", "run_id", "swarm_sha", "goal",
               "source", "outcome", "correction", "task_id", "title",
               "body", "anchors", "count", "tags", "tombstone")
_ROW_TYPES = ("id text, repo text, stand text, ts timestamptz, run_id text, "
              "swarm_sha text, goal text, source text, outcome text, "
              "correction text, task_id text, title text, body text, "
              "anchors jsonb, count int, tags jsonb, tombstone boolean")
_UPSERT_CHUNK = 200      # -v едет через argv; сотни уроков — с запасом


def _payload_row(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(rec.get("id") or ""), "repo": str(rec.get("repo") or ""),
        "stand": str(rec.get("stand") or ""), "ts": str(rec.get("ts") or ""),
        "run_id": rec.get("run_id"), "swarm_sha": rec.get("swarm_sha"),
        "goal": rec.get("goal"), "source": str(rec.get("source") or "mechanical"),
        "outcome": str(rec.get("outcome") or ""),
        "correction": rec.get("correction"), "task_id": rec.get("task_id"),
        "title": rec.get("title"), "body": str(rec.get("body") or ""),
        "anchors": rec.get("anchors") or [],
        "count": int(rec.get("count", 1)), "tags": rec.get("tags") or [],
        "tombstone": bool(rec.get("tombstone")),
    }


def upsert(config: dict[str, Any], records: list[dict[str, Any]]) -> bool:
    """Идемпотентная досыпка индекса: конфликт по id обновляет count.

    Все записи едут ОДНОЙ -v-переменной как jsonb-массив: COPY со
    встроенными данными отброшен по факту — PG 18 больше не признаёт
    `\\.` как конец данных в CSV-режиме, и хвост скрипта читался как
    строки таблицы.
    """
    if not records:
        return True
    cols = ", ".join(_ROW_FIELDS)
    # S608: в тексте SQL — только константы модуля; данные — в :'payload'.
    sql = (
        f"INSERT INTO lessons ({cols})\n"
        f"SELECT {cols} FROM jsonb_to_recordset(:'payload'::jsonb)\n"
        f"  AS r({_ROW_TYPES})\n"
        "ON CONFLICT (id) DO UPDATE SET count = excluded.count,\n"
        "  ts = excluded.ts, tombstone = excluded.tombstone,\n"
        "  correction = excluded.correction, outcome = excluded.outcome;"
    )
    for start in range(0, len(records), _UPSERT_CHUNK):
        chunk = records[start:start + _UPSERT_CHUNK]
        payload = json.dumps([_payload_row(r) for r in chunk],
                             ensure_ascii=False)
        ok, out = memory.pg(config, sql, {"payload": payload})
        if not ok:
            log.warning("память: индекс не пополнен: %s", out)
            return False
    return True


def reindex(config: dict[str, Any], store: memory.MemoryStore,
            repo: str, stand: str) -> tuple[int, int]:
    """Пересборка индекса из файлов. Чистит ТОЛЬКО свой stand: соседний
    клон того же репозитория делится уроками, а не данными на убой."""
    if not memory.ensure_schema(config):
        return (0, 0)
    ok, _ = memory.pg(config, "DELETE FROM lessons WHERE stand = :'stand';",
               {"stand": stand})
    if not ok:
        return (0, 0)
    records = [dict(r, repo=repo, stand=stand) for r in store.records()]
    if records and not memory.upsert(config, records):
        return (0, 0)
    memory._backfill_embeddings(config, records)
    return (1, len(records))


def _backfill_embeddings(config: dict[str, Any],
                         records: list[dict[str, Any]]) -> int:
    """Досыпать вектора после пересборки. Сбой эмбеддера не событие:
    строка остаётся искомой через FTS, вектор догонит следующий
    reindex/sync. Возвращает число записанных векторов."""
    model = str(config.get("memory_embed_model") or "")
    if not model or not records:
        return 0
    vectors: list[tuple[str, list[float]]] = []
    for rec in records:
        text = f"{rec.get('title') or ''} {rec.get('body') or ''}".strip()
        vec = memory.helpers.embed_text(text, model)
        if vec:
            vectors.append((str(rec.get("id")), vec))
    if not vectors:
        return 0
    if not memory.ensure_vector(config, len(vectors[0][1]), repin=True):
        return 0
    written = 0
    for lesson_id, vec in vectors:
        ok, _out = memory.pg(config, "UPDATE lessons SET embedding = :'qv'::vector "
                              "WHERE id = :'lid';",
                      {"qv": _vec_literal(vec), "lid": lesson_id})
        if ok:
            written += 1
    return written


def index_enabled(config: dict[str, Any]) -> bool:
    """Право трогать общий PG-индекс вне ручных команд.

    Два независимых входа: включённый эксперимент памяти (инъекции нужен
    свежий индекс) или явный opt-in стенда `memory_index = "auto"` —
    автоматическая индексация БЕЗ инъекции, чтобы write-side копил
    строки и вектора, пока A/B по инъекции ещё не решён. Дефолт —
    manual: прогон с дефолтным конфигом не имеет права трогать ОБЩУЮ
    базу, тесты петли уже сорили в неё уроками с временных стендов.
    """
    if str((config.get("experiments") or {}).get("memory", "off")) != "off":
        return True
    return str(config.get("memory_index") or "manual") == "auto"


def sync(config: dict[str, Any], store: memory.MemoryStore, repo: str,
         stand: str, embed_cap: int = 64) -> tuple[int, int, int] | None:
    """Инкрементальная досыпка индекса до файлов: строки, затем вектора.

    Родилась из замера: OpenRouter показал НОЛЬ вызовов эмбеддера за
    весь пилот — вектора появлялись только от ручного `reindex`, который
    никто не запускал. Идемпотентна и самовосстанавливающаяся: upsert
    всех файловых записей (конфликт по id лишь обновляет счётчики),
    затем эмбеддинг строк БЕЗ вектора — не больше `embed_cap` за вызов,
    остаток догонит следующий sync. Ручной `reindex` остаётся операцией
    ПЕРЕСБОРКИ (DELETE + полная досыпка); sync никогда не удаляет.

    Любой сбой PG -> None: индекс производный, прогон живёт (§7.3).
    Возвращает (строк стенда в индексе, векторов добавлено, осталось
    без вектора).
    """
    if not memory.ensure_schema(config):
        return None
    records = [dict(r, repo=repo, stand=stand) for r in store.records()]
    if records and not memory.upsert(config, records):
        return None
    added = 0
    missing: list[str] = []
    if str(config.get("memory_embed_model") or ""):
        ok, out = memory.pg(config,
                     "SELECT id FROM lessons WHERE stand = :'stand' "
                     "AND embedding IS NULL AND NOT tombstone;",
                     {"stand": stand})
        if not ok:
            return None
        missing = [ln.strip() for ln in out.splitlines() if ln.strip()]
        by_id = {str(r.get("id")): r for r in records}
        batch = [by_id[i] for i in missing[:embed_cap] if i in by_id]
        added = memory._backfill_embeddings(config, batch)
    return (len(records), added, max(len(missing) - added, 0))


def _repo_filter() -> str:
    """Фильтр repo — одна формула на все запросы чтения: две копии
    разошлись бы, и уроки потекли бы между репозиториями."""
    return "repo = :'repo' AND NOT tombstone"


def search_fts(config: dict[str, Any], repo: str, query: str,
               k: int) -> list[dict[str, Any]] | None:
    """FTS-поиск. None = хранилище недоступно (отличать от «не нашлось»)."""
    # S608: подставляется только _repo_filter() — константная формула
    # модуля; данные (repo, q, k) едут через -v-переменные psql.
    sql = (
        "SELECT coalesce(json_agg(t), '[]'::json) FROM ("  # noqa: S608 — данные через -v
        "SELECT id, outcome, body, title, task_id, count, anchors_ok "
        f"FROM lessons WHERE {_repo_filter()} "
        "AND tsv @@ websearch_to_tsquery('russian', :'q') "
        "ORDER BY ts_rank(tsv, websearch_to_tsquery('russian', :'q')) DESC, "
        "count DESC, id LIMIT :k) t;"
    )
    ok, out = memory.pg(config, sql, {"repo": repo, "q": query, "k": str(int(k))})
    if not ok:
        return None
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        return None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _vec_literal(vec: list[float]) -> str:
    """Литерал pgvector: данные остаются данными и едут через -v."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def ensure_vector(config: dict[str, Any], dim: int,
                  repin: bool = False) -> bool:
    """Расширение vector + колонка + пин размерности в meta.

    Рассинхрон размерности (сменили модель эмбеддера) — отказ с
    подсказкой, а не тихая каша из несравнимых векторов. Перепинить
    размерность вправе ТОЛЬКО reindex (`repin=True`): он и так
    пересобирает индекс с нуля. Первая версия отказывала и ему — и
    подсказка «нужен reindex» отправляла оператора по кругу (поймано
    живым smoke, findings E9).
    """
    ok, _out = memory.pg(config, "CREATE EXTENSION IF NOT EXISTS vector;")
    if not ok:
        return False
    ok, out = memory.pg(config,
                 "SELECT value FROM meta WHERE key = 'embed_dim';")
    if not ok:
        return False
    if out and out != str(int(dim)):
        if not repin:
            log.warning("память: размерность эмбеддера изменилась "
                        "(%s -> %s): нужен `swarm memory reindex`", out, dim)
            return False
        # Колонка общая на таблицу: чужие вектора при смене размерности
        # обнуляются — их вернёт reindex соответствующего стенда. FTS у
        # всех живёт непрерывно, теряется только сеть доп. охвата.
        log.warning("память: размерность перепинена (%s -> %s), вектора "
                    "других стендов вернёт их reindex", out, dim)
        ok, _out = memory.pg(config,
                      "ALTER TABLE lessons DROP COLUMN IF EXISTS embedding;"
                      "UPDATE meta SET value = :'dim'::jsonb "
                      "WHERE key = 'embed_dim';",
                      {"dim": str(int(dim))})
        if not ok:
            return False
    # ALTER с типом vector(N) не параметризуется -v: N прошёл через int().
    sql = (f"ALTER TABLE lessons ADD COLUMN IF NOT EXISTS "
           f"embedding vector({int(dim)});\n"
           "INSERT INTO meta (key, value) VALUES ('embed_dim', :'dim'::jsonb)"
           " ON CONFLICT (key) DO NOTHING;")
    result: tuple[bool, str] = memory.pg(config, sql, {"dim": str(int(dim))})
    return result[0]


def search_vec(config: dict[str, Any], repo: str, vec: list[float],
               k: int) -> list[dict[str, Any]] | None:
    """Векторная сеть дополнительного охвата. None = хранилище недоступно."""
    # S608: подставляется только _repo_filter(); вектор — в -v-переменной.
    sql = (
        "SELECT coalesce(json_agg(t), '[]'::json) FROM ("  # noqa: S608 — данные через -v
        "SELECT id, outcome, body, title, task_id, count, anchors_ok "
        f"FROM lessons WHERE {_repo_filter()} "
        "AND embedding IS NOT NULL "
        "ORDER BY embedding <=> :'qv'::vector, id LIMIT :k) t;"
    )
    ok, out = memory.pg(config, sql, {"repo": repo, "qv": _vec_literal(vec),
                               "k": str(int(k))})
    if not ok:
        return None
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        return None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


_TOKEN = re.compile(r"[\wа-яё]{4,}", re.IGNORECASE)


def _local_scan(records: list[dict[str, Any]], query: str,
                k: int) -> list[dict[str, Any]]:
    """Фолбэк без PG: пересечение редких токенов. Не ранжирование мечты,
    но честный поиск, который работает на выключенной базе."""
    toks = {t.lower() for t in _TOKEN.findall(query)}
    if not toks:
        return []
    scored = []
    for r in records:
        text = f"{r.get('title') or ''} {r.get('body') or ''}".lower()
        score = sum(text.count(t) for t in toks)
        if score:
            scored.append((score, int(r.get("count", 1)), str(r.get("id")), r))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [r for _s, _c, _i, r in scored[:k]]

