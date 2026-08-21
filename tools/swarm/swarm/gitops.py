"""Git-операции петли: gate, scope, revert, commit, cleanup, целостность.

Методы класса Loop, работающие с рабочим деревом и git, вынесены в
свободные функции с явным параметром `loop`. В `loop.py` остаются
тонкие делегаты — совместимость с тестами и потребителями сохранена.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import pathlib
import subprocess
import sys
from typing import TYPE_CHECKING, Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import state as state_mod  # noqa: E402

if TYPE_CHECKING:
    from loop_types import LoopLike


def sh(loop: LoopLike, cmd: list[str],
        timeout: float = 900) -> subprocess.CompletedProcess[str]:
        # check=False намеренно: `sh` — общий раннер, и КАЖДЫЙ вызывающий
        # смотрит returncode сам (ветвление по коду — суть половины
        # проверок петли). Исключение здесь лишило бы их этой ветки.
    return subprocess.run(cmd, capture_output=True, text=True,
                          cwd=loop.state.root, timeout=timeout,
                          check=False)

def gate(loop: LoopLike, task: dict[str, Any]) -> tuple[bool, str]:
    cmd = loop.config.get("gate_command") or [
        "python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
        # Холодная сборка Rust не влезает в 900 с, а таймаут здесь —
        # исключение, топившее задачу в in_progress (до §5.3-починки).
    r = loop.sh(cmd, timeout=int(loop.config.get("gate_timeout", 900)))
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-3:])
    ok = r.returncode == 0
    loop.state.metric(task=task["id"], phase="gate", ok=ok, tail=tail[:300])
    return ok, tail

def integrity_check(loop: LoopLike) -> list[str]:
    """§6.1: неприкосновенность истории и состояния петли.

    Deny-правила у Kimi недокументированы, а промпт — не гарантия:
    исполнителю ничто не мешает вызвать `git commit`, `git reset` или
    переписать `.swarm/tasks.json`, пометив задачу выполненной. Здесь
    проверяется не намерение, а ФАКТ — постфактум, но механически.
    Возвращает список нарушений.
    """
    bad = []
    head = loop.sh(["git", "rev-parse", "HEAD"]).stdout.strip()
    if loop.head_before and head != loop.head_before:
            # Кто сдвинул HEAD, проверка знать не может: у неё есть только
            # «до» и «после». На PILOT-1 его сдвинул ОПЕРАТОР — закоммитил
            # правку конфига, пока задача шла в фоне, — а формулировка
            # «исполнитель вышел за границы доверия» обвинила агента и
            # стоила круга разбирательства. Поэтому текст нейтрален, а
            # рядом показан сам коммит: автор и заголовок отвечают на
            # вопрос «моё это или нет» с одного взгляда.
        who = loop.sh(["git", "log", "-1", "--format=%an: %s",
                        head]).stdout.strip()
        bad.append(f"история изменилась во время задачи: HEAD "
                   f"{loop.head_before[:8]} -> {head[:8]}"
                   + (f" ({who})" if who else "")
                   + ". Коммитит только оркестратор; если коммит ваш —"
                     " задачу можно вернуть в очередь как есть")
    state_now = loop.state_fingerprint()
    if (loop.state_before and state_now is not None
            and state_now != loop.state_before):
            # Изменение состояния само по себе НЕ нарушение: через тот же
            # `.swarm/` работают и петля (set_status), и оператор
            # (`swarm answer` из соседнего процесса). Раньше проверка
            # сравнивала только «до» и «после» и роняла бегущую задачу,
            # когда оператор отвечал на вопрос ДРУГОЙ, — то есть инбокс,
            # заведённый чтобы спор не останавливал очередь, останавливал
            # её сам (пилот, c4rp).
            #
            # Разделитель — журнал: законная запись идёт через
            # `state.save_tasks` и объявляет там свой отпечаток. Если
            # текущее состояние совпадает с последним объявленным, все
            # изменения объяснены. Правка файла в обход API следа не
            # оставляет и остаётся нарушением.
            #
            # Граница честности: агент, дописавший в журнал поддельную
            # строку, обойдёт проверку. Она ловит небрежность и жадность,
            # а не подделку, — и это по-прежнему больше, чем ловилось до.
        declared = loop.declared_state_sha()
        if declared is None or declared != loop.state_sha(state_now):
            bad.append("состояние петли (.swarm/) изменено в обход API: "
                       "правка файла не объявлена в журнале")
        else:
                # Объяснено — сдвигаем базу, иначе следующая проверка той же
                # задачи сработает на том же самом изменении повторно.
            loop.state_before = state_now
    for marker, what in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                         ("MERGE_HEAD", "merge"),
                         ("CHERRY_PICK_HEAD", "cherry-pick")):
        if (pathlib.Path(loop.state.root) / ".git" / marker).exists():
            bad.append(f"репозиторий оставлен в состоянии {what}")
    return bad

def state_sha(blob: str) -> str:
    """Отпечаток очереди тем же способом, каким его объявляет запись."""
    state_mod = sys.modules.get("state")
    if state_mod is not None:
        sha: str = state_mod.tasks_sha(blob)
        return sha
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

def declared_state_sha(loop: LoopLike) -> str | None:
    """Последний отпечаток, объявленный законной записью состояния."""
    path = getattr(loop.state, "journal_path", None)
    if path is None:
        return None
    state_mod = sys.modules.get("state")
    if state_mod is None:
        return None
    declared: str | None = state_mod.last_declared_sha(path)
    return declared

def state_fingerprint(loop: LoopLike) -> str | None:
    path = getattr(loop.state, "tasks_path", None)
    if path is None:
        return None
    try:
        text: str = path.read_text(encoding="utf-8")
    except OSError:
        return None
    else:
        return text

def scope_check(loop: LoopLike, task: dict[str, Any],
                ) -> tuple[bool, list[str], list[str]]:
    """Границы задачи: разрешённые пути и неприкосновенность тестов.

    Принцип один: защита — от правки ЧУЖОГО, а не названного. Файл из
    защищённой зоны (tests/**, *.toml, ...) можно менять, только если
    `paths` задачи целятся в него ЯВНО.

    История правила — два урока, оба стоили кругов:

    1. Приёмка: у `feature-tests` исполнитель ОБЯЗАН писать свои
       тесты, и файлы из `paths` защищёнными не считались — но только
       для этого типа.
    2. Пилот (k3ad, s2ky — шесть сгоревших раундов): задача типа
       `feature` меняет поведение правила, чьё число диагностик
       ПРИШПИЛЕНО существующим тестом. Тест был явно назван в `paths`
       — и всё равно откатывался, потому что исключение работало
       по типу, а не по явности. Ни один тип не позволял «поменять
       код и обновить пришпиленный к нему тест» — а это самая
       обычная форма работы.

    Тонкость: широкий глоб защиту НЕ снимает. `crates/**` покрывает и
    тесты, но не целится в них; снимает защиту только паттерн, сам
    лежащий в защищённой зоне (`crates/x/tests/*.rs`, `zeus/Cargo.lock`).
    Иначе любой размашистый paths обнулял бы анти-gaming (§5.5).
    """
    allowed = task.get("paths") or []
    protected = loop.config.get("protected_paths") or ["tests/*", "tests/**"]

    def is_protected(path: str) -> bool:
        return any(fnmatch.fnmatch(path, p) for p in protected)

    unlocking = [pat for pat in allowed if is_protected(pat)]
    bad, touched_tests = [], []
    for path in loop.state.changed_files():
        if state_mod.owned_by_loop(path):
                # Конфиг и состояние ПЕТЛИ — не материал задачи. Судить
                # их страж не имеет права ни при каком раскладе: на
                # PILOT-1 незакоммиченный swarm.toml, вернувшийся из
                # стеша, был прочитан как работа агента, и исполнитель
                # откатил два решения владельца (см. state.py).
            continue
        if path in loop.pre_existing or path in loop.run_dirt:
                # Лежало в дереве ДО старта задачи (`run --force`) — не
                # работа агента. revert такие файлы щадит, а страж без
                # этого исключения читал их как нарушение каждый раунд:
                # лимит выгорал об операторскую незакоммиченную правку,
                # которую никто не мог ни убрать, ни легализовать.
                # Цена решения: правку агента ПОВЕРХ такого файла страж
                # тоже не видит — этот риск оператор принял флагом --force.
                # run_dirt — то же множество на уровне ПРОГОНА: перезапуск
                # процесса и цикл stash/restore обнуляют pre_existing, а
                # операторская грязь от этого работой агента не становится.
            continue
        if not any(fnmatch.fnmatch(path, p) for p in allowed):
            bad.append(path)
            continue
        if is_protected(path) and not any(
                fnmatch.fnmatch(path, u) for u in unlocking):
            touched_tests.append(path)
    ok = not bad and not touched_tests
    loop.state.metric(task=task["id"], phase="scope", ok=ok,
                      unexpected=bad, protected=touched_tests)
    return ok, bad, touched_tests

def revert(loop: LoopLike) -> list[str]:
    """Откат работы агента, включая СОЗДАННЫЕ файлы.

    `git checkout -- .` возвращает отслеживаемые файлы, но untracked
    не трогает: нарушитель оставался на диске и повторно ловился
    каждый раунд, делая нарушение границ неустранимым. Откатываем
    поимённо то, что реально изменено, — по всему дереву не метём,
    чтобы не задеть чужое.
    """
    untouchable: set[str] = (getattr(loop, "pre_existing", set())
                             | getattr(loop, "run_dirt", set()))
    changed = [p for p in loop.state.changed_files()
               if p not in untouchable and not state_mod.owned_by_loop(p)]
    if not changed:
        return []
    tracked: list[str] = []
    untracked: list[str] = []
    for path in changed:
        probe = loop.sh(["git", "ls-files", "--error-unmatch", path])
        (tracked if probe.returncode == 0 else untracked).append(path)
    if tracked:
        loop.sh(["git", "checkout", "--", *tracked])
    for path in untracked:
            # intent-to-add уже мог зарегистрировать файл в индексе
        loop.sh(["git", "rm", "-f", "--quiet", "--ignore-unmatch", path])
        target = loop.state.root / path
        if target.exists():
            target.unlink()
    return changed

def commit(loop: LoopLike, task: dict[str, Any]) -> str | None:
    """Коммит принятой итерации; пустой diff — не ошибка (§4.4)."""
    env = {"GIT_AUTHOR_NAME": "swarm-executor",
           "GIT_AUTHOR_EMAIL": "executor@swarm.local",
           "GIT_COMMITTER_NAME": "swarm-orchestrator",
           "GIT_COMMITTER_EMAIL": "orchestrator@swarm.local"}
        # Конфиг петли в коммит задачи не входит: `git add -A` без
        # исключений закоммитил бы операторскую правку swarm.toml как
        # работу исполнителя. Именно add-then-unstage, а не pathspec с
        # :(exclude): явный pathspec поверх игнорируемого .swarm/ роняет
        # `git add` советом «Use -f» (замерено на 2.50). reset по путям,
        # которых нет в индексе, — тихий no-op.
    subprocess.run(["git", "add", "-A"], cwd=loop.state.root, check=True)
    subprocess.run(["git", "reset", "-q", "--", *state_mod.OWNED_ROOTS],
                   cwd=loop.state.root, check=True)
        # Здесь код возврата — ОТВЕТ, а не ошибка: 0 значит «нечего
        # коммитить», 1 — «есть изменения». check=True сломал бы логику.
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"],
                            cwd=loop.state.root, check=False)
    if staged.returncode == 0:
        return None
    message = loop.agents.commit_message(task, loop.sh(["git", "diff",
                                                         "--cached"]).stdout)
    subprocess.run(["git", "commit", "-qm", message], cwd=loop.state.root,
                   check=True, env={**os.environ, **env})
        # Коммит оркестратора легален: сдвигаем базу, иначе следующая
        # проверка целостности обвинит агента в нашей же работе.
    loop.head_before = loop.sh(["git", "rev-parse", "HEAD"]).stdout.strip()
    return loop.sh(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()

def cleanup(loop: LoopLike, task: dict[str, Any], reason: str) -> str | None:
    """Терминальный исход оставляет worktree чистым (§5.5.1).

    Два урока пилота, оба стоили работы:

    1. `git stash push` ОТКАЗЫВАЕТСЯ работать поверх записей
       intent-to-add: «Entry ... not uptodate. Cannot merge». А их
       оставляет `work_diff` — тот самый `git add -A -N`, которым
       созданные файлы делаются видимыми для `git diff`. Починка
       одного представления работы ломала другое, поэтому индекс
       сбрасывается перед стешем.
    2. Метка возвращалась БЕЗ проверки кода возврата. Запись задачи
       ссылалась на стеш `swarm:g1nt-invalid-verdict`, которого не
       существовало, и отправляла оператора искать работу там, где
       её нет. Не создался — так и скажем.

    3. Конфиг петли в стеш не входит. Именно стеш и был машиной
       отмывания на PILOT-1: quota-пауза унесла незакоммиченный
       swarm.toml вместе с работой, перезапуск застал чистое дерево,
       pre_existing оказался пуст — и вернувшийся из стеша конфиг
       страж прочитал как нарушение границ. Операторская правка
       конфига остаётся в дереве на виду; preflight назовёт её.
    """
    stashable = [p for p in loop.state.changed_files()
                 if not state_mod.owned_by_loop(p)]
    if not stashable:
        return None
    loop.sh(["git", "reset", "-q"])
    label = f"swarm:{task['id']}-{reason}"
        # Стеш ПОИМЁННО, а не «всё с исключениями»: явный pathspec с
        # :(exclude) поверх игнорируемого .swarm/ роняет git тем же
        # советом «Use -f», что и add (см. commit). Список и так уже
        # вычислен — им и ограничиваемся.
    r = loop.sh(["git", "stash", "push", "-u", "-q", "-m", label,
                  "--", *stashable])
    if r.returncode != 0:
        loop.state.log("stash_failed", task=task["id"], reason=reason,
                       stderr=(r.stderr or "").strip()[:300])
        return None
    return label

def _apply_patch(loop: LoopLike, diff_text: str) -> bool:
    """Вернуть worktree к сохранённому лучшему состоянию.

    Код возврата `git apply` обязан проверяться. Здесь работа уже
    снесена `revert`, и молчаливый провал восстановления означает, что
    петля пойдёт коммитить ПУСТОТУ, считая, что откатилась к лучшему
    состоянию. Худший из возможных исходов: работа потеряна, а история
    утверждает обратное.
    """
    if not diff_text.strip():
        return True
    r = subprocess.run(["git", "apply", "-"], cwd=loop.state.root,
                       input=diff_text, text=True, capture_output=True,
                       check=False)
    if r.returncode != 0:
        loop.state.log("restore_failed", stderr=(r.stderr or "").strip()[:300])
    return r.returncode == 0
