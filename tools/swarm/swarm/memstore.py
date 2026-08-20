"""Нижний слой памяти: файловое хранилище, идентичность репо и grounding."""
import contextlib
import fcntl
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import helpers  # noqa: E402 — каталог добавлен строкой выше
import obs  # noqa: E402

log = obs.get_logger("memory")

OUTCOMES = frozenset({"useful", "dead_end", "corrected"})
BODY_CAP = 700           # символов на урок: длиннее — уже не урок, а рассказ
DEFAULT_DB = "postgresql://dakh@localhost:5432/swarm_memory"
DEFAULT_BUDGET = 2000    # символов блока инъекции, ~500 токенов
DEFAULT_TOP_K = 5
PG_TIMEOUT = 20
BREAKER = 3              # как у хелперов: N отказов подряд — сеть лежит
SCHEMA_VERSION = 1

# Изменяемое состояние модуля (словарь — присваивание через global
# запрещено гейтом): предохранитель PG и флаг «о недоступности уже
# сказали» — журнал не должен получать memory_unavailable на каждый вызов.
_state: dict[str, Any] = {"failures": 0, "unavailable_logged": False}

OUTCOME_RU = {"useful": "урок", "dead_end": "тупик", "corrected": "решение"}


# --- идентичность ---------------------------------------------------------

def repo_identity(root: str | pathlib.Path) -> tuple[str, str]:
    """(repo, stand): чем ключуются уроки и чем ключуется reindex.

    repo — нормализованный origin (клоны одного репозитория ДЕЛЯТСЯ
    уроками через общую базу — это желаемое свойство), stand — realpath
    корня (reindex чистит только свои строки, чужие клоны не трогает).
    Без remote repo == stand: локальный репозиторий сам себе идентичность.
    """
    stand = str(pathlib.Path(root).resolve())
    r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=stand,
                       capture_output=True, text=True, check=False)
    remote = (r.stdout or "").strip()
    if r.returncode != 0 or not remote:
        return stand, stand
    # ssh/https-написания одного репозитория обязаны склеиваться:
    # git@host:a/b.git и https://host/a/b — один repo.
    remote = re.sub(r"^[a-z+]+://", "", remote)
    remote = re.sub(r"^[^@]*@", "", remote)
    remote = remote.replace(":", "/").removesuffix(".git").rstrip("/").lower()
    return remote, stand


def fingerprint(repo: str, body: str) -> str:
    """Идентичность урока — контентный отпечаток, а не координаты.

    Повторно наблюдённый тот же урок инкрементирует count вместо
    дубликата: уверенность рождается из повторяемости.
    """
    norm = re.sub(r"\s+", " ", body).strip().lower()
    return hashlib.sha256(f"{repo}\n{norm}".encode()).hexdigest()[:16]


# --- файловый слой (источник истины) --------------------------------------

class MemoryStore:
    def __init__(self, root: str | pathlib.Path,
                 swarm_dir: str = ".swarm") -> None:
        self.root = pathlib.Path(root)
        self.dir = self.root / swarm_dir / "memory"
        self.lessons_path = self.dir / "lessons.jsonl"
        self.digest_path = self.dir / "LESSONS.md"
        self.queries_path = self.dir / "queries.jsonl"
        self.lock_path = self.dir / "write.lock"
        self.journal_path = self.root / swarm_dir / "log" / "run.jsonl"

    def _ensure_dir(self) -> None:
        # Каталог создаётся ПЕРВОЙ ЗАПИСЬЮ, а не конструктором: чтение
        # (records, retrieve на пустой памяти) не имеет права оставлять
        # след на диске — ровно тот класс утечки в чужое дерево, что
        # ловил AUDIT-3 у метрик хелперов.
        self.dir.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Петля и операторский `swarm memory add` пишут конкурентно;
        окно append+digest — миллисекунды, ждём, а не отказываем."""
        self._ensure_dir()
        with self.lock_path.open("w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def append(self, record: dict[str, Any]) -> str:
        """Записать урок: scrub -> отпечаток -> строка -> свежий дайджест.

        Дайджест пересобирается В ТОЙ ЖЕ транзакции: окно «запись есть,
        сводка старая» — источник тихой лжи, знакомый по доске.
        """
        if record.get("outcome") not in OUTCOMES:
            raise ValueError(f"недопустимый outcome {record.get('outcome')!r}")
        body = helpers.scrub(str(record.get("body") or ""))[:BODY_CAP]
        if not body.strip():
            raise ValueError("пустой урок не записывается")
        record = dict(record, rec=record.get("rec", "lesson"), body=body)
        record.setdefault("id", fingerprint(str(record.get("repo") or ""), body))
        obs.stamp(record)
        with self._locked():
            with self.lessons_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.write_digest()
        return str(record["id"])

    def tombstone(self, lesson_id: str) -> None:
        """Забыть = пометить, а не стереть: reindex воспроизводим из файлов."""
        row = obs.stamp({"rec": "tombstone", "id": lesson_id})
        with self._locked():
            with self.lessons_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.write_digest()

    def records(self) -> list[dict[str, Any]]:
        """Живой свод: уроки с count по повторам, минус tombstone.

        Журнал читается как данные: битая строка пропускается, а не
        роняет свод.
        """
        folded: dict[str, dict[str, Any]] = {}
        dead: set[str] = set()
        if not self.lessons_path.exists():
            return []
        for line in self.lessons_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "")
            kind = row.get("rec", "lesson")
            if kind == "tombstone" and rid:
                dead.add(rid)
            elif kind == "correction" and rid in folded:
                folded[rid]["correction"] = row.get("correction")
                folded[rid]["outcome"] = "corrected"
            elif kind == "lesson" and rid:
                if rid in folded:
                    folded[rid]["count"] = int(folded[rid].get("count", 1)) + 1
                else:
                    folded[rid] = dict(row, count=1)
        return [r for rid, r in folded.items() if rid not in dead]

    # --- дайджест ---------------------------------------------------------

    def digest(self) -> str:
        """Детерминированный LESSONS.md: те же записи — те же байты.

        Уверенность — из повторяемости и показана В ТЕКСТЕ: потребитель
        (модель) видит «повторялось N×» против «единично, проверь».
        Уроки с мёртвыми якорями не удаляются и не подаются как правда —
        они уходят в «Отвязанные»: только контекст.
        """
        records = self.records()
        live: list[dict[str, Any]] = []
        unlinked: list[dict[str, Any]] = []
        for r in records:
            anchors = r.get("anchors") or []
            if anchors and not any(
                    resolve_anchor(a, self.root, self.journal_path)
                    for a in anchors if isinstance(a, dict)):
                unlinked.append(r)
            else:
                live.append(r)

        def order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return sorted(rows, key=lambda r: (-int(r.get("count", 1)),
                                               str(r.get("ts") or ""),
                                               str(r.get("id") or "")))

        def line(r: dict[str, Any]) -> str:
            count = int(r.get("count", 1))
            conf = f"повторялось {count}×" if count >= 2 else "единично"
            mark = OUTCOME_RU.get(str(r.get("outcome")), str(r.get("outcome")))
            return (f"- [{mark}, {conf}] ({r.get('id')}) "
                    f"{str(r.get('body') or '').strip()}")

        parts = [
            "# Уроки прогонов",
            "",
            (f"_Сгенерировано механически, без LLM, из lessons.jsonl "
             f"({len(records)} записей). Не редактировать руками._"),
            "",
        ]
        useful = order([r for r in live if r.get("outcome") != "dead_end"])
        if useful:
            parts += ["## Активные уроки", ""]
            parts += [line(r) for r in useful]
            parts.append("")
        dead_ends = order([r for r in live if r.get("outcome") == "dead_end"])
        if dead_ends:
            parts += ["## Тупики — не повторять", ""]
            parts += [line(r) for r in dead_ends]
            parts.append("")
        if unlinked:
            parts += ["## Отвязанные — якоря устарели, только контекст", ""]
            parts += [line(r) for r in order(unlinked)]
            parts.append("")
        if not records:
            parts += ["(память пуста)", ""]
        cons = self.dir / "consolidation.md"
        if cons.exists():
            # Единственная НЕдетерминированная секция — и потому она
            # живёт отдельным файлом с прямой пометкой происхождения:
            # LLM-текст не смешивается с механическим сводом.
            parts += ["## Сводка хелпера (LLM)", "",
                      ("_Написано дешёвой моделью из уроков выше; строки "
                       "без ссылок на id отброшены механически._"), "",
                      cons.read_text(encoding="utf-8").strip(), ""]
        return "\n".join(parts)

    def write_digest(self) -> None:
        self._ensure_dir()
        self.digest_path.write_text(self.digest(), encoding="utf-8")

    def write_consolidation(self, text: str) -> None:
        """LLM-сводка — отдельным файлом: дайджест включит её помеченной
        секцией, не смешивая с механическим сводом."""
        self._ensure_dir()
        (self.dir / "consolidation.md").write_text(text + "\n",
                                                   encoding="utf-8")
        self.write_digest()

    def log_query(self, **payload: Any) -> None:
        """Телеметрия использования: единственный честный ответ на вопрос
        «а пользуются ли памятью вообще» (и заодно выборка для E6)."""
        row = obs.stamp(dict(payload))
        try:
            self._ensure_dir()
            with self.queries_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            log.warning("телеметрия запросов памяти не записана", exc_info=True)


# --- grounding ------------------------------------------------------------

def file_fp(path: pathlib.Path) -> str | None:
    """Контентный отпечаток файла-якоря: ловит «файл есть, но изменился» —
    урок о прежнем содержимом может быть уже неправдой."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def resolve_anchor(anchor: dict[str, Any], root: pathlib.Path,
                   journal: pathlib.Path) -> bool:
    """Жив ли якорь. Неизвестный вид якоря НЕ считается живым: пропуск
    в память нельзя получить через непроверяемую ссылку."""
    kind, ref = str(anchor.get("kind") or ""), str(anchor.get("ref") or "")
    if not ref:
        return False
    if kind == "path":
        target = root / ref
        if not target.exists():
            return False
        fp = anchor.get("fp")
        # Отпечаток записан при создании урока: несовпадение значит, что
        # файл переписан, и якорь мёртв, хотя путь жив.
        return fp is None or file_fp(target) == fp
    if kind == "commit":
        r = subprocess.run(["git", "cat-file", "-e", f"{ref}^{{commit}}"],
                           cwd=root, capture_output=True, check=False)
        return r.returncode == 0
    if kind == "task":
        if not journal.exists():
            return False
        needle = f'"task": "{ref}"'
        try:
            return needle in journal.read_text(encoding="utf-8")
        except OSError:
            return False
    if kind == "symbol":
        # git grep -w: символ ещё существует в отслеживаемом коде. Дёшево
        # и честно; точный резолв (codemap) здесь был бы платой ctags за
        # каждый вызов ворот.
        r = subprocess.run(["git", "grep", "-q", "-w", "--", ref],
                           cwd=root, capture_output=True, check=False)
        return r.returncode == 0
    return False


def admissible(record: dict[str, Any], root: pathlib.Path,
               journal: pathlib.Path) -> tuple[bool, str]:
    """Пропуск в память: useful обязан цитировать живой якорь.

    dead_end и corrected принимаются и без якорей: тупик часто
    ссылается на то, чего больше нет, — в этом его природа.
    """
    if record.get("outcome") != "useful":
        return True, ""
    anchors = [a for a in (record.get("anchors") or []) if isinstance(a, dict)]
    if any(resolve_anchor(a, root, journal) for a in anchors):
        return True, ""
    return False, ("useful-урок требует хотя бы один живой якорь "
                   "(--anchor путь/коммит/id задачи): память без якорей "
                   "нефальсифицируема")

