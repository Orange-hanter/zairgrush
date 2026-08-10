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
import fcntl
import json
import os
import pathlib
import subprocess
import time

LEGAL_STATUS = {"pending", "in_progress", "in_review", "done", "blocked"}
TERMINAL = {"done", "blocked"}


class StateError(Exception):
    """Состояние на диске противоречиво — петля обязана остановиться."""


def _atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class SwarmState:
    def __init__(self, root, swarm_dir=".swarm"):
        self.root = pathlib.Path(root)
        self.dir = self.root / swarm_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "log").mkdir(exist_ok=True)
        self._self_ignore(swarm_dir)
        self.tasks_path = self.dir / "tasks.json"
        self.journal_path = self.dir / "log" / "run.jsonl"
        self.metrics_path = self.dir / "metrics.jsonl"
        self.lock_path = self.dir / "state.lock"
        self._lock = None

    def _self_ignore(self, swarm_dir):
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

    def acquire(self):
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

    def release(self):
        if self._lock:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    # --- очередь задач ----------------------------------------------------

    def load_tasks(self):
        if not self.tasks_path.exists():
            return {"goal": "", "tasks": []}
        return json.loads(self.tasks_path.read_text(encoding="utf-8"))

    def save_tasks(self, data):
        for t in data.get("tasks", []):
            if t.get("status") not in LEGAL_STATUS:
                raise StateError(f"недопустимый статус {t.get('status')!r} "
                                 f"у задачи {t.get('id')!r}")
        _atomic_write(self.tasks_path,
                      json.dumps(data, ensure_ascii=False, indent=1) + "\n")

    def set_status(self, task_id, status, **fields):
        if status not in LEGAL_STATUS:
            raise StateError(f"недопустимый статус {status!r}")
        data = self.load_tasks()
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["status"] = status
                t.update(fields)
                break
        else:
            raise StateError(f"задача {task_id!r} не найдена")
        self.save_tasks(data)

    def ready_tasks(self):
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

    def log(self, kind, **payload):
        """Append-only журнал событий (ADR-001: jsonl первичен, md — рендер)."""
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind}
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

    def _git(self, *args, **kw):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              text=True, **kw)

    def changed_files(self):
        """Пути, изменённые в рабочем дереве, ПОИМЁННО.

        Без `-uall` git схлопывает новый каталог в одну строку `?? src/`,
        которая не совпадёт ни с одним глобом из `paths`: задача «создай
        модуль» гарантированно выжигала лимит итераций с ложным диагнозом
        «слишком крупная».
        """
        out = self._git("status", "--porcelain", "-uall").stdout
        return [line[3:].strip().strip('"') for line in out.splitlines() if line[3:].strip()]

    def work_diff(self):
        """Дифф работы агента, включая СОЗДАННЫЕ файлы.

        `git add -N` (intent-to-add) регистрирует новые файлы в индексе, не
        добавляя их содержимое, — после этого обычный `git diff` показывает
        их как добавления. Историю это не меняет.
        """
        self._git("add", "-A", "-N")
        return self._git("diff").stdout

    def total_spend(self):
        """Сколько уже стоил прогон. Считается по факту из метрик."""
        total = 0.0
        for row in self.metrics_path.read_text(encoding="utf-8").splitlines() \
                if self.metrics_path.exists() else []:
            try:
                total += json.loads(row).get("cost_usd") or 0
            except ValueError:
                continue
        return round(total, 2)

    def metric(self, **payload):
        payload["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self.metrics_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    # --- инбокс вопросов к человеку ---------------------------------------

    def ask(self, task_id, kind, question, **context):
        """Отложить вопрос человеку вместо остановки прогона.

        Смысл инбокса: одна спорная задача не должна останавливать всю
        очередь. Петля откладывает вопрос, берёт следующую задачу, а
        человек разбирает накопившееся пачкой.
        """
        qid = f"q{len(self.questions()) + 1:03d}"
        # поле называется qkind, а не kind: `kind` уже занят типом записи
        # журнала, и передача обоих ломала вызов (поймано диагностикой)
        self.log("question", qid=qid, task=task_id, qkind=kind,
                 question=question, **context)
        return qid

    def answer(self, qid, text, add_paths=None):
        """Ответ человека: вопрос закрывается, задача возвращается в работу.

        `add_paths` расширяет границы задачи. Это не удобство, а
        необходимость: ответ вроде «вынеси хелперы в другой модуль» —
        изменение ПЛАНА, а не подсказка исполнителю. Без расширения путей
        исполнитель честно пытается выполнить указание, а SCOPE-CHECK так
        же честно откатывает его работу (поймано на приёмке: две итерации
        подряд правки `_utils.py` откатывались, задача исчерпала раунды).
        """
        questions = {q["qid"]: q for q in self.questions()}
        if qid not in questions:
            raise StateError(f"вопрос {qid!r} не найден")
        if questions[qid]["status"] == "answered":
            raise StateError(f"вопрос {qid!r} уже отвечен")
        task_id = questions[qid]["task"]
        self.log("answer", qid=qid, task=task_id, text=text)
        data = self.load_tasks()
        for t in data["tasks"]:
            if t["id"] == task_id:
                # Ответ уходит в handoff следующей итерации. Решения
                # НАКАПЛИВАЮТСЯ: одно поле означало, что каждый следующий
                # ответ затирает предыдущий, и задача закрывалась вопреки
                # уже принятому решению. На приёмке v3st так и вышло —
                # архитектурное «вынеси валидатор в _utils.py» стёрлось
                # техническим ответом на следующий вопрос, ревьюер его уже
                # не видел и одобрил инлайн-вариант.
                prior = t.get("human_decisions") or (
                    [t["human_answer"]] if t.get("human_answer") else [])
                if text not in prior:
                    prior.append(text)
                t["human_decisions"] = prior
                t["human_answer"] = (prior[0] if len(prior) == 1
                                     else "\n".join(f"- {d}" for d in prior))
                t["status"] = "pending"
                t.pop("reason", None)
                for extra in add_paths or []:
                    if extra not in t.setdefault("paths", []):
                        t["paths"].append(extra)
                        self.log("paths_extended", task=task_id, path=extra,
                                 qid=qid)
                break
        self.save_tasks(data)
        return task_id

    def questions(self, only_open=False):
        """Вопросы из журнала со статусом open/answered."""
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
        out = []
        for qid, row in asked.items():
            item = dict(row)
            item["status"] = "answered" if qid in answered else "open"
            if qid in answered:
                item["answer"] = answered[qid]["text"]
            out.append(item)
        if only_open:
            out = [q for q in out if q["status"] == "open"]
        return sorted(out, key=lambda q: q["qid"])

    # --- политики прогона --------------------------------------------------

    def add_policy(self, text, match, source_qid=None):
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
        pid = f"p{len(self.policies()) + 1:03d}"
        goal = self.load_tasks().get("goal", "")
        self.log("policy", pid=pid, text=text, match=list(match), goal=goal,
                 source_qid=source_qid)
        return pid

    def drop_policy(self, pid):
        if pid not in {p["pid"] for p in self.policies()}:
            raise StateError(f"политика {pid!r} не найдена")
        self.log("policy_dropped", pid=pid)

    def policies(self):
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

    def step(self, task_id, action):
        """Контекст одного side-effect'а: intent -> действие -> done.

        Использование:
            with state.step("a1b2", "commit") as st:
                make_commit()
                st.result(commit="abc123")
        """
        return _Step(self, task_id, action)

    def unfinished_steps(self):
        """Шаги с intent без done — их оставило падение (§5.6)."""
        started, finished = {}, set()
        if not self.journal_path.exists():
            return []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") == "step_intent":
                started[row["step_id"]] = row
            elif row.get("kind") == "step_done":
                finished.add(row["step_id"])
        return [r for sid, r in started.items() if sid not in finished]


class _Step:
    def __init__(self, state, task_id, action):
        self.state = state
        self.task_id = task_id
        self.action = action
        self.step_id = f"{task_id}:{action}:{time.time():.6f}"
        self._payload = {}

    def __enter__(self):
        self.state.log("step_intent", step_id=self.step_id,
                       task=self.task_id, action=self.action)
        return self

    def result(self, **payload):
        self._payload.update(payload)

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.state.log("step_done", step_id=self.step_id,
                           task=self.task_id, action=self.action,
                           **self._payload)
        else:
            self.state.log("step_failed", step_id=self.step_id,
                           task=self.task_id, action=self.action,
                           error=f"{exc_type.__name__}: {exc}")
        return False
