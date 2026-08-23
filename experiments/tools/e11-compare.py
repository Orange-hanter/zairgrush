#!/usr/bin/env python3
"""E11: два автора тестов на одном наборе модулей — что поймал каждый.

Сравнение обязано быть ПАРНЫМ по модулям и по глубине истории: плечо A
берётся на том коммите, до которого доходит плечо B, иначе в пользу A
работают тесты, написанные ПОЗЖЕ и для других задач (замерено: на
финальном состоянии стенда выживаемость 25 %, на нужном коммите — 31 %,
и разницу дал чужой `test_errors.py`).

Печатает счёт по модулям и — главное — списки выживших рядом. Доля
говорит, СКОЛЬКО подмен прошло; списки говорят, КАКИЕ, а различить дыру
и эквивалентного мутанта может только человек.

Запуск:
    python3 e11-compare.py armA.jsonl armB.jsonl
"""
import collections
import json
import pathlib
import sys


def load(path):
    rows = []
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def key(row):
    """Мутант как позиция: файл + строка + подмена."""
    return (row["file"], row["line"], row["kind"], row["before"], row["after"])


def summarise(name, rows):
    by_mod = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        mod = r["file"].split("/")[-1]
        by_mod[mod][0] += 1
        by_mod[mod][1] += int(bool(r["survived"]))
    total = len(rows)
    alive = sum(1 for r in rows if r["survived"])
    print(f"\n=== {name} ===")
    print(f"{'модуль':16} {'мутантов':>9} {'выжило':>7} {'доля':>7}")
    for mod in sorted(by_mod):
        n, s = by_mod[mod]
        print(f"{mod:16} {n:>9} {s:>7} {100 * s / n:>6.0f} %")
    print(f"{'ИТОГО':16} {total:>9} {alive:>7} "
          f"{(100 * alive / total if total else 0):>6.0f} %")
    return {key(r) for r in rows if r["survived"]}, total, alive


def main():
    if len(sys.argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    a_rows, b_rows = load(sys.argv[1]), load(sys.argv[2])
    a_alive, a_tot, a_n = summarise("плечо A — тесты писал исполнитель", a_rows)
    b_alive, b_tot, b_n = summarise("плечо B — тесты писал тестировщик", b_rows)

    # Мутанты порождаются из КОДА, а код у плеч разный: сравнивать
    # позиции построчно нельзя, сравниваются доли и содержание списков.
    print("\n=== разница ===")
    print(f"  выживаемость: {100 * a_n / a_tot if a_tot else 0:.0f} % -> "
          f"{100 * b_n / b_tot if b_tot else 0:.0f} %")
    print(f"  мутантов:     {a_tot} -> {b_tot} "
          f"(разный код — сравнивать надо доли, не числа)")

    both = a_alive & b_alive
    if both:
        print(f"\n  ВЫЖИЛИ У ОБОИХ ({len(both)}) — общая слепая зона, "
              f"авторство ни при чём:")
        for f, line, kind, before, after in sorted(both):
            print(f"    {f}:{line} {kind} {before} -> {after}")
    only_a = a_alive - b_alive
    only_b = b_alive - a_alive
    if only_a:
        print(f"\n  ВЫЖИЛИ ТОЛЬКО У A ({len(only_a)}) — это и ловит "
              f"независимый автор:")
        for f, line, kind, before, after in sorted(only_a):
            print(f"    {f}:{line} {kind} {before} -> {after}")
    if only_b:
        print(f"\n  ВЫЖИЛИ ТОЛЬКО У B ({len(only_b)}) — цена независимости: "
              f"чего не видно из одной спецификации:")
        for f, line, kind, before, after in sorted(only_b):
            print(f"    {f}:{line} {kind} {before} -> {after}")
    print("\n  Выживший — не обязательно дыра: часть подмен поведения не "
          "меняет.\n  Списки выше существуют затем, чтобы это решал "
          "человек, а не доля.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
