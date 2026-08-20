"""Команда работы с памятью прогонов (memory)."""
import argparse
import json
import pathlib
import subprocess
import sys

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cli  # noqa: E402


def _memory_anchor(root: pathlib.Path, ref: str) -> dict[str, str]:
    """Якорь из операторской строки: вид определяется тем, что резолвится.

    Порядок проверок — от дешёвого к дорогому; ничего не резолвится —
    честный path-якорь, который страж памяти отклонит с причиной.
    """
    if (root / ref).exists():
        return {"kind": "path", "ref": ref}
    probe = subprocess.run(["git", "cat-file", "-e", f"{ref}^{{commit}}"],
                           cwd=root, capture_output=True, check=False)
    if probe.returncode == 0:
        return {"kind": "commit", "ref": ref}
    journal = pathlib.Path(root) / ".swarm" / "log" / "run.jsonl"
    if journal.exists() and f'"task": "{ref}"' in journal.read_text(
            encoding="utf-8"):
        return {"kind": "task", "ref": ref}
    return {"kind": "path", "ref": ref}



def cmd_memory(args: argparse.Namespace) -> int:
    """Память между прогонами (E9): уроки, поиск, дайджест, индекс."""
    mem = cli._load("memory")
    st = cli.state_mod.SwarmState(args.root)
    cfg = cli._config(args.root)
    store = mem.MemoryStore(args.root)
    root = pathlib.Path(args.root)
    repo, stand = mem.repo_identity(args.root)
    # Счётчик хелперов — и для CLI-путей: без этого вызовы эмбеддера из
    # search/reindex/sync не оставляли следа в helper-metrics.jsonl, и
    # вопрос «а использовался ли эмбеддер?» решался только по дашборду
    # провайдера (замерено на пилоте: metering gap).
    mem.helpers.configure(st.dir / "helper-metrics.jsonl")

    if args.mem_cmd == "add":
        anchors = [_memory_anchor(root, a) for a in (args.anchor or [])]
        record = {"repo": repo, "stand": stand,
                  "goal": st.load_tasks().get("goal", ""),
                  "source": "operator", "outcome": args.outcome,
                  "body": args.text, "anchors": anchors}
        ok, why = mem.admissible(record, root, store.journal_path)
        if not ok:
            print(why, file=sys.stderr)
            return 2
        lesson_id = store.append(record)
        st.log("memory_written", lesson=lesson_id, outcome=args.outcome)
        if mem.ensure_schema(cfg):
            stored = next((r for r in store.records()
                           if r.get("id") == lesson_id), None)
            if stored is not None:
                mem.upsert(cfg, [dict(stored, repo=repo, stand=stand)])
        print(f"урок {lesson_id} записан ({args.outcome})")
        return 0

    if args.mem_cmd == "search":
        hits = mem.retrieve(st, cfg, args.query, role="operator",
                            k=args.k)
        if args.json:
            print(json.dumps(hits, ensure_ascii=False, indent=1))
            return 0
        if not hits:
            print("ничего не найдено")
            return 0
        for h in hits:
            count = int(h.get("count", 1))
            mark = mem.OUTCOME_RU.get(str(h.get("outcome")), "урок")
            print(f"[{mark}, {count}×] {h.get('id')}  "
                  f"{str(h.get('body') or '')[:120]}")
        return 0

    if args.mem_cmd == "show":
        rec = next((r for r in store.records()
                    if str(r.get("id")) == args.id), None)
        if rec is None:
            print(f"урок {args.id!r} не найден", file=sys.stderr)
            return 2
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        return 0

    if args.mem_cmd == "forget":
        rec = next((r for r in store.records()
                    if str(r.get("id")) == args.id), None)
        if rec is None:
            print(f"урок {args.id!r} не найден", file=sys.stderr)
            return 2
        store.tombstone(args.id)
        mem.pg(cfg, "UPDATE lessons SET tombstone = true "
                    "WHERE id = :'lid';", {"lid": args.id})
        st.log("memory_forgotten", lesson=args.id)
        print(f"урок {args.id} затомбстоунен (файл — источник истины, "
              f"reindex воспроизведёт)")
        return 0

    if args.mem_cmd == "reflect":
        mem.reflect_after_run(st, cfg)
        records = store.records()
        print(f"дайджест пересобран: {store.digest_path} "
              f"(уроков {len(records)})")
        return 0

    if args.mem_cmd == "reindex":
        ok_ix, n = mem.reindex(cfg, store, repo, stand)
        if not ok_ix:
            print("индекс не пересобран: PG недоступен (уроки целы в "
                  "файлах; поиск работает локальным сканом)",
                  file=sys.stderr)
            return 2
        print(f"индекс пересобран: {n} строк(и) для стенда {stand}")
        return 0

    if args.mem_cmd == "sync":
        synced = mem.sync(cfg, store, repo, stand)
        if synced is None:
            print("индекс не досыпан: PG недоступен (уроки целы в файлах; "
                  "поиск работает локальным сканом)", file=sys.stderr)
            return 2
        rows, vectors, unembedded = synced
        if vectors:
            st.log("memory_synced", rows=rows, vectors=vectors,
                   unembedded=unembedded)
        tail = (f", без вектора осталось {unembedded}" if unembedded
                else "")
        print(f"индекс досыпан: {rows} строк(и) стенда {stand}, "
              f"векторов добавлено {vectors}{tail}")
        return 0
    return 2

