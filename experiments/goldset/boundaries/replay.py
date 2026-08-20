#!/usr/bin/env python3
"""Замер линтера границ на семи признанных спорах PILOT-1.

Ground truth здесь — решения владельца: каждый из семи споров исполнителя
был признан, и в каждом человек ДОБАВИЛ в `paths` конкретный файл.
Вопрос замера ровно один: назвал бы линтер этот файл заранее, до прогона?

Две ловушки, обе стоили ложного результата в первой попытке и обе
закрыты здесь:

1. В `.swarm/tasks.json` лежат paths ПОСЛЕ решения человека — спорный
   файл уже внутри границ, и линтер по построению не может назвать его
   спутником. Замер обязан ВОССТАНОВИТЬ до-спорную границу: вычесть
   ровно то, что человек добавил (`added` в кейсе).
2. Репозиторий стенда с тех пор изменился: работа сделана, ссылки
   появились. Сегодняшнее число — верхняя оценка того, что линтер знал
   бы в день планирования.

Стенд в git не входит (воспроизводимый scratch, §1.4 06-дока): без него
скрипт честно говорит, что мерить нечего, и выходит с кодом 0.
"""
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
PLANNER = REPO / "tools" / "swarm" / "swarm" / "planner.py"
STAND = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                     else pathlib.Path.home() / "work" / "zeus-pilot")


def load_planner():
    sys.path.insert(0, str(PLANNER.parent))
    spec = importlib.util.spec_from_file_location("planner", PLANNER)
    if spec is None or spec.loader is None:          # pragma: no cover
        raise SystemExit(f"не читается {PLANNER}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["planner"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    cases = [json.loads(ln) for ln in
             (HERE / "cases.jsonl").read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    tasks_file = STAND / ".swarm" / "tasks.json"
    if not tasks_file.exists():
        print(f"стенд {STAND} недоступен — замер пропущен "
              f"(стенды в git не входят; путь можно передать аргументом)")
        return 0
    pl = load_planner()
    raw = json.loads(tasks_file.read_text(encoding="utf-8"))
    paths_by_id = {t["id"]: list(t.get("paths") or [])
                   for t in raw.get("tasks", [])}
    protected = ["zeus/tests/**", "**/tests/**", "*.toml", "zeus/Cargo.lock"]

    ranks: dict[str, int | None] = {}
    for case in cases:
        before = [p for p in paths_by_id.get(case["task"], [])
                  if p not in case["added"]]
        warns = pl.boundary_warnings(STAND, {"paths": before}, protected,
                                     limit=25)
        wanted = set(case["wanted"])
        folders = {str(pathlib.PurePosixPath(w).parent) + "/" for w in wanted}
        pos = next((i + 1 for i, w in enumerate(warns)
                    if w["file"] in wanted or w["file"] in folders), None)
        ranks[case["qid"]] = pos
        was = case.get("rank_2026_08_20")
        drift = "" if pos == was else f"  (было {was})"
        print(f"{case['qid']} ({case['task']}): "
              f"{'место ' + str(pos) if pos else 'не назван'}"
              f" из {len(warns)} улик{drift}   {case['klass']}")

    cap = pl._LIMIT
    caught = sum(1 for p in ranks.values() if p and p <= cap)
    print(f"\nпри потолке {cap}: {caught}/{len(cases)} споров")
    for probe in (1, 2, 3, 4, 5, 6, 8, 10):
        hit = sum(1 for p in ranks.values() if p and p <= probe)
        print(f"   потолок {probe:2}: {hit}/{len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
