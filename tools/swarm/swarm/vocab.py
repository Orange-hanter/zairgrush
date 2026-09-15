"""Единый словарь петли: одна запись журнала — одна фраза.

Словари перевода жили внутри доски, и `swarm report` до них не доставал:
одно и то же событие называлось «нарушение доверия» в браузере и
`integrity_violation` в терминале. Два имени у одного факта — это два
разных знания у человека, который читает то одно, то другое.

Правило, ради которого модуль устроен именно так: **фраза не имеет права
терять поля**. Каждый шаблон объявляет, какие поля он назвал; всё
остальное из записи дописывается хвостом `ключ=значение`. Без этого
правила проза строго ХУЖЕ дампа: дамп безобразен, но полон, а гладкая
фраза молча съедает поле, которого её автор не предвидел, — и человек
делает вывод по неполной записи, не зная об этом. Новое поле в журнале
не требует правки словаря: оно появится в хвосте само.

Не попадают во фразу только поля рамки (`FRAME`): они есть у каждой
строки и печатаются не фразой, а разметкой вокруг неё — время колонкой,
задача заголовком.
"""
import json
from collections.abc import Callable
from typing import Any

# --- имена состояний и разборов ------------------------------------------

PHASE_RU = {
    "gate": "гейт", "scope": "границы", "implement": "исполнитель",
    "review": "ревьюер", "verification": "проверки", "policy": "политики",
    "integrity": "целостность",
}
STATUS_RU = {
    "pending": "в очереди", "in_progress": "в работе", "in_review": "на ревью",
    "done": "закрыта", "blocked": "заблокирована",
}
SEVERITY_RU = {"blocker": "блокер", "major": "важное", "minor": "мелочь"}
CATEGORY_RU = {
    "correctness": "корректность", "tests": "тесты", "style": "стиль",
    "scope": "границы", "architecture": "архитектура",
}
KIND_RU = {
    "question": "вопрос человеку", "answer": "ответ человека",
    "round": "раунд", "step_intent": "шаг начат", "step_done": "шаг завершён",
    "step_failed": "шаг провален", "policy_suppressed": "подавлено политикой",
    "policy": "политика заведена", "policy_dropped": "политика снята",
    "verification": "проверки исполнением",
    "verification_inconclusive": "проверки не дали результата",
    "integrity_violation": "нарушение доверия", "task_crashed": "авария",
    "budget_exhausted": "бюджет исчерпан", "baseline_red": "красный baseline",
    "review_failed": "ревью не состоялось",
    "review_budget_exhausted": "ревьюер обрублен по бюджету",
    "plan_applied": "план применён", "plan_failed": "планирование не удалось",
    "paths_extended": "границы расширены", "retry": "возврат в очередь",
    "stash_failed": "работа не спрятана",
    "restore_failed": "восстановление не удалось",
    "commit_message": "сообщение коммита",
    "preflight_forced": "запуск на грязном дереве",
    "task_accepted_by_operator": "принято оператором",
    "quota_wait": "ожидание квоты провайдера",
    "quota_pause": "пауза по квоте провайдера",
    "gate_failed": "гейт красный",
    "scope_violation": "нарушение границ",
    "signature_violation": "нарушение замороженных сигнатур",
    "deviations_declared": "исполнитель заявил отступления",
    "executor_unavailable": "исполнитель недоступен",
    "state_written": "состояние записано",
    "pre_existing_dirt": "чужая грязь в дереве",
    "run_dirt": "операторская грязь на прогон",
    "executor_failed": "вызов исполнителя не состоялся",
    "executor_denied": "исполнителю отказано в вызове инструмента",
    "verdict_salvaged": "вердикт добыт из потока",
    "tests_authored": "тесты написаны независимо",
    "tests_not_authored": "независимых тестов не получилось",
    "memory_written": "память пополнена",
    "memory_injected": "память подмешана в промпт",
    "memory_reflect": "память переосмыслена",
    "memory_unavailable": "хранилище памяти недоступно",
    "memory_forgotten": "урок забыт",
    "memory_synced": "индекс памяти досыпан",
    "quota_resume": "автовозобновление после квоты",
    "round_futile": "раунд сорвался до суждения",
    "futile_exhausted": "бесплодные раунды исчерпаны",
}
# Записи-бухгалтерия: сопровождают каждую запись состояния и событием
# прогона не являются. В блоках «прогон в целом» они хоронили под собой
# редкие и важные записи (бюджет, план) — поверхности вывода их фильтруют,
# сырьё (`--json`, журнал) остаётся полным.
BOOKKEEPING_KINDS = frozenset({"state_written"})
# Подпись к сумме потраченного — одна на все поверхности. Доска писала
# «ревьюер стоил», хотя в сумме сидел и планировщик: подпись врала ровно
# там, где человек сверяет счёт. Второй раз это случилось с движком
# исполнителя: на claude конверт отдаёт цену, она попадает в сумму, и
# перечисление «ревью+план» снова стало неправдой. Перечисление, которое
# зависит от конфига, — не подпись; поэтому здесь общее имя.
SPEND_LABEL = "дорогие роли"
# Вид вопроса в инбоксе — почему петля позвала человека.
QKIND_RU = {
    "intent": "находка о замысле", "dispute": "спор исполнителя",
    "ask_user": "требуется решение", "review_failed": "ревью не состоялось",
    "plan_failed": "планирование не удалось",
    "restore_failed": "откат не состоялся",
    "escalate_max": "раунды исчерпаны",
    "escalate_nonconvergent": "работа не сходится",
    "fp_promotion": "кандидат в политики",
}
# Причина блокировки, как она лежит в поле `reason` задачи. Формулировки
# длиннее одного слова намеренно: человек читает их в тот момент, когда
# прогон уже встал, и «invalid_verdict» ему в этот момент не помогает.
REASON_RU = {
    "baseline_red": "красный baseline — гейт падал ещё до работы агента",
    "dispute": "спор исполнителя — он считает задачу невыполнимой как поставлена",
    "integrity": "нарушена неприкосновенность истории или состояния петли",
    "invalid_verdict": "ревью не состоялось — вердикт не разобран",
    "ask_user": "находка о замысле — нужно ваше решение",
    "escalate_max": "раунды исчерпаны",
    "max_iterations": "раунды исчерпаны",
    "escalate_nonconvergent": "работа не сходится",
    "restore_failed": "откат к лучшему раунду не состоялся",
    "crash": "авария прогона",
}
# Исход раунда — что петля решила делать дальше.
OUTCOME_RU = {
    "done": "задача закрыта", "confirm": "нужно подтверждение",
    "continue": "ещё раунд исправлений", "ask_user": "вопрос человеку",
    "escalate_max": "раунды исчерпаны",
    "escalate_nonconvergent": "работа не сходится",
}

# Поля рамки: есть у каждой записи, показываются разметкой, а не фразой.
# swarm_sha здесь по той же причине, что и run_id: он одинаков у всех
# строк прогона и во фразе был бы шумом, а не фактом о событии.
FRAME = frozenset({"ts", "run_id", "kind", "task", "swarm_sha"})
MAX_VALUE = 200


def ru(table: dict[str, str], key: Any) -> str:
    """Перевод с честным запасным вариантом.

    Незнакомый код показывается как есть, а не заменяется прочерком:
    оператору нужно уметь найти его `grep`'ом по сырому журналу.
    """
    text = "" if key is None else str(key)
    return table.get(text, text)


def _s(value: Any, limit: int = MAX_VALUE) -> str:
    """Значение поля строкой: списки и словари — компактным JSON."""
    text = (json.dumps(value, ensure_ascii=False)
            if isinstance(value, list | dict) else str(value))
    return text[:limit] + ("…" if len(text) > limit else "")


def _seq(value: Any) -> list[Any]:
    """Поле-список как список, чем бы оно ни оказалось на самом деле.

    Журнал — данные, а не контракт: строку могла оставить прежняя версия
    оркестратора или правка руками. Проверка стоит здесь одна, а не в
    каждом шаблоне: иначе её забудет автор следующего, и `swarm report`
    упадёт ровно в тот момент, когда его открыли разбирать поломку.
    """
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    return [value]


# --- шаблоны фраз --------------------------------------------------------
#
# Каждый возвращает (фраза, названные поля). Второе — не украшение: по
# нему считается хвост, и забытое в наборе поле просто продублируется,
# а не исчезнет. Ошибка автора шаблона стоит шума, а не потери факта.

Narrator = Callable[[dict[str, Any]], tuple[str, set[str]]]


def _round(r: dict[str, Any]) -> tuple[str, set[str]]:
    bits = [f"раунд {r.get('round')} → {r.get('verdict')}"]
    findings = r.get("findings")
    if findings is not None:
        bits.append(f"находок {findings}" if findings else "находок нет")
    if r.get("intent"):
        bits.append(f"из них о замысле {r['intent']}")
    if r.get("outcome"):
        bits.append(ru(OUTCOME_RU, r["outcome"]))
    return ", ".join(bits), {"round", "verdict", "findings", "intent", "outcome"}


def _question(r: dict[str, Any]) -> tuple[str, set[str]]:
    head = f"вопрос {r.get('qid')} ({ru(QKIND_RU, r.get('qkind'))}): "
    return head + _s(r.get("question"), 300), {"qid", "qkind", "question"}


def _answer(r: dict[str, Any]) -> tuple[str, set[str]]:
    return (f"ответ на {r.get('qid')}: {_s(r.get('text'), 300)}",
            {"qid", "text"})


def _policy(r: dict[str, Any]) -> tuple[str, set[str]]:
    words = ", ".join(str(m) for m in _seq(r.get("match")))
    return (f"политика {r.get('pid')} заведена: {_s(r.get('text'))}"
            + (f" (слова: {words})" if words else ""),
            {"pid", "text", "match", "goal"})


def _policy_suppressed(r: dict[str, Any]) -> tuple[str, set[str]]:
    # `items` берётся как ДАННЫЕ, а не как контракт: запись мог оставить
    # прежний формат или правка руками, а отчёт читают именно тогда,
    # когда что-то уже сломано, — падать ему нельзя (тот же принцип, что
    # у доски: любое состояние `.swarm/` даёт страницу, а не traceback).
    items = _seq(r.get("items"))
    lead = (f"подавлено политикой: {r.get('count')} "
            f"замечани(й) в раунде {r.get('round')}")
    if items:
        issues = [_s(i.get("issue"), 90) if isinstance(i, dict) else _s(i, 90)
                  for i in items[:3]]
        lead += " — " + "; ".join(issues)
    return lead, {"count", "round", "items"}


def _step(verb: str) -> Narrator:
    def render(r: dict[str, Any]) -> tuple[str, set[str]]:
        # step_id не печатаем намеренно, и это не потеря: он собран из
        # `задача:действие:время`, а все три части уже стоят в строке —
        # задача в заголовке, действие во фразе, время в колонке.
        text = f"{verb} {r.get('action')}"
        if r.get("error"):
            text += f": {_s(r['error'])}"
        return text, {"action", "step_id", "error"}
    return render


def _plan_applied(r: dict[str, Any]) -> tuple[str, set[str]]:
    text = (f"план применён ({r.get('mode')}): операций {r.get('ops')}, "
            f"в очереди стало {r.get('tasks_after')}")
    return text, {"mode", "ops", "tasks_after"}


def _plan_failed(r: dict[str, Any]) -> tuple[str, set[str]]:
    errors = "; ".join(str(e) for e in _seq(r.get("errors")))
    return (f"планирование не удалось ({r.get('reason')}): {_s(errors, 300)}",
            {"mode", "reason", "errors"})


def _integrity(r: dict[str, Any]) -> tuple[str, set[str]]:
    # Формулировка нейтральна: проверка знает факт расхождения, но не автора.
    violations = "; ".join(str(v) for v in _seq(r.get("violations")))
    text = (f"нарушена неприкосновенность истории или состояния петли "
            f"(раунд {r.get('round')}): {violations}")
    return text, {"round", "violations"}


def _review_failed(r: dict[str, Any]) -> tuple[str, set[str]]:
    text = f"ревью не состоялось в раунде {r.get('round')}: {r.get('why')}"
    if r.get("gate_passed"):
        text += "; гейт при этом был зелёный — работа исполнителя цела"
    if r.get("stash"):
        text += f"; работа сохранена: {r['stash']}"
    return text, {"round", "why", "gate_passed", "stash"}


def _review_budget(r: dict[str, Any]) -> tuple[str, set[str]]:
    text = (f"вызов ревьюера обрублен по бюджету в раунде {r.get('round')}: "
            f"${r.get('cost_usd')} при потолке ${r.get('limit')} "
            f"(попытка {r.get('attempt')})")
    return text, {"round", "cost_usd", "limit", "attempt"}


def _budget_exhausted(r: dict[str, Any]) -> tuple[str, set[str]]:
    # Запись бывает двух форм: стоп ПЕРЕД задачей (stopped_before) и стоп
    # ПОСРЕДИ задачи (round). Фраза называет только то, что в записи есть.
    text = f"бюджет прогона исчерпан: ${r.get('spent')} из ${r.get('budget')}"
    if r.get("stopped_before"):
        text += f", остановлено перед задачей {r.get('stopped_before')}"
    elif r.get("round") is not None:
        text += f" посреди задачи (раунд {r.get('round')})"
    if r.get("stash"):
        text += f"; работа сохранена: {r['stash']}"
    return text, {"spent", "budget", "stopped_before", "round", "stash"}


def _quota_wait(r: dict[str, Any]) -> tuple[str, set[str]]:
    text = (f"провайдер отказал по квоте: {_s(r.get('message'))} — "
            f"ждём {r.get('wait_s')} с и повторяем")
    return text, {"message", "wait_s"}


def _quota_pause(r: dict[str, Any]) -> tuple[str, set[str]]:
    text = (f"квота не отпустила после ожиданий: {_s(r.get('message'))} — "
            f"прогон на паузе, задача возвращена в очередь")
    if r.get("stash"):
        text += f"; работа сохранена: {r['stash']}"
    return text, {"message", "stash"}


def _verification(r: dict[str, Any]) -> tuple[str, set[str]]:
    text = (f"проверки исполнением в раунде {r.get('round')}: "
            f"запрошено {r.get('requested')}, выполнено {r.get('executed')}")
    touched = _seq(r.get("touched_by_checks"))
    if touched:
        text += f"; проверки тронули дерево: {', '.join(map(str, touched))}"
    return text, {"round", "requested", "executed", "touched_by_checks"}


def _baseline_red(_r: dict[str, Any]) -> tuple[str, set[str]]:
    # Хвост гейта (`output`) намеренно назван, но не показан: это сотни
    # строк вывода тестов, место которым в `.swarm/log`, а не в строке.
    text = ("baseline красный до начала работы — задача не запускалась "
            "(это поломка репозитория, а не агента)")
    return text, {"output"}


def _crashed(r: dict[str, Any]) -> tuple[str, set[str]]:
    return f"АВАРИЯ: {_s(r.get('reason'), 300)}", {"reason"}


def _retry(r: dict[str, Any]) -> tuple[str, set[str]]:
    text = "возвращена в очередь оператором"
    if r.get("note"):
        text += f" с указанием: {_s(r['note'])}"
    added = _seq(r.get("added_paths"))
    if added:
        text += f"; границы расширены: {', '.join(map(str, added))}"
    return text, {"note", "added_paths"}


def _preflight(r: dict[str, Any]) -> tuple[str, set[str]]:
    dirty = _seq(r.get("dirty"))
    names = ", ".join(map(str, dirty[:5]))
    text = (f"запуск на грязном дереве по --force "
            f"({len(dirty)} файлов): {names}")
    return text, {"dirty"}


def _stash_failed(r: dict[str, Any]) -> tuple[str, set[str]]:
    text = (f"работу НЕ удалось спрятать ({r.get('reason')}): "
            f"{_s(r.get('stderr'))}")
    return text, {"reason", "stderr"}


def _verify_inconclusive(r: dict[str, Any]) -> tuple[str, set[str]]:
    text = (f"проверки исполнением не дали результата в раунде "
            f"{r.get('round')}: {r.get('kept')}")
    return text, {"round", "kept"}


def _paths_extended(r: dict[str, Any]) -> tuple[str, set[str]]:
    return (f"границы расширены: {r.get('path')}"
            + (f" (по {r['qid']})" if r.get("qid") else ""), {"path", "qid"})


def _gate_failed(r: dict[str, Any]) -> tuple[str, set[str]]:
    # Хвост вывода тестов уже обрезан писателем записи; здесь он нужен,
    # потому что именно по нему оператор понимает, ЧЕМ гейт красный.
    text = f"гейт красный в раунде {r.get('round')}"
    if r.get("tail"):
        text += f": {_s(r['tail'])}"
    return text, {"round", "tail"}


def _scope_violation(r: dict[str, Any]) -> tuple[str, set[str]]:
    bits = [f"границы задачи нарушены в раунде {r.get('round')}"]
    unexpected = _seq(r.get("unexpected"))
    if unexpected:
        bits.append(f"вне границ: {', '.join(map(str, unexpected))}")
    protected = _seq(r.get("protected"))
    if protected:
        bits.append(f"тронуты защищённые тесты: {', '.join(map(str, protected))}")
    return "; ".join(bits), {"round", "unexpected", "protected"}


def _signature_violation(r: dict[str, Any]) -> tuple[str, set[str]]:
    text = f"сигнатуры контракта изменены в раунде {r.get('round')}"
    changed = _seq(r.get("changed"))
    if changed:
        text += ": " + ", ".join(map(str, changed))
    return text, {"round", "changed"}


def _deviations_declared(r: dict[str, Any]) -> tuple[str, set[str]]:
    items = _seq(r.get("deviations"))
    text = f"исполнитель заявил отступления в раунде {r.get('round')}"
    if items:
        text += ": " + "; ".join(map(str, items))
    return text, {"round", "deviations"}


def _executor_unavailable(r: dict[str, Any]) -> tuple[str, set[str]]:
    return (f"исполнитель недоступен, прогон остановлен: {_s(r.get('stderr'))}",
            {"stderr"})


NARRATORS: dict[str, Narrator] = {
    "round": _round,
    "question": _question,
    "answer": _answer,
    "policy": _policy,
    "policy_dropped": lambda r: (f"политика {r.get('pid')} снята", {"pid"}),
    "policy_suppressed": _policy_suppressed,
    "step_intent": _step("начат шаг"),
    "step_done": _step("завершён шаг"),
    "step_failed": _step("ПРОВАЛЕН шаг"),
    "plan_applied": _plan_applied,
    "plan_failed": _plan_failed,
    "integrity_violation": _integrity,
    "review_failed": _review_failed,
    "review_budget_exhausted": _review_budget,
    "budget_exhausted": _budget_exhausted,
    "quota_wait": _quota_wait,
    "quota_pause": _quota_pause,
    "verification": _verification,
    "verification_inconclusive": _verify_inconclusive,
    "baseline_red": _baseline_red,
    "task_crashed": _crashed,
    "retry": _retry,
    "preflight_forced": _preflight,
    "paths_extended": _paths_extended,
    "commit_message": lambda r: (
        f"сообщение коммита сочинил {r.get('source')}", {"source"}),
    "stash_failed": _stash_failed,
    "restore_failed": lambda r: (
        f"откат к лучшему раунду не состоялся: {_s(r.get('stderr'))}",
        {"stderr"}),
    "gate_failed": _gate_failed,
    "scope_violation": _scope_violation,
    "signature_violation": _signature_violation,
    "deviations_declared": _deviations_declared,
    "executor_unavailable": _executor_unavailable,
    "state_written": lambda r: (
        f"состояние записано (отпечаток {r.get('sha')})", {"sha"}),
    # Запуск --force поверх незакоммиченных правок: страж границ эти файлы
    # дальше не судит, и оператор обязан видеть, ЧТО задача пошла поверх них.
    "pre_existing_dirt": lambda r: (
        "задача пошла поверх незакоммиченных правок (--force), страж границ "
        "их не судит: " + ", ".join(map(str, _seq(r.get("files")) or ["?"])),
        {"files"}),
    # Тот же смысл на уровне ПРОГОНА: снимок переживает перезапуски
    # процесса и циклы stash/restore, которые обнуляют _pre_existing.
    "run_dirt": lambda r: (
        "прогон пошёл поверх незакоммиченных правок, страж границ их "
        "не судит: " + ", ".join(map(str, _seq(r.get("files")) or ["?"])),
        {"files"}),
    # stderr — единственное место, где провайдер объясняет отказ: ради него
    # запись и заведена (диагноз «квота кончилась» стоил шести запросов).
    "executor_failed": lambda r: (
        (f"вызов исполнителя не состоялся в раунде {r.get('round')} "
         f"({_s(r.get('reason'))}): {_s(r.get('stderr')) or 'stderr пуст'}"),
        {"round", "reason", "stderr"}),
    # Вердикт, добытый из потока, обязан быть ОТЛИЧИМ от пришедшего
    # конвертом: это та же работа ревьюера, но полученная в обход
    # штатного канала, и молчать об этом нельзя — иначе поверхности
    # покажут обычное ревью там, где сработало спасение.
    "verdict_salvaged": lambda r: (
        (f"вердикт добыт из потока в раунде {r.get('round')}: "
         f"{_s(r.get('verdict'))}, находок {r.get('findings')}"
         + (" (поля разобраны из одного)" if r.get("repaired") else "")),
        {"round", "verdict", "findings", "repaired"}),
    # Триаж P1 (ADR-027): экономия бэнды меряется по этим записям —
    # молчащий триаж был бы одновременно невидимой экономией и
    # невидимым риском (пропущенное ревью, которого никто не видел).
    "band_hit": lambda r: (
        (f"триаж P1 в раунде {r.get('round')}: маршрут {_s(r.get('action'))} "
         f"({r.get('diff_files')} ф., {r.get('diff_lines')} строк)"
         + (f", рука {_s(r.get('model'))}" if r.get("model") else "")
         + (f" — {_s(r.get('reason'))}" if r.get("reason") else "")),
        {"round", "action", "model", "effort", "reason",
         "diff_files", "diff_lines", "docs_only"}),
    "guard_block": lambda r: (
        (f"триаж P1 остановлен стражем в раунде {r.get('round')} "
         f"({_s(r.get('reason'))}) — полное ревью "
         f"({r.get('diff_files')} ф., {r.get('diff_lines')} строк)"),
        {"round", "reason", "diff_files", "diff_lines"}),
    # NXT-012: гигант не дошёл НИ ДО ОДНОГО вызова ревью — молчащее
    # исключение было бы пропущенным ревью, которого никто не видел.
    "giant_excluded": lambda r: (
        (f"гигантский дифф исключён из ревью в раунде {r.get('round')} "
         f"(порог {_s(r.get('reason'))}: {r.get('diff_files')} ф., "
         f"{r.get('diff_lines')} строк, {r.get('diff_chars')} символов) — "
         f"ручное ревью владельца"),
        {"round", "reason", "diff_files", "diff_lines", "diff_chars"}),
    # Плечо B E11 видно построчно: кто написал тесты — вопрос замера, и
    # «неясное в спецификации» ценнее самих тестов, потому что это
    # находка о ПОСТАНОВКЕ, а не о коде.
    "tests_authored": lambda r: (
        ("тесты написаны независимо: "
         + ", ".join(map(str, _seq(r.get("files")) or ["?"]))
         + f"; случаев {r.get('cases')}"
         + (f"; неясного в спеке: {len(_seq(r.get('unclear')))}"
            if r.get("unclear") else "")),
        {"files", "cases", "unclear"}),
    "tests_not_authored": lambda r: (
        (f"независимых тестов не получилось ({_s(r.get('reason'))}): "
         f"{_s(r.get('degraded'))}"),
        {"reason", "degraded"}),
    # Запрет, который сработал, обязан быть видимым. На движке claude
    # «git для тебя только для чтения» — не фраза промпта, а правило
    # разрешений: отказ приходит ДО выполнения команды. Молчать о нём
    # значило бы не знать, ПЫТАЛСЯ ли исполнитель выйти за правило.
    "executor_denied": lambda r: (
        (f"исполнителю отказано в вызове инструмента в раунде "
         f"{r.get('round')} ({r.get('count')}): "
         + "; ".join(map(str, _seq(r.get("commands")) or ["?"]))),
        {"round", "count", "commands"}),
    # Прозрачность инъекции: оператор обязан видеть, ЧТО и СКОЛЬКО
    # подмешано в промпт, — id уроков прослеживаются до записей памяти.
    "memory_injected": lambda r: (
        (f"в промпт роли {_s(r.get('role'))} подмешано "
         f"{r.get('count')} урок(а) ({r.get('chars')} символов): "
         + ", ".join(map(str, _seq(r.get("ids")) or ["?"]))),
        {"role", "count", "chars", "ids"}),
    "memory_written": lambda r: (
        (f"память пополнена уроком {_s(r.get('lesson'))} "
         f"({_s(r.get('outcome'))})"),
        {"lesson", "outcome"}),
    "memory_reflect": lambda r: (
        f"дайджест памяти пересобран: уроков {r.get('lessons')}",
        {"lessons"}),
    "memory_forgotten": lambda r: (
        f"урок {_s(r.get('lesson'))} затомбстоунен", {"lesson"}),
    "memory_synced": lambda r: (
        (f"индекс памяти досыпан: строк {r.get('rows')}, "
         f"векторов добавлено {r.get('vectors')}, "
         f"без вектора {r.get('unembedded')}"),
        {"rows", "vectors", "unembedded"}),
    "quota_resume": lambda r: (
        (f"автовозобновление {r.get('attempt')}/{r.get('of')}: ждём "
         f"{r.get('wait_s')} с (до {_s(r.get('until'))}): "
         f"{_s(r.get('message'))}"),
        {"attempt", "of", "wait_s", "until", "message"}),
    # Раунд, не дошедший до суждения о работе, лимит исправлений не тратит:
    # человек обязан видеть и сам факт, и то, обо что раунд сгорел.
    "round_futile": lambda r: (
        (f"раунд {r.get('round')} сорвался до суждения о работе "
         f"({_s(r.get('cause'))}: {_s(r.get('detail'))}) — лимит исправлений "
         f"не тронут, бесплодных {r.get('futile')} из {r.get('of')}"),
        {"round", "cause", "detail", "futile", "of"}),
    "futile_exhausted": lambda r: (
        (f"бесплодные раунды исчерпаны: {r.get('rounds')} сорвались до "
         f"суждения ({', '.join(map(str, _seq(r.get('causes')) or ['?']))})"),
        {"rounds", "causes", "stash"}),
}


def narrate(row: dict[str, Any]) -> str:
    """Запись журнала одной фразой. Ни одно поле не теряется.

    Пустое значение (None, "", [], {}) в хвост не идёт: отсутствие факта —
    не факт. Ноль идёт: «находок 0» — это результат, а не пустота.
    """
    kind = str(row.get("kind") or "")
    narrator = NARRATORS.get(kind)
    named: set[str] = set()
    if narrator is None:
        # Незнакомый вид записи — не повод молчать: имя показываем как
        # есть, а всё содержимое уходит в хвост целиком.
        phrase = ru(KIND_RU, kind) or "(запись без вида)"
    else:
        phrase, named = narrator(row)
    tail = [f"{k}={_s(v)}" for k, v in row.items()
            if k not in FRAME and k not in named
            and v is not None and v != "" and v != [] and v != {}]
    return phrase + (f"  [{'; '.join(tail)}]" if tail else "")


def finding(f: dict[str, Any]) -> str:
    """Находка ревьюера одной строкой: тяжесть, класс, место, суть."""
    where = str(f.get("file") or "")
    if where and f.get("line"):
        where += f":{f['line']}"
    head = (f"[{ru(SEVERITY_RU, f.get('severity'))}/"
            f"{ru(CATEGORY_RU, f.get('category'))}]")
    return " ".join(p for p in (head, where, "—", _s(f.get("issue"), 400)) if p)
