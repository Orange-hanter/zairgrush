#!/usr/bin/env python3
"""Judge for D2 — pattern recognition. Reads answer.json, prints score JSON."""
import json
import pathlib
import sys

TRUTH = {
    # s1: EventBus — observer. App берёт на себя users/orders/payments/sessions/
    # админку/дайджест — god-object. Ловушка: singleton тут НЕТ.
    "s1": {"patterns": {"observer"}, "antipatterns": {"god-object"}},
    # s2: выбор поведения по типу — strategy (пусть и на if'ах);
    # строковая диспетчеризация — stringly-typed; три одинаковых if-elif —
    # copy-paste. Ловушка: factory тут нет.
    "s2": {"patterns": {"strategy"}, "antipatterns": {"stringly-typed", "copy-paste"}},
    # s3: пул соединений — object-pool. Ловушки: это не singleton (пул ≠ один
    # инстанс) и не facade (ReportService — обычный сервис, не упрощённый
    # интерфейс к подсистеме).
    "s3": {"patterns": {"object-pool"}, "antipatterns": set()},
}

def f1(tp, fp, fn):
    if tp == 0 and fp == 0 and fn == 0:
        return 1.0  # пустой ответ против пустой истины — точное попадание
    if tp == 0:
        return 0.0
    p, r = tp / (tp + fp), tp / (tp + fn)
    return 2 * p * r / (p + r)

def main():
    workdir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(".")
    try:
        ans = json.loads((workdir / "answer.json").read_text())
    except Exception as e:
        print(json.dumps({"passed": 0, "failed": 1, "errors": 1, "score": 0.0,
                          "details": f"answer.json unreadable: {e}"}))
        return
    tp = fp = fn = 0
    scores = []
    for sid, truth in TRUTH.items():
        got = ans.get(sid) or {}
        for kind in ("patterns", "antipatterns"):
            g = set(got.get(kind) or [])
            t = truth[kind]
            tp += len(g & t)
            fp += len(g - t)
            fn += len(t - g)
            scores.append(f1(len(g & t), len(g - t), len(t - g)))
    score = round(sum(scores) / len(scores), 3)
    print(json.dumps({"passed": tp, "failed": fp + fn, "errors": 0,
                      "score": score,
                      "details": f"tp={tp} fp={fp} fn={fn} meanF1={score}"}))

if __name__ == "__main__":
    main()
