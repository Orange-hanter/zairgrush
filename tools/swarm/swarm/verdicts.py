"""Механика вердиктов, квот и решений петли (§4–§5).

Чистые функции и исключения, которые раньше жили в loop.py: разбор
вердикта ревьюера, фильтрация политиками, классификация находок,
принятие решения по раунду, а также детекторы отказов по квоте.
Вынесены в отдельный модуль, чтобы петля и её потребители могли
использовать их без загрузки всего конечного автомата.
"""
import datetime as dt
import re
import zoneinfo
from typing import Any

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

QUOTA_MARKERS = ("session limit", "rate limit", "quota", "usage limit",
                 "429", "too many requests")
# Замеренный формат: «You've hit your session limit · resets 3:10pm
# (Europe/Minsk)»; понимаем и 24-часовую форму, и «resets 3pm».
_QUOTA_RESET_RE = re.compile(
    r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
    r"(?:\s*\(([^)]+)\))?", re.IGNORECASE)


def parse_quota_reset(message: str,
                      now: dt.datetime | None = None) -> dt.datetime | None:
    """Момент сброса квоты из текста провайдера, если он там назван.

    Часовой пояс — из скобок после времени; не назван или незнаком —
    время читается в поясе машины (оператор и провайдер в одном поясе —
    замеренный случай). Названное время уже прошло — значит, завтра.
    Ничего не разобрано -> None: наверху решают fallback-паузой, а не
    выдуманным временем.
    """
    m = _QUOTA_RESET_RE.search(message or "")
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if not m.group(2) and not ampm:
        return None          # голое «resets 3» — слишком мало, не гадаем
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    moment = now or dt.datetime.now(dt.UTC)
    tz: dt.tzinfo | None = None
    if m.group(4):
        try:
            tz = zoneinfo.ZoneInfo(m.group(4).strip())
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, KeyError):
            tz = None        # незнакомый пояс = пояс машины, честный дефолт
    if tz is None:
        tz = moment.astimezone().tzinfo
    local_now = moment.astimezone(tz)
    target = local_now.replace(hour=hour, minute=minute,
                               second=0, microsecond=0)
    if target <= local_now:
        target += dt.timedelta(days=1)
    return target


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

    Модули петли грузятся по путям (importlib), и разные копии loop могли
    бы иметь разные объекты класса QuotaExceededError, если бы он жил в
    loop.py. Сейчас единое определение — в verdicts.py, но детекция по имени
    сохранена: исключение может прийти из чужой копии модуля или вообще от
    отдельного класса с тем же именем, а `except QuotaExceededError` ловит
    только исключение с точно таким же объектом класса. Имя класса остаётся
    стабильным контрактом независимо от того, как загружен модуль.
    """
    return type(exc).__name__ == "QuotaExceededError"


class EscalationError(Exception):
    """Ситуация, которую обязан разобрать человек (§1.1)."""


def quota_error(envelope: Any) -> str | None:
    """Отказ провайдера, который лечится ожиданием, а не ретраем ответа.

    Два класса сигналов. Первый — дружелюбные формулировки квоты
    (QUOTA_MARKERS). Второй — замеренная на PILOT-1 транзиентная кромка
    session limit: HTTP 403 «Failed to authenticate» с НУЛЕВОЙ работой
    (нет ни токенов, ни стоимости — провайдер отверг запрос до начала).
    Прямой probe минутами позже отвечал OK, а петля тем временем
    классифицировала оба захода как invalid_verdict и сожгла
    ~50-минутный раунд исполнителя терминальным блоком.

    Постоянный 403 (отозванный токен) отсюда не отличим — и не нужно:
    он исчерпает лестницу бэкоффа и поставит на паузу ВЕСЬ прогон с этим
    же сообщением провайдера. Это правильный исход и для него — в
    отличие от блокировки невиновной задачи с ложным диагнозом.

    403 С выполненной работой (токены потрачены) — не квота: провайдер
    что-то делал, и отказ надо разбирать, а не пережидать.
    """
    if not isinstance(envelope, dict) or not envelope.get("is_error"):
        return None
    text = str(envelope.get("result") or "").lower()
    if any(m in text for m in QUOTA_MARKERS):
        return str(envelope.get("result"))[:200]
    usage = envelope.get("usage") or {}
    # «Нулевая работа» — ни одного токена ЛЮБОГО вида, включая кэш:
    # отказ, пришедший после чтения кэша, — работа провайдера, и его
    # разбирают, а не пережидают. Ужесточение безопасно в одну сторону:
    # сомнительный конверт остаётся настоящим отказом, как до фикса.
    no_work = not (envelope.get("total_cost_usd")
                   or usage.get("input_tokens") or usage.get("output_tokens")
                   or usage.get("cache_read_input_tokens")
                   or usage.get("cache_creation_input_tokens"))
    if envelope.get("api_error_status") == 403 and no_work:
        return str(envelope.get("result")
                   or "HTTP 403 без выполненной работы")[:200]
    return None


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
