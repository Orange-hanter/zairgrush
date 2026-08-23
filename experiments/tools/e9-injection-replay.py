#!/usr/bin/env python3
"""E9 этап 4 и E6: что память РЕАЛЬНО подмешала бы исполнителю.

Гипотеза E9 звучит так: уроки прошлых прогонов сокращают повторные тупики
и раунды. Проверять её сразу двумя дорогими плечами — значит платить за
проверку ПОСЫЛКИ: если под задачу, где петля трижды повторила
отвергнутую работу, память не достаёт нужного урока, плечи мерить нечего.
Поэтому сначала сухой реплей — без единого вызова модели.

Считает две вещи.

1. E9: для каждой закрытой задачи стенда строится тот самый блок, что
   ушёл бы исполнителю (`meminject.inject_block("executor", …)`), и
   проверяется, попал ли в него урок, ЯКОРЬ которого указывает на эту же
   задачу. Урок про свою задачу — самый мягкий из возможных тестов
   релевантности: если память не находит даже его, разговор о сокращении
   раундов преждевременен.

2. E6: вклад ВЕКТОРА поверх полнотекстового поиска. `queries.jsonl`
   пишет только общий backend (`fts+vec`), но разложить его можно:
   повторяем FTS тем же запросом и вычитаем — остаток и есть то, чего
   FTS не нашёл. Ровно этот вопрос E6 и задаёт.

Реплей идёт ЧЕРЕЗ настоящий `retrieve`, а тот пишет телеметрию — иначе
замерялся бы не тот код, что работает в петле. Поэтому `queries.jsonl`
снимается до прогона и возвращается после: синтетические запросы не
имеют права смешаться с настоящими, по которым считается adoption. Урок
получен на себе — первый прогон дописал в телеметрию стенда полсотни
строк.

Запуск (стенд читается; телеметрия восстанавливается):
    python3 e9-injection-replay.py ~/work/zeus-pilot
"""
import argparse
import importlib.util
import json
import pathlib
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


def anchors_task(lesson):
    """Задача, на которую указывает урок.

    Форма якоря — `[{"kind": "task", "ref": "m6pe"}]`; поле `task_id`
    урока хранит то же самое и служит запасным путём. Первая редакция
    этого разбора искала ключ `value` вместо `ref`, молча возвращала
    пустую строку и дала ложный вывод «свой урок не найден ни разу» —
    при том что блок этот урок содержал. Отсюда правило: детектор,
    сообщающий НОЛЬ, обязан быть проверен на заведомо положительном
    случае, иначе он измеряет себя.
    """
    for a in lesson.get("anchors") or []:
        if isinstance(a, dict) and a.get("kind") == "task" and a.get("ref"):
            return str(a["ref"])
    return str(lesson.get("task_id") or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stand")
    ap.add_argument("--role", default="executor")
    args = ap.parse_args()
    stand = pathlib.Path(args.stand).expanduser().resolve()

    cli = _load("cli")
    state_mod = _load("state")
    meminject = _load("meminject")
    memory = _load("memory")

    cfg = dict(cli.load_config(stand))
    # Плечо меряется НЕ тем флагом, что стоит на стенде: инъекцию для
    # роли включаем принудительно, иначе замерили бы конфиг, а не память.
    cfg["experiments"] = dict(cfg.get("experiments") or {})
    cfg["experiments"]["memory"] = args.role
    st = state_mod.SwarmState(stand)

    # Снимок телеметрии: реплей ходит настоящим путём и потому пишет.
    qpath = stand / ".swarm" / "memory" / "queries.jsonl"
    snapshot = qpath.read_text(encoding="utf-8") if qpath.exists() else None

    store = memory.MemoryStore(stand)
    lessons = list(store.records())
    by_task = {}
    for les in lessons:
        tid = anchors_task(les)
        if tid:
            by_task.setdefault(tid, []).append(str(les.get("id")))
    print(f"уроков в памяти: {len(lessons)}; с якорем на задачу: "
          f"{sum(len(v) for v in by_task.values())} "
          f"по {len(by_task)} задачам")

    tasks = json.loads((stand / ".swarm" / "tasks.json").read_text(
        encoding="utf-8"))["tasks"]
    done = [t for t in tasks if t.get("status") == "done"]
    print(f"\n=== E9: блок для роли «{args.role}» по {len(done)} закрытым "
          f"задачам ===")
    hit_own, had_block, empty = 0, 0, 0
    for task in sorted(done, key=lambda t: str(t.get("id"))):
        tid = str(task.get("id"))
        block = meminject.inject_block(args.role, task, st, cfg)
        own = by_task.get(tid, [])
        if not block:
            empty += 1
            mark = "пусто"
        else:
            had_block += 1
            got_own = [i for i in own if i in block]
            if got_own:
                hit_own += 1
            mark = (f"{len(block)} симв."
                    + (f", СВОЙ урок в блоке ({len(got_own)})" if got_own
                       else (", своего урока НЕТ" if own else ", своих нет")))
        print(f"  {tid}  {mark}")
    print(f"\n  блок непуст у {had_block} из {len(done)}; "
          f"свой урок найден в {hit_own}")

    # Запросы РЕПЛЕЯ — свежие и потому самые честные: они построены тем
    # же кодом, что сейчас в петле. Берём их до восстановления снимка.
    replayed = []
    if qpath.exists():
        all_rows = [json.loads(x) for x in
                    qpath.read_text(encoding="utf-8").splitlines() if x.strip()]
        old_n = len(snapshot.splitlines()) if snapshot else 0
        replayed = all_rows[old_n:]
    if snapshot is not None:
        qpath.write_text(snapshot, encoding="utf-8")
        print(f"\n  телеметрия стенда восстановлена ({len(replayed)} "
              f"синтетических запросов убрано)")

    print("\n=== E6: что добавил вектор поверх FTS ===")
    if not qpath.exists():
        print("  queries.jsonl пуст — телеметрии нет")
        return 0
    repo, _stand_id = memory.repo_identity(stand)
    total_extra = 0
    rows = replayed or [json.loads(x) for x in
                        qpath.read_text(encoding="utf-8").splitlines()
                        if x.strip()]
    for row in rows:
        recorded = [str(h) for h in (row.get("hits") or [])]
        k = int(row.get("k") or len(recorded) or 5)
        fts = memory.search_fts(cfg, repo, str(row.get("query") or ""), k)
        if fts is None:
            print("  PG недоступен — вклад вектора не разложить")
            return 0
        fts_ids = [str(h.get("id")) for h in fts]
        extra = [h for h in recorded if h not in fts_ids]
        total_extra += len(extra)
        print(f"  {str(row.get('role')):9} backend={row.get('backend'):14} "
              f"всего {len(recorded)}, FTS {len(fts_ids)}, "
              f"только вектор: {len(extra)}")
    print(f"\n  ИТОГО по {len(rows)} запросам: вектор добавил {total_extra} "
          f"хитов сверх FTS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
