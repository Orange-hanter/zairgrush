# ruff: noqa: E402
"""Память между прогонами (E9): уроки, дайджест, поиск, инъекция.

Устройство повторяет проверенную двухъярусную схему: эпизодические записи
append-only в файлах + детерминированный дайджест, пересобираемый на
каждой записи. Файлы — источник истины (ADR-001: jsonl первичен);
Postgres — ПРОИЗВОДНЫЙ пересобираемый индекс для поиска, как board.html —
производный рендер журнала. «PG упал» деградирует качество поиска, но
никогда — сохранность данных и сам прогон.

Три правила, ради которых модуль существует:

1. **Grounding как пропуск.** useful-урок не принимается без хотя бы
   одного ЖИВОГО якоря (файл существует / коммит в истории / задача в
   журнале). Память без якорей нефальсифицируема — а значит, копит
   галлюцинации с уверенным видом.
2. **Fail-open на каждом пути.** Отсутствие памяти не имеет права
   стоить прогона: любой сбой хранилища, поиска или рендера — пустой
   блок и запись в диагностику, не исключение наружу.
3. **Память — данные, а не инструкции.** Текст уроков попадает в
   промпты; инъекция всегда несёт преамбулу об этом, тексты проходят
   секрет-фильтр на записи, бюджет блока ограничен, каждая строка несёт
   id — оператор может отследить происхождение любого слова.

SQL идёт только через одну точку `pg()`: данные — в `-v`-переменных psql
(он сам экранирует `:'name'`), пачки — jsonb-массивом в одной переменной;
интерполяция данных в текст SQL запрещена. psql, а не psycopg: инструмент живёт
запуском процессов (kimi/claude/git/ctags), и первый pip-пакет — событие,
которое не случается ради сотни строк в таблице.
"""
import pathlib
import subprocess
import sys

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import helpers
import obs

log = obs.get_logger("memory")

# Тесты перезагружают memory.py через importlib.util; без принудительной
# перезагрузки плоские модули остались бы привязаны к предыдущей копии
# memory, и патчи `memory.<name>` новой копии не брались бы.
for _mem_mod in ("memstore", "mempg", "meminject", "memreflect"):
    sys.modules.pop(_mem_mod, None)

from meminject import (
    _render_hits,
    enabled_for,
    inject_block,
    norms_block,
    retrieve,
)
from mempg import (
    _cfg_db,
    _payload_row,
    _repo_filter,
    _vec_literal,
    backfill_embeddings,
    ensure_schema,
    ensure_vector,
    index_enabled,
    local_scan,
    pg,
    reindex,
    search_fts,
    search_vec,
    sync,
    upsert,
)
from memreflect import (
    _commit_paths,
    _commit_symbols,
    _propose_policies,
    _trajectory,
    fp_candidates,
    record_task_outcome,
    reflect_after_run,
)
from memstore import (
    BODY_CAP,
    BREAKER,
    DEFAULT_BUDGET,
    DEFAULT_DB,
    DEFAULT_TOP_K,
    OUTCOME_RU,
    OUTCOMES,
    PG_TIMEOUT,
    SCHEMA_VERSION,
    MemoryStore,
    admissible,
    breaker_state,
    file_fp,
    fingerprint,
    repo_identity,
    resolve_anchor,
)

__all__ = (
    "BODY_CAP",
    "BREAKER",
    "DEFAULT_BUDGET",
    "DEFAULT_DB",
    "DEFAULT_TOP_K",
    "OUTCOMES",
    "OUTCOME_RU",
    "PG_TIMEOUT",
    "SCHEMA_VERSION",
    "MemoryStore",
    "_cfg_db",
    "_commit_paths",
    "_commit_symbols",
    "_payload_row",
    "_propose_policies",
    "_render_hits",
    "_repo_filter",
    "_trajectory",
    "_vec_literal",
    "admissible",
    "backfill_embeddings",
    "breaker_state",
    "enabled_for",
    "ensure_schema",
    "ensure_vector",
    "file_fp",
    "fingerprint",
    "fp_candidates",
    "helpers",
    "index_enabled",
    "inject_block",
    "local_scan",
    "log",
    "norms_block",
    "pg",
    "record_task_outcome",
    "reflect_after_run",
    "reindex",
    "repo_identity",
    "resolve_anchor",
    "retrieve",
    "search_fts",
    "search_vec",
    "subprocess",
    "sync",
    "upsert",
)
