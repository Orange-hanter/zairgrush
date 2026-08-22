#!/usr/bin/env python3
"""Реплей отказов формы вердикта: спасение суждения из потока.

Замерено на E13 (2026-08-22): 5 вызовов ревьюера из 17 не дали валидного
вердикта и сожгли $0.67 — 37 % всех денег ревью и больше, чем всё
расстояние между сравниваемыми плечами. Два режима, оба на
claude-sonnet-5 при effort=medium:

1. ЗАГЛУШКА — `{"analysis": "Test", "verdict": "approve", …}`. Схему
   проходит, смысл нет; ловит порог существенности (MIN_ANALYSIS,
   MIN_SUMMARY). Чинить нечего: вердикта не было.
2. ВЕРДИКТ В ОДНОМ ПОЛЕ — модель кладёт весь ответ в `analysis`, размечая
   остальные поля тегами (`</analysis><verdict>approve</verdict>…`).
   Провайдер отклоняет форму, модель на каждом ретрае переписывает ПРОЗУ,
   а не форму, ретраи кончаются, конверт приходит пустым. Суждение при
   этом лежит в потоке целиком — и было оплачено.

Набор заморожен из НАСТОЯЩИХ полезных нагрузок этих отказов плюс
синтетические негативы: спасение обязано отказывать там, где чинить
нечего или починенное негодно.

Стендов не требует — payload'ы лежат рядом в cases.jsonl, поэтому набор
гоняется в общем гейте (tests/test_verdict_salvage.py).

Запуск: python3 replay.py
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SWARM = HERE.parent.parent.parent / "tools" / "swarm" / "swarm"
CASES = HERE / "cases.jsonl"
EXPECTED_CASES = 8


def _load(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, SWARM / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def cases():
    rows = []
    for line in CASES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def measure():
    """Прогон набора: список результатов, один на кейс.

    Источник истины делится со скриптом и тестом — печать отдельно от
    замера (урок E12: тест и скрипт, считающие по-разному, однажды
    разойдутся, и разойдутся молча).
    """
    parsing = _load("parsing")
    verdicts = _load("verdicts")
    out = []
    for case in cases():
        payload = case["payload"]
        expect = case["expect"]
        fixed = parsing.repair_verdict(payload)
        verdict = fixed if fixed is not None else payload
        valid = verdicts.validate_verdict(verdict)
        problem = verdicts.verdict_problem(verdict)
        checks = {"repaired": (fixed is not None) == expect["repaired"],
                  "valid": valid == expect["valid"]}
        if "verdict" in expect:
            checks["verdict"] = verdict.get("verdict") == expect["verdict"]
        if "findings" in expect:
            checks["findings"] = (
                len(verdict.get("findings") or []) == expect["findings"])
        if "problem_has" in expect:
            checks["problem"] = expect["problem_has"] in (problem or "")
        out.append({"id": case["id"], "real": case.get("real", False),
                    "ok": all(checks.values()), "checks": checks,
                    "repaired": fixed is not None, "valid": valid,
                    "problem": problem, "source": case["source"]})
    return out


def main():
    results = measure()
    if len(results) != EXPECTED_CASES:
        print(f"НАБОР ИЗМЕНИЛСЯ: кейсов {len(results)}, ждали "
              f"{EXPECTED_CASES}", file=sys.stderr)
        return 2
    width = max(len(r["id"]) for r in results)
    for r in results:
        mark = "  ok " if r["ok"] else "ПРОВАЛ"
        flag = "живой" if r["real"] else "синт."
        print(f"[{mark}] {r['id']:{width}}  {flag}  "
              f"чинили={r['repaired']!s:5} годен={r['valid']!s:5} "
              f"{r['problem'] or ''}")
        if not r["ok"]:
            print(f"          не сошлось: {r['checks']}")
    real = [r for r in results if r["real"]]
    saved = [r for r in real if r["repaired"] and r["valid"]]
    print(f"\nсошлось {sum(1 for r in results if r['ok'])}/{len(results)}; "
          f"из {len(real)} настоящих отказов спасено {len(saved)}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
