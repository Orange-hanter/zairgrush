#!/usr/bin/env python3
"""E11: два автора тестов на одном наборе модулей — что поймал каждый.

Сравнение обязано быть ПАРНЫМ по модулям и по глубине истории: плечо A
берётся на том коммите, до которого доходит плечо B, иначе в пользу A
работают тесты, написанные ПОЗЖЕ и для других задач (замерено: на
финальном состоянии стенда выживаемость 25 %, на нужном коммите — 31 %,
и разницу дал чужой `test_errors.py`).

Печатает счёт по модулям и — главное — списки выживших рядом. Доля
говорит, СКОЛЬКО подмен прошло; списки говорят, КАКИЕ.

И печатает ВТОРОЙ счёт — против спецификации. Сырая выживаемость
считает дырой всё, что выжило, а выжить мутант может по трём разным
причинам, и только одна из них про тесты:

  real_gap    — тест мог поймать и не поймал. Это и есть дыра;
  equivalent  — поведение не изменилось, убить нельзя ничем;
  spec_silent — спецификация о таком не говорит, и тест, написанный по
                ней честно, обязан пропустить.

Третий класс — сердце E11. Плечо A пишет тесты, ЧИТАЯ свою реализацию,
и закрепляет числа, которых в спеке нет (код выхода 2, размер кэша);
плечо B пишет по одной спеке и такие места объявляет в поле unclear.
Считать первое победой — значит награждать тесты за то, что они
детекторы изменений, а не проверки поведения. Поэтому мутанты классов
equivalent и spec_silent убираются из знаменателя ОБОИХ плеч, и убираются
вместе с пойманными: иначе у одного плеча вычиталась бы только числитель.

Классификация — суждение, и потому лежит данными (goldset/e11/
classification.jsonl), а не в коде: у каждой строки есть причина, и с
ней можно не согласиться, не переписывая инструмент. Утверждения об
эквивалентности проверены перебором входов, а не объявлены.

Запуск:
    python3 e11-compare.py armA.jsonl armB.jsonl [classification.jsonl]
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


# Набор лежит в git, а не в gitignore'd experiments/bench/: замер, чьи
# входные данные не пережили чистку рабочего каталога, невоспроизводим —
# и его число превращается в фольклор. 64 строки, цена хранения нулевая.
GOLDSET = pathlib.Path(__file__).resolve().parent.parent / "goldset" / "e11"
DEFAULT_CLASSES = GOLDSET / "classification.jsonl"
DEFAULT_ARMS = (GOLDSET / "armA-mutants.jsonl", GOLDSET / "armB-mutants.jsonl")


def load_classes(path):
    """Классификация как {(плечо, файл, строка, вид, до, после): строка}."""
    out = {}
    if not path or not pathlib.Path(path).exists():
        return out
    for row in load(path):
        out[(row["arm"], row["file"], row["line"], row["kind"],
             row["before"], row["after"])] = row
    return out


def spec_score(name, arm, rows, classes):
    """Счёт против спецификации: без неубиваемых и без неоговорённых.

    Возвращает (осталось, выжило, снятые). Снимаются и пойманные тоже —
    выбрасывать из знаменателя только выживших значило бы дарить очко
    тому плечу, у которого таких позиций больше.
    """
    kept, alive, dropped = 0, 0, []
    for r in rows:
        hit = classes.get((arm, r["file"], r["line"], r["kind"],
                           r["before"], r["after"]))
        if hit:
            dropped.append((hit, r))
            continue
        kept += 1
        alive += int(bool(r["survived"]))
    return kept, alive, dropped


def measure(a_path=None, b_path=None, classes_path=None):
    """Числа замера без печати — чтобы тест и скрипт делили один источник.

    Возвращает {'A': (всего, выжило, по_спеке_всего, по_спеке_выжило),
    'B': то же}. Разведение замера и печати — то же решение, что в
    реплее границ (E12): пока число жило внутри функции печати, тест мог
    проверять только текст.
    """
    a_rows = load(a_path or DEFAULT_ARMS[0])
    b_rows = load(b_path or DEFAULT_ARMS[1])
    classes = load_classes(classes_path or DEFAULT_CLASSES)
    out = {}
    for arm, rows in (("A", a_rows), ("B", b_rows)):
        kept, alive, _dropped = spec_score(arm, arm, rows, classes)
        out[arm] = (len(rows), sum(1 for r in rows if r["survived"]),
                    kept, alive)
    return out


def main():
    if len(sys.argv) not in (1, 3, 4):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    a_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ARMS[0]
    b_path = sys.argv[2] if len(sys.argv) > 1 else DEFAULT_ARMS[1]
    a_rows, b_rows = load(a_path), load(b_path)
    classes = load_classes(sys.argv[3] if len(sys.argv) == 4
                           else DEFAULT_CLASSES)
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

    if not classes:
        print("\n  Классификации нет — счёта против спецификации не будет.")
        return 0

    print("\n\n=== СЧЁТ ПРОТИВ СПЕЦИФИКАЦИИ ===")
    print("  снято то, что тест по спеке поймать не мог: неубиваемое "
          "(equivalent)\n  и неоговорённое (spec_silent) — у обоих плеч и "
          "вместе с пойманным.")
    res = {}
    for label, arm, rows, raw_tot, raw_alive in (
            ("A (тесты писал исполнитель)", "A", a_rows, a_tot, a_n),
            ("B (тесты писал тестировщик)", "B", b_rows, b_tot, b_n)):
        kept, alive, dropped = spec_score(label, arm, rows, classes)
        res[arm] = (kept, alive)
        by_class = collections.Counter(h["class"] for h, _r in dropped)
        surv_dropped = sum(1 for _h, r in dropped if r["survived"])
        print(f"\n  плечо {label}")
        print(f"    сырое:   {raw_alive} из {raw_tot} "
              f"({100 * raw_alive / raw_tot if raw_tot else 0:.0f} %)")
        print(f"    снято:   {len(dropped)} "
              f"({', '.join(f'{k}={v}' for k, v in sorted(by_class.items()))}"
              f"; из них выживших {surv_dropped})")
        print(f"    по спеке: {alive} из {kept} "
              f"({100 * alive / kept if kept else 0:.0f} %) — вот это дыры")
        for hit, r in sorted(dropped, key=lambda t: (t[1]["file"],
                                                     t[1]["line"])):
            mark = "выжил " if r["survived"] else "пойман"
            print(f"      [{hit['class']:11}] {mark} "
                  f"{r['file']}:{r['line']} {r['before']} -> {r['after']}")
    (ak, aa), (bk, ba) = res["A"], res["B"]
    print(f"\n  ИТОГ по спеке: A {100 * aa / ak if ak else 0:.0f} % против "
          f"B {100 * ba / bk if bk else 0:.0f} % "
          f"(сырьё было {100 * a_n / a_tot if a_tot else 0:.0f} % против "
          f"{100 * b_n / b_tot if b_tot else 0:.0f} %).")
    # Падение долей считается, а не пишется: прежняя редакция держала
    # числа раунда 1 в строке, и перезапуск (раунд 2) печатал бы чужой
    # вывод. Правило «число сначала считают, потом называют» — из того
    # же замера, где тест поймал «обе упали больше чем вдвое».
    a_drop = 100 * (1 - (aa / ak) / (a_n / a_tot)) if ak and a_n else 0.0
    b_drop = 100 * (1 - (ba / bk) / (b_n / b_tot)) if bk and b_n else 0.0
    print(f"  Падение от сырья к счёту по спеке: A на {a_drop:.0f} %, "
          f"B на {b_drop:.0f} %. Сырая выживаемость меряла\n  не только "
          "тесты, но и молчание спецификации с щедростью генератора\n  "
          "мутантов — сравнивать плечи надо после разбора, не до.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
