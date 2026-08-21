#!/usr/bin/env python3
"""English coach: глоссарий к последней реплике ассистента.

Идея: переписка идёт на английском (экономия токенов + практика языка),
а сложные слова и грамматику разбирает ДЕШЁВАЯ модель Ollama Cloud —
дорогая модель не тратит выходные токены на справку.

Как работает: читает wire.jsonl активной сессии kimi-code, берёт текст
последнего хода ассистента (события content.part с type=text) и просит
Ollama Cloud составить компактный глоссарий на русском: слова уровня
B2+ с переводом и 1–3 грамматических замечания.

Транспорт — нативный /api/chat по контракту, замеренному в OLLAMA-1
(tools/swarm/swarm/helpers.py): think:false, seed, top_k=1,
repeat_penalty=1.0 — иначе reasoning-модель сжигает лимит в размышления.

Любая ошибка -> тихий пропуск (fail-open): справка опциональна и не
имеет права ломать основной поток.
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


def main() -> int:
    text = last_reply_text()
    if len(text.split()) < MIN_WORDS:
        print("(реплика короткая — справка не нужна)")
        return 0
    key = os.environ.get("OLLAMA_API_KEY")
    if not key or not BASE_URL.startswith("https://"):
        print("(нет OLLAMA_API_KEY или недопустимый URL — пропуск)")
        return 0
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
        print(f"(Ollama недоступна: {type(e).__name__} — пропуск)")
        return 0
    out = (resp.get("message", {}).get("content") or "").strip()
    if out and out != "ПУСТО":
        print(out)
        save_words(out)
    else:
        print("(модель не нашла сложного)")
    return 0


def save_words(glossary: str) -> None:
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
        print(f"(слова сохранены: {log})")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
