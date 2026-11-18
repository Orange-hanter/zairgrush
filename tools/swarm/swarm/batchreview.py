"""Пакетное ревью по майлстоуну (§5.8 дизайн-дока, `review_mode = "batch"`).

Замер полевой сессии ZeusLogic (2026-09-20, 17 задач) показал, что
ревью — 68 % денег петли ($39.38 из $58), и почти вся цена вызова не
зависит от диффа: 38 строк стоили $0.82, 1114 строк — $1.82. Ревьюер
каждый раз заново читает контекст репозитория, и платится это за
задачу. Подтверждающий повтор (`confirmations = 2`) изменил исход один
раз из шестнадцати за $10.14.

Отсюда режим: задача без риска проходит гейт и границы, коммитится и
закрывается с пометкой `review_pending = <майлстоун>`, очередь идёт
дальше. Когда в майлстоуне не осталось незакрытой работы (или дифф
пакета перерос `batch_max_lines`), ОДНО ревью смотрит общий дифф
пакета. Находки разносятся по задачам; по каждой задаче с находками
заводится задача-исправление с немедленным ревью — её дифф мал, и это
и есть повторная проверка.

Что ревьюится сразу, как раньше: задача без майлстоуна (пакета нет),
задача-исправление и дифф с риском — защищённые пути или новая
зависимость (те же guard'ы, что у триажа ADR-027).

Петля не пушит (§5.4 — push делает человек), поэтому отдельная ветка
для пакета не нужна: локальная история и есть промежуточная зона, а
`review_pending` на задаче говорит человеку, что выкладывать рано.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import boundarynote  # noqa: E402
import obs  # noqa: E402
import parsing as parsing_mod  # noqa: E402
import state as state_mod  # noqa: E402
import triage  # noqa: E402
from verdicts import apply_policies, classify_findings  # noqa: E402

if TYPE_CHECKING:
    from loop_types import LoopLike

log = obs.get_logger("batchreview")

DEFAULT_MAX_LINES = 1500
# Статусы, при которых в майлстоуне ещё идёт работа: пакет ждёт её.
# blocked сюда не входит — заблокированная задача ждёт человека, а не
# очередь, и держать из-за неё ревью остальных незачем.
OPEN_STATUSES = frozenset({"pending", "in_progress", "in_review"})
# Пустое дерево git: база диффа, когда старейший коммит пакета — корневой.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def enabled(config: dict[str, Any]) -> bool:
    return str(config.get("review_mode") or "task") == "batch"


def immediate_reason(config: dict[str, Any], task: dict[str, Any],
                     diff: str) -> str | None:
    """Почему эту задачу ревьюить сразу; None — ревью уходит в пакет.

    Порядок проверок — от дешёвого к содержательному; первая сработавшая
    причина и называется в журнале.
    """
    if not enabled(config):
        return "mode"
    if not task.get("milestone"):
        return "no_milestone"
    if task.get("fix_of") or task.get("review") == "task":
        return "fix"
    paths = boundarynote.diff_files(diff)
    protected = boundarynote.normalize_protected(config.get("protected_paths"))
    if any(fnmatch.fnmatch(p, pat) for p in paths for pat in protected):
        return "protected"
    if any(triage.is_dep_manifest(p) for p in paths):
        return "new_dependency"
    return None


def _tasks(data: dict[str, Any]) -> list[dict[str, Any]]:
    # Очередь читается как данные: строка вместо словаря не роняет пакет.
    return [t for t in data.get("tasks", []) if isinstance(t, dict)]


def pending(data: dict[str, Any], milestone: str) -> list[dict[str, Any]]:
    """Закрытые задачи майлстоуна, чьё ревью отложено, в порядке очереди."""
    return [t for t in _tasks(data)
            if t.get("status") == "done"
            and t.get("review_pending") == milestone and t.get("commit")]


def pending_milestones(data: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for t in _tasks(data):
        m = t.get("review_pending")
        if t.get("status") == "done" and isinstance(m, str) and m \
                and m not in seen:
            seen.append(m)
    return seen


def work_open(data: dict[str, Any], milestone: str) -> bool:
    return any(t.get("milestone") == milestone
               and t.get("status") in OPEN_STATUSES for t in _tasks(data))


def _git(loop: LoopLike, *args: str) -> str:
    return str(loop.state.git(*args).stdout)


def _order(loop: LoopLike, shas: list[str]) -> list[str]:
    """Коммиты от старого к новому: по числу предков, а не по очереди
    задач — задача могла закрыться позже стоящей после неё в файле."""
    def depth(sha: str) -> int:
        out = _git(loop, "rev-list", "--count", sha).strip()
        return int(out) if out.isdigit() else 0
    return sorted(shas, key=depth)


def _touched(loop: LoopLike, sha: str) -> list[str]:
    out = _git(loop, "diff-tree", "--no-commit-id", "--name-only", "-r",
               "--root", sha)
    return [line.strip() for line in out.splitlines() if line.strip()]


def batch_diff(loop: LoopLike, tasks: list[dict[str, Any]]
               ) -> tuple[str, dict[str, str]]:
    """Общий дифф пакета и карта «файл → задача, тронувшая его последней».

    База — родитель старейшего коммита пакета, конец — HEAD, пути —
    только файлы, которые трогали коммиты пакета. Между коммитами пакета
    могли лечь коммиты других майлстоунов; в те же файлы их правки
    попадут в дифф, в чужие — нет. Это осознанная цена: ревьюер видит
    итоговое состояние файла, а не склейку патчей поверх друг друга.
    """
    by_sha = {str(t["commit"]): t for t in tasks}
    ordered = _order(loop, list(by_sha))
    owner: dict[str, str] = {}
    for sha in ordered:
        for path in _touched(loop, sha):
            owner[path] = str(by_sha[sha]["id"])
    if not owner:
        return "", owner
    parent = _git(loop, "rev-parse", "--verify", "-q",
                  f"{ordered[0]}^").strip()
    base = parent or EMPTY_TREE
    diff = _git(loop, "diff", base, "HEAD", "--", *sorted(owner),
                *state_mod.OWNED_EXCLUDE_PATHSPECS)
    return diff, owner


def diff_lines(loop: LoopLike, tasks: list[dict[str, Any]]) -> int:
    diff, _ = batch_diff(loop, tasks)
    return parsing_mod.diff_size(diff)[1]


def batch_task(milestone: str, tasks: list[dict[str, Any]],
               owner: dict[str, str]) -> dict[str, Any]:
    """Синтетическая задача для ревьюера: спецификация — список задач
    пакета, приёмка — их приёмки с меткой задачи.

    Требование поля `task` в каждой находке живёт в спецификации, а не в
    правилах ревьюера: правила общие для обоих режимов, и их отпечаток
    (rules_sha) не должен зависеть от режима.
    """
    lines = [
        (f"Пакетное ревью майлстоуна «{milestone}»: {len(tasks)} задач, "
         f"каждая уже прошла гейт и проверку границ и закоммичена. Дифф "
         f"ниже — их общий итог от родителя старейшего коммита до HEAD. "
         f"Отдельный коммит задачи смотри через `git show <commit>`."),
        "",
        ("В КАЖДОЙ находке заполни поле `task` — id задачи пакета, к работе "
         "которой она относится. Находка о взаимодействии задач — к той, "
         "что пришла позже."),
        "",
        "Задачи пакета:",
    ]
    for t in tasks:
        spec = str(t.get("spec") or "").strip().replace("\n", " ")
        lines.append(f"- {t['id']} (коммит {t.get('commit')}): {t.get('title')}")
        if spec:
            lines.append(f"  спецификация: {spec[:600]}")
    acceptance = [f"[{t['id']}] {a}" for t in tasks
                  for a in t.get("acceptance") or []]
    paths: list[str] = []
    for t in tasks:
        for p in t.get("paths") or []:
            if p not in paths:
                paths.append(p)
    for p in sorted(owner):
        if p not in paths:
            paths.append(p)
    # Проверки исполнением при verification="milestone" запускаются на
    # задаче, закрывающей майлстоун. Её ревью теперь — ревью пакета, и
    # без переноса флага проверка молча терялась бы.
    closing = any(t.get("milestone_close") for t in tasks)
    return {"id": f"batch-{_slug(milestone)}", "title": f"пакет {milestone}",
            "type": "feature", "milestone": milestone, "spec": "\n".join(lines),
            "acceptance": acceptance, "paths": paths,
            "batch": [str(t["id"]) for t in tasks],
            **({"milestone_close": True} if closing else {})}


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:40] or "m"


def attribute(findings: list[dict[str, Any]], tasks: list[dict[str, Any]],
              owner: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Находки по задачам: поле `task` ревьюера, иначе владелец файла,
    иначе последняя задача пакета. Ничего не теряется — нераспознанная
    находка всё равно попадает к живой задаче."""
    ids = [str(t["id"]) for t in tasks]
    out: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        tid = str(f.get("task") or "")
        if tid not in ids:
            tid = owner.get(str(f.get("file") or ""), ids[-1])
        out.setdefault(tid, []).append(f)
    return out


def _new_id(seed: str, used: set[str]) -> str:
    n = 0
    while True:
        cand = hashlib.sha256(f"{seed}#{n}".encode()).hexdigest()[:4]
        if cand not in used:
            return cand
        n += 1


def fix_task(orig: dict[str, Any], milestone: str,
             findings: list[dict[str, Any]], used: set[str]) -> dict[str, Any]:
    """Задача-исправление: границы исходной плюс файлы, названные
    находками (иначе страж границ откатит саму правку), немедленное
    ревью (`review = "task"`), тот же майлстоун."""
    items = []
    acceptance = []
    for f in findings:
        place = f"{f.get('file')}:{f.get('line')}" if f.get("line") \
            else str(f.get("file") or "")
        items.append(f"- [{f.get('severity')}/{f.get('category')}] {place}: "
                     f"{f.get('issue')}"
                     + (f" — {f['suggestion']}" if f.get("suggestion") else ""))
        acceptance.append(f"закрыта находка пакетного ревью: {f.get('issue')} "
                          f"({place})")
    acceptance.append(f"приёмка исходной задачи {orig['id']} не нарушена")
    paths = list(orig.get("paths") or [])
    for f in findings:
        p = str(f.get("file") or "")
        if p and p not in paths and not any(fnmatch.fnmatch(p, g) for g in paths):
            paths.append(p)
    title = f"исправления пакетного ревью: {orig.get('title')}"
    return {"id": _new_id(f"{orig['id']}:{title}", used), "title": title,
            "type": "feature", "milestone": milestone, "status": "pending",
            "deps": [], "paths": paths,
            "spec": (f"Пакетное ревью майлстоуна «{milestone}» нашло в работе "
                     f"задачи {orig['id']} (коммит {orig.get('commit')}) "
                     f"замечания ниже. Исправь их, не меняя остального "
                     f"поведения.\n" + "\n".join(items)),
            "acceptance": acceptance, "fix_of": orig["id"], "review": "task",
            **({"gate": orig["gate"]} if orig.get("gate") else {}),
            **({"test_module": orig["test_module"]}
               if orig.get("test_module") else {})}


def review(loop: LoopLike, milestone: str) -> str:
    """Одно ревью пакета майлстоуна. Исход: approve | fixes | failed | empty.

    Квота и бюджет поднимаются исключениями наружу, как у ревью задачи:
    пометки `review_pending` при этом остаются, и следующий прогон
    возьмёт пакет снова.
    """
    data = loop.state.load_tasks()
    tasks = pending(data, milestone)
    if not tasks:
        return "empty"
    ids = [str(t["id"]) for t in tasks]
    diff, owner = batch_diff(loop, tasks)
    files, lines = parsing_mod.diff_size(diff)
    synth = batch_task(milestone, tasks, owner)
    if not diff.strip():
        _close(loop, tasks, milestone, "approve")
        loop.state.log("batch_review", milestone=milestone, tasks=ids,
                       verdict="approve", findings=0, fixes=[],
                       diff_lines=0, note="пустой дифф пакета")
        return "approve"
    ok, tail = loop.gate(synth)
    if not ok:
        # HEAD красный после закоммиченных задач — судить пакет поверх
        # красного сьюта нельзя, и чья это поломка, ревью не скажет.
        loop.state.log("batch_review_failed", milestone=milestone, tasks=ids,
                       why="gate_red", tail=(tail or "")[-300:])
        loop.ui(f"    ПАКЕТ {milestone}: гейт на HEAD красный — ревью отложено")
        return "failed"
    loop.ui(f"=== пакет {milestone}: {len(ids)} задач, {files} ф., "
            f"{lines} строк — одно ревью")
    verdict = loop.agents.review(synth, tail, 1, diff=diff)
    if verdict is None:
        why = getattr(loop.agents, "last_review_failure", None) or "invalid"
        loop.state.log("batch_review_failed", milestone=milestone, tasks=ids,
                       why=why)
        loop.ui(f"    ПАКЕТ {milestone}: ревью не состоялось ({why}); "
                f"задачи остаются в ожидании, следующий прогон повторит")
        return "failed"
    raw = [f for f in verdict.get("findings") or [] if isinstance(f, dict)]
    findings, suppressed = apply_policies(raw, loop.state.policies())
    if suppressed:
        loop.state.log("policy_suppressed", task=synth["id"], round=1,
                       count=len(suppressed),
                       items=[{"policy": f["suppressed_by"],
                               "severity": f.get("severity"),
                               "issue": str(f.get("issue"))[:200]}
                              for f in suppressed])
    kind = str(verdict.get("verdict"))
    if raw and not findings and kind != "blocked":
        kind = "approve"
    if kind == "approve":
        # Одобренный пакет с мелочами закрывается так же, как задача
        # с мелочами при approve: мелочь не повод для нового круга.
        _close(loop, tasks, milestone, "approve")
        loop.state.log("batch_review", milestone=milestone, tasks=ids,
                       verdict="approve", findings=len(findings), fixes=[],
                       diff_lines=lines)
        loop.ui(f"    пакет {milestone}: approve")
        return "approve"
    per_task = attribute(findings, tasks, owner)
    if kind == "blocked" and not per_task:
        per_task = {ids[-1]: [{"severity": "blocker", "category": "scope",
                               "file": "", "issue": str(verdict.get("summary"))}]}
    fixes = _file_fixes(loop, tasks, milestone, per_task,
                        blocked=(kind == "blocked"))
    clean = [t for t in tasks if str(t["id"]) not in per_task]
    _close(loop, clean, milestone, "approve")
    _close(loop, [t for t in tasks if str(t["id"]) in per_task], milestone,
           kind, fixes)
    loop.state.log("batch_review", milestone=milestone, tasks=ids,
                   verdict=kind, findings=len(findings),
                   fixes=sorted(fixes.values()), diff_lines=lines)
    loop.ui(f"    пакет {milestone}: {kind}, находок {len(findings)}, "
            f"задач-исправлений {len(fixes)}")
    return "fixes"


def _close(loop: LoopLike, tasks: list[dict[str, Any]], milestone: str,
           verdict: str, fixes: dict[str, str] | None = None) -> None:
    if not tasks:
        return
    want = {str(t["id"]) for t in tasks}
    with loop.state.mutate():
        data = loop.state.load_tasks()
        for t in _tasks(data):
            if str(t.get("id")) in want:
                t.pop("review_pending", None)
                t["batch_review"] = {"milestone": milestone, "verdict": verdict,
                                     **({"fix": fixes[str(t["id"])]}
                                        if fixes and str(t["id"]) in fixes
                                        else {})}
        loop.state.save_tasks(data)


def _file_fixes(loop: LoopLike, tasks: list[dict[str, Any]], milestone: str,
                per_task: dict[str, list[dict[str, Any]]],
                blocked: bool) -> dict[str, str]:
    """Завести задачи-исправления. Находки о замысле (и вердикт blocked)
    уходят человеку тем же инбоксом, что у задачи: исправление заводится
    заблокированным с вопросом, ответ возвращает его в очередь."""
    by_id = {str(t["id"]): t for t in tasks}
    created: dict[str, str] = {}
    asks: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
    with loop.state.mutate():
        data = loop.state.load_tasks()
        used = {str(t.get("id")) for t in _tasks(data)}
        for tid, items in per_task.items():
            orig = by_id[tid]
            fix = fix_task(orig, milestone, items, used)
            used.add(fix["id"])
            intent, mechanical = classify_findings(items)
            if blocked or intent:
                fix["status"] = "blocked"
                fix["reason"] = "ask_user"
                asks.append((fix["id"], intent or items, mechanical))
            data["tasks"].append(fix)
            created[tid] = fix["id"]
        loop.state.save_tasks(data)
    for fid, intent, mechanical in asks:
        question = "; ".join(str(f.get("issue", ""))[:200] for f in intent)
        qid = loop.state.ask(
            fid, "intent", question,
            findings=[{"category": f.get("category"),
                       "severity": f.get("severity"),
                       "issue": f.get("issue"),
                       "suggestion": f.get("suggestion")} for f in intent],
            mechanical_left=len(mechanical), batch=milestone)
        loop.state.set_status(fid, "blocked", reason="ask_user", question_id=qid)
    for tid, fid in created.items():
        loop.state.log("batch_fix_created", task=fid, fix_of=tid,
                       milestone=milestone,
                       findings=len(per_task[tid]))
    return created
