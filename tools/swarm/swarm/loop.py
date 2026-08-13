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
import json
import pathlib
import subprocess
import time

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
}
QUOTA_MARKERS = ("session limit", "rate limit", "quota", "usage limit",
                 "429", "too many requests")


class QuotaExceeded(Exception):
    """Провайдер отказал по квоте: петля ждёт, а не блокирует задачу (§5.3)."""


class Escalation(Exception):
    """Ситуация, которую обязан разобрать человек (§1.1)."""


def quota_error(envelope):
    if not isinstance(envelope, dict) or not envelope.get("is_error"):
        return None
    text = str(envelope.get("result") or "").lower()
    return str(envelope.get("result"))[:200] if any(
        m in text for m in QUOTA_MARKERS) else None


def validate_verdict(v):
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


def apply_policies(findings, policies):
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


def classify_findings(findings):
    """Разделить находки на задевающие замысел и механические.

    Смысл различения: «переименуй переменную» исполнитель чинит сам, а
    «эта абстракция лишняя» или «работа вышла за границы задачи» —
    решение о замысле, и правильный ответ знает только человек.
    """
    intent, mechanical = [], []
    for f in findings or []:
        marked = f.get("intent")
        is_intent = (marked if isinstance(marked, bool)
                     else f.get("category") in INTENT_CATEGORIES)
        (intent if is_intent else mechanical).append(f)
    return intent, mechanical


def decide(round_no, verdict, history, max_rounds=MAX_ITER, confirmations=2):
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
        approvals = sum(1 for r in history if r.get("verdict") == "approve") + 1
        return (DONE, EXIT_OK) if approvals >= confirmations else (CONFIRM, EXIT_WORKING)

    if verdict["verdict"] == "blocked":
        return ESCALATE_MAX, EXIT_ESCALATE

    if round_no >= max_rounds:
        return ESCALATE_MAX, EXIT_ESCALATE

    findings = verdict.get("findings") or []
    prev = history[-1] if history else None
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
    def __init__(self, state, config, agents, ui=None):
        self.state = state
        self.config = config
        self.agents = agents          # объект с .implement() и .review()
        self.ui = ui or (lambda *a, **k: None)
        self._pre_existing = set()    # дерево до старта задачи
        self._head_before = None      # история до старта задачи
        self._state_before = None     # состояние петли до старта задачи

    # --- механические шаги ------------------------------------------------

    def _sh(self, cmd, timeout=900):
        return subprocess.run(cmd, capture_output=True, text=True,
                              cwd=self.state.root, timeout=timeout)

    @property
    def max_iter(self):
        """Лимит раундов исправлений. Три хватало на стенде; на реальном
        проекте задача может честно требовать больше, и упираться в
        зашитую константу — значит эскалировать по чужой причине."""
        return int(self.config.get("max_iterations", MAX_ITER))

    def gate(self, task):
        cmd = self.config.get("gate_command") or [
            "python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
        # Холодная сборка Rust не влезает в 900 с, а таймаут здесь —
        # исключение, топившее задачу в in_progress (до §5.3-починки).
        r = self._sh(cmd, timeout=int(self.config.get("gate_timeout", 900)))
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-3:])
        ok = r.returncode == 0
        self.state.metric(task=task["id"], phase="gate", ok=ok, tail=tail[:300])
        return ok, tail

    def integrity_check(self):
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
            bad.append(f"история изменена: HEAD {self._head_before[:8]} -> "
                       f"{head[:8]} (коммитит только оркестратор)")
        state_now = self._state_fingerprint()
        if self._state_before and state_now != self._state_before:
            bad.append("состояние петли (.swarm/) изменено агентом")
        for marker, what in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                             ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick")):
            if (pathlib.Path(self.state.root) / ".git" / marker).exists():
                bad.append(f"репозиторий оставлен в состоянии {what}")
        return bad

    def _state_fingerprint(self):
        path = getattr(self.state, "tasks_path", None)
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def scope_check(self, task):
        """Границы задачи: разрешённые пути и неприкосновенность тестов.

        Тонкость, стоившая трёх итераций на приёмке: у задач типа
        `feature-tests` исполнитель ОБЯЗАН писать свои тесты, и файлы,
        явно перечисленные в `paths`, защищёнными не считаются. Защита
        нужна от правки ЧУЖИХ тестов, а не собственных.
        """
        allowed = task.get("paths") or []
        protected = self.config.get("protected_paths") or ["tests/*", "tests/**"]
        writes_own_tests = task.get("type") in ("test-task", "feature-tests")
        bad, touched_tests = [], []
        for path in self.state.changed_files():
            explicitly_allowed = any(fnmatch.fnmatch(path, p) for p in allowed)
            if not explicitly_allowed:
                bad.append(path)
                continue
            if not writes_own_tests and any(
                    fnmatch.fnmatch(path, p) for p in protected):
                touched_tests.append(path)
        ok = not bad and not touched_tests
        self.state.metric(task=task["id"], phase="scope", ok=ok,
                          unexpected=bad, protected=touched_tests)
        return ok, bad, touched_tests

    def revert(self, task):
        """Откат работы агента, включая СОЗДАННЫЕ файлы.

        `git checkout -- .` возвращает отслеживаемые файлы, но untracked
        не трогает: нарушитель оставался на диске и повторно ловился
        каждый раунд, делая нарушение границ неустранимым. Откатываем
        поимённо то, что реально изменено, — по всему дереву не метём,
        чтобы не задеть чужое.
        """
        untouchable = getattr(self, "_pre_existing", set())
        changed = [p for p in self.state.changed_files() if p not in untouchable]
        if not changed:
            return []
        tracked, untracked = [], []
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

    def commit(self, task, iteration):
        """Коммит принятой итерации; пустой diff — не ошибка (§4.4)."""
        env = dict(GIT_AUTHOR_NAME="swarm-executor",
                   GIT_AUTHOR_EMAIL="executor@swarm.local",
                   GIT_COMMITTER_NAME="swarm-orchestrator",
                   GIT_COMMITTER_EMAIL="orchestrator@swarm.local")
        subprocess.run(["git", "add", "-A"], cwd=self.state.root, check=True)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                cwd=self.state.root)
        if staged.returncode == 0:
            return None
        import os
        message = self.agents.commit_message(task, self._sh(["git", "diff",
                                                             "--cached"]).stdout)
        subprocess.run(["git", "commit", "-qm", message], cwd=self.state.root,
                       check=True, env=dict(os.environ, **env))
        # Коммит оркестратора легален: сдвигаем базу, иначе следующая
        # проверка целостности обвинит агента в нашей же работе.
        self._head_before = self._sh(["git", "rev-parse", "HEAD"]).stdout.strip()
        return self._sh(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()

    def cleanup(self, task, reason):
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

    def _apply_patch(self, diff_text):
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
                           input=diff_text, text=True, capture_output=True)
        if r.returncode != 0:
            self.state.log("restore_failed", stderr=(r.stderr or "").strip()[:300])
        return r.returncode == 0

    @staticmethod
    def _diagnose(outcome, history):
        """Несходимость требует ДИАГНОЗА, а не очередного повтора.

        Закрытый список гипотез (FuguNano): человеку эскалируется не голый
        факт «три раунда подряд», а версия о причине.
        """
        if outcome == ESCALATE_MAX:
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

    def run_task(self, task):
        tid = task["id"]
        self.ui(f"=== {tid}: {task['title']}")
        self.state.set_status(tid, "in_progress")
        # Всё, что уже лежало в дереве, работой агента не является и
        # откату не подлежит: иначе незакоммиченная работа человека
        # уничтожается безвозвратно при первом же нарушении границ.
        self._pre_existing = set(self.state.changed_files())
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
        history = []
        best = {"findings": None, "round": None, "diff": None}
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
        while iteration < self.max_iter + confirm_rounds:
            iteration += 1
            # В подтверждающем раунде исполнитель не вызывается: гейт и
            # границы перепроверяются (дёшево), ревью идёт по тому же диффу.
            was_confirmation = confirming
            if confirming:
                confirming = False
            else:
                report = self.agents.implement(task, feedback, iteration)
                if report is None:
                    feedback = {"note": "предыдущий ответ не содержал валидного "
                                        "JSON-отчёта — повтори, соблюдая контракт"}
                    continue
                if report.get("status") == "dispute":
                    stash = self.cleanup(task, "dispute")
                    qid = self.state.ask(tid, "dispute",
                                         report.get("summary", "спор исполнителя"),
                                         stash=stash, round=iteration,
                                         dispute=report.get("dispute"))
                    self.state.set_status(tid, "blocked", reason="dispute",
                                          stash=stash, iterations=iteration,
                                          question_id=qid)
                    self.ui(f"    спор исполнителя [{qid}]")
                    return "blocked"

            ok, tail = self.gate(task)
            if not ok:
                feedback = {"note": "gate провален", "tests": tail}
                continue

            violations = self.integrity_check()
            if violations:
                self.state.log("integrity_violation", task=tid, round=iteration,
                               violations=violations)
                self.state.metric(task=tid, iter=iteration, phase="integrity",
                                  ok=False, violations=violations)
                stash = self.cleanup(task, "integrity")
                qid = self.state.ask(tid, ASK_USER,
                                     "исполнитель вышел за границы доверия: "
                                     + "; ".join(violations), stash=stash)
                self.state.set_status(tid, "blocked", reason="integrity",
                                      stash=stash, iterations=iteration,
                                      question_id=qid)
                self.ui(f"    НАРУШЕНИЕ ДОВЕРИЯ [{qid}]: {violations[0]}")
                return "blocked"

            sok, bad, tests_touched = self.scope_check(task)
            if not sok:
                self.revert(task)
                feedback = {"note": "нарушение границ задачи",
                            "unexpected_files": bad,
                            "protected_tests": tests_touched}
                continue

            verdict = self.agents.review(task, tail, iteration)
            if verdict is None:
                # Работа могла быть готовой и зелёной — сорвалось РЕВЬЮ.
                # Молчаливый blocked оставлял оператора без единого слова
                # о том, что произошло: инбокс пуст, журнал пуст, статус
                # «invalid_verdict». Диагноз обязателен.
                stash = self.cleanup(task, "invalid-verdict")
                why = getattr(self.agents, "last_review_failure", None)
                diagnosis = REVIEW_DIAGNOSIS.get(
                    why, REVIEW_DIAGNOSIS["invalid"])
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
            outcome, code = decide(iteration, verdict, history,
                                   max_rounds=self.max_iter,
                                   confirmations=self.config.get("confirmations", 2))
            history.append({"round": iteration, "verdict": verdict["verdict"],
                            "findings": len(findings),
                            "categories": sorted({f.get("category")
                                                  for f in findings})})
            self.state.log("round", task=tid, round=iteration,
                           verdict=verdict["verdict"], outcome=outcome,
                           findings=len(findings), intent=len(intent))

            # keep-best: раунд, где находок меньше всего, запоминается —
            # если следующий окажется хуже, откатываемся к лучшему, чтобы
            # цикл не деградировал (приём FuguNano).
            if best["findings"] is None or len(findings) < best["findings"]:
                best.update(findings=len(findings), round=iteration,
                            diff=self.state.work_diff())
            elif (len(findings) > best["findings"] and best["diff"]
                    and not was_confirmation):
                # Подтверждающий раунд ревьюет ТОТ ЖЕ дифф: исполнитель в нём
                # не вызывался, кода никто не трогал. Рост числа находок там
                # — разброс ревьюера, а не регресс. На PILOT-1 (g2pf) это
                # дало ложный «регресс 2 против 1» и совершенно ненужный
                # цикл revert + git apply поверх неизменного дерева.
                self.ui(f"    регресс: {len(findings)} находок против "
                        f"{best['findings']} в раунде {best['round']} — откат")
                self.revert(task)
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

            if outcome in (DONE, CONFIRM):
                if outcome == CONFIRM:
                    confirm_rounds += 1
                    confirming = True
                    self.ui(f"    approve #{iteration}, нужно ещё подтверждение "
                            f"(повторное ревью того же диффа, без исполнителя)")
                    continue
                with self.state.step(tid, "commit") as step:
                    sha = self.commit(task, iteration)
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

            feedback = {"findings": mechanical or findings}
            if human:
                feedback.update(human)
            self.ui(f"    request_changes ({len(findings)} замечаний, "
                    f"механических {len(mechanical)})")

        # Выход из цикла по исчерпанию раундов — тоже терминальный исход, и
        # он обязан попасть в инбокс. Иначе задача, где исполнитель раз за
        # разом не возвращал валидный отчёт, блокируется МОЛЧА и человек о
        # ней не узнаёт (поймано на приёмке: v3st исчезла из виду).
        stash = self.cleanup(task, "max-iterations")
        diagnosis = self._diagnose(ESCALATE_MAX, history)
        qid = self.state.ask(tid, ESCALATE_MAX, diagnosis, stash=stash,
                             round=self.max_iter, history=history)
        self.state.set_status(tid, "blocked", reason="max_iterations",
                              stash=stash, iterations=self.max_iter,
                              exit_code=EXIT_ESCALATE, diagnosis=diagnosis,
                              question_id=qid)
        self.ui(f"    эскалация [{qid}]: {diagnosis}")
        return "blocked"

    def run(self, limit=None):
        results = {}
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
            except KeyboardInterrupt:
                self._rescue(task, "прервано человеком")
                raise
            except Exception as e:                          # noqa: BLE001
                results[task["id"]] = self._rescue(task, f"{type(e).__name__}: {e}")
                break
            if limit and len(results) >= limit:
                break
        return results

    def _rescue(self, task, reason):
        """Спасти задачу и работу после аварии: stash, blocked, вопрос."""
        tid = task["id"]
        self.state.log("task_crashed", task=tid, reason=reason)
        try:
            stash = self.cleanup(task, "crash")
        except Exception:                                   # noqa: BLE001
            stash = None
        try:
            qid = self.state.ask(tid, ASK_USER,
                                 f"прогон прерван аварией: {reason}", stash=stash)
            self.state.set_status(tid, "blocked", reason="crash",
                                  stash=stash, question_id=qid)
        except Exception:                                   # noqa: BLE001
            pass
        self.ui(f"    АВАРИЯ на {tid}: {reason}")
        return "blocked"
