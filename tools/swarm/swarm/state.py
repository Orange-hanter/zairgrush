"""Состояние петли: очередь задач, step-journal, атомарная запись.

То, чего не было ни в одном прототипе: раннеры бенча держали состояние в
памяти и не умели возобновляться. Здесь реализованы требования §4.3 и
§5.6 дизайн-дока.

Ключевые свойства:
- единственный писатель — оркестратор; запись атомарная (временный файл +
  rename), поверх — advisory-блокировка, чтобы второй запуск не испортил
  состояние;
- **step-journal**: у каждого side-effect'а (коммит, смена статуса) есть
  запись «намерение» до и «сделано» после. Без этого падение между
  коммитом и записью статуса даёт повторную итерацию или дубль коммита
  (§5.6);
- очередь — состояние ПРОГОНА, а не общий файл: урок E8, где статусы от
  прошлого запуска молча обнулили выборку задач.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from types import TracebackType
from typing import IO, Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом, и
# межмодульный импорт по имени иначе не работает. Вставка защищена от
# повтора; альтернатива — шестая копия importlib-обвязки из cli.py.
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import obs  # noqa: E402 — каталог добавлен строкой выше

log = obs.get_logger("state")

LEGAL_STATUS = {"pending", "in_progress", "in_review", "done", "blocked"}
TERMINAL = {"done", "blocked"}

# Файлы, принадлежащие ПЕТЛЕ и оператору, а не задаче: конфиг прогона и
# состояние роя. Ни одно git-представление работы агента их не касается:
# страж границ не судит, revert/commit/stash не трогают, ревьюер не
# видит. Урок PILOT-1 (2026-08-19): незакоммиченный swarm.toml уехал в
# стеш терминального исхода, перезапуск застал чистое дерево, а
# восстановленный из стеша конфиг был прочитан как работа агента — и
# исполнитель послушно откатил два решения владельца. Журнал при этом
# сказал только scope_violation, ни слова «решение потеряно».
OWNED_ROOTS = ("swarm.toml", ".swarm")
# То же множество языком git pathspec — для diff.
OWNED_EXCLUDE_PATHSPECS = tuple(f":(exclude){p}" for p in OWNED_ROOTS)


def owned_by_loop(path: str) -> bool:
    """Файл принадлежит оркестратору, а не задаче."""
    return any(path == r or path.startswith(r + "/") for r in OWNED_ROOTS)


class StateError(Exception):
    """Состояние на диске противоречиво — петля обязана остановиться."""


def tasks_sha(blob: str) -> str:
    """Отпечаток очереди. Одна функция на запись и на проверку: две копии
    формулы разошлись бы, и целостность начала бы врать в обе стороны."""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def last_declared_sha(journal_path: pathlib.Path) -> str | None:
    """Последний отпечаток, объявленный законной записью состояния."""
    if not journal_path.exists():
        return None
    sha = None
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("kind") == "state_written" and row.get("sha"):
            sha = str(row["sha"])
    return sha


def _atomic_write(path: pathlib.Path, text: str) -> None:
    """Временный файл + rename, с fsync до и после.

    Голый rename атомарен для ПРОЦЕССА, но не для машины: без fsync
    содержимое живёт в кэше страниц, и падение хоста может оставить
    переименованный файл пустым или каталог — без записи о rename.
    Для файла, в котором лежит вся очередь задач, это невосстановимо.

    Имя временного файла уникально на писателя: парный стенд и дуэль
    пишут фазы из двух потоков, и общее имя `*.tmp` давало гонку —
    чужой rename уносил файл до replace, и наблюдение падало с
    FileNotFoundError (поймано смоуком pair 2026-09-15). Литер
    писателя в имени гонку снимает; атомарность rename от неё не
    зависит.
    """
    tmp = path.with_suffix(
        path.suffix + f".tmp-{os.getpid()}-{threading.get_ident()}")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return                      # fsync каталога поддержан не везде
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def record_decision(task: dict[str, Any], text: str) -> None:
    """Добавить решение человека в задачу, НЕ затирая прежние.

    Одно поле `human_answer` означало, что каждый следующий ответ стирает
    предыдущий, и задача закрывалась вопреки уже принятому решению. На
    приёмке v3st так и вышло: архитектурное «вынеси валидатор в
    _utils.py» стёрлось техническим ответом на следующий вопрос, ревьюер
    его уже не видел и одобрил инлайн-вариант.

    Функция существует отдельно, потому что писателей у этого поля ДВА, а
    правило знал один. `swarm retry --note` присваивал `human_answer`
    напрямую — и служебная записка оператора («прогон остановлен, вернул
    в очередь») занимала место решения владельца. Поймано на себе
    2026-08-24: после ручной остановки прогона v9lb исполнитель получил
    бы мою уборочную заметку ВМЕСТО двух ответов на q022/q023. Сами
    решения уцелели в `human_decisions`, но в промпт уезжает
    производное поле — то есть потеря была полной там, где она важна.

    Урок общий: правило, записанное в одном из двух путей к полю, — не
    правило, а совпадение.
    """
    prior = task.get("human_decisions") or (
        [task["human_answer"]] if task.get("human_answer") else [])
    if text not in prior:
        prior.append(text)
    task["human_decisions"] = prior
    task["human_answer"] = (prior[0] if len(prior) == 1
                            else "\n".join(f"- {d}" for d in prior))


class SwarmState:
    def __init__(self, root: str | pathlib.Path,
                 swarm_dir: str = ".swarm") -> None:
        self.root = pathlib.Path(root)
        self.dir = self.root / swarm_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "log").mkdir(exist_ok=True)
        self._self_ignore(swarm_dir)
        self.tasks_path = self.dir / "tasks.json"
        self.journal_path = self.dir / "log" / "run.jsonl"
        self.metrics_path = self.dir / "metrics.jsonl"
        self.lock_path = self.dir / "state.lock"
        self.write_lock_path = self.dir / "write.lock"
        # Отметка о настоящем (см. `phase`): единственный файл, который
        # существует, ПОКА фаза идёт, и исчезает, когда она кончилась.
        self.now_path = self.dir / "now.json"
        self._lock: IO[str] | None = None
        self._mutating = False
        self._phases: list[dict[str, Any]] = []

    def _self_ignore(self, swarm_dir: str) -> None:
        """Состояние петли не должно выглядеть как чужие правки.

        §6.3 требует держать `.swarm/` вне git. Пишем не в `.gitignore`
        пользователя, а в `.git/info/exclude` — локальный игнор, который
        не требует коммита и не меняет файлы целевого проекта. Без этого
        preflight §5.1 срабатывает на собственное состояние оркестратора
        (поймано тестами CLI).
        """
        git_dir = self.root / ".git"
        if git_dir.is_file():                      # worktree: .git — файл-ссылка
            try:
                target = git_dir.read_text().split("gitdir:", 1)[1].strip()
                git_dir = pathlib.Path(target)
            except (IndexError, OSError):
                return
        exclude = git_dir / "info" / "exclude"
        if not exclude.parent.exists():
            return
        entry = f"/{swarm_dir}/"
        try:
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if entry not in current:
                with exclude.open("a", encoding="utf-8") as f:
                    f.write(f"\n# состояние петли агентов (swarm)\n{entry}\n")
        except OSError:
            pass

    # --- блокировка -------------------------------------------------------

    def acquire(self) -> None:
        """Один живой оркестратор на репозиторий (§4.3)."""
        self._lock = self.lock_path.open("w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self._lock.close()
            self._lock = None
            raise StateError(
                "состояние занято другим запуском swarm (state.lock)") from e
        self._lock.write(f"{os.getpid()}\n")
        self._lock.flush()

    def release(self) -> None:
        if self._lock:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def is_running(self) -> bool:
        """Жив ли прогон — то есть держит ли кто-то блокировку состояния.

        Не по pid из файла: pid переиспользуется системой, и мёртвый
        прогон изредка «оживал» чужим процессом с тем же номером. Тот же
        флок, которым петля объявляет себя единственным писателем (§4.3),
        отвечает на вопрос точно: захватился — держать некому.

        Ответ нужен доске: файл `now.json`, переживший убитый процесс,
        и фаза, которая идёт прямо сейчас, — это один и тот же файл, и
        различает их только живость петли.
        """
        if self._lock is not None:
            return True            # держим сами: доска строится внутри петли
        try:
            fh = self.lock_path.open("r")
        except OSError:
            return False           # файла нет — блокировку никто не брал
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True            # занят — на том конце живой оркестратор
        else:
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
        finally:
            fh.close()

    def __enter__(self) -> "SwarmState":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    @contextlib.contextmanager
    def mutate(self) -> Iterator[None]:
        """Короткая транзакция load→modify→save: один писатель на ЗАПИСЬ.

        Прогонная блокировка (state.lock) живёт весь запуск и операторским
        командам не подходит: инбокс задуман, чтобы отвечать ВО ВРЕМЯ
        прогона. Но совсем без замка составное load→modify→save из
        `swarm answer` и set_status бегущей петли теряли запись друг друга
        (lost update), а проверка целостности пропажу не видит: она
        сверяет отпечаток с последним ОБЪЯВЛЕННЫМ, и последняя запись
        объявлена честно — просто собрана из устаревшего чтения.

        Поэтому замок отдельный и короткий — на окно самой транзакции.
        Ждём, а не отказываем: окно — миллисекунды, и «идёт прогон —
        приходите позже» здесь было бы лекарством хуже болезни.
        Реентерабелен в пределах экземпляра: вложенная транзакция уже
        под замком внешней, второй flock того же файла в том же процессе
        был бы самоблокировкой.
        """
        if self._mutating:
            yield
            return
        with self.write_lock_path.open("w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self._mutating = True
            try:
                yield
            finally:
                self._mutating = False
                fcntl.flock(fh, fcntl.LOCK_UN)

    # --- очередь задач ----------------------------------------------------

    def load_tasks(self) -> dict[str, Any]:
        if not self.tasks_path.exists():
            return {"goal": "", "tasks": []}
        data: dict[str, Any] = json.loads(
            self.tasks_path.read_text(encoding="utf-8"))
        return data

    def save_tasks(self, data: dict[str, Any]) -> None:
        """Записать очередь — и ОБЪЯВИТЬ запись в журнале.

        Отпечаток попадает в журнал не ради истории, а ради проверки
        целостности (§6.1). Она сравнивает состояние до и после задачи и
        не может знать, кто его менял. На пилоте это дважды обвинило не
        того: оператор отвечал через `swarm answer` на вопрос ОДНОЙ задачи,
        пока шла ДРУГАЯ, — и роняло бегущую. Инбокс заведён ровно затем,
        чтобы спор не останавливал очередь, а получалось наоборот.

        Разделитель прост: законная запись идёт через этот метод и сама
        себя протоколирует; правка файла в обход API следа не оставляет.
        Проверка сверяет текущий отпечаток с последним объявленным.
        """
        for t in data.get("tasks", []):
            if t.get("status") not in LEGAL_STATUS:
                raise StateError(f"недопустимый статус {t.get('status')!r} "
                                 f"у задачи {t.get('id')!r}")
        blob = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
        _atomic_write(self.tasks_path, blob)
        self.log("state_written", sha=tasks_sha(blob))

    def set_status(self, task_id: str, status: str, **fields: Any) -> None:
        if status not in LEGAL_STATUS:
            raise StateError(f"недопустимый статус {status!r}")
        with self.mutate():
            data = self.load_tasks()
            for t in data["tasks"]:
                if t["id"] == task_id:
                    t["status"] = status
                    t.update(fields)
                    break
            else:
                raise StateError(f"задача {task_id!r} не найдена")
            self.save_tasks(data)

    def ready_tasks(self) -> list[dict[str, Any]]:
        """Задачи, готовые к запуску: pending и все deps закрыты (§5.0).

        Неразрешимый остаток — ошибка, а не тишина: молча пропущенные
        задачи уже стоили одного пустого прогона.
        """
        data = self.load_tasks()
        by_id = {t["id"]: t for t in data["tasks"]}
        done = {t["id"] for t in data["tasks"] if t["status"] == "done"}
        pending = [t for t in data["tasks"] if t["status"] == "pending"]
        ready = [t for t in pending if all(d in done for d in t.get("deps") or [])]
        if pending and not ready:
            # Ждать зависимости, ушедшей в blocked, — нормальный исход:
            # человек разберёт её через инбокс. Тупиком считается только
            # неразрешимая ссылка или цикл (поймано на приёмке tenacity).
            waiting_on_blocked = any(
                by_id.get(d, {}).get("status") == "blocked"
                for t in pending for d in t.get("deps") or [])
            if not waiting_on_blocked:
                raise StateError(
                    f"тупик зависимостей: не запускаются "
                    f"{[t['id'] for t in pending]}")
        return ready

    # --- журнал и шаги ----------------------------------------------------

    def log(self, kind: str, **payload: Any) -> dict[str, Any]:
        """Append-only журнал событий (ADR-001: jsonl первичен, md — рендер)."""
        row: dict[str, Any] = obs.stamp({"kind": kind})
        row.update(payload)
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    # --- единое представление работы агента -------------------------------
    #
    # Петля разговаривала с git через три разных представления: границы
    # проверялись по `git status --porcelain`, ревью шло по `git diff`,
    # коммит делал `git add -A`. Созданный файл виден первому и третьему,
    # но НЕ второму — ревьюер получал пустой дифф и мог одобрить пустоту,
    # которую коммит затем вносил в историю. Теперь источник один.

    def git(self, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        # check=False намеренно: вызывающие ветвятся по returncode
        # (`ls-files --error-unmatch` тем и работает, что падает).
        return subprocess.run(["git", *args], cwd=self.root,
                              capture_output=True, text=True, check=False, **kw)

    def changed_files(self) -> list[str]:
        """Пути, изменённые в рабочем дереве, ПОИМЁННО.

        Без `-uall` git схлопывает новый каталог в одну строку `?? src/`,
        которая не совпадёт ни с одним глобом из `paths`: задача «создай
        модуль» гарантированно выжигала лимит итераций с ложным диагнозом
        «слишком крупная».
        """
        out = self.git("status", "--porcelain", "-uall").stdout
        return [line[3:].strip().strip('"') for line in out.splitlines()
                if line[3:].strip()]

    def work_diff(self) -> str:
        """Дифф работы агента, включая СОЗДАННЫЕ файлы.

        `git add -N` (intent-to-add) регистрирует новые файлы в индексе, не
        добавляя их содержимое, — после этого обычный `git diff` показывает
        их как добавления. Историю это не меняет.

        Файлы оркестратора (OWNED_ROOTS) в дифф не входят: правка
        конфига петли — не работа агента, и ревьюер не должен ни судить
        её, ни тратить на неё бюджет.
        """
        self.git("add", "-A", "-N")
        return self.git("diff", "--", ".", *OWNED_EXCLUDE_PATHSPECS).stdout

    def total_spend(self) -> float:
        """Сколько уже стоил прогон. Считается по факту из метрик.

        Считаются ВСЕ дорогие роли. Планировщик пишет в отдельный поток
        (`plan-metrics.jsonl`), и пока он в счёт не входил, стоп по
        `total_budget_usd` не видел целой роли: на PILOT-1 две обрубленные
        попытки планирования за $3.30 остались вне бюджета прогона.

        Слепое пятно, которое надо знать, — теперь оно про ДВИЖОК, а не
        про роль. Поток kimi не содержит ни usage, ни стоимости (проверено
        по сырым логам: у событий вообще нет таких полей), и выдумывать
        цену вместо отсутствующего факта хуже, чем честно её не знать: на
        движке kimi исполнитель в счёт по-прежнему не входит. На движке
        claude конверт отдаёт `total_cost_usd`, эта цена пишется в метрику
        раунда и считается здесь — то есть `total_budget_usd` впервые
        ограничивает петлю ЦЕЛИКОМ, и потолок стенда достигается раньше,
        чем достигался вчера. Это не регресс, а конец умолчания.
        """
        total = 0.0
        for path in (self.metrics_path, self.dir / "plan-metrics.jsonl"):
            if not path.exists():
                continue
            for row in path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(row)
                except ValueError:
                    continue
                # Валидный JSON — ещё не запись, а число — не любой
                # cost_usd: строка `"x"` или строковая цена из чужого
                # инструмента роняли ровно ту команду, которой считают
                # деньги. Журнал читается как данные, а не как контракт.
                cost = rec.get("cost_usd") if isinstance(rec, dict) else None
                if isinstance(cost, int | float):
                    total += cost
        return round(total, 2)

    def metric(self, **payload: Any) -> None:
        obs.stamp(payload)
        with self.metrics_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    # --- инбокс вопросов к человеку ---------------------------------------

    @staticmethod
    def _next_id(prefix: str, used: set[str]) -> str:
        """Следующий свободный id вида `q007`/`p003`.

        Номер — максимум занятых плюс один, а НЕ количество записей:
        счётчик по длине выдаёт уже использованный id, как только хоть
        одна запись потерялась (ротация, обрезанный журнал), а `answer`
        ключуется именно по qid — реюз означает ответ не на тот вопрос.
        """
        top = 0
        for uid in used:
            if uid.startswith(prefix) and uid[len(prefix):].isdigit():
                top = max(top, int(uid[len(prefix):]))
        return f"{prefix}{top + 1:03d}"

    def ask(self, task_id: str, kind: str, question: str,
            **context: Any) -> str:
        """Отложить вопрос человеку вместо остановки прогона.

        Смысл инбокса: одна спорная задача не должна останавливать всю
        очередь. Петля откладывает вопрос, берёт следующую задачу, а
        человек разбирает накопившееся пачкой.

        Занятые id собираются не только из журнала, но и из ссылок
        `question_id` в tasks.json: очередь переживает потерю журнала, и
        новый вопрос не имеет права получить id, на который ещё ссылается
        живая задача.
        """
        used = {str(q["qid"]) for q in self.questions()}
        used |= {str(t["question_id"]) for t in self.load_tasks().get("tasks", [])
                 if t.get("question_id")}
        qid = self._next_id("q", used)
        # поле называется qkind, а не kind: `kind` уже занят типом записи
        # журнала, и передача обоих ломала вызов (поймано диагностикой)
        self.log("question", qid=qid, task=task_id, qkind=kind,
                 question=question, **context)
        return qid

    def answer(self, qid: str, text: str,
               add_paths: list[str] | None = None) -> str | None:
        """Ответ человека: вопрос закрывается, задача возвращается в работу.

        `add_paths` расширяет границы задачи. Это не удобство, а
        необходимость: ответ вроде «вынеси хелперы в другой модуль» —
        изменение ПЛАНА, а не подсказка исполнителю. Без расширения путей
        исполнитель честно пытается выполнить указание, а SCOPE-CHECK так
        же честно откатывает его работу (поймано на приёмке: две итерации
        подряд правки `_utils.py` откатывались, задача исчерпала раунды).
        """
        with self.mutate():
            return self._answer_locked(qid, text, add_paths)

    def _answer_locked(self, qid: str, text: str,
                       add_paths: list[str] | None) -> str | None:
        questions = {q["qid"]: q for q in self.questions()}
        if qid not in questions:
            raise StateError(f"вопрос {qid!r} не найден")
        if questions[qid]["status"] == "answered":
            raise StateError(f"вопрос {qid!r} уже отвечен")
        task_id = questions[qid]["task"]
        data = self.load_tasks()
        target = next((t for t in data["tasks"] if t["id"] == task_id), None)
        # Задача, закрывшаяся ДРУГИМ путём (`swarm retry` вместо ответа),
        # оставляет вопрос без адресата. Воскрешать её ответом нельзя —
        # безусловный pending отправлял done-работу на повторное
        # исполнение. Но и терять ответ незачем: вопрос — ЗАПИСЬ о
        # случившемся, и отказ вместе с ответом выбрасывал разбор
        # причины (E13: диагноз отказа ревьюера деть было некуда).
        # Поэтому ответ пишется, решение копится, а СТАТУС не трогается.
        closed = target is not None and target.get("status") == "done"
        self.log("answer", qid=qid, task=task_id, text=text,
                 reopened=not closed)
        for t in data["tasks"]:
            if t["id"] == task_id:
                # Ответ уходит в handoff следующей итерации, и решения
                # НАКАПЛИВАЮТСЯ — см. record_decision().
                record_decision(t, text)
                if not closed:
                    t["status"] = "pending"
                    t.pop("reason", None)
                for extra in add_paths or []:
                    if extra not in t.setdefault("paths", []):
                        t["paths"].append(extra)
                        self.log("paths_extended", task=task_id, path=extra,
                                 qid=qid)
                break
        self.save_tasks(data)
        return str(task_id) if task_id else None

    def question_is_stale(self, question: dict[str, Any],
                          closed_tasks: set[str] | None = None) -> bool:
        """Вопрос, у которого не осталось адресата: задача уже закрыта.

        Такой вопрос не держит очередь — держать её нечем, работа сделана.
        Но и молча исчезнуть он не должен: `status` объявлял «очередь не
        пойдёт дальше без решения» над очередью 6/6 (E13, задача b4wr
        вернулась в работу через `swarm retry`, а не ответом).

        Пропажу задачи из tasks.json СТАРЫМ вопросом не считаем: файл
        задач можно потерять или заменить план-диффом целиком, и тогда
        «устарели все» спрятало бы живые вопросы — отказ хуже дефекта.
        """
        if closed_tasks is None:
            closed_tasks = self.closed_task_ids()
        return str(question.get("task")) in closed_tasks

    def closed_task_ids(self) -> set[str]:
        """Id закрытых задач. Очередь читается как ДАННЫЕ: правка руками
        оставляет в ней строку вместо словаря, и сводка, падающая на
        такой строке, — худший способ узнать о поломке файла."""
        return {str(t["id"]) for t in self.load_tasks().get("tasks", [])
                if isinstance(t, dict) and t.get("status") == "done"
                and t.get("id") is not None}

    def questions(self, only_open: bool = False,
                  blocking: bool = False) -> list[dict[str, Any]]:
        """Вопросы из журнала со статусом open/answered.

        `blocking=True` — только те, что действительно держат очередь:
        открытые и с незакрытой задачей. Разница не косметическая: над
        закрытой очередью подсказка «ответьте, иначе не поедем» посылает
        человека делать невозможное (см. `question_is_stale`).
        """
        asked, answered = {}, {}
        if not self.journal_path.exists():
            return []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") == "question":
                asked[row["qid"]] = row
            elif row.get("kind") == "answer":
                answered[row["qid"]] = row
        closed = self.closed_task_ids()
        out = []
        for qid, row in asked.items():
            item = dict(row)
            item["status"] = "answered" if qid in answered else "open"
            if qid in answered:
                item["answer"] = answered[qid]["text"]
            item["stale"] = self.question_is_stale(item, closed)
            out.append(item)
        if only_open or blocking:
            out = [q for q in out if q["status"] == "open"]
        if blocking:
            out = [q for q in out if not q["stale"]]
        return sorted(out, key=lambda q: q["qid"])

    # --- политики прогона --------------------------------------------------

    def add_policy(self, text: str, match: dict[str, Any],
                   source_qid: str | None = None) -> str:
        """Решение уровня ПРОГОНА, а не задачи.

        Часть решений человека относится ко всей цели, а не к одной задаче:
        «release notes в этом прогоне не трогаем». Без такого уровня человек
        повторяет одно и то же на каждой задаче — ровно та стоимость
        внимания, ради экономии которой делался инбокс.

        Политика НЕ передаётся ревьюеру: он продолжает сообщать всё, что
        видит (принцип coverage-first, §4.2). Фильтрует оркестратор, и
        подавленное остаётся в журнале — иначе мы потеряли бы способность
        мерить recall.
        """
        if not match:
            raise StateError("политике нужны ключевые слова для сопоставления")
        with self.mutate():
            return self._add_policy_locked(text, match, source_qid)

    def _add_policy_locked(self, text: str, match: dict[str, Any],
                           source_qid: str | None) -> str:
        # Занятость считается по ВСЕМУ журналу, а не по policies():
        # та отфильтровывает снятые политики и чужие цели, и id снятой
        # политики достался бы новой — со всей историей подавлений старой.
        used = set()
        if self.journal_path.exists():
            for line in self.journal_path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("kind") == "policy" and row.get("pid"):
                    used.add(str(row["pid"]))
        pid = self._next_id("p", used)
        goal = self.load_tasks().get("goal", "")
        self.log("policy", pid=pid, text=text, match=list(match), goal=goal,
                 source_qid=source_qid)
        return pid

    def drop_policy(self, pid: str) -> None:
        if pid not in {p["pid"] for p in self.policies()}:
            raise StateError(f"политика {pid!r} не найдена")
        self.log("policy_dropped", pid=pid)

    def policies(self) -> list[dict[str, Any]]:
        """Активные политики текущей цели.

        Привязка к цели не формальность: при смене цели прогона старые
        решения теряют силу, и тащить их дальше опаснее, чем спросить
        человека заново.
        """
        goal = self.load_tasks().get("goal", "")
        active, dropped = {}, set()
        if not self.journal_path.exists():
            return []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") == "policy":
                active[row["pid"]] = row
            elif row.get("kind") == "policy_dropped":
                dropped.add(row["pid"])
        return [p for pid, p in sorted(active.items())
                if pid not in dropped and (not goal or p.get("goal") == goal)]

    def step(self, task_id: str, action: str, **payload: Any) -> "_Step":
        """Контекст одного side-effect'а: intent -> действие -> done.

        `payload` попадает в intent-запись. Это не украшение, а материал
        для реконсиляции (§5.6): интент коммита без записанного `head`
        нельзя доиграть механически — resume не знает, с какого состояния
        действие стартовало, и вынужден звать человека.

        Использование:
            with state.step("a1b2", "commit", head="abc123") as st:
                make_commit()
                st.result(commit="def456")
        """
        return _Step(self, task_id, action, **payload)

    def phase(self, phase: str, task: str | None = None,
              **fields: Any) -> "_Phase":
        """Отметка «идёт прямо сейчас» — единственный факт о настоящем.

        И журнал, и метрики пишутся ПОСЛЕ фазы. Пока исполнитель работает
        семь минут, в `.swarm/` не появляется ни строки: доска показывает
        последний завершённый раунд, и человек не может отличить «идёт
        новый» от «петля встала». Живой сервер доски эту слепоту не лечит,
        а маскирует — страница исправно обновляется тем же прошлым.

        Отметка живёт ровно столько, сколько идёт фаза: `now.json`
        появляется на входе и исчезает на выходе. Файл, переживший
        прогон, — тоже факт, и притом ценный: он называет фазу, на
        которой процесс убили, — `finally` при SIGKILL не отрабатывает.
        Поэтому читать отметку в отрыве от `is_running` нельзя: одна и та
        же запись означает «идёт» у живой петли и «оборвалось здесь» у
        мёртвой.

        Использование:
            with state.phase("implement", task["id"], iter=2, model=...):
                run_executor()
        """
        return _Phase(self, phase, task, **fields)

    def current_phase(self) -> dict[str, Any] | None:
        """Отметка о настоящем, если она есть. Сырьё, не суждение:
        живость петли проверяет вызывающий (`is_running`)."""
        try:
            row = json.loads(self.now_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return row if isinstance(row, dict) else None

    def _phase_write(self) -> None:
        """Верхушка стека фаз — на диск; пустой стек — файла нет.

        Стек, а не одна запись: проверки внутри ревью — фаза внутри фазы,
        и выход из вложенной не значит, что петля бездельничает.
        Наблюдение не имеет права ронять работу (§9.3): сбой записи
        уходит в диагностику вместе с трассировкой.
        """
        try:
            if self._phases:
                _atomic_write(self.now_path,
                              json.dumps(self._phases[-1], ensure_ascii=False))
            else:
                self.now_path.unlink(missing_ok=True)
        except OSError:
            log.warning("отметка о текущей фазе не записана", exc_info=True)

    def unfinished_steps(self) -> list[dict[str, Any]]:
        """Шаги с intent без исхода — их оставило падение (§5.6).

        Исход — это и `step_done`, и `step_failed`: провал с записанной
        причиной — РАЗОБРАННАЯ история (исключение уже ушло в _rescue, у
        задачи есть вопрос в инбоксе). Незавершённым считается только
        интент без какого-либо исхода — то есть падение процесса между
        действием и записью о нём. Пока провал не закрывал шаг, каждый
        следующий `resume` требовал --force за давно разобранное.
        """
        started: dict[str, dict[str, Any]] = {}
        finished: set[str] = set()
        if not self.journal_path.exists():
            return []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") == "step_intent":
                started[row["step_id"]] = row
            elif row.get("kind") in ("step_done", "step_failed"):
                finished.add(row["step_id"])
        return [r for sid, r in started.items() if sid not in finished]


class _Phase:
    """Контекст одной фазы: запись о настоящем на входе, снятие на выходе."""

    def __init__(self, state: SwarmState, phase: str, task: str | None,
                 **fields: Any) -> None:
        self.state = state
        self.row: dict[str, Any] = {"phase": phase, "task": task,
                                    "since": obs.now(), **fields}

    def __enter__(self) -> "_Phase":
        self.state._phases.append(self.row)  # noqa: SLF001 — _Phase и SwarmState одна пара
        self.state._phase_write()            # noqa: SLF001 — см. выше
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        if self.state._phases:               # noqa: SLF001 — см. выше
            self.state._phases.pop()         # noqa: SLF001 — см. выше
        self.state._phase_write()            # noqa: SLF001 — см. выше


class _Step:
    def __init__(self, state: SwarmState, task_id: str, action: str,
                 **intent_payload: Any) -> None:
        self.state = state
        self.task_id = task_id
        self.action = action
        self.step_id = f"{task_id}:{action}:{time.time():.6f}"
        self._intent = intent_payload
        self._payload: dict[str, Any] = {}

    def __enter__(self) -> "_Step":
        self.state.log("step_intent", step_id=self.step_id,
                       task=self.task_id, action=self.action, **self._intent)
        return self

    def result(self, **payload: Any) -> None:
        self._payload.update(payload)

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        if exc_type is None:
            self.state.log("step_done", step_id=self.step_id,
                           task=self.task_id, action=self.action,
                           **self._payload)
        else:
            self.state.log("step_failed", step_id=self.step_id,
                           task=self.task_id, action=self.action,
                           error=f"{exc_type.__name__}: {exc}")
