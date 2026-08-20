#!/usr/bin/env python3
"""REVIEWARM Step 0, часть вторая: свод результатов панели присяжных.

`replay.py` производит сырьё; этот скрипт отвечает на вопросы, ради
которых нулевой шаг вообще существует (09-cheap-review-contour.md §4):

1. **Проходит ли панель ворота объёма** — после дедупа меньше 10
   кандидатов на дифф, иначе адъюдикатор захлебнётся.
2. **Кто из присяжных вообще годен** — доля вызовов, вернувших разбираемый
   ответ (у дешёвого контура формат не гарантирован, ADR-004), доля
   кандидатов, назвавших файл ИЗ диффа, время.
3. **Совпадает ли панель с дорогим ревьюером по адресу** — на уровне
   файлов; это единственное соединение, переживающее расхождение
   «дифф после исправления» (см. findings E12/goldset-recall).

Чего здесь по-прежнему нет и почему — recall по одобренным ярлыкам:
дефект исправлен ДО коммита, закрывающего задачу; замер recall требует
реплея по журналам исполнителя, а не по диффам.

Usage:
    python3 analyze.py --out out --goldset ../goldset
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
from typing import Any


def read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="out")
    ap.add_argument("--goldset", default=str(pathlib.Path(__file__).parent.parent
                                             / "goldset"))
    args = ap.parse_args()
    out = pathlib.Path(args.out).expanduser()
    goldset = pathlib.Path(args.goldset).expanduser()

    raw = read_jsonl(out / "panel-raw.jsonl")
    cands = read_jsonl(out / "panel-candidates.jsonl")
    verdicts = read_jsonl(goldset / "verdicts.jsonl")
    summary = json.loads((out / "panel-summary.json").read_text(encoding="utf-8"))

    paid_files: dict[str, set[str]] = {}
    paid_major: dict[str, set[str]] = {}
    for row in verdicts:
        task = str(row.get("task"))
        for f in (row.get("findings") or []):
            if not isinstance(f, dict) or not f.get("file"):
                continue
            name = str(f["file"]).lstrip("./")
            paid_files.setdefault(task, set()).add(name)
            if f.get("severity") == "major":
                paid_major.setdefault(task, set()).add(name)

    print("=== присяжные: годность вызова ===")
    print(f"{'модель/линза':38} {'вызовов':>7} {'пусто':>6} {'обрыв':>6} "
          f"{'канд':>5} {'мимо':>5} {'с/выз':>6}")
    per_model: dict[str, dict[str, float]] = collections.defaultdict(
        lambda: collections.defaultdict(float))
    for r in raw:
        key = f"{r['model']}/{r['lens']}"
        m = per_model[key]
        m["calls"] += 1
        m["wall"] += r.get("wall_s", 0.0)
        m["parsed"] += r.get("parsed", 0)
        if r.get("refusal"):
            m["refused"] += 1
        if not r.get("parsed"):
            m["empty"] += 1
    # «мимо диффа» считается в replay.py на этапе разбора; берём из свода
    by_model_sum = summary.get("by_model", {})
    for key, m in sorted(per_model.items()):
        model = key.split("/")[0]
        s = by_model_sum.get(model, {})
        print(f"{key:38} {int(m['calls']):>7} {int(m['empty']):>6} "
              f"{int(m['refused']):>6} {int(m['parsed']):>5} "
              f"{int(s.get('off_diff', 0)):>5} "
              f"{m['wall'] / max(m['calls'], 1):>6.1f}")

    print("\n=== объём на адъюдикатора ===")
    tasks = summary.get("by_task", [])
    if tasks:
        merged = [t["merged"] for t in tasks]
        merged.sort()
        avg = sum(merged) / len(merged)
        print(f"диффов: {len(merged)}; после дедупа кандидатов на дифф — "
              f"среднее {avg:.1f}, медиана {merged[len(merged) // 2]}, "
              f"максимум {merged[-1]}")
        over = [t["task"] for t in tasks if t["merged"] >= 10]
        print(f"ворота §4 (< 10 на дифф): "
              f"{'ПРОЙДЕНЫ' if not over else 'НЕ пройдены на ' + ', '.join(over)}")
    sev = collections.Counter(c["severity"] for c in cands)
    print(f"тяжесть по мнению присяжных: {dict(sev)}")

    print("\n=== совпадение по адресу с дорогим ревьюером ===")
    hit = miss = extra = 0
    major_hit = major_total = 0
    for t in tasks:
        task = t["task"]
        named = {c["file"] for c in cands if c["task"] == task}
        paid = paid_files.get(task, set())
        hit += len(named & paid)
        miss += len(paid - named)
        extra += len(named - paid)
        pm = paid_major.get(task, set())
        major_total += len(pm)
        major_hit += len(named & pm)
    print(f"файлов назвал дорогой ревьюер: {hit + miss}; из них панель "
          f"назвала тоже: {hit} ({hit / max(hit + miss, 1) * 100:.0f}%)")
    print(f"файлов панель назвала сверх дорогого: {extra}")
    print(f"файлы с major дорогого ревьюера: {major_total}; накрыты "
          f"панелью: {major_hit}")
    print("\nЭто НЕ recall находок: совпадает адрес, а не претензия. "
          "Recall по одобренным ярлыкам из коммитов не измерим "
          "(findings E12/goldset-recall).")

    print("\n=== цена ===")
    calls = sum(int(m["calls"]) for m in per_model.values())
    wall = sum(m["wall"] for m in per_model.values())
    # Параллельный дифф стоит НЕ среднего вызова, а САМОГО МЕДЛЕННОГО
    # присяжного: панель отдаёт материал, когда ответил последний.
    slowest = max((m["wall"] / max(m["calls"], 1) for m in per_model.values()),
                  default=0.0)
    print(f"вызовов: {calls}; метрируемых долларов: $0.00 (подписка); "
          f"настенных секунд подряд: {wall:.0f} "
          f"({wall / max(len(tasks), 1):.0f} с на дифф последовательно; "
          f"параллельно дифф ограничен самым медленным присяжным — "
          f"{slowest:.1f} с)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
