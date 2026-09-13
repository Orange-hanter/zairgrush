"""Вызов ревьюера: промпт, раунды, разбор вердикта, проверка исполнением.
Вынесено из agents.py: функции получают объект Agents первым аргументом
(AgentsLike), класс держит делегаты — точки вызова не изменились.

Журнальная заметка о границах пишется ОДИН раз на (задача, текст):
повтор раундов не дублирует строку, смена диффа — да.
"""
import hashlib
import json
import pathlib
import sys
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
SCHEMAS = HERE.parent / "schemas"

_HERE = str(HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import boundarynote  # noqa: E402
import modlock  # noqa: E402
import parsing as parsing_mod  # noqa: E402
import pathsafe  # noqa: E402
import promptbuilder  # noqa: E402
import spending  # noqa: E402
from agents_types import AgentsLike  # noqa: E402

# Имя логера оставлено "agents": журнал наблюдаемости — контракт,
# переименование модуля не должно менять имена потоков.
log = modlock.load_module("obs").get_logger("agents")

_NOTED: set[tuple[str, str]] = set()
_NOTED_CAP = 10_000


def _boundary_note_for(agents: AgentsLike, task: dict[str, Any],
                       diff: str, iteration: int) -> str | None:
    """Предревью-заметка о границах: собрать, журналануть, fail-open.

    Разбор диффа и сверка границ — детерминированный код, но любая его
    ошибка обязана стоить НОЛЬ: вызов ревьюера не откладывается из-за
    заметки (§7.3 — fail-open на API каждого помощника). Сбой ловится,
    пишется warning, ревьюер идёт без заметки, как шло до REV-005.
    """
    try:
        files = boundarynote.diff_files(diff)
        note = boundarynote.boundary_note(
            task, files, agents.config.get("protected_paths"))
    except Exception:
        log.warning("предревью-заметка о границах не собрана", exc_info=True)
        return None
    if note and (task["id"], note) not in _NOTED:
        if len(_NOTED) >= _NOTED_CAP:
            _NOTED.clear()
        _NOTED.add((task["id"], note))
        agents.state.log("boundary_note", task=task["id"],
                         round=iteration, note=note[:300])
    return note



def _severity_counts(verdict: dict[str, Any] | None) -> dict[str, int]:
    """Находки по тяжести — разложение, без которого счёт находок нем.

    Строка метрик несла одно число `findings`, и «мажоров на доллар» из
    неё не считался: три minor и три blocker выглядели одинаково. Порог
    эскалации руки и цена принятой задачи опираются на тяжесть, а не на
    количество, — значит тяжесть обязана быть в журнале, а не только в
    вердикте.

    Журнал читается как ДАННЫЕ (§9.3): находка не той формы (не словарь,
    severity вне enum) не роняет замер и не подменяет собой другую
    тяжесть — она просто не попадает ни в один счётчик. Поэтому сумма
    трёх полей может быть меньше `findings`, и это честнее, чем ноль на
    всей строке из-за одной кривой записи.
    """
    counts = {"blockers": 0, "majors": 0, "minors": 0}
    key = {"blocker": "blockers", "major": "majors", "minor": "minors"}
    findings = (verdict or {}).get("findings")
    if not isinstance(findings, list):
        return counts
    for item in findings:
        if isinstance(item, dict):
            name = key.get(str(item.get("severity")))
            if name:
                counts[name] += 1
    return counts


def review(agents: AgentsLike, task: dict[str, Any], gate_tail: str, iteration: int,
           attempt: int = 1,
           verify_results: list[dict[str, Any]] | None = None,
           confirming: bool = False,
           retry_note: str = "") -> dict[str, Any] | None:
    # Голый `git diff` не показывает созданные файлы: ревьюер получал
    # пустоту и мог одобрить её, а `git add -A` вносил непроверенное
    # в историю. Единый источник — state.work_diff (intent-to-add).
    raw_diff = agents.work_diff()
    diff = parsing_mod.condense_diff(raw_diff)
    diff_files, diff_lines = parsing_mod.diff_size(raw_diff)
    # Предревью-заметка о границах (REV-005, P4): та же геометрия, что у
    # стража gitops.scope_check, но со стороны рецензента и до вызова.
    # 7/7 споров пилота удовлетворены — постановка, а не код; ревьюер,
    # увидевший расслоение «paths ↔ дифф» до вызова, не сжигает раунд
    # на спор о границах. Fail-open: на чистом диффе note is None и
    # промпт не меняется ни на байт (параметр по умолчанию пуст).
    note = _boundary_note_for(agents, task, diff, iteration)
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
    lens = str(agents.config.get("confirm_lens", "")) if confirming else ""
    rules_part, task_part, tail_part = promptbuilder.review_prompt_parts(
        task, gate_tail, diff,
        want_verification=(verify_results is None
                           and wants_verification(agents, task)),
        verify_results=verify_results,
        memory=promptbuilder.norms_for(agents, task), lens=lens,
        retry_note=retry_note, boundary_note=note or "")
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
           # Потолок вызова — политика денег, а не деталь argv ревьюера.
           # Умолчание $1 жило здесь и срабатывало: «ревьюер обрублен по
           # бюджету» — штатный диагноз петли. В режиме money_bin флага
           # нет вовсе, явно заданный — действует (см. spending.py).
           *spending.budget_flags(agents.config, "review_budget_usd"),
           *promptbuilder.tuning(agents, "review", confirming)]
    drv = agents.driver.AgentDriver(
        cwd=str(agents.state.root),
        silence_timeout=agents.config.get("silence_timeout", 600),
        wall_clock_cap=agents.config.get("wall_clock_cap", 1800))
    # Страж потолка прогона ДО диспетча: ревьюер — самый дорогой вызов
    # хода, и до стража он уходил даже с уже пробитым бюджетом.
    spending.guard(agents.config, agents.state, "review",
                   "review_budget_usd")
    run = drv.start(cmd, parser=agents.driver.parse_claude)
    result = run.collect(agents.driver.extract_result_envelope)
    env: dict[str, Any] | None = result.report
    # Фаза входит в имя: второй проход (после верификации) писал в тот
    # же файл и затирал первый вердикт — на пилоте так потерялся
    # валидный approve за $1.22, и разбираться было не по чему.
    phase = "v" if verify_results is not None else "a"
    # id задачи — данные недоверенные: в stem он идёт только через
    # санитайзер, иначе '../..' в id уводил запись лога за пределы
    # каталога состояния (тот же вектор, что закрыт в tester/unclear).
    # Для обычных id (aaaa, s1ch) санитайзер тождественен — доска и
    # `swarm why` находят файлы по прежнему glob.
    stem = f"{pathsafe.safe_filename(task['id'])}-i{iteration}-{phase}{attempt}"
    # Конверт — под прежним именем (его читают доска и `swarm why`);
    # полный поток — рядом, под именем, которое их глобы не ловят.
    # Поток — УЛИКА, и она не имеет права затираться. Задача, возвращённая
    # в очередь (`swarm retry`), начинает нумерацию раундов заново и берёт
    # тот же stem: на E13 повтор стёр поток единственного отказа, который
    # и надо было разбирать. Конверт остаётся под прежним именем (его
    # читают доска и `swarm why`), а поток уходит в свободное имя рядом —
    # глоб `*-review-stream.jsonl` ловит их все.
    stream_rel = f"log/{stem}-review-stream.jsonl"
    if pathsafe.escapes_root(agents.state.dir, stream_rel):
        # Симлинк log/ или state.dir наружу: угроза — не авария роли.
        # Улика не пишется, но вердикт разбирается как обычно.
        log.warning(
            "поток ревью не записан: путь выходит за пределы каталога",
            extra={"swarm_task": str(task.get("id"))},
        )
    else:
        free_path(agents.state.dir / "log",
                  f"{stem}-review-stream", ".jsonl").write_text(
            run.raw_stream(), encoding="utf-8")
    # Тот же containment для конверта: симлинк log/ увёл бы его наружу
    # вместе с потоком — писать безусловно значило бы закрыть один
    # вектор и оставить соседний на том же каталоге.
    env_rel = f"log/{stem}-review.json"
    if pathsafe.escapes_root(agents.state.dir, env_rel):
        log.warning(
            "конверт ревью не записан: путь выходит за пределы каталога",
            extra={"swarm_task": str(task.get("id"))},
        )
    else:
        (agents.state.dir / env_rel).write_text(
            json.dumps(env, ensure_ascii=False) if env is not None
            else run.raw_stream(), encoding="utf-8")
    verdict: dict[str, Any] | None = None
    cost: float | None = None
    terminal: str | None = None
    if env is not None:
        quota = agents.loop_mod.quota_error(env)
        if quota:
            agents.state.metric(task=task["id"], phase="review",
                              quota_wait=True, provider_message=quota)
            raise agents.loop_mod.QuotaExceededError(quota)
        verdict = env.get("structured_output")
        cost = env.get("total_cost_usd")
        terminal = env.get("terminal_reason")
    salvaged = False
    if not isinstance(verdict, dict):
        verdict = salvage(agents, task, iteration, run.raw_stream())
        salvaged = verdict is not None
    valid = agents.loop_mod.validate_verdict(verdict)
    # usage — конверт Claude как есть: поле отсутствует на любом исходе
    # без успешного result-события, и это НЕ то же самое, что нулевые
    # токены — журнал читается как данные (§9.3), отсюда None, не 0.
    usage: dict[str, Any] = (env or {}).get("usage") or {}
    # Выбор руки — часть замера, а не деталь запуска: жребий, не
    # попавший в журнал, делает прогон невоспроизводимым шумом.
    agents.state.metric(task=task["id"], iter=iteration, phase="review",
                      attempt=attempt, dur_s=round(result.wall_s, 1),
                      run_reason=result.reason,
                      cost_usd=cost, verdict=(verdict or {}).get("verdict"),
                      findings=len((verdict or {}).get("findings", [])),
                      **_severity_counts(verdict),
                      diff_files=diff_files, diff_lines=diff_lines,
                      valid=valid, terminal_reason=terminal,
                      salvaged=salvaged or None,
                      confirming=confirming, rules_sha=rules_sha,
                      task_sha=task_sha,
                      cache_read=usage.get("cache_read_input_tokens"),
                      cache_write=usage.get("cache_creation_input_tokens"),
                      tokens_in=usage.get("input_tokens"),
                      tokens_out=usage.get("output_tokens"),
                      **agents.last_tuning)
    # Цена записана: повтор (attempt=2) или второй проход верификации
    # ниже не имеют права уйти, если потолок прогона уже пробит.
    spending.guard(agents.config, agents.state, "review")
    if not valid and terminal == "budget_exhausted":
        # Повтор обречён: тот же промпт кончится на том же месте.
        # На пилоте вторая попытка стоила ещё $3.23 и дала то же
        # самое. Детерминированный отказ ретраить нельзя — эскалируем.
        agents.last_review_failure = "budget_exhausted"
        agents.state.log("review_budget_exhausted", task=task["id"],
                       round=iteration, attempt=attempt, cost_usd=cost,
                       limit=agents.config.get("review_budget_usd", 1.0))
        return None
    if not valid and attempt == 1:
        # Повтор с ТОЙ ЖЕ причиной, что назвал отказ: прежде он уходил
        # с байт-в-байт прежним промптом, и модель второй раз угадывала,
        # чего от неё хотят. На E13 два отказа из пяти были заглушкой
        # (`analysis: "Test"`), а три — вердиктом, сложенным в одно поле.
        problem = agents.loop_mod.verdict_problem(verdict) if verdict is not None \
            else "структурный вывод не заполнен: полей вердикта нет вовсе"
        return review(agents, task, gate_tail, iteration, attempt=2,
                           verify_results=verify_results,
                           confirming=confirming,
                           retry_note=str(problem))
    if not valid or verdict is None:
        # Диагноз — причина, а не факт: «убит по тишине» и «ответ не
        # прошёл схему» лечатся по-разному, и оператору отдаётся то,
        # что драйвер знает о прогоне (silence, wall_clock, crash,
        # no_report), а не общий ярлык.
        agents.last_review_failure = (result.reason
                                    if result.reason != "done" else "invalid")
        return None
    agents.last_review_failure = None
    # Второй вызов с результатами — ровно один раунд на итерацию
    # (анти-петля): иначе ревьюер может запрашивать проверки бесконечно.
    requests = verdict.get("verification_requests")
    # Исполняем ТОЛЬКО когда механизм включён для этой задачи: ревьюер
    # может прислать запросы и без спроса, а каждый такой раунд удваивает
    # стоимость ревью (ADR-005: +51 %). На пилоте это случилось на первой
    # же задаче — верификация запустилась при verification="milestone" и
    # задаче без milestone_close.
    if (requests and verify_results is None
            and wants_verification(agents, task)):
        vf = modlock.load_module("verify")
        try:
            before: set[str] | None = set(
                vf.worktree_dirty(agents.state.root))
        except OSError:
            log.exception("состояние дерева до проверок не определено")
            before = None
        results = vf.run_requests(requests, agents.state.root)
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
                    set(vf.worktree_dirty(agents.state.root)) - before)
            except OSError:
                log.exception("состояние дерева после проверок "
                              "не определено")
        agents.state.log("verification", task=task["id"], round=iteration,
                       requested=len(requests), executed=len(results),
                       touched_by_checks=touched)
        agents.state.metric(task=task["id"], iter=iteration,
                          phase="verification", requested=len(requests),
                          rejected=sum(1 for r in results
                                       if r.get("status") == "rejected"))
        confirmed = review(agents, task, gate_tail, iteration,
                                verify_results=vf.format_results(results),
                                confirming=confirming)
        # Верификация — УЛУЧШЕНИЕ вердикта, а не условие его силы. Если
        # второй проход не удался (бюджет, квота, невалидный ответ),
        # возвращаем первый — он был полноценным и за него уплачено.
        # Иначе задача с валидным approve уходит в blocked из-за сбоя
        # необязательного шага (поймано на первой задаче пилота).
        if confirmed is None:
            agents.state.log("verification_inconclusive", task=task["id"],
                           round=iteration,
                           kept="первый вердикт: повторный проход не удался")
            return verdict
        return confirmed
    return verdict


def free_path(directory: pathlib.Path, stem: str, suffix: str) -> pathlib.Path:
    """Свободное имя рядом: `stem.suffix`, затем `stem-2.suffix` и далее.

    Улику не затирают. Второй прогон той же задачи после `swarm retry`
    берёт тот же stem, и без этого поток первого — единственная запись
    того, что там произошло, — исчезает вместе с разбором.
    """
    candidate = directory / f"{stem}{suffix}"
    n = 1
    while candidate.exists():
        n += 1
        candidate = directory / f"{stem}-{n}{suffix}"
    return candidate


def salvage(agents: AgentsLike, task: dict[str, Any], iteration: int,
            stream: str) -> dict[str, Any] | None:
    """Вердикт из потока, когда конверт пуст: работа сделана и оплачена.

    Замеренный на E13 отказ (3 вызова из 17): модель кладёт весь вердикт
    в поле `analysis`, размечая остальные поля тегами; провайдер такой
    вызов отклоняет и исчерпывает свои ретраи, конверт приходит пустым —
    а суждение лежит в потоке целиком. Выбрасывать его значит платить
    дважды и блокировать задачу за чужую ошибку формы.

    Восстановленное НЕ получает поблажек: `validate_verdict` дальше по
    коду проверяет его наравне со всеми, и негодное будет отвергнуто
    ровно так же. Спасение видно в журнале и в метрике — вердикт,
    добытый из потока, обязан быть отличим от пришедшего конвертом.
    """
    payload = agents.driver.last_structured_output(stream)
    if payload is None:
        return None
    fixed = parsing_mod.repair_verdict(payload)
    verdict = fixed if fixed is not None else payload
    if not isinstance(verdict, dict) or not verdict.get("verdict"):
        return None
    agents.state.log("verdict_salvaged", task=task["id"], round=iteration,
                   repaired=fixed is not None,
                   verdict=verdict.get("verdict"),
                   findings=len(verdict.get("findings") or []))
    return verdict


def wants_verification(agents: AgentsLike, task: dict[str, Any]) -> bool:
    """ADR-005: механизм выборочный, а не постоянный.

    Платить +51 % за подтверждение того, что и так подтверждается,
    смысла нет. Включаем там, где цена ошибки выше цены проверки.
    """
    mode = agents.config.get("verification", "milestone")
    if mode in (True, "always"):
        return True
    if mode in (False, "never", None):
        return False
    return bool(task.get("milestone_close") or task.get("verify"))
