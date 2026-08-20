"""Запись исхода задачи и рефлексия после прогона."""

from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import memory  # noqa: E402
import obs  # noqa: E402

log = obs.get_logger("memory")


def _trajectory(journal: pathlib.Path, task_id: str) -> dict[str, Any]:
    """Траектория раундов из журнала. Журнал — данные: битые строки и
    отсутствие файла — не событие, а пустая траектория."""
    rounds: list[dict[str, Any]] = []
    scope_files: set[str] = set()
    if not journal.exists():
        return {"rounds": 0, "findings": [], "verdicts": []}
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("task") != task_id:
            continue
        if row.get("kind") == "round":
            rounds.append(row)
        elif row.get("kind") == "scope_violation":
            scope_files.update(str(p) for p in (row.get("unexpected") or []))
            scope_files.update(str(p) for p in (row.get("protected") or []))
    out: dict[str, Any] = {
        "rounds": len(rounds),
        "findings": [int(r.get("findings") or 0) for r in rounds],
        "verdicts": [str(r.get("verdict") or "") for r in rounds]}
    if scope_files:
        out["scope_files"] = sorted(scope_files)
    return out


def _commit_paths(root: pathlib.Path, commit: str) -> list[str]:
    r = memory.subprocess.run(["git", "show", "--name-only", "--format=", commit],
                       cwd=root, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return []
    return [p for p in r.stdout.splitlines() if p.strip()][:5]


_HUNK_SYMBOL = re.compile(r"^@@ .+ @@ .*?(?:def|class|fn)\s+(\w+)",
                          re.MULTILINE)


def _commit_symbols(root: pathlib.Path, commit: str) -> list[str]:
    """Символы из заголовков ханков: дешёвый якорь «урок про эту функцию».

    git сам пишет контекст ханка (имя функции/класса) — парсим его, а не
    строим индекс: якорю хватает признака «символ ещё существует»."""
    r = memory.subprocess.run(["git", "show", "--format=", "--unified=0", commit],
                       cwd=root, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return []
    seen: list[str] = []
    for name in _HUNK_SYMBOL.findall(r.stdout):
        if name not in seen:
            seen.append(name)
    return seen[:3]


def record_task_outcome(state: Any, task: dict[str, Any],
                        config: dict[str, Any]) -> str | None:
    """Механический урок из терминального исхода задачи. Без LLM: факты
    берутся из tasks.json и журнала, формулировка — шаблонная и честная.

    Маппинг: решения человека -> corrected (самое ценное — принятое
    решение); blocked -> dead_end с диагнозом; done -> useful.
    """
    tid = str(task.get("id") or "")
    fresh = next((t for t in state.load_tasks().get("tasks", [])
                  if t.get("id") == tid), task)
    status = str(fresh.get("status") or "")
    if status not in ("done", "blocked"):
        return None
    repo, stand = memory.repo_identity(state.root)
    store = memory.MemoryStore(state.root)
    traj = _trajectory(store.journal_path, tid)
    title = str(fresh.get("title") or "")
    decisions = [str(d) for d in (fresh.get("human_decisions") or [])]
    anchors: list[dict[str, Any]] = [{"kind": "task", "ref": tid}]
    commit = str(fresh.get("commit") or "")
    if commit:
        anchors.append({"kind": "commit", "ref": commit})
        anchors += [{"kind": "path", "ref": p, "fp": memory.file_fp(store.root / p)}
                    for p in _commit_paths(store.root, commit)]
        anchors += [{"kind": "symbol", "ref": s}
                    for s in _commit_symbols(store.root, commit)]
    if decisions:
        outcome = "corrected"
        body = (f"«{title}»: решения владельца, обязательные и дальше: "
                + "; ".join(decisions))
    elif status == "blocked":
        outcome = "dead_end"
        why = str(fresh.get("diagnosis") or fresh.get("reason") or "блокирована")
        body = f"«{title}»: {why}"
        if traj.get("scope_files"):
            body += ("; раунды горели о файлы: "
                     + ", ".join(traj["scope_files"][:4]))
    else:
        outcome = "useful"
        body = (f"«{title}»: закрыта за {traj['rounds']} раунд(а), "
                f"коммит {commit or 'нет'}")
    record = {
        "rec": "lesson", "repo": repo, "stand": stand,
        "goal": str(state.load_tasks().get("goal") or ""),
        "source": "mechanical", "outcome": outcome,
        "task_id": tid, "title": title, "body": body,
        "anchors": anchors, "trajectory": traj,
        "human_decisions": decisions,
        "diagnosis": fresh.get("diagnosis"),
    }
    ok, why_not = memory.admissible(record, store.root, store.journal_path)
    if not ok:
        log.warning("память: урок по %s не принят: %s", tid, why_not)
        return None
    lesson_id: str = store.append(record)
    state.log("memory_written", task=tid, lesson=lesson_id, outcome=outcome)
    # Файлы — всегда (память копится и при выключенном эксперименте);
    # PG — по праву index_enabled: включённый эксперимент или явный
    # memory_index="auto" стенда. Прогон с дефолтным конфигом по-прежнему
    # не имеет права трогать ОБЩУЮ базу — тесты петли на живом PG уже
    # насорили в неё уроками с временных стендов. sync вместо точечного
    # upsert: досыпаются и строки, и ВЕКТОРА (замер OpenRouter показал
    # ноль вызовов эмбеддера за пилот — вектора рождались только от
    # ручного reindex), плюс автоматически догоняется накопившийся хвост.
    if memory.index_enabled(config):
        synced = memory.sync(config, store, repo, stand)
        if synced and synced[1]:
            state.log("memory_synced", rows=synced[0], vectors=synced[1],
                      unembedded=synced[2])
    return lesson_id


def fp_candidates(state: Any) -> list[dict[str, Any]]:
    """Кандидаты в политики: подавления, повторившиеся ≥2 раз.

    Политика привязана к цели и умирает со сменой цели, а норма
    репозитория цели переживает. Повторяющееся подавление — сигнал
    закрепить решение заново. Предлагает reflect, решает ЧЕЛОВЕК через
    inbox: автопромоции нет — память не смеет затыкать ревьюера сама.
    """
    journal = memory.MemoryStore(state.root).journal_path
    if not journal.exists():
        return []
    policies_meta: dict[str, dict[str, Any]] = {}
    counts: dict[str, dict[str, Any]] = {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("kind") == "policy" and row.get("pid"):
            policies_meta[str(row["pid"])] = row
        elif row.get("kind") == "policy_suppressed":
            for item in row.get("items") or []:
                if not isinstance(item, dict):
                    continue
                issue = str(item.get("issue") or "").strip()
                if not issue:
                    continue
                key = re.sub(r"\s+", " ", issue).lower()
                bucket = counts.setdefault(
                    key, {"issue": issue, "count": 0,
                          "pid": str(item.get("policy") or "")})
                bucket["count"] += 1
    active = state.policies()
    out = []
    for bucket in counts.values():
        if bucket["count"] < 2:
            continue
        text_l = str(bucket["issue"]).lower()
        if any(any(str(m).lower() in text_l for m in (p.get("match") or []))
               for p in active):
            continue        # действующая политика уже покрывает
        src = policies_meta.get(bucket["pid"], {})
        out.append({"issue": bucket["issue"], "count": bucket["count"],
                    "policy_text": str(src.get("text") or bucket["issue"]),
                    "match": [str(m) for m in (src.get("match") or [])]})
    out.sort(key=lambda c: (-int(c["count"]), str(c["issue"])))
    return out[:3]


def _propose_policies(state: Any) -> None:
    open_fp = [str(q.get("question") or "")
               for q in state.questions(only_open=True)
               if q.get("qkind") == "fp_promotion"]
    for cand in fp_candidates(state):
        marker = cand["issue"][:80]
        if any(marker in q for q in open_fp):
            continue    # вопрос уже висит — не дублировать
        matches = " ".join(f'--match "{m}"' for m in cand["match"]) or (
            '--match "<ключевое слово>"')
        state.ask("*", "fp_promotion",
                  (f"подавление повторилось {cand['count']}×: "
                   f"«{cand['issue'][:150]}». Если это норма репозитория — "
                   f"закрепите: swarm policy add "
                   f"\"{cand['policy_text'][:80]}\" {matches}"),
                  count=cand["count"])


def reflect_after_run(state: Any, config: dict[str, Any]) -> None:
    """«Сновидение» после прогона: дайджест, ре-валидация якорей, факт.

    Детерминированное и мгновенное — LLM-консолидация живёт отдельно,
    за собственным флагом (этап 3).
    """
    try:
        store = memory.MemoryStore(state.root)
        if not store.lessons_path.exists():
            # Нечего переосмысливать — и не о чем оставлять след: пустая
            # рефлексия не создаёт каталогов и не пишет в журнал.
            return
        store.write_digest()
        records = store.records()
        # Ре-валидация якорей: отпечаток пути ловит «файл есть, но
        # переписан». Мёртвые якоря видны в дайджесте («Отвязанные») и в
        # индексе (anchors_ok) — урок не удаляется и не подаётся правдой.
        unlinked = 0
        marks: list[dict[str, Any]] = []
        for r in records:
            anchors = [a for a in (r.get("anchors") or [])
                       if isinstance(a, dict)]
            ok_flag = (not anchors) or any(
                memory.resolve_anchor(a, store.root, store.journal_path)
                for a in anchors)
            if not ok_flag:
                unlinked += 1
            marks.append({"id": str(r.get("id")), "ok": ok_flag})
        if marks and memory.index_enabled(config):
            _repo, stand = memory.repo_identity(state.root)
            # Рефлексия — второй триггер автоиндексации (первый — каждый
            # терминальный исход): догоняет всё, что пропустили сбои
            # эмбеддера или cap за раунд, прежде чем валидировать якоря.
            synced = memory.sync(config, store, _repo, stand)
            if synced and synced[1]:
                state.log("memory_synced", rows=synced[0],
                          vectors=synced[1], unembedded=synced[2])
            # S608: только константный текст; пары id/ok — в -v jsonb.
            memory.pg(config,
               "UPDATE lessons l SET anchors_ok = r.ok, "
               "validated_ts = now() "
               "FROM jsonb_to_recordset(:'marks'::jsonb) "
               "AS r(id text, ok boolean) "
               "WHERE l.id = r.id AND l.stand = :'stand';",
               {"marks": json.dumps(marks), "stand": stand})
        # Кандидаты в политики и LLM-консолидация — свои границы
        # деградации: их сбой не должен глушить сам факт рефлексии.
        try:
            _propose_policies(state)
        except Exception:
            log.exception("память: кандидаты в политики не разобраны")
        try:
            if (config.get("experiments") or {}).get(
                    "memory_llm_consolidation"):
                summary = memory.helpers.consolidate_lessons(records)
                if summary:
                    store.write_consolidation(summary)
        except Exception:
            log.exception("память: консолидация не состоялась")
        state.log("memory_reflect", lessons=len(records), unlinked=unlinked)
    except Exception:
        log.exception("память: рефлексия после прогона не состоялась")
