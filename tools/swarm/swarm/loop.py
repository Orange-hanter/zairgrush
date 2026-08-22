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
import datetime as dt
import hashlib
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
import gitops  # noqa: E402
import memory as memory_mod  # noqa: E402
import obs  # noqa: E402
import pyindex  # noqa: E402
from gitops import (  # noqa: E402
    _apply_patch as _git_apply_patch,
    declared_state_sha as _git_declared_state_sha,
    sh as _git_sh,
    state_fingerprint as _git_state_fingerprint,
    state_sha as _git_state_sha,
)
from reviewcycle import (  # noqa: E402
    _arm as _rc_arm,
    _diagnose as _rc_diagnose,
    _implement_with_quota_wait as _rc_implement_with_quota_wait,
    _review_with_quota_wait as _rc_review_with_quota_wait,
    _reviewers_disagreed as _rc_reviewers_disagreed,
)
from verdicts import (  # noqa: E402,F401
    ASK_USER,
    CATEGORIES,
    CONFIRM,
    CONTINUE,
    DONE,
    ESCALATE_MAX,
    ESCALATE_NONCONV,
    EXIT_ASK_USER,
    EXIT_ESCALATE,
    EXIT_OK,
    EXIT_WORKING,
    INTENT_CATEGORIES,
    MAX_ITER,
    MIN_ANALYSIS,
    MIN_SUMMARY,
    SEVERITIES,
    EscalationError,
    ExecutorUnavailableError,
    QuotaExceededError,
    apply_policies,
    classify_findings,
    decide,
    parse_quota_reset,
    quota_error,
    quota_exception,
    validate_verdict,
    verdict_problem,
)

log = obs.get_logger("loop")

# Явный re-export: planner и тесты обращаются к исключениям и quota_error
# через `loop.X`; __all__ нужен для mypy --strict (explicit reexport).
__all__ = [
    "EscalationError",
    "ExecutorUnavailableError",
    "QuotaExceededError",
    "quota_error",
]

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


class Loop:
    def __init__(self, state: Any, config: dict[str, Any], agents: Any,
                 ui: Callable[..., None] | None = None) -> None:
        self.state = state
        self.config = config
        self.agents = agents          # объект с .implement() и .review()
        self.ui = ui or (lambda *_a, **_k: None)
        self.pre_existing: set[str] = set()   # дерево до старта задачи
        self.run_dirt: set[str] = set()       # дерево до старта ПРОГОНА
        self.head_before: str | None = None   # история до старта задачи
        self.state_before: str | None = None  # состояние петли до старта
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

    def sh(self, cmd: list[str],
            timeout: float = 900) -> subprocess.CompletedProcess[str]:
        return _git_sh(self, cmd, timeout)


    @property
    def max_iter(self) -> int:
        """Лимит раундов исправлений. Три хватало на стенде; на реальном
        проекте задача может честно требовать больше, и упираться в
        зашитую константу — значит эскалировать по чужой причине."""
        return int(self.config.get("max_iterations", MAX_ITER))

    def gate(self, task: dict[str, Any]) -> tuple[bool, str]:
        return gitops.gate(self, task)

    def integrity_check(self) -> list[str]:
        return gitops.integrity_check(self)

    def state_sha(self, blob: str) -> str:
        return _git_state_sha(blob)

    def declared_state_sha(self) -> str | None:
        return _git_declared_state_sha(self)

    def state_fingerprint(self) -> str | None:
        return _git_state_fingerprint(self)

    def scope_check(self, task: dict[str, Any],
                    ) -> tuple[bool, list[str], list[str]]:
        return gitops.scope_check(self, task)

    def revert(self) -> list[str]:
        return gitops.revert(self)

    def commit(self, task: dict[str, Any]) -> str | None:
        return gitops.commit(self, task)

    def cleanup(self, task: dict[str, Any], reason: str) -> str | None:
        return gitops.cleanup(self, task, reason)

    def _apply_patch(self, diff_text: str) -> bool:
        return _git_apply_patch(self, diff_text)

    def _review_with_quota_wait(self, task: dict[str, Any], tail: str,
                                iteration: int, confirming: bool,
                                ) -> dict[str, Any] | None:
        return _rc_review_with_quota_wait(self, task, tail, iteration, confirming)

    def _implement_with_quota_wait(self, task: dict[str, Any],
                                   feedback: Any, iteration: int) -> Any:
        # Тип отчёта — контракт агентов, а не петли: `agents` здесь Any,
        # и сужать его тут значило бы объявить сузившееся знание, которого
        # у делегата нет (feedback на деле словарь находок, не строка).
        return _rc_implement_with_quota_wait(self, task, feedback, iteration)

    @staticmethod
    def _reviewers_disagreed(history: list[dict[str, Any]]
                             ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        return _rc_reviewers_disagreed(history)

    @staticmethod
    def _arm(row: dict[str, Any]) -> str:
        return _rc_arm(row)

    @staticmethod
    def _diagnose(outcome: str, history: list[dict[str, Any]],
                  scope_failures: list[list[str]] | None = None,
                  sig_failures: list[list[str]] | None = None,
                  exec_failures: list[dict[str, Any]] | None = None) -> str:
        return _rc_diagnose(outcome, history, scope_failures, sig_failures,
                            exec_failures)

    def _escalate_futile(self, task: dict[str, Any],
                         history: list[dict[str, Any]],
                         futile: list[dict[str, Any]],
                         scope_failures: list[list[str]],
                         sig_failures: list[list[str]],
                         exec_failures: list[dict[str, Any]],
                         iteration: int) -> str:
        """Потолок бесплодных раундов: эскалация с ПРИЧИНОЙ.

        Отдельный исход от «раунды исправлений исчерпаны» именно потому,
        что исправлений не было: работу ни разу не судили. Диагноз здесь
        всегда механический — futile-раунд по построению знает свою
        причину, и `_diagnose` выбирает из них точную.
        """
        tid = task["id"]
        causes = sorted({str(f.get("cause")) for f in futile})
        stash = self.cleanup(task, "futile-rounds")
        diagnosis = self._diagnose(ESCALATE_MAX, history,
                                   scope_failures=scope_failures,
                                   sig_failures=sig_failures,
                                   exec_failures=exec_failures)
        diagnosis = (f"{len(futile)} раунд(ов) сорвались, не дойдя до "
                     f"суждения о работе ({', '.join(causes)}); лимит "
                     f"исправлений не тронут. " + diagnosis)
        self.state.log("futile_exhausted", task=tid, rounds=len(futile),
                       causes=causes, stash=stash)
        qid = self.state.ask(tid, ESCALATE_MAX, diagnosis, stash=stash,
                             round=iteration, history=history)
        self.state.set_status(tid, "blocked", reason="futile_rounds",
                              stash=stash, iterations=iteration,
                              exit_code=EXIT_ESCALATE, diagnosis=diagnosis,
                              question_id=qid)
        self.ui(f"    эскалация [{qid}]: {diagnosis}")
        return "blocked"

    # --- цикл --------------------------------------------------------------

    def run_task(self, task: dict[str, Any]) -> str:
        tid = task["id"]
        self.ui(f"=== {tid}: {task['title']}")
        self.state.set_status(tid, "in_progress")
        # Всё, что уже лежало в дереве, работой агента не является и
        # откату не подлежит: иначе незакоммиченная работа человека
        # уничтожается безвозвратно при первом же нарушении границ.
        self.pre_existing = set(self.state.changed_files())
        if self.pre_existing:
            # Факт в журнал: страж границ эти файлы дальше не видит, и
            # оператор обязан знать, что задача пошла поверх его правок.
            self.state.log("pre_existing_dirt", task=tid,
                           files=sorted(self.pre_existing))
        self.head_before = self.sh(["git", "rev-parse", "HEAD"]).stdout.strip()
        self.state_before = self.state_fingerprint()

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
        # ЧЕСТНОСТЬ БЮДЖЕТА РАУНДОВ (золотой набор: диагнозы «задача слишком
        # крупная» — 0 из 6 верных). Лимит исправлений тратит только раунд,
        # в котором работа ДОШЛА ДО МЕХАНИЧЕСКОГО СУЖДЕНИЯ: гейт вынес
        # приговор или ревьюер вынес вердикт. Раунд, сорвавшийся раньше —
        # исполнитель умер, работу снёс страж границ, сигнатуры контракта
        # разъехались, — попыткой не был: судить было нечего. Пока такие
        # раунды тратили лимит, петля объявляла «раунды исчерпаны» и
        # называла размер задачи причиной того, что случилось до первой
        # оценки: у s2ky это были таймаут, откат по границам и авария
        # kimi, у k3ad — три отката подряд, ни одного ревью.
        productive = 0
        futile: list[dict[str, Any]] = []
        exec_failures: list[dict[str, Any]] = []
        # Бесплодные раунды не бесконечны: свой потолок, своя эскалация —
        # с ПРИЧИНОЙ, а не с догадкой о размере. Умолчание тянется за
        # max_iterations: терпение — одна ручка, и оператор, поднявший
        # лимит исправлений, ждёт большей терпимости и к срывам среды.
        max_futile = int(self.config.get("max_futile_rounds",
                                         max(4, self.max_iter)))
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
        while productive < self.max_iter + confirm_rounds:
            if len(futile) >= max_futile:
                # Бесплодные раунды исчерпаны. Это НЕ «раунды исправлений
                # кончились»: до исправлений дело не дошло ни разу, и
                # эскалация обязана называть то, обо что раунды сгорели.
                return self._escalate_futile(task, history, futile,
                                             scope_failures, sig_failures,
                                             exec_failures, iteration)
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
                report = self._implement_with_quota_wait(task, feedback,
                                                         iteration)
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
                    # Работы не было — судить нечего: лимит исправлений не
                    # тратится, но факт идёт в диагноз и в свой потолок.
                    reason = str(fail.get("reason") or "invalid")
                    exec_failures.append({"round": iteration, "reason": reason,
                                          "wall_s": fail.get("wall_s")})
                    futile.append({"round": iteration,
                                   "cause": "executor_failed",
                                   "detail": reason})
                    self.state.log("round_futile", task=tid, round=iteration,
                                   cause="executor_failed", detail=reason,
                                   futile=len(futile), of=max_futile)
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
                    # id вопроса сам по себе ничего не говорит о задаче:
                    # оператор видел «спор исполнителя [q011]» в выводе и
                    # шёл читать сырой JSON, чтобы понять, о чём вообще
                    # спор. Полный текст — в state.ask, здесь только то,
                    # что должно быть видно не открывая инбокс отдельно.
                    self.ui(f"    спор исполнителя [{qid}]")
                    self.ui(f"        {question[:160]}")
                    self.ui("        детали и ответ: swarm inbox")
                    return "blocked"

            ok, tail = self.gate(task)
            if not ok:
                # В журнал, а не только в метрики: без этого swarm why и
                # report показывали пустоту, и разбор «почему сгорели
                # раунды» шёл через метрики вручную (пилот, k3ad).
                self.state.log("gate_failed", task=tid, round=iteration,
                               tail=(tail or "")[-300:])
                # Гейт — механическое суждение о РЕАЛЬНОЙ работе: попытка
                # состоялась и провалилась. Раунд потрачен честно.
                productive += 1
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
                # Работа снесена откатом — ревьюер её не увидит. Судить
                # нечего, лимит исправлений не тратится (k3ad: три таких
                # раунда подряд обвинили размер задачи).
                futile.append({"round": iteration, "cause": "scope_violation",
                               "detail": ", ".join(sorted(set(bad))[:3])})
                self.state.log("round_futile", task=tid, round=iteration,
                               cause="scope_violation",
                               detail=", ".join(sorted(set(bad))[:3]),
                               futile=len(futile), of=max_futile)
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
                    # Контракт скелета нарушен — до ревью работа не дошла.
                    futile.append({"round": iteration,
                                   "cause": "signature_violation",
                                   "detail": ", ".join(changed[:3])})
                    self.state.log("round_futile", task=tid, round=iteration,
                                   cause="signature_violation",
                                   detail=", ".join(changed[:3]),
                                   futile=len(futile), of=max_futile)
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

            # Вердикт получен — работа СУДИМА, раунд потрачен по делу.
            productive += 1
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
            # Счётчик БЮДЖЕТА, а не номер раунда: `decide` решает «раунды
            # исчерпаны», и считать в этом решении сорванные до суждения
            # раунды значит эскалировать за то, чего не судили. Номер
            # раунда (iteration) остаётся сквозным — по нему журнал, имена
            # файлов вердиктов и история.
            outcome, code = decide(productive, verdict, history,
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
                                     head=self.head_before) as step:
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
                # Та же причина, что у спора исполнителя выше: счётчик
                # находок в одной строке не заменяет саму формулировку,
                # ради которой петля позвала человека.
                self.ui(f"    вопрос человеку [{qid}]: {len(intent)} находок "
                        f"о замысле (механических: {len(mechanical)})")
                self.ui(f"        {question[:160]}")
                self.ui("        детали и ответ: swarm inbox")
                return "ask_user"

            if outcome in (ESCALATE_MAX, ESCALATE_NONCONV):
                stash = self.cleanup(task, outcome.replace("_", "-"))
                # Все три вида улик, а не только история вердиктов: без них
                # эскалация посреди цикла знала меньше, чем эскалация по
                # выходу из него, и выдавала догадку там, где рядом лежал
                # механический факт.
                diagnosis = self._diagnose(outcome, history,
                                           scope_failures=scope_failures,
                                           sig_failures=sig_failures,
                                           exec_failures=exec_failures)
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
                                   sig_failures=sig_failures,
                                   exec_failures=exec_failures)
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
        # Операторская грязь фиксируется на уровне ПРОГОНА, не только
        # задачи: pre_existing пересобирается каждым run_task и обнуляется
        # перезапуском процесса, а цикл stash/restore успевает показать
        # стражу чистое дерево. Снимок здесь — тот же список, что печатает
        # preflight, — переживает всё это и вычитается из суждений стража
        # и revert наравне с pre_existing (PILOT-1, scope-guard).
        self.run_dirt = set(self.state.changed_files())
        if self.run_dirt:
            self.state.log("run_dirt", files=sorted(self.run_dirt))
        # Счётчик автовозобновлений — на ПРОГОН, не на задачу: квота
        # общая, и каждая пауза тратит одну попытку из quota_resume_max.
        resumes = 0
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
                    # ровно тот дефект, который эта ветка чинит. Проверка
                    # по имени класса: исключение могло подняться из чужой
                    # копии модуля.
                    self.refresh_board()
                    wait_s = self._quota_resume_wait(str(e), resumes)
                    if wait_s is None:
                        # Дефолт прежний: наверх, к exit-коду 4 — «когда
                        # продолжить» решает человек.
                        raise
                    resumes += 1
                    cap = int(self.config.get("quota_resume_max", 3))
                    until = (dt.datetime.now(dt.UTC)
                             + dt.timedelta(seconds=wait_s)).astimezone()
                    self.state.log("quota_resume", attempt=resumes, of=cap,
                                   wait_s=wait_s,
                                   until=until.isoformat(timespec="seconds"),
                                   message=str(e)[:200])
                    self.ui(f"    АВТОВОЗОБНОВЛЕНИЕ {resumes}/{cap}: ждём "
                            f"{wait_s // 60} мин (до {until:%H:%M}), "
                            f"причина: {str(e)[:120]}")
                    time.sleep(wait_s)
                    continue
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

    def _quota_resume_wait(self, message: str, resumes: int) -> int | None:
        """Сколько спать перед автовозобновлением; None = отдать человеку.

        Строго opt-in (`quota_resume = "auto"`): пауза по квоте остаётся
        дефолтом — автопродолжение тратит деньги без человека в контуре,
        такое включают явно. Ограничения, каждое отдаёт решение человеку,
        а не молча ждёт: исчерпан лимит попыток на прогон
        (`quota_resume_max`); названный сброс дальше потолка ожидания
        (`quota_resume_max_wait_s` — завтрашняя квота это решение о
        деньгах и приоритетах, не таймер). Время сброса не разобрано —
        ждём `quota_resume_fallback_s`: короткая слепая пауза дешевле
        потерянного вечера, а лимит попыток не даст ей зациклиться.

        Родилось из замера PILOT-1/speed-analysis: календарное время
        прогона определяли не вычисления, а паузы по квоте плюс задержка
        человека на перезапуск (медиана 0.4 ч, худшее 21 ч).
        """
        if str(self.config.get("quota_resume") or "off") != "auto":
            return None
        if resumes >= int(self.config.get("quota_resume_max", 3)):
            return None
        reset = parse_quota_reset(message)
        if reset is None:
            wait = int(self.config.get("quota_resume_fallback_s", 3600))
        else:
            now = dt.datetime.now(dt.UTC)
            # +90 с запаса: граница сброса и рассинхрон часов; попытка
            # ровно в названную минуту снова упирается в лимит.
            wait = int((reset - now).total_seconds()) + 90
        if wait > int(self.config.get("quota_resume_max_wait_s", 21600)):
            return None
        return max(wait, 60)

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
