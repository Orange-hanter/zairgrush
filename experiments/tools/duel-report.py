#!/usr/bin/env python3
"""Разбор дуэлей: парные наблюдения, снятые на обычной работе.

Дуэль (`swarm/duel.py`) прогоняет два плеча ОДНОВРЕМЕННО на одной задаче,
поэтому наблюдение здесь ПАРНОЕ: сравнивать надо разницу внутри задачи, а
не средние по группам. Разброс между задачами — главный шум фонового
жребия — тут вычитается сам.

## Что инструмент честно НЕ умеет, и это свойство дуэли, а не разбора

Теневое плечо не ревьюится. Живое проходит весь путь — гейт, границы,
вердикт ревьюера; теневое доходит только до собственного гейта, и дальше
его работа выбрасывается. Значит сравнивать можно:

  - прошла ли работа плеча СВОЙ гейт;
  - сколько плечо написало (файлы и строки);
  - дошло ли плечо до отчёта вообще.

И НЕЛЬЗЯ — качество вердикта, число находок ревьюера, раунды до
сходимости: у теневого плеча всего этого нет по устройству. Кто захочет
их сравнить, тому нужен второй ревьюер на теневой дифф, а это ещё одна
дорогая роль и отдельное решение.

## Почему инструмент отказывается делать вывод на малом n

Ровно тот урок, что стоил программе двух платных стендов: доля от одной
задачи — не число, а фольклор. Паре хватает меньшего n, чем непарной
выборке, но не единицы. Порог назван и печатается.

Запуск:
    python3 duel-report.py ~/work/zeus-pilot [ещё стенды...]
"""
import argparse
import collections
import json
import pathlib
import sys


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


def collect(stand):
    swarm = stand / ".swarm"
    journal = (rows(swarm / "log" / "run.jsonl") or rows(swarm / "run.jsonl"))
    metrics = rows(swarm / "metrics.jsonl")

    duels = {}
    for r in journal:
        kind, tid = r.get("kind"), str(r.get("task") or "")
        if kind == "duel_start":
            duels.setdefault(tid, {})["start"] = r
        elif kind == "duel_shadow":
            duels.setdefault(tid, {})["shadow"] = r
        elif kind == "duel_shadow_failed":
            duels.setdefault(tid, {})["broken"] = r

    live_gate, live_impl, cost = {}, {}, collections.defaultdict(float)
    for r in metrics:
        tid = str(r.get("task") or "")
        if isinstance(r.get("cost_usd"), (int, float)):
            cost[tid] += float(r["cost_usd"])
        if r.get("phase") == "gate" and tid:
            # Последний гейт задачи — тот, с которым она закрылась.
            live_gate[tid] = bool(r.get("ok"))
        if r.get("phase") == "implement" and tid:
            live_impl[tid] = r

    status = {}
    tp = swarm / "tasks.json"
    if tp.exists():
        for t in json.loads(tp.read_text(encoding="utf-8"))["tasks"]:
            status[str(t.get("id"))] = t.get("status")

    out = []
    for tid, d in duels.items():
        start = d.get("start") or {}
        shadow = d.get("shadow") or {}
        out.append({
            "stand": stand.name, "task": tid,
            "factor": start.get("ambient_factor") or start.get("factor"),
            "live_arm": start.get("live_arm"),
            "shadow_arm": start.get("shadow_arm"),
            "live_report": bool(live_impl.get(tid, {}).get("report")),
            "live_gate": live_gate.get(tid),
            "live_status": status.get(tid),
            "shadow_report": shadow.get("report"),
            "shadow_gate": shadow.get("gate"),
            "shadow_diff": shadow.get("diff") or {},
            "shadow_broken": bool(d.get("broken")),
            "cost": round(cost.get(tid, 0.0), 2),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stands", nargs="+")
    ap.add_argument("--min-n", type=int, default=5,
                    help="сколько ПАР нужно, чтобы инструмент делал вывод "
                         "(умолчание 5: паре хватает меньшего n, чем "
                         "непарной выборке, но не единицы)")
    args = ap.parse_args()

    obs = []
    for raw in args.stands:
        stand = pathlib.Path(raw).expanduser().resolve()
        got = collect(stand)
        obs += got
        print(f"{stand.name}: дуэлей {len(got)}")

    if not obs:
        print("\nдуэлей нет: фактор не включён ни на одном стенде")
        return 0

    # НЕЗАКОНЧЕННАЯ дуэль — не сломанная. Пока задача в работе, теневого
    # ряда в журнале ещё нет, и звать это поломкой прибора значит
    # клеветать на живой прогон (поймано на первом же чтении).
    running = [o for o in obs
               if not o["shadow_broken"] and o["shadow_report"] is None
               and o["live_status"] == "in_progress"]
    broken = [o for o in obs if o["shadow_broken"]
              or (o["shadow_report"] is None and o not in running)]
    usable = [o for o in obs if o not in broken and o not in running]

    print(f"\n{'задача':10} {'фактор':17} {'живое':>16} {'теневое':>22} "
          f"{'$':>7}")
    for o in sorted(obs, key=lambda x: x["task"]):
        live = (f"{o['live_arm']}: "
                f"{'отчёт' if o['live_report'] else 'НЕТ'}"
                f"/{'гейт+' if o['live_gate'] else 'гейт-'}")
        if o["shadow_broken"]:
            shadow = f"{o['shadow_arm']}: ПРИБОР СЛОМАН"
        elif o["shadow_report"] is None:
            shadow = (f"{o['shadow_arm']}: в работе"
                      if o["live_status"] == "in_progress"
                      else f"{o['shadow_arm']}: нет данных")
        else:
            d = o["shadow_diff"]
            shadow = (f"{o['shadow_arm']}: "
                      f"{'отчёт' if o['shadow_report'] else 'НЕТ'}"
                      f"/{'гейт+' if o['shadow_gate'] else 'гейт-'}"
                      f" {d.get('files', 0)}ф +{d.get('added', 0)}")
        print(f"{o['task']:10} {str(o['factor']):17} {live:>16} "
              f"{shadow:>22} {o['cost']:>7.2f}")

    if running:
        print(f"\n  ещё в работе: {len(running)} — дуэль не закончилась, "
              f"это не поломка")
    if broken:
        print(f"\n  приборов сломано: {len(broken)} — эти задачи в счёт "
              f"пар не идут")

    print(f"\n=== пар пригодных: {len(usable)} ===")
    if len(usable) < args.min_n:
        print(f"  ВЫВОДА НЕТ: порог {args.min_n} пар, есть {len(usable)}. "
              f"Нужно ещё {args.min_n - len(usable)}.")
        print("  Строки выше — наблюдения, а не результат. Доля от одной-двух\n"
              "  задач уже дважды оказывалась фольклором (E9, плечо "
              "исполнителя).")
        return 0

    agree = sum(1 for o in usable if o["live_gate"] == o["shadow_gate"])
    print(f"  гейт совпал у {agree} из {len(usable)} пар")
    for arm in ("on", "off"):
        passed = [o for o in usable
                  if (o["live_gate"] if o["live_arm"] == arm
                      else o["shadow_gate"])]
        print(f"  плечо {arm:3}: гейт прошло {len(passed)} из {len(usable)}")
    print("\n  Напоминание: теневое плечо НЕ ревьюится. Находки, вердикт и "
          "раунды\n  до сходимости у него отсутствуют по устройству — "
          "сравнивать их нечем.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
