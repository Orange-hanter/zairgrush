"""Третий контур петли: хелперы на дешёвых моделях (§7 дизайн-дока).

Принципы, зашитые в код:
- §7.3 fail-open: любая ошибка хелпера -> None, петля продолжается. Хелпер
  НИКОГДА не бросает исключение наружу и не останавливает работу.
- §7.3 секрет-фильтр: перед отправкой во внешний API текст чистится от
  похожего на ключи/токены.
- §7.1: если задачу можно решить без LLM — она решается без LLM. Поэтому
  дедупликация findings сначала отсекает точные совпадения (file, category)
  механически и зовёт модель только на остатке.
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
METRICS = pathlib.Path(os.environ.get("HELPER_METRICS",
                                      pathlib.Path(__file__).parent / "metrics.jsonl"))

SECRET_PATTERNS = [
    re.compile(r"(?i)\b(?:sk|pk|ghp|gho|xox[baprs])-[A-Za-z0-9_\-]{16,}"),
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


def _metric(**row: Any) -> None:
    obs.stamp(row)
    try:
        with METRICS.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def ollama_chat(prompt: str, name: str, max_tokens: int = 400,
                temperature: float = 0.0) -> str | None:
    """Один вызов дешёвой модели через НАТИВНЫЙ /api/chat.

    Почему не OpenAI-совместимый /v1 (замерено на OLLAMA-1):
    - `think: false` работает только нативно; на /v1 он молча игнорируется,
      и reasoning-модель сжигает весь лимит в размышления, возвращая пустой
      content (679 токенов против 15 на той же задаче);
    - `options` (num_predict, seed, temperature) есть только нативно; seed
      даёт воспроизводимость, а `done_reason == "length"` — единственный
      честный признак обрезанного ответа.

    Любая ошибка -> None (§7.3 fail-open).
    """
    key = os.environ.get("OLLAMA_API_KEY")
    if not key:
        _metric(helper=name, skipped="no_api_key")
        return None
    body = json.dumps({
        "model": MODEL, "stream": False,
        "think": False,                       # не платим за размышления (§7.1)
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
        _metric(helper=name, model=MODEL, api_error=f"недопустимый URL: {BASE_URL}")
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
            _metric(helper=name, model=MODEL, api_error=str(resp["error"])[:120],
                    request_id=request_id)
            return None
        text = (resp.get("message", {}).get("content") or "").strip()
        truncated = resp.get("done_reason") == "length"
        _metric(helper=name, model=MODEL, dur_s=round(time.time() - t0, 1),
                tokens_in=resp.get("prompt_eval_count"),
                # eval_count включает reasoning-токены, если think не выключен
                tokens_out=resp.get("eval_count"),
                truncated=truncated, request_id=request_id, ok=bool(text))
        if truncated:
            # Обрезанный ответ дешевле выбросить, чем чинить: хелпер
            # опционален, а половина JSON хуже отсутствия JSON.
            return None
    except urllib.error.HTTPError as e:
        # 429 (rate limit) и 502 (cloud-модель недоступна) ретраибельны, но
        # хелпер опционален: один шанс и уходим (§7.3), петля не ждёт.
        _metric(helper=name, model=MODEL, dur_s=round(time.time() - t0, 1),
                http_error=e.code, retryable=e.code in (429, 502))
        return None
    except Exception as e:
        log.warning("хелпер %s не ответил", name, exc_info=True)
        _metric(helper=name, model=MODEL, dur_s=round(time.time() - t0, 1),
                error=type(e).__name__)
        return None
    else:
        return text or None


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
    if not line or len(line) > 72 or line.startswith("```"):
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
    (file, category) отсекаются механически (§7.1), модель зовётся только
    на остатке. -> {"mechanical": [...], "semantic": [...]} или None."""
    def key(f: dict[str, Any]) -> tuple[Any, Any]:
        return (f.get("file"), f.get("category"))

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
    upper = text.upper()
    if "SAME" in upper:
        return True
    if "DIFFERENT" in upper:
        return False
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
