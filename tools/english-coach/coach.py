#!/usr/bin/env python3
"""English coach: глоссарий к последней реплике ассистента.

Идея: переписка идёт на английском (экономия токенов + практика языка),
а сложные слова и грамматику разбирает ДЕШЁВАЯ модель Ollama Cloud —
дорогая модель не тратит выходные токены на справку.

Два входа, потому что реплики приходят из двух разных агентов:

- ХУК Claude Code (`Stop`): на stdin приходит JSON с `transcript_path`,
  оттуда берётся текст последнего ответа. Ответ печатается полем
  `systemMessage` — так его показывает интерфейс.
- РУКАМИ, без stdin: читается wire.jsonl последней сессии kimi-code —
  исходный режим инструмента, он никуда не делся.

Дальше одинаково: Ollama Cloud просят составить компактный глоссарий на
русском — слова уровня B2+ с переводом и 1–3 грамматических замечания.

Транспорт — нативный /api/chat по контракту, замеренному в OLLAMA-1
(tools/swarm/swarm/helpers.py): think:false, seed, top_k=1,
repeat_penalty=1.0 — иначе reasoning-модель сжигает лимит в размышления.

Любая ошибка -> тихий пропуск (fail-open): справка опциональна и не
имеет права ломать основной поток. Хук, роняющий сессию ради глоссария,
хуже отсутствия глоссария.
"""
import glob
import json
import os
import pathlib
import sys
import urllib.request

BASE_URL = os.environ.get("HELPER_BASE_URL", "https://ollama.com")
MODEL = os.environ.get("HELPER_MODEL", "gemma4:31b")
TIMEOUT = int(os.environ.get("HELPER_TIMEOUT", "90"))
SEED = int(os.environ.get("HELPER_SEED", "17"))
MIN_WORDS = 80  # короткие реплики не заслуживают справки
# Транскрипт сессии — файл на десятки мегабайт, и читать его целиком ради
# последней реплики значит платить секундами на каждом ходе. Хвоста
# заведомо хватает: одна реплика — единицы килобайт.
TAIL_BYTES = 2_000_000
MAX_CHARS = 12_000  # длинную реплику режем: справка нужна по языку, не по объёму

PROMPT = """Ты — преподаватель английского для русскоязычного инженера.
Ниже — английский текст из рабочей переписки. Составь по нему КОМПАКТНУЮ
учебную справку на русском:

1. СЛОВАРЬ: 5–10 слов уровня B2 и выше (редкие, профессиональные или
   коварные — false friends, неочевидные значения). Формат строки:
   «слово — перевод; одна короткая пометка (нюанс/произношение/пара)».
   Базовую лексику уровня A2 не включай.
2. ГРАММАТИКА: 1–3 конструкции из текста, которые русскоязычный
   обычно строит неправильно (порядок слов, артикли, инверсия, модальные,
   фразовые глаголы). Формат: «конструкция из текста → почему так».

Без вступлений и заключений. Если текст короткий или весь элементарный —
ответь одним словом: ПУСТО.

Текст:
"""


def last_reply_text() -> str:
    """Текст последнего хода ассистента из самого свежего wire.jsonl."""
    wires = glob.glob(os.path.expanduser(
        "~/.kimi-code/sessions/*/session_*/agents/main/wire.jsonl"))
    if not wires:
        return ""
    wire = max(wires, key=os.path.getmtime)
    parts: list[tuple[str, str]] = []  # (turnId, text)
    with open(wire, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = row.get("event", {})
            part = ev.get("part", {})
            if (row.get("type") == "context.append_loop_event"
                    and ev.get("type") == "content.part"
                    and part.get("type") == "text" and part.get("text")):
                parts.append((ev.get("turnId", ""), part["text"]))
    if not parts:
        return ""
    last_turn = parts[-1][0]
    return "\n".join(t for turn, t in parts if turn == last_turn)


def hook_payload() -> dict:
    """JSON хука со stdin, либо пустой словарь.

    Пустой — это и «запустили руками», и «stdin не JSON». Оба случая
    означают одно: работаем по-старому, от wire.jsonl kimi.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def claude_reply_text(transcript: str) -> str:
    """Текст последнего ответа из транскрипта Claude Code.

    Идём с конца и собираем text-блоки ассистента, пока не упрёмся в ход
    ЧЕЛОВЕКА (строка user с текстом, а не с результатом инструмента).
    Так в справку попадает весь финальный ответ, включая промежуточные
    реплики того же хода, и не попадает предыдущий разговор.
    """
    path = pathlib.Path(transcript)
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    rows = []
    # Первая строка хвоста почти наверняка обрезана посередине — она и
    # отбрасывается разбором, но только она: остальное валидно.
    for line in tail.splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    chunks: list[str] = []
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        if row.get("type") == "assistant":
            chunks.extend(reversed(texts))
            if sum(len(c) for c in chunks) >= MAX_CHARS:
                break   # набрали на справку; остальное всё равно срежется
        elif row.get("type") == "user" and any(t.strip() for t in texts):
            break     # дошли до хода человека — дальше чужая речь
    return "\n".join(reversed(chunks))[-MAX_CHARS:]


def main() -> int:
    payload = hook_payload()
    as_hook = bool(payload.get("transcript_path"))
    text = (claude_reply_text(payload["transcript_path"]) if as_hook
            else last_reply_text())
    if len(text.split()) < MIN_WORDS:
        return say(as_hook, "(реплика короткая — справка не нужна)", quiet=True)
    key = os.environ.get("OLLAMA_API_KEY")
    if not key or not BASE_URL.startswith("https://"):
        return say(as_hook, "(нет OLLAMA_API_KEY или недопустимый URL — пропуск)",
                   quiet=True)
    body = json.dumps({
        "model": MODEL, "stream": False, "think": False,
        "options": {"num_predict": 700, "temperature": 0.0, "top_k": 1,
                    "repeat_penalty": 1.0, "seed": SEED},
        "messages": [{"role": "user", "content": PROMPT + text}],
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/chat", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as raw:
            resp = json.load(raw)
    except Exception as e:  # справка опциональна — глотаем всё
        return say(as_hook, f"(Ollama недоступна: {type(e).__name__} — пропуск)",
                   quiet=True)
    out = (resp.get("message", {}).get("content") or "").strip()
    if not out or out == "ПУСТО":
        return say(as_hook, "(модель не нашла сложного)", quiet=True)
    where = save_words(out)
    return say(as_hook, out + (f"\n\n(слова сохранены: {where})" if where else ""))


def say(as_hook: bool, message: str, quiet: bool = False) -> int:
    """Одна точка вывода: хуку — JSON, человеку — текст.

    `quiet` — про пропуски. В терминале о них сказать полезно (запустил и
    ждёшь), а в интерфейсе сессии строка «справка не нужна» после каждого
    короткого ответа — шум, ради которого хук и выключат.
    """
    if not as_hook:
        print(message)
        return 0
    if quiet:
        return 0
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    return 0


def save_words(glossary: str) -> pathlib.Path | None:
    """Копить справку в words.jsonl рядом со скриптом.

    Без журнала слова умирают вместе со scrollback'ом терминала: справка
    читается один раз и забывается. JSONL, а не БД, — чтобы журнал можно
    было читать глазами, грепать и строить поверх него повторялку.
    """
    import datetime as dt
    log = pathlib.Path(__file__).parent / "words.jsonl"
    row = {"date": dt.date.today().isoformat(), "glossary": glossary}
    try:
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return log


if __name__ == "__main__":
    sys.exit(main())
