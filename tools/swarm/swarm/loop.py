"""Конечный автомат петли (§5) поверх состояния на диске.

В прототипах FSM жил внутри раннера бенча и держал всё в памяти: прогон
нельзя было прервать и продолжить. Здесь тот же цикл, но каждый переход
проходит через SwarmState — значит петля переживает падение процесса.

Порядок шагов ровно как в §5:
    0a. BASELINE-GATE  — красный до начала = вина репозитория, не агента
    1.  IMPLEMENT      — исполнитель; dispute завершает задачу
    2.  GATE           — тесты запускает оркестратор, не агент
    3.  SCOPE-CHECK    — diff против разрешённых путей и тестовых файлов
    4.  REVIEW         — ревьюер; вердикт валидируется механически
    5.  COMMIT | FEEDBACK
"""
import fnmatch
import hashlib
import os
import pathlib
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import board  # noqa: E402 — каталог добавлен строкой выше
import memory as memory_mod  # noqa: E402
import obs  # noqa: E402
import pyindex  # noqa: E402

log = obs.get_logger("loop")

MAX_ITER = 3
SEVERITIES = {"blocker", "major", "minor"}
CATEGORIES = {"correctness", "tests", "style", "scope", "architecture"}

# Категории, которые задевают ЗАМЫСЕЛ, а не исполнение: их нельзя чинить
# автоматически, потому что правильный ответ зависит от намерения автора
# задачи. Приём заимствован у FuguNano: человека дёргают только по таким
# находкам, механические уходят исполнителю в тот же раунд.
INTENT_CATEGORIES = {"architecture", "scope"}

# Исходы цикла и коды возврата — машинный контракт для внешнего скрипта.
DONE, CONFIRM, CONTINUE, ASK_USER = "done", "confirm", "continue", "ask_user"
ESCALATE_MAX, ESCALATE_NONCONV = "escalate_max", "escalate_nonconvergent"

EXIT_OK = 0          # задача закрыта
EXIT_WORKING = 10    # петля продолжает сама
EXIT_ASK_USER = 11   # нужен ответ человека по замыслу
EXIT_ESCALATE = 20   # разбирается человеком целиком
MIN_ANALYSIS = 40
MIN_SUMMARY = 20

# Ревью может не состояться по разным причинам, и лечение у них разное.
# Оператору отдаётся диагноз, а не голое «invalid_verdict»: на пилоте
# задача с зелёным гейтом и чистыми границами ушла в blocked молча —
# инбокс пуст, журнал пуст, и понять причину можно было только чтением
# сырых ответов ревьюера.
REVIEW_DIAGNOSIS = {
    "budget_exhausted": (
        "ревьюер обрублен по бюджету, вердикта нет. Работа исполнителя "
        "цела, гейт был зелёный. Почти всегда причина — объём диффа: "
        "проверь, не попал ли в него сгенерированный файл; при "
        "необходимости подними review_budget_usd"),
    "invalid": (
        "ревьюер дважды не вернул разбираемый вердикт: ответ не проходит "
        "схему. Смотри сырые ответы в .swarm/log/*-review.json"),
    # Причины ниже приходят из драйвера: ревью шло потоком, и прогон
    # не дожил до конверта. Работа исполнителя во всех случаях цела.
    "silence": (
        "ревьюер замолчал дольше silence_timeout и был остановлен; "
        "вердикта нет, работа исполнителя цела. Смотри "
        ".swarm/log/*-review-stream.jsonl; если он честно думал — "
        "подними silence_timeout"),
    "wall_clock": (
        "ревьюер упёрся в wall_clock_cap и был остановлен; вердикта нет, "
        "работа исполнителя цела. Обычно так выглядит слишком большой "
        "дифф — проверь, что в него попало"),
    "crash": (
        "процесс ревьюера завершился с ошибкой до вердикта. Смотри "
        ".swarm/log/*-review-stream.jsonl"),
    "no_report": (
        "поток ревьюера кончился без финального result-события — вердикт "
        "снять не с чего. Смотри .swarm/log/*-review-stream.jsonl"),
}
QUOTA_MARKERS = ("session limit", "rate limit", "quota", "usage limit",
                 "429", "too many requests")


class ExecutorUnavailableError(Exception):
    """Исполнитель не запускается: мгновенная авария процесса.

    Это окружение, а не работа: квота провайдера, битый бинарь, отозванный
    токен. Жечь об это раунды и блокировать задачи с диагнозом «слишком
    крупная» — вдвойне ложь; на пилоте каскад мгновенных аварий за минуты
    прошёлся по трём задачам очереди. Прогон останавливается целиком,
    задача возвращается в pending: она ни в чём не виновата.
    """


class QuotaExceededError(Exception):
    """Провайдер отказал по квоте: петля ждёт, а не блокирует задачу (§5.3)."""


def quota_exception(exc: BaseException) -> bool:
    """Это отказ по квоте? Сверка по ИМЕНИ класса, а не по identity.

    Модули петли грузятся по путям (importlib), и каждый `_load` создаёт
    СВЕЖУЮ копию модуля: у cli, у Agents и у планировщика — свои объекты
    класса QuotaExceededError. `except QuotaExceededError` ловит только
    исключение из собственной копии; квота, поднятая другой копией,
    пролетала мимо всех трёх ловушек и превращалась в «аварию» с blocked —
    при зелёных тестах, потому что тесты поднимают исключение из той же
    копии, из которой построен Loop. Пока рой не устанавливается пакетом,
    имя класса — единственный стабильный признак.
    """
    return type(exc).__name__ == "QuotaExceededError"


class EscalationError(Exception):
    """Ситуация, которую обязан разобрать человек (§1.1)."""


def quota_error(envelope: Any) -> str | None:
    if not isinstance(envelope, dict) or not envelope.get("is_error"):
        return None
    text = str(envelope.get("result") or "").lower()
    return str(envelope.get("result"))[:200] if any(
        m in text for m in QUOTA_MARKERS) else None


def validate_verdict(v: Any) -> bool:
    """Форма И смысл (§4.2): схема не ловит заглушку вроде analysis='test'."""
    if not isinstance(v, dict):
        return False
    if v.get("verdict") not in ("approve", "request_changes", "blocked"):
        return False
    for f in v.get("findings", []):
        if f.get("severity") not in SEVERITIES or f.get("category") not in CATEGORIES:
            return False
    if v["verdict"] == "approve" and any(
            f["severity"] in ("blocker", "major") for f in v.get("findings", [])):
        return False
    if v["verdict"] in ("request_changes", "blocked") and not v.get("findings"):
        return False
    if len((v.get("analysis") or "").strip()) < MIN_ANALYSIS:
        return False
    return len((v.get("summary") or "").strip()) >= MIN_SUMMARY


def apply_policies(findings: list[dict[str, Any]],
                   policies: list[dict[str, Any]],
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Развести находки на действующие и подавленные политиками прогона.

    Фильтрация живёт ЗДЕСЬ, а не в промпте ревьюера, сознательно: инструкция
    «не выноси findings по этой теме» роняет recall (это уже измерялось на
    «сообщай только важное»), а фильтр на стороне оркестратора оставляет
    ревьюера зрячим и сохраняет подавленное в журнале для метрик.

    Сопоставление механическое, без LLM (§7.1): совпадение ключевого слова
    в тексте находки. `blocker` не подавляется никогда — политика может
    снять требование к оформлению, но не к корректности.
    """
    active, suppressed = [], []
    for f in findings or []:
        if f.get("severity") == "blocker":
            active.append(f)
            continue
        text = " ".join(str(f.get(k, "")) for k in
                        ("issue", "suggestion", "category")).lower()
        hit = next((p for p in policies
                    if any(m.lower() in text for m in p.get("match") or [])), None)
        if hit:
            suppressed.append(dict(f, suppressed_by=hit["pid"]))
        else:
            active.append(f)
    return active, suppressed


def classify_findings(findings: list[dict[str, Any]] | None,
                      ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Разделить находки на задевающие замысел и механические.

    Смысл различения: «переименуй переменную» исполнитель чинит сам, а
    «эта абстракция лишняя» или «работа вышла за границы задачи» —
    решение о замысле, и правильный ответ знает только человек.
    """
    intent: list[dict[str, Any]] = []
    mechanical: list[dict[str, Any]] = []
    for f in findings or []:
        marked = f.get("intent")
        is_intent = (marked if isinstance(marked, bool)
                     else f.get("category") in INTENT_CATEGORIES)
        (intent if is_intent else mechanical).append(f)
    return intent, mechanical


def decide(round_no: int, verdict: dict[str, Any],
           history: list[dict[str, Any]], max_rounds: int = MAX_ITER,
           confirmations: int = 2, diff_sha: str | None = None,
           confirming: bool = False) -> tuple[str, int]:
    """Решение цикла по вердикту и истории раундов.

    Порядок проверок важен и заимствован у FuguNano:
      1. approve засчитывается, но DONE требует N независимых подтверждений
         — верификация вероятностна, один зелёный вердикт не терминал;
      2. исчерпание раундов — эскалация;
      3. несходимость (те же категории находок / число не убывает) —
         отдельный исход: нужен ДИАГНОЗ, а не очередной повтор;
      4. находки о замысле — вопрос человеку;
      5. иначе — продолжаем.
    """
    if verdict["verdict"] == "approve":
        # Подтверждение — свойство ОДНОГО состояния кода, а не задачи:
        # approve, полученный до последующего исправления, относится к
        # другому диффу и в счёт не идёт. Пока approvals считались по всей
        # истории, цепочка approve → флип на подтверждении → фикс → approve
        # закрывала задачу, финальный код которой видел ровно один ревьюер.
        approvals = sum(
            1 for r in history
            if r.get("verdict") == "approve"
            and (diff_sha is None or r.get("diff_sha") == diff_sha)) + 1
        return ((DONE, EXIT_OK) if approvals >= confirmations
                else (CONFIRM, EXIT_WORKING))

    if verdict["verdict"] == "blocked":
        return ESCALATE_MAX, EXIT_ESCALATE

    if round_no >= max_rounds:
        return ESCALATE_MAX, EXIT_ESCALATE

    findings = verdict.get("findings") or []
    # Сходимость меряется по исправительным раундам. Подтверждающий ревьюит
    # ТОТ ЖЕ дифф — часто другой рукой жребия, — и его находки говорят о
    # разбросе ревьюеров, а не о динамике задачи: сравнение с ним (или его
    # самого с прошлым) объявляло несходимость там, где исполнитель ещё
    # ничего не менял. Расхождение на одном диффе ловит и называет
    # _reviewers_disagreed, здесь ему делать нечего.
    prev = (None if confirming else next(
        (r for r in reversed(history) if not r.get("confirming")), None))
    if prev:
        same_class = ({f.get("category") for f in findings}
                      == set(prev.get("categories") or []))
        not_shrinking = len(findings) >= (prev.get("findings") or 0)
        if same_class and not_shrinking:
            return ESCALATE_NONCONV, EXIT_ESCALATE

    intent, _ = classify_findings(findings)
    if intent:
        return ASK_USER, EXIT_ASK_USER
    return CONTINUE, EXIT_WORKING


class Loop:
    def __init__(self, state: Any, config: dict[str, Any], agents: Any,
                 ui: Callable[..., None] | None = None) -> None:
        self.state = state
        self.config = config
        self.agents = agents          # объект с .implement() и .review()
        self.ui = ui or (lambda *_a, **_k: None)
        self._pre_existing: set[str] = set()   # дерево до старта задачи
        self._head_before: str | None = None   # история до старта задачи
        self._state_before: str | None = None  # состояние петли до старта
        self.live_board = bool(config.get("live_board", True))

    # --- механические шаги ------------------------------------------------

    def refresh_board(self) -> None:
        """Переписать `.swarm/board.html` по текущему состоянию.

        Доска обещала в подвале «обновляется по F5», а руководство
        оператора — «держите её открытой в соседней вкладке во время
        прогона». Оба утверждения были неправдой: данные вшиваются в
        страницу при генерации, а генерировал её только `swarm board` и
        хвост `swarm go`. Человек весь прогон смотрел на снимок прошлого
        и не имел способа об этом узнать.

        Сбой сборки доски петлю не останавливает: наблюдение — не работа.
        Но и молчать о нём нельзя, иначе страница тихо застынет снова.
        """
        if not self.live_board:
            return
        try:
            board.build(self.state.root)
        except Exception:
            # Граница деградации: запись с трассировкой обязательна (§6.7).
            log.exception("доска не обновлена",
                          extra={"swarm_phase": "board"})

    def _memory_record(self, task: dict[str, Any]) -> None:
        """Урок из терминального исхода. Память — наблюдение, не работа:
        её сбой оставляет трассировку и не стоит прогона (правило доски).

        Запись идёт ВСЕГДА, независимо от флага инъекции: память копится
        и до того, как её начали подмешивать, — посевной прогон E9
        строится ровно на этом свойстве.
        """
        try:
            memory_mod.record_task_outcome(self.state, task, self.config)
        except Exception:
            log.exception("память: урок не записан",
                          extra={"swarm_task": task.get("id")})

    def _memory_reflect(self) -> None:
        """«Сновидение» после прогона: дайджест пересобран, факт записан."""
        try:
            memory_mod.reflect_after_run(self.state, self.config)
        except Exception:
            log.exception("память: рефлексия не состоялась")

    def _sh(self, cmd: list[str],
            timeout: float = 900) -> subprocess.CompletedProcess[str]:
        # check=False намеренно: `_sh` — общий раннер, и КАЖДЫЙ вызывающий
        # смотрит returncode сам (ветвление по коду — суть половины
        # проверок петли). Исключение здесь лишило бы их этой ветки.
        return subprocess.run(cmd, capture_output=True, text=True,
                              cwd=self.state.root, timeout=timeout,
                              check=False)

    @property
    def max_iter(self) -> int:
        """Лимит раундов исправлений. Три хватало на стенде; на реальном
        проекте задача может честно требовать больше, и упираться в
        зашитую константу — значит эскалировать по чужой причине."""
        return int(self.config.get("max_iterations", MAX_ITER))

    def gate(self, task: dict[str, Any]) -> tuple[bool, str]:
        cmd = self.config.get("gate_command") or [
            "python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
        # Холодная сборка Rust не влезает в 900 с, а таймаут здесь —
        # исключение, топившее задачу в in_progress (до §5.3-починки).
        r = self._sh(cmd, timeout=int(self.config.get("gate_timeout", 900)))
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-3:])
        ok = r.returncode == 0
        self.state.metric(task=task["id"], phase="gate", ok=ok, tail=tail[:300])
        return ok, tail

    def integrity_check(self) -> list[str]:
        """§6.1: неприкосновенность истории и состояния петли.

        Deny-правила у Kimi недокументированы, а промпт — не гарантия:
        исполнителю ничто не мешает вызвать `git commit`, `git reset` или
        переписать `.swarm/tasks.json`, пометив задачу выполненной. Здесь
        проверяется не намерение, а ФАКТ — постфактум, но механически.
        Возвращает список нарушений.
        """
        bad = []
        head = self._sh(["git", "rev-parse", "HEAD"]).stdout.strip()
        if self._head_before and head != self._head_before:
            # Кто сдвинул HEAD, проверка знать не может: у неё есть только
            # «до» и «после». На PILOT-1 его сдвинул ОПЕРАТОР — закоммитил
            # правку конфига, пока задача шла в фоне, — а формулировка
            # «исполнитель вышел за границы доверия» обвинила агента и
            # стоила круга разбирательства. Поэтому текст нейтрален, а
            # рядом показан сам коммит: автор и заголовок отвечают на
            # вопрос «моё это или нет» с одного взгляда.
            who = self._sh(["git", "log", "-1", "--format=%an: %s",
                            head]).stdout.strip()
            bad.append(f"история изменилась во время задачи: HEAD "
                       f"{self._head_before[:8]} -> {head[:8]}"
                       + (f" ({who})" if who else "")
                       + ". Коммитит только оркестратор; если коммит ваш —"
                         " задачу можно вернуть в очередь как есть")
        state_now = self._state_fingerprint()
        if (self._state_before and state_now is not None
                and state_now != self._state_before):
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
            declared = self._declared_state_sha()
            if declared is None or declared != self._state_sha(state_now):
                bad.append("состояние петли (.swarm/) изменено в обход API: "
                           "правка файла не объявлена в журнале")
            else:
                # Объяснено — сдвигаем базу, иначе следующая проверка той же
                # задачи сработает на том же самом изменении повторно.
                self._state_before = state_now
        for marker, what in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                             ("MERGE_HEAD", "merge"),
                             ("CHERRY_PICK_HEAD", "cherry-pick")):
            if (pathlib.Path(self.state.root) / ".git" / marker).exists():
                bad.append(f"репозиторий оставлен в состоянии {what}")
        return bad

    def _state_sha(self, blob: str) -> str:
        """Отпечаток очереди тем же способом, каким его объявляет запись."""
        state_mod = sys.modules.get("state")
        if state_mod is not None:
            sha: str = state_mod.tasks_sha(blob)
            return sha
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def _declared_state_sha(self) -> str | None:
        """Последний отпечаток, объявленный законной записью состояния."""
        path = getattr(self.state, "journal_path", None)
        if path is None:
            return None
        state_mod = sys.modules.get("state")
        if state_mod is None:
            return None
        declared: str | None = state_mod.last_declared_sha(path)
        return declared

    def _state_fingerprint(self) -> str | None:
        path = getattr(self.state, "tasks_path", None)
        if path is None:
            return None
        try:
            text: str = path.read_text(encoding="utf-8")
        except OSError:
            return None
        else:
            return text

    def scope_check(self, task: dict[str, Any],
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
        protected = self.config.get("protected_paths") or ["tests/*", "tests/**"]

        def is_protected(path: str) -> bool:
            return any(fnmatch.fnmatch(path, p) for p in protected)

        unlocking = [pat for pat in allowed if is_protected(pat)]
        bad, touched_tests = [], []
        for path in self.state.changed_files():
            if path in self._pre_existing:
                # Лежало в дереве ДО старта задачи (`run --force`) — не
                # работа агента. revert такие файлы щадит, а страж без
                # этого исключения читал их как нарушение каждый раунд:
                # лимит выгорал об операторскую незакоммиченную правку,
                # которую никто не мог ни убрать, ни легализовать.
                # Цена решения: правку агента ПОВЕРХ такого файла страж
                # тоже не видит — этот риск оператор принял флагом --force.
                continue
            if not any(fnmatch.fnmatch(path, p) for p in allowed):
                bad.append(path)
                continue
            if is_protected(path) and not any(
                    fnmatch.fnmatch(path, u) for u in unlocking):
                touched_tests.append(path)
        ok = not bad and not touched_tests
        self.state.metric(task=task["id"], phase="scope", ok=ok,
                          unexpected=bad, protected=touched_tests)
        return ok, bad, touched_tests

    def revert(self) -> list[str]:
        """Откат работы агента, включая СОЗДАННЫЕ файлы.

        `git checkout -- .` возвращает отслеживаемые файлы, но untracked
        не трогает: нарушитель оставался на диске и повторно ловился
        каждый раунд, делая нарушение границ неустранимым. Откатываем
        поимённо то, что реально изменено, — по всему дереву не метём,
        чтобы не задеть чужое.
        """
        untouchable: set[str] = getattr(self, "_pre_existing", set())
        changed = [p for p in self.state.changed_files() if p not in untouchable]
        if not changed:
            return []
        tracked: list[str] = []
        untracked: list[str] = []
        for path in changed:
            probe = self._sh(["git", "ls-files", "--error-unmatch", path])
            (tracked if probe.returncode == 0 else untracked).append(path)
        if tracked:
            self._sh(["git", "checkout", "--", *tracked])
        for path in untracked:
            # intent-to-add уже мог зарегистрировать файл в индексе
            self._sh(["git", "rm", "-f", "--quiet", "--ignore-unmatch", path])
            target = self.state.root / path
            if target.exists():
                target.unlink()
        return changed

    def commit(self, task: dict[str, Any]) -> str | None:
        """Коммит принятой итерации; пустой diff — не ошибка (§4.4)."""
        env = {"GIT_AUTHOR_NAME": "swarm-executor",
               "GIT_AUTHOR_EMAIL": "executor@swarm.local",
               "GIT_COMMITTER_NAME": "swarm-orchestrator",
               "GIT_COMMITTER_EMAIL": "orchestrator@swarm.local"}
        subprocess.run(["git", "add", "-A"], cwd=self.state.root, check=True)
        # Здесь код возврата — ОТВЕТ, а не ошибка: 0 значит «нечего
        # коммитить», 1 — «есть изменения». check=True сломал бы логику.
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                cwd=self.state.root, check=False)
        if staged.returncode == 0:
            return None
        message = self.agents.commit_message(task, self._sh(["git", "diff",
                                                             "--cached"]).stdout)
        subprocess.run(["git", "commit", "-qm", message], cwd=self.state.root,
                       check=True, env={**os.environ, **env})
        # Коммит оркестратора легален: сдвигаем базу, иначе следующая
        # проверка целостности обвинит агента в нашей же работе.
        self._head_before = self._sh(["git", "rev-parse", "HEAD"]).stdout.strip()
        return self._sh(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()

    def cleanup(self, task: dict[str, Any], reason: str) -> str | None:
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
        """
        if not self._sh(["git", "status", "--porcelain"]).stdout.strip():
            return None
        self._sh(["git", "reset", "-q"])
        label = f"swarm:{task['id']}-{reason}"
        r = self._sh(["git", "stash", "push", "-u", "-q", "-m", label])
        if r.returncode != 0:
            self.state.log("stash_failed", task=task["id"], reason=reason,
                           stderr=(r.stderr or "").strip()[:300])
            return None
        return label

    def _apply_patch(self, diff_text: str) -> bool:
        """Вернуть worktree к сохранённому лучшему состоянию.

        Код возврата `git apply` обязан проверяться. Здесь работа уже
        снесена `revert`, и молчаливый провал восстановления означает, что
        петля пойдёт коммитить ПУСТОТУ, считая, что откатилась к лучшему
        состоянию. Худший из возможных исходов: работа потеряна, а история
        утверждает обратное.
        """
        if not diff_text.strip():
            return True
        r = subprocess.run(["git", "apply", "-"], cwd=self.state.root,
                           input=diff_text, text=True, capture_output=True,
                           check=False)
        if r.returncode != 0:
            self.state.log("restore_failed", stderr=(r.stderr or "").strip()[:300])
        return r.returncode == 0

    def _review_with_quota_wait(self, task: dict[str, Any], tail: str,
                                iteration: int, confirming: bool,
                                ) -> dict[str, Any] | None:
        """§5.3: отказ по квоте — пауза с бэкоффом, а не авария.

        Квота — заведомо временное и заведомо повторяемое состояние:
        блокировать за него задачу значит наказывать её за погоду у
        провайдера. Ровно это и происходило: QuotaExceededError долетал до
        общего `except Exception` в run(), задача уходила в blocked с
        диагнозом «авария», очередь останавливалась, а ветка «пауза по
        квоте» в CLI была недостижима.

        Теперь ждём по нарастающей (1→2→4 мин по умолчанию, конфиг
        `quota_backoff_s`) и повторяем. Не отпустило — работа в stash,
        задача возвращается в очередь как есть (она ни в чём не виновата),
        исключение уходит наверх: прогон ставится на паузу целиком, и
        «когда продолжить» решает человек.
        """
        delays = list(self.config.get("quota_backoff_s", (60, 120, 240)))
        while True:
            try:
                verdict: dict[str, Any] | None = self.agents.review(
                    task, tail, iteration, confirming=confirming)
            except Exception as e:
                # По имени, не по классу: у Agents своя копия модуля loop,
                # и её QuotaExceededError — другой объект (см. quota_exception).
                if not quota_exception(e):
                    raise
                if not delays:
                    stash = self.cleanup(task, "quota-pause")
                    self.state.log("quota_pause", task=task["id"],
                                   round=iteration, stash=stash,
                                   message=str(e)[:200])
                    self.state.set_status(task["id"], "pending", stash=stash)
                    self.ui(f"    ПАУЗА ПО КВОТЕ: {str(e)[:120]}")
                    raise
                delay = delays.pop(0)
                self.state.log("quota_wait", task=task["id"], round=iteration,
                               wait_s=delay, message=str(e)[:200])
                self.state.metric(task=task["id"], iter=iteration,
                                  phase="review", quota_wait_s=delay)
                self.ui(f"    квота провайдера: ждём {delay} с")
                time.sleep(delay)
            else:
                return verdict

    @staticmethod
    def _reviewers_disagreed(history: list[dict[str, Any]]
                             ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Последний раунд был подтверждающим и сменил вердикт?

        Подтверждающий раунд ревьюет ТОТ ЖЕ дифф: исполнитель в нём не
        вызывается, код между раундами не менялся. Значит, смена вердикта
        — свойство ревьюеров, а не работы. Отличить это от несходимости
        задачи можно только здесь: дальше по тексту диагноза информации
        уже нет.
        """
        if len(history) < 2 or not history[-1].get("confirming"):
            return None
        prev, last = history[-2], history[-1]
        if prev.get("verdict") == last.get("verdict"):
            return None
        return prev, last

    @staticmethod
    def _arm(row: dict[str, Any]) -> str:
        """Рука замера словами: без неё «разошлись» нечем проверить."""
        model = row.get("model") or "сессионная модель"
        effort = row.get("effort")
        return f"{model}/{effort}" if effort else str(model)

    @staticmethod
    def _diagnose(outcome: str, history: list[dict[str, Any]],
                  scope_failures: list[list[str]] | None = None,
                  sig_failures: list[list[str]] | None = None) -> str:
        """Несходимость требует ДИАГНОЗА, а не очередного повтора.

        Закрытый список гипотез (FuguNano): человеку эскалируется не голый
        факт «три раунда подряд», а версия о причине.

        Диагноз обязан называть только то, что диагност МОЖЕТ знать. Пока
        любое исчерпание раундов объявлялось «задача слишком крупная»,
        петля посылала человека расщеплять задачу, которую сама же
        одобрила раундом раньше (пилот, e7in), и заодно прятала главное
        наблюдение прогона — расхождение двух рук на одном диффе. Это
        третий случай того же класса после проверки целостности и стража
        путей: предохранитель, называющий причину, которой не знает.
        """
        if outcome == ESCALATE_MAX:
            # Сначала — то, что диагност ЗНАЕТ наверняка (§5.7.2). Сигнатуры
            # проверяются РАНЬШЕ границ: нарушение замороженного контракта —
            # факт более точный, чем общий выход за paths, и называть общую
            # причину, когда известна точная, значит соврать умолчанием.
            if sig_failures and len(sig_failures) >= 2:
                changed = sorted({s for row in sig_failures for s in row})
                shown = ", ".join(changed[:5]) + ("…" if len(changed) > 5 else "")
                return (f"{len(sig_failures)} раунд(ов) сгорели на нарушении "
                        f"замороженных сигнатур контракта: {shown}. Это не "
                        f"вопрос размера задачи — исполнитель меняет то, что "
                        f"договором запрещено менять. Если контракт скелета "
                        f"невыполним, нужен пересмотр сигнатур в задаче-"
                        f"скелете или dispute, а не новая попытка")
            # Раунды, сгоревшие на границах, — факт из журнала, а не гипотеза:
            # на пилоте k3ad и s2ky получили «задача слишком крупная —
            # расщепить», когда обе бились об один защищённый файл.
            # Расщепление там не помогло бы: любой осколок упёрся бы туда же.
            if scope_failures and len(scope_failures) >= 2:
                files = sorted({f for row in scope_failures for f in row})
                shown = ", ".join(files[:5]) + ("…" if len(files) > 5 else "")
                return (f"{len(scope_failures)} раунд(ов) сгорели на "
                        f"нарушении границ — исполнитель каждый раз правил: "
                        f"{shown}. Расщепление не поможет: любой осколок "
                        f"упрётся туда же. Добавьте файл в paths задачи явно "
                        f"или пересмотрите protected_paths")
            flip = Loop._reviewers_disagreed(history)
            if flip:
                prev, last = flip
                return (
                    f"ревьюеры разошлись на ОДНОМ И ТОМ ЖЕ диффе: раунд "
                    f"{prev['round']} ({Loop._arm(prev)}) — {prev['verdict']}, "
                    f"находок {prev['findings']}; подтверждающий раунд "
                    f"{last['round']} ({Loop._arm(last)}) — {last['verdict']}, "
                    f"находок {last['findings']}. Исполнитель между раундами "
                    f"не вызывался, код не менялся. Расщеплять задачу не "
                    f"нужно — прочтите оба вердикта в .swarm/log и решите, "
                    f"чья правда")
            return "исчерпаны раунды: задача, вероятно, слишком крупная — расщепить"
        counts = [h["findings"] for h in history]
        cats = [tuple(h["categories"]) for h in history]
        if len(set(cats)) == 1 and len(cats) > 1:
            return ("замечания одного класса повторяются: либо требование "
                    "сформулировано неясно, либо ревьюер строже спецификации")
        if counts and counts == sorted(counts, reverse=True):
            return "находки убывают, но не до нуля: не хватило раундов"
        return ("число находок не убывает — вероятны качели fix→break; "
                "нужна другая реализация, а не правки поверх")

    # --- цикл --------------------------------------------------------------

    def run_task(self, task: dict[str, Any]) -> str:
        tid = task["id"]
        self.ui(f"=== {tid}: {task['title']}")
        self.state.set_status(tid, "in_progress")
        # Всё, что уже лежало в дереве, работой агента не является и
        # откату не подлежит: иначе незакоммиченная работа человека
        # уничтожается безвозвратно при первом же нарушении границ.
        self._pre_existing = set(self.state.changed_files())
        if self._pre_existing:
            # Факт в журнал: страж границ эти файлы дальше не видит, и
            # оператор обязан знать, что задача пошла поверх его правок.
            self.state.log("pre_existing_dirt", task=tid,
                           files=sorted(self._pre_existing))
        self._head_before = self._sh(["git", "rev-parse", "HEAD"]).stdout.strip()
        self._state_before = self._state_fingerprint()

        ok, tail = self.gate(task)
        if not ok:
            self.state.log("baseline_red", task=tid, output=tail)
            self.state.set_status(tid, "blocked", reason="baseline_red")
            self.ui("    baseline красный — задача не запускается")
            return "blocked"

        # Решение человека действует на ВСЮ задачу, а не на одну итерацию:
        # иначе после первого же ревью feedback перезаписывается находками,
        # и указание теряется (поймано на приёмке: исполнитель не вынес
        # хелперы, ревьюер повторно поднял то же замечание).
        human = None
        if task.get("human_answer"):
            human = {"human_answer": task["human_answer"],
                     "note": "решение человека по замыслу — обязательно к "
                             "исполнению во всех последующих итерациях"}
        feedback = dict(human) if human else None
        history: list[dict[str, Any]] = []
        best: dict[str, Any] = {"findings": None, "round": None, "diff": None,
                                "items": None}
        # Подтверждающие раунды не расходуют лимит исправлений: они не
        # меняют код, а перепроверяют уже принятый. Иначе approve на
        # последней итерации обречён — подтверждать его негде (поймано на
        # приёмке: v3st получила approve на 3-м раунде из 3 и ушла в
        # эскалацию вместо закрытия).
        # Подтверждение — это ПОВТОРНОЕ РЕВЬЮ того же диффа, а не новый круг
        # работы. Пока подтверждающий раунд шёл через исполнителя, ломались
        # обе половины замысла: во-первых, исполнитель волен изменить код, и
        # второй голос относился уже к другому состоянию — независимой
        # перепроверки одного результата не получалось; во-вторых, любой сбой
        # исполнителя (no_report) съедал раунд, и одобренная задача с зелёным
        # гейтом уходила в эскалацию. Ровно так v3st и заблокировалась на
        # приёмке дважды подряд.
        confirm_rounds = 0
        iteration = 0
        confirming = False
        # Раунды, сгоревшие на границах: диагност обязан отличать «задача
        # не сходится» от «исполнитель бьётся о защищённый файл».
        scope_failures: list[list[str]] = []
        # То же для замороженных сигнатур (E10, fill-задача): нарушение
        # контракта скелета — отдельная причина, не «задача не сходится».
        sig_failures: list[list[str]] = []
        instant_crashes = 0
        # Снимок сигнатур — ДО первого вызова исполнителя: сравнивать после
        # первого раунда не с чем, если снимок взят после него. Один файл на
        # fill-задачу — гарантия валидатора плана (E10, planner._skeleton_
        # errors), не петли: берём первый путь и не спорим с тем, что уже
        # проверено раньше.
        sig_file: pathlib.Path | None = None
        sig_baseline: list[str] | None = None
        if task.get("frozen_signatures"):
            paths = task.get("paths") or []
            if paths:
                sig_file = self.state.root / paths[0]
                sig_baseline = pyindex.file_signatures(sig_file)
        while iteration < self.max_iter + confirm_rounds:
            # Бюджет проверяется перед КАЖДОЙ итерацией, а не только между
            # задачами: проверка раз в задачу означала, что одна задача
            # вольна пробить потолок на любую величину — сколько раундов
            # влезет. Задача при этом ни в чём не виновата, поэтому pending,
            # а не blocked: подняв бюджет, прогон продолжают тем же `go`.
            budget = self.config.get("total_budget_usd")
            spent = self.state.total_spend() if budget else 0.0
            if budget and spent >= float(budget):
                stash = self.cleanup(task, "budget")
                self.state.log("budget_exhausted", task=tid, round=iteration,
                               spent=spent, budget=budget, stash=stash)
                self.state.set_status(tid, "pending", stash=stash)
                self.ui(f"    БЮДЖЕТ ИСЧЕРПАН посреди задачи: ${spent} из "
                        f"${budget}; работа в stash, задача возвращена "
                        f"в очередь")
                return "budget_stop"
            iteration += 1
            # В подтверждающем раунде исполнитель не вызывается: гейт и
            # границы перепроверяются (дёшево), ревью идёт по тому же диффу.
            was_confirmation = confirming
            if confirming:
                confirming = False
            else:
                report = self.agents.implement(task, feedback, iteration)
                if report is None:
                    fail = getattr(self.agents, "last_implement_failure",
                                   None) or {}
                    # Мгновенная смерть процесса (секунды, поток пуст) — не
                    # неудачная работа, а невозможность работать. Два раза
                    # подряд = устойчивое состояние среды: дальше жечь
                    # раунды бессмысленно, и следующая задача умрёт так же.
                    if (fail.get("reason") == "crash"
                            and fail.get("wall_s", 1e9) < 10
                            and fail.get("events", 1e9) <= 1):
                        instant_crashes += 1
                        if instant_crashes >= 2:
                            self.state.set_status(
                                tid, "pending",
                                reason="executor_unavailable")
                            raise ExecutorUnavailableError(
                                fail.get("stderr")
                                or "процесс исполнителя умирает на старте")
                    feedback = {"note": "предыдущий ответ не содержал валидного "
                                        "JSON-отчёта — повтори, соблюдая контракт"}
                    continue
                # Отступления — заявление исполнителя О СЕБЕ, не находка
                # ревьюера: ревьюер их не увидит (асимметрия §3), в журнал
                # они идут как есть. Форма поля не гарантирована контрактом
                # — журнал читается как данные: строка оборачивается в
                # список из одного элемента, мусор (dict, пустота) молчит.
                deviations = report.get("deviations")
                if isinstance(deviations, str) and deviations.strip():
                    deviations = [deviations]
                if isinstance(deviations, list) and deviations:
                    self.state.log("deviations_declared", task=tid,
                                   round=iteration,
                                   deviations=[str(d)[:200] for d in deviations])
                if report.get("status") == "dispute":
                    stash = self.cleanup(task, "dispute")
                    question = report.get("summary", "спор исполнителя")
                    deps = task.get("deps") or []
                    if task.get("executor_model") and deps:
                        # Fill-задача (E10): спор чаще всего означает
                        # сломанный контракт скелета, а не саму заливку —
                        # подсказка экономит круг ручного разбора.
                        question += (f"; контракт скелета спорен — "
                                    f"рассмотрите swarm replan {deps[0]}")
                    qid = self.state.ask(tid, "dispute", question,
                                         stash=stash, round=iteration,
                                         dispute=report.get("dispute"))
                    self.state.set_status(tid, "blocked", reason="dispute",
                                          stash=stash, iterations=iteration,
                                          question_id=qid)
                    self.ui(f"    спор исполнителя [{qid}]")
                    return "blocked"

            ok, tail = self.gate(task)
            if not ok:
                # В журнал, а не только в метрики: без этого swarm why и
                # report показывали пустоту, и разбор «почему сгорели
                # раунды» шёл через метрики вручную (пилот, k3ad).
                self.state.log("gate_failed", task=tid, round=iteration,
                               tail=(tail or "")[-300:])
                feedback = {"note": "gate провален", "tests": tail}
                continue

            violations = self.integrity_check()
            if violations:
                self.state.log("integrity_violation", task=tid, round=iteration,
                               violations=violations)
                self.state.metric(task=tid, iter=iteration, phase="integrity",
                                  ok=False, violations=violations)
                stash = self.cleanup(task, "integrity")
                # Формулировка нейтральна намеренно: проверка знает ФАКТ
                # расхождения, но не автора. Обвинение исполнителя, когда
                # HEAD сдвинул оператор, стоило круга разбирательства.
                qid = self.state.ask(tid, ASK_USER,
                                     "нарушена неприкосновенность истории или "
                                     "состояния петли: "
                                     + "; ".join(violations), stash=stash)
                self.state.set_status(tid, "blocked", reason="integrity",
                                      stash=stash, iterations=iteration,
                                      question_id=qid)
                self.ui(f"    ЦЕЛОСТНОСТЬ НАРУШЕНА [{qid}]: {violations[0]}")
                return "blocked"

            sok, bad, tests_touched = self.scope_check(task)
            if not sok:
                scope_failures.append(sorted(set(bad) | set(tests_touched)))
                self.state.log("scope_violation", task=tid, round=iteration,
                               unexpected=bad, protected=tests_touched)
                self.revert()
                feedback = {"note": "нарушение границ задачи",
                            "unexpected_files": bad,
                            "protected_tests": tests_touched}
                continue

            if sig_baseline is not None and sig_file is not None:
                sig_now = pyindex.file_signatures(sig_file)
                changed = sorted(set(sig_baseline) ^ set(sig_now))
                if changed:
                    # НЕ revert: границы — про ЧУЖОЕ, сигнатуры — про
                    # СВОЁ. Тело заливки внутри пришпиленной сигнатуры
                    # может быть спасаемо, а откат снёс бы и его.
                    sig_failures.append(changed)
                    self.state.log("signature_violation", task=tid,
                                   round=iteration, changed=changed)
                    feedback = {"note": "сигнатуры контракта изменены — "
                                        "верни их в точности; если контракт "
                                        "невыполним, канал dispute",
                                "changed_signatures": changed}
                    continue

            verdict = self._review_with_quota_wait(task, tail, iteration,
                                                   was_confirmation)
            if verdict is None:
                # Работа могла быть готовой и зелёной — сорвалось РЕВЬЮ.
                # Молчаливый blocked оставлял оператора без единого слова
                # о том, что произошло: инбокс пуст, журнал пуст, статус
                # «invalid_verdict». Диагноз обязателен.
                stash = self.cleanup(task, "invalid-verdict")
                why = getattr(self.agents, "last_review_failure", None)
                diagnosis = REVIEW_DIAGNOSIS.get(
                    why or "", REVIEW_DIAGNOSIS["invalid"])
                self.state.log("review_failed", task=tid, round=iteration,
                               why=why or "invalid", stash=stash,
                               gate_passed=True)
                qid = self.state.ask(tid, "review_failed", diagnosis,
                                     stash=stash, round=iteration)
                self.state.set_status(tid, "blocked", reason="invalid_verdict",
                                      stash=stash, iterations=iteration,
                                      question_id=qid, diagnosis=diagnosis)
                self.ui(f"    РЕВЬЮ НЕ СОСТОЯЛОСЬ [{qid}]: {diagnosis}")
                return "blocked"

            raw_findings = verdict.get("findings") or []
            findings, suppressed = apply_policies(
                raw_findings, self.state.policies())
            if suppressed:
                self.state.log("policy_suppressed", task=tid, round=iteration,
                               count=len(suppressed),
                               items=[{"policy": f["suppressed_by"],
                                       "severity": f.get("severity"),
                                       "issue": str(f.get("issue"))[:200]}
                                      for f in suppressed])
                self.state.metric(task=tid, iter=iteration,
                                  phase="policy", suppressed=len(suppressed))
                self.ui(f"    подавлено политиками: {len(suppressed)} "
                        f"(осталось {len(findings)})")
            # решение принимается по действующим находкам: если все замечания
            # относятся к тому, что человек уже отменил, задача прошла ревью
            verdict = dict(verdict, findings=findings)
            if raw_findings and not findings and verdict["verdict"] != "blocked":
                verdict["verdict"] = "approve"
            intent, mechanical = classify_findings(findings)
            # Отпечаток отревьюенного состояния: по нему `decide` считает
            # подтверждения, а история позволяет отличить «второй голос по
            # тому же коду» от «голос по уже другому».
            work = self.state.work_diff()
            work_sha = hashlib.sha256(work.encode("utf-8")).hexdigest()[:12]
            # Тот же бюджет, что и у цикла выше. Пока `decide` считал по
            # голому max_iter, а цикл — по max_iter + confirm_rounds, они
            # расходились ровно на число выданных подтверждений: на
            # пилоте задача e7in получила approve на 2-м раунде, на 3-м
            # (подтверждающем) вердикт сменился — и вместо разрешённого
            # 4-го раунда исправлений ушла в эскалацию «раунды
            # исчерпаны». Две записи одного лимита обязаны быть одной.
            outcome, code = decide(iteration, verdict, history,
                                   max_rounds=self.max_iter + confirm_rounds,
                                   confirmations=self.config.get("confirmations", 2),
                                   diff_sha=work_sha,
                                   confirming=was_confirmation)
            history.append({"round": iteration, "verdict": verdict["verdict"],
                            "findings": len(findings),
                            # Без этих двух полей диагност не может
                            # отличить «задача не сходится» от
                            # «ревьюеры разошлись на одном диффе».
                            "confirming": was_confirmation,
                            "diff_sha": work_sha,
                            **getattr(self.agents, "last_tuning", {}),
                            "categories": sorted({str(f.get("category") or "")
                                                  for f in findings})})
            self.state.log("round", task=tid, round=iteration,
                           verdict=verdict["verdict"], outcome=outcome,
                           findings=len(findings), intent=len(intent))
            # Раунд — самая мелкая единица, о которой человеку есть что
            # сказать: он длится минуты, и до конца задачи наблюдать за
            # прогоном было нечем, кроме бегущих строк в терминале.
            self.refresh_board()

            # keep-best: раунд, где находок меньше всего, запоминается —
            # если следующий окажется хуже, откатываемся к лучшему, чтобы
            # цикл не деградировал (приём FuguNano).
            rolled_back_to: int | None = None
            if best["findings"] is None or len(findings) < best["findings"]:
                best.update(findings=len(findings), round=iteration,
                            diff=work, items=list(findings))
            elif (len(findings) > best["findings"] and best["diff"]
                    and not was_confirmation
                    and verdict["verdict"] == "request_changes"):
                # Подтверждающий раунд ревьюет ТОТ ЖЕ дифф: исполнитель в нём
                # не вызывался, кода никто не трогал. Рост числа находок там
                # — разброс ревьюера, а не регресс. На PILOT-1 (g2pf) это
                # дало ложный «регресс 2 против 1» и совершенно ненужный
                # цикл revert + git apply поверх неизменного дерева.
                # Approve с лишними minor-находками — тоже не регресс:
                # откатывать одобренное дерево и коммитить вместо него
                # прошлый раунд значило бы подменить предмет вердикта.
                self.ui(f"    регресс: {len(findings)} находок против "
                        f"{best['findings']} в раунде {best['round']} — откат")
                self.revert()
                if not self._apply_patch(best["diff"]):
                    # Работа снесена, восстановить не удалось. Коммитить
                    # тут нечего, и делать вид, что откат состоялся, нельзя.
                    diagnosis = ("откат к лучшему раунду не состоялся: "
                                 "git apply отверг сохранённый дифф, работа "
                                 "раунда потеряна. Смотри restore_failed в "
                                 "журнале и переоткрой задачу заново")
                    qid = self.state.ask(tid, "restore_failed", diagnosis,
                                         round=iteration)
                    self.state.set_status(tid, "blocked", reason="restore_failed",
                                          iterations=iteration,
                                          question_id=qid, diagnosis=diagnosis)
                    self.ui(f"    ОТКАТ НЕ СОСТОЯЛСЯ [{qid}]")
                    return "blocked"
                rolled_back_to = int(best["round"])

            if outcome in (DONE, CONFIRM):
                if outcome == CONFIRM:
                    confirm_rounds += 1
                    confirming = True
                    self.ui(f"    approve #{iteration}, нужно ещё подтверждение "
                            f"(повторное ревью того же диффа, без исполнителя)")
                    continue
                # `head` в интенте — точка отсчёта для реконсиляции (§5.6):
                # resume сравнит её с текущим HEAD и решит, состоялся ли
                # коммит, вместо того чтобы посылать человека смотреть.
                with self.state.step(tid, "commit",
                                     head=self._head_before) as step:
                    sha = self.commit(task)
                    step.result(commit=sha)
                self.state.set_status(tid, "done", iterations=iteration,
                                      commit=sha, exit_code=code,
                                      no_change_needed=report.get("status")
                                      == "no_change_needed")
                self.ui(f"    done (итерация {iteration})"
                        + ("" if sha else " [без коммита: пустой diff]"))
                return "done"

            if outcome == ASK_USER:
                stash = self.cleanup(task, "ask-user")
                question = "; ".join(f.get("issue", "")[:200] for f in intent)
                qid = self.state.ask(
                    tid, "intent", question,
                    findings=[{"category": f.get("category"),
                               "severity": f.get("severity"),
                               "issue": f.get("issue"),
                               "suggestion": f.get("suggestion")} for f in intent],
                    stash=stash, round=iteration,
                    mechanical_left=len(mechanical))
                self.state.set_status(tid, "blocked", reason="ask_user",
                                      stash=stash, iterations=iteration,
                                      exit_code=code, question_id=qid)
                self.ui(f"    вопрос человеку [{qid}]: {len(intent)} находок "
                        f"о замысле (механических: {len(mechanical)})")
                return "ask_user"

            if outcome in (ESCALATE_MAX, ESCALATE_NONCONV):
                stash = self.cleanup(task, outcome.replace("_", "-"))
                diagnosis = self._diagnose(outcome, history)
                qid = self.state.ask(tid, outcome, diagnosis, stash=stash,
                                     round=iteration, history=history)
                self.state.set_status(tid, "blocked", reason=outcome,
                                      stash=stash, iterations=iteration,
                                      exit_code=code, diagnosis=diagnosis,
                                      question_id=qid)
                self.ui(f"    эскалация [{qid}]: {diagnosis}")
                return "blocked"

            if rolled_back_to is None:
                feedback = {"findings": mechanical or findings}
            else:
                # После отката в дереве лежит код ЛУЧШЕГО раунда, а findings
                # текущего вердикта описывают уже снесённый. Отдавать их
                # исполнителю значило посылать его чинить призраки: раунд
                # уходил на поиск кода, которого нет. Замечания — по тому
                # состоянию, которое он реально увидит.
                _, best_mech = classify_findings(list(best.get("items") or []))
                feedback = {"note": (f"код возвращён к лучшему раунду "
                                     f"{rolled_back_to}; замечания ниже — "
                                     f"по нему, дерево ему соответствует"),
                            "findings": best_mech or list(best.get("items")
                                                          or [])}
            if human:
                feedback.update(human)
            self.ui(f"    request_changes ({len(findings)} замечаний, "
                    f"механических {len(mechanical)})")

        # Выход из цикла по исчерпанию раундов — тоже терминальный исход, и
        # он обязан попасть в инбокс. Иначе задача, где исполнитель раз за
        # разом не возвращал валидный отчёт, блокируется МОЛЧА и человек о
        # ней не узнаёт (поймано на приёмке: v3st исчезла из виду).
        stash = self.cleanup(task, "max-iterations")
        diagnosis = self._diagnose(ESCALATE_MAX, history,
                                   scope_failures=scope_failures,
                                   sig_failures=sig_failures)
        qid = self.state.ask(tid, ESCALATE_MAX, diagnosis, stash=stash,
                             round=self.max_iter, history=history)
        self.state.set_status(tid, "blocked", reason="max_iterations",
                              stash=stash, iterations=self.max_iter,
                              exit_code=EXIT_ESCALATE, diagnosis=diagnosis,
                              question_id=qid)
        self.ui(f"    эскалация [{qid}]: {diagnosis}")
        return "blocked"

    def run(self, limit: int | None = None) -> dict[str, str]:
        results: dict[str, str] = {}
        # Доска должна существовать с первой секунды прогона, а не с
        # первого раунда: открыть её человек хочет сразу.
        self.refresh_board()
        while True:
            ready = self.state.ready_tasks()
            if not ready:
                break
            task = ready[0]
            # Бюджет прогона — деньги владельца, а не абстракция: петля
            # обязана остановиться сама и сказать об этом, а не выяснять
            # потолок постфактум по счёту.
            budget = self.config.get("total_budget_usd")
            spent = self.state.total_spend()
            if budget and spent >= float(budget):
                self.state.log("budget_exhausted", spent=spent, budget=budget,
                               stopped_before=task["id"])
                self.ui(f"\nБЮДЖЕТ ПРОГОНА ИСЧЕРПАН: ${spent} из ${budget}. "
                        f"Остановлено перед задачей {task['id']}.")
                results["_budget"] = "exhausted"
                break
            # Любой сбой на задаче (таймаут гейта, отказ провайдера,
            # сетевая ошибка) обязан оставить её в РАЗБИРАЕМОМ состоянии.
            # Без этого исключение выходило наружу, задача навсегда
            # оставалась in_progress, и вернуть её могла только ручная
            # правка tasks.json: ready_tasks берёт только pending, а
            # retry требовал blocked.
            try:
                results[task["id"]] = self.run_task(task)
                self._memory_record(task)
            except ExecutorUnavailableError as e:
                # Задача уже возвращена в pending внутри run_task.
                self.state.log("executor_unavailable", task=task["id"],
                               stderr=str(e)[:400])
                results["_executor"] = "unavailable"
                self.ui(f"\nИСПОЛНИТЕЛЬ НЕДОСТУПЕН — прогон остановлен.\n"
                        f"    {str(e)[:200]}\n"
                        f"    Задача {task['id']} возвращена в очередь; "
                        f"продолжайте после устранения причины.")
                break
            except KeyboardInterrupt:
                self._rescue(task, "прервано человеком")
                raise
            except Exception as e:
                if quota_exception(e):
                    # Не авария: задача уже возвращена в pending, работа в
                    # stash (_review_with_quota_wait). _rescue здесь
                    # превратил бы паузу в blocked с диагнозом «авария» —
                    # ровно тот дефект, который эта ветка чинит. Наверх,
                    # к exit-коду 4. Проверка по имени класса: исключение
                    # могло подняться из чужой копии модуля.
                    self.refresh_board()
                    raise
                # убивать очередь; задача обязана остаться разбираемой
                log.exception("задача упала", extra={"swarm_task": task["id"]})
                results[task["id"]] = self._rescue(task, f"{type(e).__name__}: {e}")
                self._memory_record(task)
                self.refresh_board()
                break
            self.refresh_board()
            if results.get(task["id"]) == "budget_stop":
                # Задача вернулась в pending — без break ready_tasks выдал
                # бы её снова, и петля кружила бы по «исчерпано» вечно.
                results["_budget"] = "exhausted"
                break
            if limit and len(results) >= limit:
                break
        if results:
            self._memory_reflect()
        return results

    def _rescue(self, task: dict[str, Any], reason: str) -> str:
        """Спасти задачу и работу после аварии: stash, blocked, вопрос."""
        tid = task["id"]
        self.state.log("task_crashed", task=tid, reason=reason)
        try:
            stash = self.cleanup(task, "crash")
        except Exception:
            log.exception("stash при аварии не создан",
                          extra={"swarm_task": tid})
            stash = None
        try:
            qid = self.state.ask(tid, ASK_USER,
                                 f"прогон прерван аварией: {reason}", stash=stash)
            self.state.set_status(tid, "blocked", reason="crash",
                                  stash=stash, question_id=qid)
        except Exception as e:
            # Отказ самого регистратора аварии нельзя терять: иначе задача
            # молча остаётся в work и оператор не узнает почему.
            log.exception("авария не записана", extra={"swarm_task": tid})
            self.ui(f"    не записана авария {tid}: {type(e).__name__}: {e}")
        self.ui(f"    АВАРИЯ на {tid}: {reason}")
        return "blocked"
