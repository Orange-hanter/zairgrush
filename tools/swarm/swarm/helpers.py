"""Третий контур петли: хелперы на дешёвых моделях (§7 дизайн-дока).

Принципы, зашитые в код:
- §7.3 fail-open: любая ошибка хелпера -> None, петля продолжается. Хелпер
  НИКОГДА не бросает исключение наружу и не останавливает работу.
- §7.3 секрет-фильтр: перед отправкой во внешний API текст чистится от
  похожего на ключи/токены.
- §7.1: если задачу можно решить без LLM — она решается без LLM. Поэтому
  дедупликация findings сначала отсекает точные совпадения
  (file, category, issue) механически и зовёт модель только на остатке.
- §7.2: хелпер не принимает решений, влияющих на код, — он готовит текст,
  который либо проверяется механически, либо уходит человеку.

Модель по умолчанию — gemma4:31b (не reasoning): reasoning-модели сжигают
бюджет в размышления и возвращают пустой content на коротких лимитах.
"""
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, TypeVar, cast

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import obs  # noqa: E402 — каталог добавлен строкой выше

F = TypeVar("F", bound=Callable[..., Any])

BASE_URL = os.environ.get("HELPER_BASE_URL", "https://ollama.com")
MODEL = os.environ.get("HELPER_MODEL", "gemma4:31b")
TIMEOUT = int(os.environ.get("HELPER_TIMEOUT", "90"))
SEED = int(os.environ.get("HELPER_SEED", "17"))

# Изменяемое состояние модуля (словарь, а не пара глобалов: присваивание
# через global запрещено гейтом): куда писать метрики и счётчик отказов
# предохранителя сети.
_state: dict[str, Any] = {"metrics_path": None, "failures": 0}


def configure(metrics_path: str | pathlib.Path | None) -> None:
    """Задать файл метрик хелперов явно. None — вернуться к HELPER_METRICS.

    Явная настройка петли сильнее окружения; окружение читается В МОМЕНТ
    записи, а не при импорте; без того и другого метрики честно не пишутся
    вовсе. Прежний дефолт — файл в каталоге ПАКЕТА: каждый прогон тестов и
    каждый реальный запуск дописывал строки прямо в исходники инструмента.
    """
    _state["metrics_path"] = pathlib.Path(metrics_path) if metrics_path else None


def _metrics_path() -> pathlib.Path | None:
    configured = _state["metrics_path"]
    if configured is not None:
        return cast("pathlib.Path", configured)
    env = os.environ.get("HELPER_METRICS")
    return pathlib.Path(env) if env else None


# Токены реальных провайдеров разделяются и дефисом, и подчёркиванием:
# ghp_/gho_/github_pat_ у GitHub, sk_live_/sk_test_ у Stripe. Прежний
# шаблон требовал строго дефис — и ровно эти токены улетали в API нетронутыми.
SECRET_PATTERNS = [
    re.compile(r"(?i)\b(?:sk|pk|ghp|gho|github_pat|xox[baprs])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\b[A-Za-z0-9_\-]{0,10}(?:api[_-]?key|secret|token|password|passwd)"
               r"\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{12,})['\"]?"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?"
               r"-----END [A-Z ]*PRIVATE KEY-----"),
]


def scrub(text: str) -> str:
    """Секрет-фильтр перед отправкой во внешний API (§7.3)."""
    if not text:
        return text
    out = text
    for pat in SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


LATEX_ARROWS = {r"\rightarrow": "->", r"\to": "->", r"\Rightarrow": "=>",
                r"\leftarrow": "<-", r"\times": "x", r"\ldots": "..."}


def strip_fences(text: str) -> str:
    """Снять ```-обёртку вокруг ответа.

    Замерено на OLLAMA-1: `format` (и схема, и "json") на Ollama Cloud НЕ
    принуждает — модель возвращает то markdown-fences, то жирный текст
    вместо JSON. Поэтому структурированный ответ всегда чистим и парсим
    сами, а схему проверяем своим валидатором.
    """
    if not text:
        return text
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def clean_markup(line: str) -> str:
    """Дешёвые модели подмешивают LaTeX/markdown в простой текст.

    §7.2: текст хелпера применяется только после механической проверки —
    вот она. Найдено на HELP-1: gemma4 выдавала `$\\rightarrow$` в выжимке
    журнала, которая идёт прямо в промпт исполнителя.
    """
    out = line
    for tex, plain in LATEX_ARROWS.items():
        out = out.replace(f"${tex}$", plain).replace(tex, plain)
    out = re.sub(r"\$([^$]{1,40})\$", r"\1", out)      # остатки $...$
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)          # **bold**
    out = re.sub(r"`{1,3}", "", out)                    # ``` и `
    return out.strip()


log = obs.get_logger("helpers")

# Сколько сетевых отказов ПОДРЯД размыкают предохранитель: дальше вызовы
# пропускаются до конца процесса. Хелпер опционален (§7.3), и платить по
# TIMEOUT за каждый вызов при лежащем API — часы ожидания ни за что.
BREAKER_THRESHOLD = int(os.environ.get("HELPER_BREAKER", "3"))


def _metric(**row: Any) -> None:
    path = _metrics_path()
    if path is None:
        # Место не задано — не пишем никуда: молчание честнее файла,
        # тайно растущего в каталоге пакета.
        return
    obs.stamp(row)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def ollama_chat(prompt: str, name: str, max_tokens: int = 400,
                temperature: float = 0.0, model: str | None = None,
                think: bool | str = False) -> str | None:
    """Один вызов дешёвой модели через НАТИВНЫЙ /api/chat.

    Почему не OpenAI-совместимый /v1 (замерено на OLLAMA-1):
    - `think: false` работает только нативно; на /v1 он молча игнорируется,
      и reasoning-модель сжигает весь лимит в размышления, возвращая пустой
      content (679 токенов против 15 на той же задаче);
    - `options` (num_predict, seed, temperature) есть только нативно; seed
      даёт воспроизводимость, а `done_reason == "length"` — единственный
      честный признак обрезанного ответа.

    `think` — НЕ глобальная константа, а свойство модели (замерено
    2026-08-20, REVIEWARM; подтверждено docs.ollama.com/capabilities/
    thinking). Поле принимает булево ИЛИ уровень "low"|"medium"|"high"|
    "max", и семейства расходятся ровно наоборот:

    - deepseek-v4-flash, glm-5.1, qwen3.5, kimi-k2.7-code: `False`
      выключает размышления (ответ за 317–422 токена); уровень "low",
      наоборот, их ВКЛЮЧАЕТ и съедает весь num_predict;
    - gpt-oss: булево **игнорируется** (так сказано в документации), модель
      рассуждает всегда и ждёт уровень. С `think=False` она сожгла 900
      токенов на 4450 символов размышления и вернула ПУСТОЙ content;
      с `think="low"` — 558 токенов и годный ответ.

    Отсюда правило вызывающего: think выбирается на модель. Значение по
    умолчанию `False` сохранено — оно верно для всех моделей третьего
    контура, кроме gpt-oss.

    Размышления приходят ОТДЕЛЬНЫМ полем `message.thinking` и не попадают
    в результат, но токены на них тратятся из того же `num_predict`.
    Поэтому метрика несёт `thinking_chars`: строка «обрезан» без него
    читается как «модель плоха», хотя на деле бюджет ушёл в размышления,
    которых мы не просили.

    `model=None` — модель хелперов третьего контура (MODEL, умолчание §7).
    Явный `model` пускает через тот же контракт вызов ЛЮБОЙ модели Ollama
    Cloud (E10, chat-fill исполнителя) — транспорт и предохранитель общие,
    занижать надёжность отдельной модели незачем.

    Структурный вывод (`format` со схемой) не задаётся сознательно: облако
    Ollama его не поддерживает — это сказано в документации вендора и
    замерено ещё раз 2026-08-20 (ответ пришёл по промпту, а не по схеме).
    Формат вымогается промптом и проверяется вызывающим (ADR-004).

    Любая ошибка -> None (§7.3 fail-open).
    """
    resolved_model = model or MODEL
    key = os.environ.get("OLLAMA_API_KEY")
    if not key:
        _metric(helper=name, skipped="no_api_key")
        return None
    if _state["failures"] >= BREAKER_THRESHOLD:
        # Предохранитель: при лежащем API каждый вызов платил бы полный
        # TIMEOUT (до 90 с) за опциональный слой. Пропуск — с честной
        # строкой метрики, а не молча.
        _metric(helper=name, skipped="circuit_breaker",
                consecutive_failures=_state["failures"])
        return None
    body = json.dumps({
        "model": resolved_model, "stream": False,
        "think": think,                       # не платим за размышления (§7.1)
        "options": {
            "num_predict": max_tokens,        # дефолт -1 = без ограничения
            "temperature": temperature,
            "top_k": 1,                       # temperature=0 одна не даёт greedy
            "repeat_penalty": 1.0,            # дефолт 1.1 давит повторы ключей JSON
            "seed": SEED,
        },
        # num_ctx/truncate/shift НЕ задаём: на Ollama Cloud они игнорируются —
        # контекст всегда максимальный для модели (замерено: num_ctx=4096 на
        # промпте 51k токенов не обрезал ничего).
        "messages": [{"role": "user", "content": scrub(prompt)}],
    }).encode()
    url = f"{BASE_URL}/api/chat"
    if not url.startswith("https://"):
        # BASE_URL приходит из окружения: без этой проверки его подмена на
        # file:// или http:// уводит ключ и промпт в чужие руки. Проверка
        # стоит здесь, а не в конфиге, — тогда её нельзя обойти правкой env.
        _metric(helper=name, model=resolved_model,
                api_error=f"недопустимый URL: {BASE_URL}")
        return None
    req = urllib.request.Request(url, data=body,  # noqa: S310 — схема проверена
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    t0 = time.time()
    try:
        raw = urllib.request.urlopen(req, timeout=TIMEOUT)  # noqa: S310 — схема проверена выше
        # x-request-id — единственная зацепка для саппорта
        request_id = raw.headers.get("x-request-id")
        resp = json.load(raw)
        if resp.get("error"):
            # Ошибка может приехать телом при HTTP 200 — проверяем до разбора.
            _state["failures"] += 1
            _metric(helper=name, model=resolved_model,
                    api_error=str(resp["error"])[:120], request_id=request_id)
            return None
        _state["failures"] = 0
        message = resp.get("message", {}) or {}
        text = (message.get("content") or "").strip()
        # Размышления не идут в результат, но тратят тот же num_predict.
        # Без этого числа обрезанный ответ не отличить от «модель плоха».
        thinking_chars = len((message.get("thinking") or "").strip())
        truncated = resp.get("done_reason") == "length"
        _metric(helper=name, model=resolved_model, dur_s=round(time.time() - t0, 1),
                tokens_in=resp.get("prompt_eval_count"),
                # eval_count включает reasoning-токены, если think не выключен
                tokens_out=resp.get("eval_count"),
                think=think, thinking_chars=thinking_chars,
                truncated=truncated, request_id=request_id,
                # ok — пригодность результата: обрезанный ответ строкой ниже
                # выбрасывается, и ok=True при этом был ложью метрики.
                ok=bool(text) and not truncated)
        if truncated:
            # Обрезанный ответ дешевле выбросить, чем чинить: хелпер
            # опционален, а половина JSON хуже отсутствия JSON.
            return None
    except urllib.error.HTTPError as e:
        # 429 (rate limit) и 502 (cloud-модель недоступна) ретраибельны, но
        # хелпер опционален: один шанс и уходим (§7.3), петля не ждёт.
        _state["failures"] += 1
        _metric(helper=name, model=resolved_model, dur_s=round(time.time() - t0, 1),
                http_error=e.code, retryable=e.code in (429, 502))
        return None
    except Exception as e:
        log.warning("хелпер %s не ответил", name, exc_info=True)
        _state["failures"] += 1
        _metric(helper=name, model=resolved_model, dur_s=round(time.time() - t0, 1),
                error=type(e).__name__)
        return None
    else:
        return text or None


OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"


def _embed_request(text: str, model: str) -> tuple[str, str, bytes] | str:
    """(url, ключ, тело) под транспорт модели, либо причина отказа.

    Два транспорта, выбор — префиксом модели:
    - `openrouter:vendor/slug[@dim]` — OpenAI-совместимый эндпоинт
      OpenRouter; probe 2026-08-18: qwen/qwen3-embedding-8b жив,
      dim 2048 (Matryoshka, замер оракула: без потерь против 4096),
      $0.0000006 за урок. `@dim` уходит параметром dimensions.
    - иначе — нативный /api/embed Ollama; probe 2026-08-18: в облаке
      владельца эмбеддинг-моделей НЕТ (unauthorized) — ветка живёт для
      локального ollama и других аккаунтов.
    """
    if model.startswith("openrouter:"):
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            return "no_api_key"
        slug, _, dim = model.removeprefix("openrouter:").partition("@")
        payload: dict[str, Any] = {"model": slug, "input": scrub(text)}
        if dim.isdigit():
            payload["dimensions"] = int(dim)
        return OPENROUTER_URL, key, json.dumps(payload).encode()
    key = os.environ.get("OLLAMA_API_KEY")
    if not key:
        return "no_api_key"
    url = f"{BASE_URL}/api/embed"
    if not url.startswith("https://"):
        # Та же граница, что у /api/chat: подмена BASE_URL не должна
        # уводить ключ и текст урока в чужие руки.
        return f"недопустимый URL: {BASE_URL}"
    return url, key, json.dumps({"model": model,
                                 "input": scrub(text)}).encode()


def _embed_vector(resp: dict[str, Any]) -> list[float]:
    """Вектор из ответа любого транспорта: ollama кладёт embeddings[0],
    OpenAI-совместимые — data[0].embedding."""
    first: Any = None
    vecs = resp.get("embeddings")
    if isinstance(vecs, list) and vecs:
        first = vecs[0]
    data = resp.get("data")
    if first is None and isinstance(data, list) and data \
            and isinstance(data[0], dict):
        first = data[0].get("embedding")
    return [float(x) for x in first] if isinstance(first, list) else []


def embed_text(text: str, model: str) -> list[float] | None:
    """Эмбеддинг урока памяти. Любая ошибка -> None (§7.3).

    Слой опционален по построению: без ключа, при лежащем API или пустом
    ответе память остаётся на FTS — вектор лишь сеть дополнительного
    охвата, и его отсутствие не событие, а нормальный режим.
    """
    if not model:
        _metric(helper="embed", skipped="no_model")
        return None
    if _state["failures"] >= BREAKER_THRESHOLD:
        _metric(helper="embed", skipped="circuit_breaker",
                consecutive_failures=_state["failures"])
        return None
    prepared = _embed_request(text, model)
    if isinstance(prepared, str):
        _metric(helper="embed", model=model, skipped=prepared)
        return None
    url, key, body = prepared
    req = urllib.request.Request(url, data=body,  # noqa: S310 — только https
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    t0 = time.time()
    try:
        raw = urllib.request.urlopen(req, timeout=TIMEOUT)  # noqa: S310 — только https
        resp = json.load(raw)
        if resp.get("error"):
            _state["failures"] += 1
            _metric(helper="embed", model=model,
                    api_error=str(resp["error"])[:120])
            return None
        vec = _embed_vector(resp)
    except urllib.error.HTTPError as e:
        _state["failures"] += 1
        _metric(helper="embed", model=model, http_error=e.code,
                retryable=e.code in (429, 502))
        return None
    except Exception:
        log.warning("эмбеддер не ответил", exc_info=True)
        _state["failures"] += 1
        _metric(helper="embed", model=model, error="exception")
        return None
    if not vec:
        _metric(helper="embed", model=model, api_error="пустой ответ")
        return None
    _state["failures"] = 0
    cost = (resp.get("usage") or {}).get("cost") if isinstance(
        resp.get("usage"), dict) else None
    _metric(helper="embed", model=model, dim=len(vec),
            dur_s=round(time.time() - t0, 1), cost_usd=cost, ok=True)
    return vec


def consolidate_lessons(records: list[dict[str, Any]],
                        max_themes: int = 6) -> str | None:
    """Темы из уроков памяти (E9, этап 3). §7.2 буквально: хелпер готовит
    текст, который проверяется механически, — строка без ссылки на
    существующий id урока отбрасывается. Хелпер не имеет права добавлять
    в память факты без происхождения.
    """
    known = {str(r.get("id") or "") for r in records} - {""}
    if not known:
        return None
    listing = "\n".join(f"{r.get('id')}: {str(r.get('body') or '')[:200]}"
                        for r in records[:40])
    prompt = (f"Сгруппируй уроки в темы, не больше {max_themes} строк.\n"
              "Формат строки: тема — суть (id, id). Обязательно указывай "
              "id процитированных уроков в скобках. Только строки тем, "
              "без преамбулы и пояснений.\n\n" + listing)
    reply = ollama_chat(prompt, "consolidate", max_tokens=400)
    if not reply:
        return None
    kept = [clean_markup(line.strip()) for line in reply.splitlines()
            if line.strip() and any(k in line for k in known)]
    return "\n".join(kept[:max_themes]) or None


def fail_open(default: Any) -> Callable[[F], F]:
    """§7.3: ЛЮБОЕ падение хелпера гасится здесь. Наружу — только default.

    Гарантия даётся на уровне API хелпера, а не только сетевого вызова:
    сломаться может и разбор ответа, и сама логика.
    """
    def deco(fn: F) -> F:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                log.warning("хелпер %s погашен", fn.__name__, exc_info=True)
                _metric(helper=fn.__name__, error=type(e).__name__, failed_open=True)
                return default(*args, **kwargs) if callable(default) else default
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return cast("F", wrapper)
    return deco


# --- §7.2: роли хелперов -------------------------------------------------

@fail_open(lambda _task, _diff, fallback: (fallback, "fallback:exception"))
def commit_message(task: dict[str, Any], diff: str,
                   fallback: str) -> tuple[str, str]:
    """Коммит-сообщение по диффу. Проверяется механически: одна строка,
    <= 72 символов, без fences. Не прошло — берём шаблон (fail-open)."""
    prompt = (
        "Сгенерируй ОДНУ строку git commit subject на русском для этого диффа.\n"
        "Требования: повелительное наклонение, до 72 символов, без кавычек, "
        "без markdown, без префикса типа 'feat:'. Ответь только строкой.\n\n"
        f"Задача: {task.get('title', '')}\n\nДифф:\n{diff[:6000]}"
    )
    text = ollama_chat(prompt, "commit_message", max_tokens=120)
    if not text:
        return fallback, "fallback:no_response"
    first = strip_fences(text).splitlines()[0]
    line = clean_markup(first).strip('"').lstrip("#").strip()
    # Проверки на ``` здесь нет намеренно: clean_markup уже снял все
    # бэктики, и ветка startswith("```") была мёртвой.
    if not line or len(line) > 72:
        return fallback, "fallback:validation"
    return line, "helper"


def _issues(findings: list[dict[str, Any]]) -> str:
    """Только текст находок: индексы в ответе модели — позиции в этом списке."""
    return json.dumps([f.get("issue") for f in findings],
                      ensure_ascii=False, indent=1)


@fail_open(lambda _prev_findings, _curr_findings: {"mechanical": [], "semantic": []})
def dedup_findings(prev_findings: list[dict[str, Any]],
                   curr_findings: list[dict[str, Any]],
                   ) -> dict[str, list[dict[str, Any]]] | None:
    """Какие findings повторяются между итерациями. Точные совпадения
    (file, category, нормализованный issue) отсекаются механически (§7.1),
    модель зовётся только на остатке.
    -> {"mechanical": [...], "semantic": [...]} или None."""
    def key(f: dict[str, Any]) -> tuple[Any, Any, str]:
        # Текст находки — часть ключа: по одним (file, category) НОВАЯ
        # проблема в том же файле записывалась в повторы и терялась.
        issue = re.sub(r"\s+", " ", str(f.get("issue") or "").strip().lower())
        return (f.get("file"), f.get("category"), issue)

    prev_keys = {key(f) for f in prev_findings}
    mechanical = [f for f in curr_findings if key(f) in prev_keys]
    rest = [f for f in curr_findings if key(f) not in prev_keys]
    if not rest or not prev_findings:
        return {"mechanical": mechanical, "semantic": []}
    prompt = (
        "Ниже замечания ревьюера с ПРОШЛОЙ итерации и с ТЕКУЩЕЙ.\n"
        "Определи, какие текущие замечания повторяют прошлые ПО СМЫСЛУ "
        "(та же проблема другими словами), даже если файл другой.\n"
        'Ответь только JSON: {"repeated": [<индексы текущих>]} без пояснений.\n\n'
        f"ПРОШЛЫЕ:\n{_issues(prev_findings)}\n\n"
        f"ТЕКУЩИЕ:\n{_issues(rest)}"
    )
    text = ollama_chat(prompt, "dedup_findings", max_tokens=200)
    semantic: list[dict[str, Any]] = []
    if text:
        try:
            # Ответ модели может не содержать JSON вовсе — тогда `search`
            # вернёт None, и это НЕ ошибка петли: хелпер опционален.
            found = re.search(r"\{.*\}", strip_fences(text), re.DOTALL)
            idx = json.loads(found.group(0)).get("repeated", []) if found else []
            semantic = [rest[i] for i in idx
                        if isinstance(i, int) and 0 <= i < len(rest)]
        except Exception:
            log.debug("ответ dedup_findings не разобран", exc_info=True)
            semantic = []
    return {"mechanical": mechanical, "semantic": semantic}


@fail_open(None)
def stagnation_hint(prev_issue: str, curr_issue: str) -> bool | None:
    """Семантическая близость двух issue поверх механической проверки
    (§5.3). -> True/False/None (None = хелпер недоступен, решает механика)."""
    prompt = (
        "Две претензии код-ревьюера. Это ОДНА И ТА ЖЕ проблема, "
        "сформулированная по-разному, или разные проблемы?\n"
        "Ответь одним словом: SAME или DIFFERENT.\n\n"
        f"A: {prev_issue}\n\nB: {curr_issue}"
    )
    text = ollama_chat(prompt, "stagnation_hint", max_tokens=16)
    if not text:
        return None
    # Отрицания и DIFFERENT — раньше SAME, а SAME — только точным первым
    # словом: подстрочный поиск делал из «NOT THE SAME» ответ True.
    words = re.findall(r"[A-Z]+", clean_markup(strip_fences(text)).upper())
    if not words:
        return None
    if "DIFFERENT" in words or words[0] == "NOT":
        return False
    if words[0] == "SAME":
        return True
    return None


@fail_open(None)
def summarize_log(entries: list[str], max_lines: int = 6) -> str | None:
    """Выжимка «что уже пробовали и не сработало» для handoff (§8)."""
    if not entries:
        return None
    prompt = (
        f"Сожми историю итераций до {max_lines} строк для передачи исполнителю.\n"
        "Формат: маркированный список, каждая строка — что пробовали и чем "
        "кончилось. Без вступления и заключения, только список.\n\n"
        + "\n".join(f"- {e}" for e in entries[-40:])
    )
    text = ollama_chat(prompt, "summarize_log", max_tokens=400)
    if not text:
        return None
    lines = [clean_markup(line) for line in text.splitlines()
             if line.strip()][:max_lines]
    return "\n".join(line for line in lines if line) or None
