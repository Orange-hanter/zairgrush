"""Вызовы дорогих ролей: исполнитель и ревьюер (§4, §6.0).

Собрано из прототипов: извлечение отчёта из последнего assistant-события
(урок SMOKE-1), схема вердикта через `--json-schema`, детект отказа по
квоте, карта символов в handoff по условию (ADR-006), хелпер
коммит-сообщений (§7.2).
"""
import ast
import hashlib
import importlib.util
import json
import pathlib
import random
import sys
import time
from types import ModuleType
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
SCHEMAS = HERE.parent / "schemas"

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import parsing as parsing_mod  # noqa: E402
import promptbuilder  # noqa: E402


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"не удалось загрузить модуль {name}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


log = _load("obs").get_logger("agents")

# Парсинг ответов вынесен в parsing.py; здесь shim для потребителей.
condense_diff = parsing_mod.condense_diff
_extract_fenced_code = parsing_mod.extract_fenced_code


class Agents:
    # Классовые дефолты кэшей: часть тестов собирает Agents через
    # __new__ без __init__, и метод, споткнувшийся об отсутствующий
    # атрибут, ломал бы им ревью из-за опционального слоя памяти.
    memory_cache: tuple[str, str] | None = None
    norms_cache: tuple[str, str] | None = None

    def __init__(self, state: Any, config: dict[str, Any]) -> None:
        self.state = state
        self.config = config
        self.driver = _load("driver")
        self.loop_mod = _load("loop")
        self._helpers: ModuleType | None = None
        self.codemap: ModuleType | None = None
        self.map_cache: tuple[tuple[str, int], str] | None = None
        # Блоки памяти считаются один раз на ЗАДАЧУ: между раундами они
        # обязаны быть байт-стабильны, иначе каждый раунд переписывает
        # префикс-кэш промпта (§8 — экономика порядка блоков).
        self.memory_cache: tuple[str, str] | None = None
        self.norms_cache: tuple[str, str] | None = None
        # Почему ревью не состоялось: оркестратору нужен диагноз, а не
        # голое None. «Кончился бюджет» и «модель ответила мусором» —
        # разные болезни с разным лечением.
        self.last_review_failure: str | None = None
        # Почему не состоялся ПОСЛЕДНИЙ вызов исполнителя: причина, время,
        # события, stderr. None = вызов удался.
        self.last_implement_failure: dict[str, Any] | None = None
        # Жребий для пулов моделей и усилий. Отдельный экземпляр, а не
        # глобальный random: тесты подменяют его сидом, не трогая
        # состояние процесса.
        self.rng = random.Random(config.get("tuning_seed"))  # noqa: S311 — жребий руки замера, не ключи
        self.last_tuning: dict[str, Any] = {}

    # --- контекст ---------------------------------------------------------

    def repo_map(self, task: dict[str, Any]) -> str | None:
        """Делегат к `promptbuilder.repo_map`."""
        return promptbuilder.repo_map(self, task)

    def _tree_fingerprint(self) -> str:
        """Делегат к `promptbuilder.tree_fingerprint`."""
        return promptbuilder.tree_fingerprint(self)

    # --- исполнитель ------------------------------------------------------

    def memory_block(self, task: dict[str, Any]) -> str:
        """Делегат к `promptbuilder.memory_block`."""
        return promptbuilder.memory_block(self, task)

    def norms_for(self, task: dict[str, Any]) -> str:
        """Делегат к `promptbuilder.norms_for`."""
        return promptbuilder.norms_for(self, task)

    def handoff(self, task: dict[str, Any], feedback: str | None,
                repo_map: str | None, memory: str | None = None) -> str:
        """Делегат к `promptbuilder.handoff`."""
        return promptbuilder.handoff(self, task, feedback, repo_map, memory)

    def _skeleton_on(self) -> bool:
        """E10 за флагом (§1, 06-док): опечатка в имени эксперимента не
        обязана включать умолчание — незнакомый ключ гейт уже подсвечивает
        в cli.load_config, здесь просто явный дефолт "выключено"."""
        return bool((self.config.get("experiments") or {}).get("skeleton", False))

    def implement(self, task: dict[str, Any], feedback: str | None,
                  iteration: int) -> dict[str, Any] | None:
        # Поле задачи читается ТОЛЬКО за флагом: с flag=off implement()
        # обязан остаться байт-в-байт сегодняшним (E10, замер против
        # исходного поведения).
        exec_model = task.get("executor_model") if self._skeleton_on() else None
        if isinstance(exec_model, str) and exec_model.startswith("ollama:"):
            return self._implement_fill(task, feedback, iteration)
        prompt = self.handoff(task, feedback, self.repo_map(task),
                              memory=self.memory_block(task) or None)
        cmd = ["kimi", "-p", prompt, "--output-format", "stream-json"]
        model = self.config.get("executor_model")
        if isinstance(exec_model, str) and exec_model:
            # Задача переопределяет модель прогона — точечный выбор
            # исполнителя дороже/дешевле общего умолчания на конкретную
            # работу (E10), а не смена умолчания для всей очереди.
            model = exec_model
        if model:
            cmd[1:1] = ["-m", model]
        drv = self.driver.AgentDriver(
            cwd=str(self.state.root),
            silence_timeout=self.config.get("silence_timeout", 600),
            wall_clock_cap=self.config.get("wall_clock_cap", 1800))
        run = drv.start(cmd)
        result = run.collect(self._extract_report)
        raw = self.state.dir / "log" / f"{task['id']}-i{iteration}-executor.jsonl"
        raw.write_text(run.raw_stream())
        self.state.metric(task=task["id"], iter=iteration, phase="implement",
                          reason=result.reason, wall_s=round(result.wall_s, 1),
                          report=bool(result.report), events=result.events)
        report: dict[str, Any] | None = result.report
        if report is None or result.reason != "done":
            # stderr — единственное место, где провайдер объясняет отказ.
            # Пока он не сохранялся, диагноз «квота Kimi исчерпана» занял
            # шесть запросов вместо чтения одной строки журнала: три
            # мгновенные аварии подряд выглядели как «нет отчёта».
            stderr = run.stderr_tail(400).strip()
            self.last_implement_failure = {
                "reason": result.reason, "wall_s": result.wall_s,
                "events": result.events, "stderr": stderr}
            self.state.log("executor_failed", task=task["id"], round=iteration,
                           reason=result.reason, wall_s=round(result.wall_s, 1),
                           events=result.events, stderr=stderr)
        else:
            self.last_implement_failure = None
        return report

    def _fill_target(self, task: dict[str, Any]) -> pathlib.Path | None:
        """Единственный конкретный существующий файл задачи, либо None.

        Ответ chat-fill — весь файл в ОДНОМ fence: формат не умеет назвать,
        к какому из нескольких файлов относится код. Контракт ломается уже
        на втором пути или на маске, а не на исполнении.
        """
        paths = task.get("paths") or []
        if len(paths) != 1 or not isinstance(paths[0], str):
            return None
        rel = paths[0]
        if any(ch in rel for ch in "*?[]"):
            return None
        full = self.state.root / rel
        return full if full.is_file() else None

    def _fill_failure(self, task_id: str, iteration: int, wall_s: float,
                      model: str, reason: str, detail: str) -> None:
        """Общий выход провала chat-fill: метрика + диагноз, НИКОГДА raise.

        Причина в last_implement_failure — то же место, что и у обычного
        исполнителя: петля различает провалы одинаково, независимо от
        того, каким путём implement() до них дошёл (§7.3, тот же принцип
        fail-open, что и у хелперов третьего контура)."""
        self.state.metric(task=task_id, iter=iteration, phase="implement",
                          reason=reason, wall_s=wall_s, report=False,
                          fill=True, model=model)
        self.last_implement_failure = {"reason": reason, "detail": detail}
        return

    def _implement_fill(self, task: dict[str, Any], feedback: str | None,
                        iteration: int) -> dict[str, Any] | None:
        """E10: заполнение контракта дешёвой чат-моделью (`ollama:` префикс).

        Формат вывода дешёвых чат-моделей на Ollama Cloud снят на пробе
        (2026-08-18): один ```python fence без посторонней прозы, сигнатуры
        целы, обрезание ловится по `done_reason`. Контракт держится не на
        CLI-конверте (его тут нет), а на механическом разборе ответа —
        отсюда строгость формы.
        """
        task_id = str(task.get("id"))
        target = self._fill_target(task)
        if target is None:
            self.last_implement_failure = {
                "reason": "fill_misconfigured",
                "detail": f"paths обязан быть ровно один конкретный "
                          f"существующий файл: {task.get('paths')!r}"}
            return None
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as e:
            self.last_implement_failure = {"reason": "fill_misconfigured",
                                           "detail": str(e)}
            return None
        acc = "\n".join("- " + a for a in task.get("acceptance") or [])
        fb = ""
        if feedback:
            fb = ("\n## Feedback — you must address it\n"
                  + json.dumps(feedback, ensure_ascii=False, indent=1) + "\n")
        prompt = f"""You are filling in a contracted stub file. Your reply is \
parsed by machine.

## Task
{task_id}: {task.get('spec') or task.get('title', '')}

Acceptance:
{acc}
{fb}
## Current file ({target.relative_to(self.state.root)})
```python
{content}
```

## Output contract
Return the COMPLETE file in ONE fenced block ```python ...```; do not change
any signature or docstring of existing defs; no elision; no text outside the
fence.
"""
        model = str(task.get("executor_model")).removeprefix("ollama:")
        if self._helpers is None:
            try:
                self._helpers = _load("helpers")
            except Exception:
                log.warning("хелперы недоступны для chat-fill", exc_info=True,
                            extra={"swarm_task": task_id})
                self._fill_failure(task_id, iteration, 0.0, model,
                                   "fill_no_reply", "модуль хелперов не загружен")
                return None
            self._helpers.configure(self.state.dir / "helper-metrics.jsonl")
        max_tokens = self.config.get("fill_num_predict", 8000)
        t0 = time.time()
        try:
            reply = self._helpers.ollama_chat(prompt, "fill",
                                              max_tokens=max_tokens, model=model)
        except Exception:
            # ollama_chat сам fail-open (§7.3) и не должен бросать, но
            # chat-fill обязан пережить и дефект самого хелпера — это его
            # СОБСТВЕННОЕ обещание "никогда не роняет петлю", не только
            # обещание вызываемого модуля.
            log.warning("chat-fill упал при вызове модели", exc_info=True,
                        extra={"swarm_task": task_id})
            reply = None
        wall_s = round(time.time() - t0, 1)
        if not reply:
            self._fill_failure(task_id, iteration, wall_s, model,
                               "fill_no_reply", "модель не ответила")
            return None
        code = parsing_mod.extract_fenced_code(reply)
        if code is None:
            self._fill_failure(task_id, iteration, wall_s, model, "fill_no_fence",
                               "ответ не прошёл контракт: не один чистый fence")
            return None
        if target.suffix == ".py":
            try:
                ast.parse(code)
            except SyntaxError as e:
                self._fill_failure(task_id, iteration, wall_s, model,
                                   "fill_syntax", str(e))
                return None
        try:
            target.write_text(code, encoding="utf-8")
            log_path = self.state.dir / "log" / f"{task_id}-i{iteration}-fill.txt"
            log_path.write_text(reply, encoding="utf-8")
        except OSError as e:
            self._fill_failure(task_id, iteration, wall_s, model,
                               "fill_misconfigured", str(e))
            return None
        self.state.metric(task=task_id, iter=iteration, phase="implement",
                          reason="done", wall_s=wall_s, report=True, fill=True,
                          model=model)
        self.last_implement_failure = None
        return {"status": "done", "summary": "заполнение по контракту применено",
               "fill": True}

    @staticmethod
    def _report_in(text: str) -> dict[str, Any] | None:
        """Делегат к `parsing.report_in`."""
        return parsing_mod.report_in(text)

    @classmethod
    def _extract_report(cls, stream: str) -> dict[str, Any] | None:
        """Делегат к `parsing.extract_report`."""
        return parsing_mod.extract_report(stream)

    # --- ревьюер ----------------------------------------------------------

    def _review_prompt_parts(self, task: dict[str, Any], gate_tail: str, diff: str,
                             want_verification: bool = False,
                             verify_results: list[dict[str, Any]] | None = None,
                             memory: str = "", lens: str = "",
                             ) -> tuple[str, str, str]:
        """Делегат к `promptbuilder.review_prompt_parts`."""
        return promptbuilder.review_prompt_parts(
            task, gate_tail, diff, want_verification, verify_results,
            memory, lens)

    def review_prompt(self, task: dict[str, Any], gate_tail: str, diff: str,
                      want_verification: bool = False,
                      verify_results: list[dict[str, Any]] | None = None,
                      memory: str = "", lens: str = "") -> str:
        """Делегат к `promptbuilder.review_prompt`."""
        return promptbuilder.review_prompt(
            task, gate_tail, diff, want_verification,
            verify_results, memory, lens)

    def work_diff(self) -> str:
        """То, что ревьюер обязан увидеть, — включая созданные файлы."""
        diff: str = self.state.work_diff()
        return diff

    def _wants_verification(self, task: dict[str, Any]) -> bool:
        """ADR-005: механизм выборочный, а не постоянный.

        Платить +51 % за подтверждение того, что и так подтверждается,
        смысла нет. Включаем там, где цена ошибки выше цены проверки.
        """
        mode = self.config.get("verification", "milestone")
        if mode in (True, "always"):
            return True
        if mode in (False, "never", None):
            return False
        return bool(task.get("milestone_close") or task.get("verify"))

    def _tuning(self, prefix: str, confirming: bool = False) -> list[str]:
        """Делегат к `promptbuilder.tuning`."""
        return promptbuilder.tuning(self, prefix, confirming)

    def _draw(self, key: str, confirming: bool) -> Any:
        """Делегат к `promptbuilder.draw`."""
        return promptbuilder.draw(self, key, confirming)

    def review(self, task: dict[str, Any], gate_tail: str, iteration: int,
               attempt: int = 1,
               verify_results: list[dict[str, Any]] | None = None,
               confirming: bool = False) -> dict[str, Any] | None:
        # Голый `git diff` не показывает созданные файлы: ревьюер получал
        # пустоту и мог одобрить её, а `git add -A` вносил непроверенное
        # в историю. Единый источник — state.work_diff (intent-to-add).
        diff = parsing_mod.condense_diff(self.work_diff())
        schema = (SCHEMAS / "verdict-v1.schema.json").read_text()
        # Самый дорогой вызов системы шёл в обход слоя живости: голый
        # subprocess.run с жёстким таймаутом, который не отличал «думает»
        # от «завис», а по истечении ронял исключением всю очередь.
        # Теперь ревьюер идёт через тот же драйвер, что и исполнитель:
        # stream-json как признак жизни, heartbeat по тишине, жёсткий
        # потолок стены времени — и любой исход возвращается результатом,
        # а не исключением. Конверт (`--output-format json` целиком) лежит
        # в финальном result-событии потока.
        # Линза — свойство ПОДТВЕРЖДАЮЩЕГО раунда (E10, confirm_lens), тем же
        # правилом, что confirm_model/confirm_effort в _tuning: обычный
        # проход о ней не знает, иначе A/B по флагу меряет не то.
        lens = str(self.config.get("confirm_lens", "")) if confirming else ""
        rules_part, task_part, tail_part = self._review_prompt_parts(
            task, gate_tail, diff,
            want_verification=(verify_results is None
                               and self._wants_verification(task)),
            verify_results=verify_results,
            memory=self.norms_for(task), lens=lens)
        # sha1[:12] стабильных частей — не для секретности, а как отпечаток
        # (Feature 4): мутация «неизменного» блока меняет rules_sha ровно
        # так же, как мутация кода меняет sha256 в condense_diff — метрика
        # ловит расхождение между тем, что промпт ОБЯЗАН быть, и тем, чем
        # он стал, без повторного чтения текста промпта глазами.
        rules_sha = hashlib.sha1(rules_part.encode(),
                                 usedforsecurity=False).hexdigest()[:12]
        task_sha = hashlib.sha1(task_part.encode(),
                                usedforsecurity=False).hexdigest()[:12]
        cmd = ["claude", "-p", rules_part + task_part + tail_part,
               "--output-format", "stream-json", "--verbose",
               "--include-partial-messages",
               "--json-schema", schema,
               "--allowedTools", "Read,Grep,Glob,Bash(git diff:*)",
               "--max-budget-usd", str(self.config.get("review_budget_usd", 1.0)),
               *self._tuning("review", confirming)]
        drv = self.driver.AgentDriver(
            cwd=str(self.state.root),
            silence_timeout=self.config.get("silence_timeout", 600),
            wall_clock_cap=self.config.get("wall_clock_cap", 1800))
        run = drv.start(cmd, parser=self.driver.parse_claude)
        result = run.collect(self.driver.extract_result_envelope)
        env: dict[str, Any] | None = result.report
        # Фаза входит в имя: второй проход (после верификации) писал в тот
        # же файл и затирал первый вердикт — на пилоте так потерялся
        # валидный approve за $1.22, и разбираться было не по чему.
        phase = "v" if verify_results is not None else "a"
        stem = f"{task['id']}-i{iteration}-{phase}{attempt}"
        # Конверт — под прежним именем (его читают доска и `swarm why`);
        # полный поток — рядом, под именем, которое их глобы не ловят.
        (self.state.dir / "log" / f"{stem}-review-stream.jsonl").write_text(
            run.raw_stream())
        (self.state.dir / "log" / f"{stem}-review.json").write_text(
            json.dumps(env, ensure_ascii=False) if env is not None
            else run.raw_stream())
        verdict: dict[str, Any] | None = None
        cost: float | None = None
        terminal: str | None = None
        if env is not None:
            quota = self.loop_mod.quota_error(env)
            if quota:
                self.state.metric(task=task["id"], phase="review",
                                  quota_wait=True, provider_message=quota)
                raise self.loop_mod.QuotaExceededError(quota)
            verdict = env.get("structured_output")
            cost = env.get("total_cost_usd")
            terminal = env.get("terminal_reason")
        valid = self.loop_mod.validate_verdict(verdict)
        # usage — конверт Claude как есть: поле отсутствует на любом исходе
        # без успешного result-события, и это НЕ то же самое, что нулевые
        # токены — журнал читается как данные (§9.3), отсюда None, не 0.
        usage: dict[str, Any] = (env or {}).get("usage") or {}
        # Выбор руки — часть замера, а не деталь запуска: жребий, не
        # попавший в журнал, делает прогон невоспроизводимым шумом.
        self.state.metric(task=task["id"], iter=iteration, phase="review",
                          attempt=attempt, dur_s=round(result.wall_s, 1),
                          run_reason=result.reason,
                          cost_usd=cost, verdict=(verdict or {}).get("verdict"),
                          findings=len((verdict or {}).get("findings", [])),
                          valid=valid, terminal_reason=terminal,
                          confirming=confirming, rules_sha=rules_sha,
                          task_sha=task_sha,
                          cache_read=usage.get("cache_read_input_tokens"),
                          cache_write=usage.get("cache_creation_input_tokens"),
                          tokens_in=usage.get("input_tokens"),
                          tokens_out=usage.get("output_tokens"),
                          **self.last_tuning)
        if not valid and terminal == "budget_exhausted":
            # Повтор обречён: тот же промпт кончится на том же месте.
            # На пилоте вторая попытка стоила ещё $3.23 и дала то же
            # самое. Детерминированный отказ ретраить нельзя — эскалируем.
            self.last_review_failure = "budget_exhausted"
            self.state.log("review_budget_exhausted", task=task["id"],
                           round=iteration, attempt=attempt, cost_usd=cost,
                           limit=self.config.get("review_budget_usd", 1.0))
            return None
        if not valid and attempt == 1:
            return self.review(task, gate_tail, iteration, attempt=2,
                               verify_results=verify_results,
                               confirming=confirming)
        if not valid or verdict is None:
            # Диагноз — причина, а не факт: «убит по тишине» и «ответ не
            # прошёл схему» лечатся по-разному, и оператору отдаётся то,
            # что драйвер знает о прогоне (silence, wall_clock, crash,
            # no_report), а не общий ярлык.
            self.last_review_failure = (result.reason
                                        if result.reason != "done" else "invalid")
            return None
        self.last_review_failure = None
        # Второй вызов с результатами — ровно один раунд на итерацию
        # (анти-петля): иначе ревьюер может запрашивать проверки бесконечно.
        requests = verdict.get("verification_requests")
        # Исполняем ТОЛЬКО когда механизм включён для этой задачи: ревьюер
        # может прислать запросы и без спроса, а каждый такой раунд удваивает
        # стоимость ревью (ADR-005: +51 %). На пилоте это случилось на первой
        # же задаче — верификация запустилась при verification="milestone" и
        # задаче без milestone_close.
        if (requests and verify_results is None
                and self._wants_verification(task)):
            vf = _load("verify")
            try:
                before: set[str] | None = set(
                    vf.worktree_dirty(self.state.root))
            except OSError:
                log.exception("состояние дерева до проверок не определено")
                before = None
            results = vf.run_requests(requests, self.state.root)
            # Сравниваем ДО и ПОСЛЕ: список изменённых файлов сам по себе
            # всегда содержит работу исполнителя и ничего не говорит о том,
            # напортили ли проверки.
            # `None` здесь означает «не смогли посмотреть», и это НЕ то же
            # самое, что «ничего не тронуто»: пустой список сказал бы
            # ревьюеру и журналу неправду о свойстве §5.1.
            touched: list[str] | None = None
            if before is not None:
                try:
                    touched = sorted(
                        set(vf.worktree_dirty(self.state.root)) - before)
                except OSError:
                    log.exception("состояние дерева после проверок "
                                  "не определено")
            self.state.log("verification", task=task["id"], round=iteration,
                           requested=len(requests), executed=len(results),
                           touched_by_checks=touched)
            self.state.metric(task=task["id"], iter=iteration,
                              phase="verification", requested=len(requests),
                              rejected=sum(1 for r in results
                                           if r.get("status") == "rejected"))
            confirmed = self.review(task, gate_tail, iteration,
                                    verify_results=vf.format_results(results),
                                    confirming=confirming)
            # Верификация — УЛУЧШЕНИЕ вердикта, а не условие его силы. Если
            # второй проход не удался (бюджет, квота, невалидный ответ),
            # возвращаем первый — он был полноценным и за него уплачено.
            # Иначе задача с валидным approve уходит в blocked из-за сбоя
            # необязательного шага (поймано на первой задаче пилота).
            if confirmed is None:
                self.state.log("verification_inconclusive", task=task["id"],
                               round=iteration,
                               kept="первый вердикт: повторный проход не удался")
                return verdict
            return confirmed
        return verdict

    # --- хелперы ----------------------------------------------------------

    def commit_message(self, task: dict[str, Any], diff: str) -> str:
        # Хелпер дешёвый, но не бесплатный, и 336 КБ эталона ему так же
        # нечего читать, как и ревьюеру.
        diff = parsing_mod.condense_diff(diff)
        fallback = f"{task['id']}: {task['title']}"
        if self._helpers is None:
            try:
                self._helpers = _load("helpers")
            except Exception:
                log.warning("хелперы недоступны, сообщение коммита по шаблону",
                            exc_info=True)
                return fallback
            # Метрики хелперов — факт ПРОГОНА и живут в .swarm стенда.
            # Без настройки они оседали в каталоге пакета: прогоны разных
            # проектов смешивались в один файл в исходниках инструмента.
            self._helpers.configure(self.state.dir / "helper-metrics.jsonl")
        message, source = self._helpers.commit_message(task, diff, fallback)
        self.state.log("commit_message", task=task["id"], source=source)
        return str(message)
