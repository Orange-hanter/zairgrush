#!/usr/bin/env python3
"""REVIEWARM: отбор состава присяжных по ВСЕМУ каталогу аккаунта.

Зачем (замечание владельца 2026-08-20): первый состав был собран из
моделей, которые попались под руку, и половина из них устарела —
`glm-5.1` при живом `glm-5.2`, `qwen3.5` вместо чего посвежее, minimax и
kimi-k3 не пробовались вовсе. Плюс `num_predict = 900` был взят с потолка.

Про потолок отдельно, потому что он оказался ошибкой не настройки, а
рассуждения. Правило §7.1 «не платим за размышления» пришло из
МЕТРИРУЕМОГО контура, где каждый токен размышления — деньги. На плоской
подписке выходные токены не стоят ничего: размышления стоят только
задержки. То есть год мы душили reasoning ради экономии, которой на этом
контуре не существует. Этот стенд задаёт вопрос, который прежний потолок
задать не давал: **становится ли присяжный лучше, если дать ему думать?**

Что меряется по каждой модели:

- **годность вызова** при щедром `num_predict`: отвечает ли вообще,
  доходит ли до `done_reason == "stop"`;
- **think на модель**: булево и уровень пробуются оба, побеждает тот, что
  дал разбираемый ответ (у gpt-oss булево игнорируется, у остальных
  уровень включает трассу — контракт инвертирован, ADR-004 уточнение);
- **выход находок и мимо диффа**: сколько кандидатов, сколько из них
  называют файл, которого в диффе нет;
- **цена в единственной валюте, которая здесь есть, — в секундах.**

Usage:
    python3 bakeoff.py --stand ~/work/zeus-pilot --commit baf027e \
        --max-tokens 4000 --out out-bakeoff
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.request
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "swarm" / "swarm"))

import helpers
from replay import (
    LENSES,
    PROMPT,
    changed_files,
    diff_of,
    parse_candidates,
)

# Порядок проб think: сначала выключить, потом уровни. Первый разбираемый
# ответ и есть контракт модели — гадать по имени семейства не нужно.
THINK_LADDER: list[bool | str] = [False, "low", "medium"]


def probe(model: str, prompt: str, files: set[str], max_tokens: int,
          think: bool | str) -> dict[str, Any]:
    t0 = time.monotonic()
    raw = helpers.ollama_chat(prompt, name=f"bakeoff:{model}",
                              max_tokens=max_tokens, model=model, think=think)
    wall = time.monotonic() - t0
    cands, refusal = parse_candidates(raw or "", files)
    return {"model": model, "think": think, "wall_s": round(wall, 1),
            "answered": bool(raw), "refusal": refusal or ("" if raw else "no_answer"),
            "cands": len(cands), "chars": len(raw or ""),
            "off_diff": sum(1 for c in cands if c["off_diff"]),
            "major": sum(1 for c in cands if c["severity"] == "major"),
            "candidates": cands}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stand", required=True)
    ap.add_argument("--commit", default="baf027e")
    ap.add_argument("--lens", default="contract", choices=sorted(LENSES))
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--max-diff-lines", type=int, default=1200)
    ap.add_argument("--models", default="", help="через запятую; пусто = весь каталог")
    ap.add_argument("--out", default="out-bakeoff")
    args = ap.parse_args()

    stand = pathlib.Path(args.stand).expanduser()
    out = pathlib.Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    helpers.configure(out / "helper-metrics.jsonl")

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = catalog()
    if not models:
        print("каталог не прочитан и список моделей не задан")
        return 1

    diff = diff_of(stand, args.commit)
    if not diff:
        print(f"коммит {args.commit} не читается на стенде {stand}")
        return 1
    lines = diff.splitlines()
    if len(lines) > args.max_diff_lines:
        diff = "\n".join(lines[:args.max_diff_lines])
    files = changed_files(diff)
    prompt = PROMPT.format(lens_name=args.lens, lens_desc=LENSES[args.lens],
                           cap=5, title="", diff=diff)
    print(f"дифф {args.commit}: файлов {len(files)}, строк {len(lines)}; "
          f"линза {args.lens}; потолок вывода {args.max_tokens} токенов\n")

    print(f"{'модель':30} {'think':8} {'отв':>4} {'канд':>5} {'major':>6} "
          f"{'мимо':>5} {'симв':>6} {'сек':>6}  причина")
    rows: list[dict[str, Any]] = []
    for model in models:
        # Предохранитель хелперов считает подряд идущие отказы, чтобы петля
        # не платила таймаутом за лежащий API. Здесь он вреден: недоступная
        # модель — это ФАКТ ЗАМЕРА, а не авария, и три подряд не должны
        # гасить опрос остального каталога. Сбрасываем на каждой модели.
        helpers._state["failures"] = 0
        for think in THINK_LADDER:
            r = probe(model, prompt, files, args.max_tokens, think)
            rows.append(r)
            print(f"{model:30} {think!s:8} {'да' if r['answered'] else 'нет':>4} "
                  f"{r['cands']:>5} {r['major']:>6} {r['off_diff']:>5} "
                  f"{r['chars']:>6} {r['wall_s']:>6.1f}  {r['refusal']}")
            if r["answered"] and not r["refusal"]:
                break          # контракт найден, лестницу дальше не идём

    (out / "bakeoff.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")

    good = [r for r in rows if r["answered"] and not r["refusal"]]
    print(f"\n=== годны: {len(good)} из {len(models)} моделей ===")
    for r in sorted(good, key=lambda r: (-r["cands"], r["wall_s"])):
        print(f"  {r['model']:30} think={r['think']!s:8} "
              f"кандидатов {r['cands']:>2} (major {r['major']}), "
              f"мимо диффа {r['off_diff']}, {r['wall_s']:.1f} с")
    dead = sorted({r["model"] for r in rows} - {r["model"] for r in good})
    if dead:
        print("\nне дали разбираемого ответа ни на одной ступени think:")
        for m in dead:
            why = [r["refusal"] for r in rows if r["model"] == m]
            print(f"  {m:30} {', '.join(why)}")
    print("\nСырьё:", out / "bakeoff.jsonl")
    return 0


def catalog() -> list[str]:
    """Список моделей аккаунта. Пустой список вместо исключения: стенд
    обязан работать и по явному --models, когда каталог недоступен."""
    key = os.environ.get("OLLAMA_API_KEY")
    if not key:
        return []
    req = urllib.request.Request("https://ollama.com/api/tags",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as raw:  # noqa: S310 — схема https в литерале выше
            data = json.load(raw)
    except (OSError, ValueError):
        return []
    names = [m.get("name") or m.get("model") for m in data.get("models", [])]
    return sorted(n for n in names if n)


if __name__ == "__main__":
    raise SystemExit(main())
