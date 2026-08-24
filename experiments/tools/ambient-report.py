#!/usr/bin/env python3
"""Разбор фонового замера: что накопилось в обычной работе.

Читает журналы и метрики стендов, собирает задачи по плечам фактора и
печатает сравнение. Три правила, каждое куплено ошибкой.

1. ОТКАЗЫВАЕТСЯ объявлять победителя ниже порога. Весь урок 2026-08-24
   в том, что процент от одной-двух задач — не число, а фольклор: на
   одной задаче счётчик раундов решала гигиена линтера, а не изучаемый
   фактор. Порог по умолчанию — 8 задач в КАЖДОМ плече, и он назван в
   выводе, а не спрятан.

2. ПЕРЕСЧИТЫВАЕТ жребий из id задач и сверяет с записанным. Журнал —
   данные, а не истина; если запись расходится с пересчётом, значит
   поменялся сид или фактор, и складывать такие наблюдения в одну
   выборку нельзя. Инструмент, который не проверяет собственный вход,
   меряет себя.

3. Печатает РАЗБРОС, а не только среднее. Средняя стоимость задачи по
   трём наблюдениям с разбросом в три раза — не сравнение.

Запуск:
    python3 ambient-report.py ~/work/zeus-pilot [ещё стенды...]
    python3 ambient-report.py --min-n 5 ~/work/zeus-pilot
"""
import argparse
import collections
import importlib.util
import json
import math
import pathlib
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
SWARM = HERE.parent.parent / "tools" / "swarm" / "swarm"
sys.path.insert(0, str(SWARM))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SWARM / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def rows(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def collect(stand, ambient):
    """Наблюдения одного стенда: по задаче — плечо, цена, раунды, исход."""
    swarm_dir = stand / ".swarm"
    journal = (rows(swarm_dir / "log" / "run.jsonl")
               or rows(swarm_dir / "run.jsonl"))
    metrics = rows(swarm_dir / "metrics.jsonl")

    arms, mismatched = {}, []
    for r in journal:
        if r.get("kind") != "ambient_arm":
            continue
        tid = str(r.get("task"))
        arms[tid] = {"arm": r.get("ambient_arm"),
                     "factor": r.get("ambient_factor"),
                     "seed": r.get("ambient_seed")}
    # Пересчёт жребия — проверка входа, а не украшение.
    for tid, rec in arms.items():
        cfg = {"experiments": {"ambient": rec["factor"],
                               "ambient_seed": rec["seed"]}}
        try:
            if ambient.arm(cfg, tid) != rec["arm"]:
                mismatched.append(tid)
        except ValueError:
            mismatched.append(tid)

    cost = collections.defaultdict(float)
    rounds = collections.defaultdict(int)
    findings = collections.defaultdict(int)
    for r in metrics:
        tid = str(r.get("task") or "")
        if tid not in arms:
            continue
        if isinstance(r.get("cost_usd"), (int, float)):
            cost[tid] += float(r["cost_usd"])
        if r.get("phase") == "review" and r.get("iter"):
            rounds[tid] = max(rounds[tid], int(r["iter"]))
        if isinstance(r.get("findings"), int):
            findings[tid] += r["findings"]

    tasks_path = swarm_dir / "tasks.json"
    status = {}
    if tasks_path.exists():
        for t in json.loads(tasks_path.read_text(encoding="utf-8"))["tasks"]:
            status[str(t.get("id"))] = t.get("status")

    obs = []
    for tid, rec in arms.items():
        obs.append({"stand": stand.name, "task": tid, **rec,
                    "cost": cost.get(tid, 0.0), "rounds": rounds.get(tid, 0),
                    "findings": findings.get(tid, 0),
                    "status": status.get(tid)})
    return obs, mismatched


def spread(values):
    if not values:
        return "—"
    if len(values) == 1:
        return f"{values[0]:.2f}"
    return (f"{statistics.mean(values):.2f} "
            f"±{statistics.stdev(values):.2f} "
            f"[{min(values):.2f}…{max(values):.2f}]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stands", nargs="+")
    ap.add_argument("--min-n", type=int, default=8,
                    help="сколько задач в КАЖДОМ плече нужно, чтобы "
                         "инструмент вообще сравнивал (умолчание 8)")
    args = ap.parse_args()
    ambient = _load("ambient")

    all_obs, bad = [], []
    for raw in args.stands:
        stand = pathlib.Path(raw).expanduser().resolve()
        obs, mism = collect(stand, ambient)
        all_obs += obs
        bad += [(stand.name, t) for t in mism]
        print(f"{stand.name}: наблюдений {len(obs)}")

    # Предупредить и всё равно посчитать — значит противоречить себе в
    # соседних строках. Непересчитываемое наблюдение ВЫБРАСЫВАЕТСЯ из
    # сравнения, и число выброшенных называется: молча уменьшившаяся
    # выборка — та же ложь, что молча увеличившаяся.
    suspect = {(stand, tid) for stand, tid in bad}
    if bad:
        print(f"\nВНИМАНИЕ: у {len(bad)} задач записанное плечо не "
              f"пересчитывается из (сид, фактор, id) — вероятно, менялся "
              f"сид. Из сравнения ИСКЛЮЧЕНЫ:")
        for stand, tid in bad[:10]:
            print(f"    {stand}/{tid}")
        if len(bad) > 10:
            print(f"    …и ещё {len(bad) - 10}")

    by_factor = collections.defaultdict(lambda: collections.defaultdict(list))
    for o in all_obs:
        if (o["stand"], o["task"]) in suspect:
            continue
        by_factor[o["factor"]][o["arm"]].append(o)

    if not by_factor:
        print("\nфоновых наблюдений нет: фактор не включён ни на одном стенде")
        return 0

    for factor, arms in sorted(by_factor.items()):
        on, off = arms.get("on", []), arms.get("off", [])
        print(f"\n=== фактор {factor}: on {len(on)} задач, "
              f"off {len(off)} задач ===")
        print(f"{'метрика':14} {'плечо on':>26} {'плечо off':>26}")
        for label, key in (("$ за задачу", "cost"), ("раундов", "rounds"),
                           ("находок", "findings")):
            a = [float(o[key]) for o in on]
            b = [float(o[key]) for o in off]
            print(f"{label:14} {spread(a):>26} {spread(b):>26}")
        for arm_name, group in (("on", on), ("off", off)):
            mix = collections.Counter(o["status"] for o in group)
            print(f"  исходы {arm_name:3}: "
                  + (", ".join(f"{k}={v}" for k, v in sorted(
                      mix.items(), key=lambda kv: str(kv[0]))) or "—"))

        n = min(len(on), len(off))
        if n < args.min_n:
            need = args.min_n - n
            print(f"\n  ВЫВОДА НЕТ: в меньшем плече {n} задач(и), порог "
                  f"{args.min_n}. Нужно ещё минимум {need} задач(и) в нём.")
            print("  Числа выше — для чтения глазами, не для решения. "
                  "Выборка НЕПАРНАЯ:\n  в плечах разные задачи, и разброс "
                  "между задачами тут не вычитается.")
            continue
        ca, cb = [o["cost"] for o in on], [o["cost"] for o in off]
        ra, rb = [o["rounds"] for o in on], [o["rounds"] for o in off]
        dc = statistics.mean(ca) - statistics.mean(cb)
        dr = statistics.mean(ra) - statistics.mean(rb)
        # Грубая оценка «шире ли разница разброса»: не тест значимости, и
        # назван так прямо. Настоящий тест на таких n был бы точностью,
        # которой в данных нет.
        pooled = math.sqrt((statistics.pstdev(ra) ** 2
                            + statistics.pstdev(rb) ** 2) / 2) or 1e-9
        print(f"\n  порог пройден (n={n}): цена {dc:+.2f} $/задача, "
              f"раунды {dr:+.2f}")
        print(f"  разница раундов в долях общего разброса: "
              f"{dr / pooled:+.2f} — это НЕ тест значимости, а грубая "
              f"мера того,\n  сравнима ли разница с обычным разбросом "
              f"между задачами.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
