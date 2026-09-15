"""Пульт прогона: одна страница, на которой видно всё происходящее.

Существующие команды (`status`, `report`, `inbox`) отвечают на отдельные
вопросы и требуют знать, какой вопрос задать. Этого мало: человеку нужна
картина целиком и без посредника — что идёт прямо сейчас, сколько
потрачено, где его ждут, что уже сделано и можно ли этому верить.

Порядок разделов — не вкусовой, а по убыванию срочности, и он же был
главной ошибкой первой доски. Та показывала плоский список карточек: и
задача, ждущая ответа человека, и закрытая полчаса назад выглядели
одинаково, а всё содержательное (находки ревьюера, траектория схождения,
дифф) пряталось за кликом. Список — указатель, а поверхность вывода
обязана нести знание (§9.3). Теперь сверху вниз: что идёт СЕЙЧАС → где
ждут человека → чем кончился прогон → как он шёл во времени → задачи →
хроника.

Три факта, которых у прежней доски не было вовсе:

- **Настоящее.** Журнал и метрики пишутся ПОСЛЕ фазы, поэтому семь минут
  работы исполнителя не оставляли на странице ни следа: живой сервер
  исправно обновлял её тем же прошлым. Настоящее приходит из отметки
  `.swarm/now.json` (`state.phase`) и читается только вместе с живостью
  петли (`state.is_running`) — один и тот же файл означает «идёт» у
  живого прогона и «оборвалось здесь» у мёртвого.
- **Время.** Раунды, фазы и их длительности разложены по общей шкале
  прогона: топтание, ретраи и дорогое ревью видно формой, а не чтением
  таблиц.
- **Деньги по ролям.** Сумма ничего не говорит о том, кто её потратил;
  замер E13 (37 % денег ревью ушло в вызовы без вердикта) читался из
  jsonl руками, хотя это ровно тот факт, ради которого доску открывают.

Страница самодостаточна: ни одного внешнего ресурса, данные встроены в
неё при генерации. Потоки исполнителя (сотни килобайт на задачу) внутрь
не кладутся — на них даётся путь к файлу.

Во время прогона страницу отдаёт живой сервер (`boardserve.BoardServer`):
адрес печатает команда запуска, и браузер обновляет содержимое на месте
без перезагрузки — раскрытые карточки, поиск и прокрутка не сбрасываются
(см. блок поллинга в конце `JS`). Файл `.swarm/board.html` на диске
остаётся снимком: его переписывает петля после каждого раунда, и вне
прогона он читается как обычный статичный файл.
"""

import datetime
import html
import json
import pathlib
import re
import subprocess
import sys
import time
import tomllib
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import state as state_mod  # noqa: E402 — каталог добавлен строкой выше
import vocab  # noqa: E402 — каталог добавлен строкой выше

# Словарь у доски и у терминала обязан быть ОДИН: пока он лежал здесь,
# `swarm report` до него не доставал и звал те же события кодами.
PHASE_RU = vocab.PHASE_RU
STATUS_RU = vocab.STATUS_RU
SEVERITY_RU = vocab.SEVERITY_RU
CATEGORY_RU = vocab.CATEGORY_RU
KIND_RU = vocab.KIND_RU
MAX_DIFF_CHARS = 12000  # больше человек в браузере всё равно не читает

# Порядок фаз на шкале и в легенде — тот же, что в жизни прогона.
PHASE_ORDER = (
    "implement",
    "gate",
    "scope",
    "review",
    "verification",
    "policy",
    "integrity",
)
# Цвет фазы — общий для шкалы времени и легенды денег; в JS та же
# таблица (PCOL): расхождение здесь означало бы, что одна и та же фаза на
# одной странице показана двумя цветами.
PHASE_COLOR = {
    "implement": "var(--acc)",
    "review": "var(--live)",
    "gate": "var(--ok)",
    "scope": "var(--faint)",
    "verification": "var(--mut)",
    "policy": "var(--mut)",
    "integrity": "var(--bad)",
}
# Состояние вопроса, к которому привязана эскалация: внутренние «open»/
# «answered» на странице читаются фразой, а не кодом.
_Q_STATUS_RU = {"open": "ждёт ответа", "answered": "вопрос закрыт"}


def _key(row: dict[str, Any], field: str = "task") -> str:
    """Ключ группировки из журнальной записи. Записи без поля попадают в
    пустой ключ: с идентификатором задачи он не совпадёт никогда, поэтому
    такая строка не пристанет к чужой задаче."""
    return str(row.get(field) or "")


def _num(value: Any) -> float | None:
    """Число из журнальной записи — или ничего.

    Журнал читается как данные, а не как контракт: строка на месте
    `cost_usd` или `wall_s` (ручная правка, чужая версия) не должна ни
    ронять доску, ни попадать в арифметику под видом нуля.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _epoch(ts: Any) -> float | None:
    """Момент времени из ISO-строки журнала. Неразбираемое — не время."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        # Валидный JSON — ещё не запись: строка `"x"` или `[1]` (ручная
        # правка, чужой инструмент) роняла доску на первом же .get.
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    ).stdout


def _budget(root: pathlib.Path) -> float | None:
    """Потолок прогона из `swarm.toml` — тот же ключ, что читает страж
    бюджета (`spending.run_budget`).

    Без потолка сумма на доске — просто число: $12 это много или мало,
    знает только конфиг. Читаем сами, а не через cli.load_config: доска
    обязана собираться из файлов и в чужом каталоге, где никакого
    оркестратора нет.
    """
    path = root / "swarm.toml"
    if not path.exists():
        return None
    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    return _num(cfg.get("total_budget_usd"))


def _round_key(stem: str) -> tuple[int, int, str, int, str]:
    """Порядок раундов — числовой, а не алфавитный.

    Лексикографика ставит `i10-…` раньше `i2-…`, и с десятого раунда
    «последний вердикт» в `why` и на доске оказывался не последним.
    Неразбираемое имя уходит в конец: о нём нельзя утверждать порядок.
    """
    m = re.fullmatch(r"i(\d+)-([a-z])(\d+)", stem)
    if not m:
        return (1, 0, "", 0, stem)
    return (0, int(m.group(1)), m.group(2), int(m.group(3)), stem)


def _verdicts(swarm_dir: pathlib.Path, tid: str) -> list[dict[str, Any]]:
    """Вердикты ревьюера по раундам — то, из-за чего задача идёт по кругу.

    Читаем сырые ответы: имя вида `<id>-i<раунд>-<фаза><попытка>-review.json`,
    где фаза `a` — обычный проход, `v` — повторный после проверок.
    """
    out = []
    paths = sorted(
        (swarm_dir / "log").glob(f"{tid}-i*-review.json"),
        key=lambda p: _round_key(p.stem.replace(f"{tid}-", "").replace("-review", "")),
    )
    for path in paths:
        stem = path.stem.replace(f"{tid}-", "").replace("-review", "")
        try:
            env = json.loads(path.read_text(encoding="utf-8"))
        except OSError as err:
            # Нечитаемый файл — та же история, что и нечитаемый ответ ниже:
            # раунд был, а вердикта нет. Молчаливый пропуск противоречил
            # собственному правилу доски и прятал самое интересное.
            out.append(
                {
                    "round": stem,
                    "failed": True,
                    "why": f"файл вердикта не прочитан: {err}",
                }
            )
            continue
        except ValueError:
            # Нечитаемый ответ — САМОЕ интересное для оператора: раунд был,
            # деньги потрачены, вердикта нет. Пропуская такой файл, доска
            # показывала задачу так, будто ревью и не запускалось.
            out.append(
                {"round": stem, "failed": True, "why": "ответ ревьюера не разобран"}
            )
            continue
        v = env.get("structured_output")
        if not isinstance(v, dict):
            out.append(
                {
                    "round": stem,
                    "failed": True,
                    "why": (
                        env.get("terminal_reason")
                        or env.get("subtype")
                        or "ответ не разобран"
                    ),
                }
            )
            continue
        out.append(
            {
                "round": stem,
                "verdict": v.get("verdict"),
                "summary": v.get("summary"),
                "analysis": v.get("analysis"),
                "findings": v.get("findings") or [],
                "notes": v.get("out_of_scope_notes") or [],
                "requests": v.get("verification_requests") or [],
            }
        )
    return out


def _timeline(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Фазы на общей шкале времени.

    Метрика пишется в конце фазы, поэтому начало — это `ts` минус
    длительность (`wall_s` у исполнителя, `dur_s` у ревью). У гейта и
    границ длительности нет: это отметка момента, а не отрезок, и
    рисуется она засечкой — врать про ширину нельзя.
    """
    segs = []
    for m in metrics:
        phase = m.get("phase")
        end = _epoch(m.get("ts"))
        if not phase or end is None:
            continue
        dur = _num(m.get("wall_s")) or _num(m.get("dur_s")) or 0.0
        segs.append(
            {
                "task": _key(m),
                "iter": m.get("iter"),
                "phase": str(phase),
                "t0": round(end - dur, 1),
                "t1": round(end, 1),
                "dur": round(dur, 1),
                "ok": m.get("ok"),
                "cost": _num(m.get("cost_usd")),
                "verdict": m.get("verdict"),
                "note": str(m.get("reason") or m.get("run_reason") or ""),
            }
        )
    return sorted(segs, key=lambda s: (s["t0"], s["t1"]))


def _totals(
    metrics: list[dict[str, Any]], tasks: list[dict[str, Any]]
) -> dict[str, Any]:
    """Итоги прогона по ролям, гейту, вердиктам и находкам.

    Одна сумма денег не отвечает на вопрос, ради которого доску
    открывают: дорого — это КТО. Раскладка по фазам считается из тех же
    метрик, что и общая сумма, поэтому разойтись они не могут.
    """
    phases: dict[str, dict[str, float]] = {}
    tokens = {"in": 0.0, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    gate = {"ok": 0, "fail": 0}
    for m in metrics:
        phase = str(m.get("phase") or "")
        if phase:
            row = phases.setdefault(phase, {"n": 0.0, "cost": 0.0, "sec": 0.0})
            row["n"] += 1
            row["cost"] += _num(m.get("cost_usd")) or 0.0
            row["sec"] += _num(m.get("wall_s")) or _num(m.get("dur_s")) or 0.0
        if phase == "gate":
            gate["ok" if m.get("ok") else "fail"] += 1
        for key, field in (
            ("in", "tokens_in"),
            ("out", "tokens_out"),
            ("cache_read", "cache_read"),
            ("cache_write", "cache_write"),
        ):
            tokens[key] += _num(m.get(field)) or 0.0
    for row in phases.values():
        row["cost"] = round(row["cost"], 2)
        row["sec"] = round(row["sec"], 1)

    verdicts: dict[str, int] = {}
    severity: dict[str, int] = {}
    for t in tasks:
        for v in t.get("_verdicts") or []:
            name = "нет вердикта" if v.get("failed") else str(v.get("verdict"))
            verdicts[name] = verdicts.get(name, 0) + 1
            for f in v.get("findings") or []:
                if isinstance(f, dict):
                    sev = str(f.get("severity") or "?")
                    severity[sev] = severity.get(sev, 0) + 1
    return {
        "phases": phases,
        "tokens": {k: int(v) for k, v in tokens.items()},
        "gate": gate,
        "verdicts": verdicts,
        "severity": severity,
    }


def _run_facts(
    st: Any,
    root: pathlib.Path,
    journal: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    segs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Паспорт прогона: чей код, когда начался, жив ли, чем занят.

    Живость — не «есть свежие записи»: тихий прогон (идёт длинная фаза)
    и мёртвый выглядят в файлах одинаково. Спрашиваем блокировку
    состояния (`state.is_running`) — она врать не умеет.
    """
    stamps = [
        e for e in (_epoch(r.get("ts")) for r in journal + metrics) if e is not None
    ]
    stamps += [s["t0"] for s in segs]
    last_row = next((r for r in reversed(journal + metrics) if r.get("run_id")), {})
    live = st.is_running()
    now = st.current_phase()
    return {
        "id": str(last_row.get("run_id") or ""),
        "sha": str(last_row.get("swarm_sha") or ""),
        "t0": min(stamps) if stamps else None,
        "t1": max(stamps) if stamps else None,
        "live": live,
        # Отметка о настоящем без живой петли — не настоящее, а место
        # обрыва: SIGKILL не даёт `finally` снять файл (см. state.phase).
        "now": now if live else None,
        "broke_at": None if live else now,
        "budget": _budget(root),
        "built_epoch": time.time(),
    }


def collect(root: str | pathlib.Path) -> dict[str, Any]:
    """Все данные доски. Ничего не додумывает — только факты из файлов."""
    root = pathlib.Path(root)
    swarm = root / ".swarm"
    try:
        data = json.loads((swarm / "tasks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"goal": "", "tasks": []}
    if not isinstance(data, dict):
        # Валидный JSON не значит очередь: файл правят руками, и список
        # или строка на месте объекта не должны ронять доску.
        data = {"goal": "", "tasks": []}
    # Оба потока метрик: без plan-metrics доска показывала $38.43 там,
    # где страж бюджета видел $40.12 — два авторитета на одну цифру
    # (пилот). Считать деньги обязана одна формула: как total_spend().
    metrics = _read_jsonl(swarm / "metrics.jsonl") + _read_jsonl(
        swarm / "plan-metrics.jsonl"
    )
    journal = _read_jsonl(swarm / "log" / "run.jsonl")

    # Арифметика денег — та же, что у state.total_spend(): сырая сумма и
    # ОДНО округление в конце. Округление на каждом сложении давало сумму
    # округлённых, а не округлённую сумму — и cmd_go печатал два «итога»
    # прогона, расходящихся на центы. Строка вместо числа в cost_usd —
    # данные, а не контракт: пропускаем, а не падаем.
    spend_raw: dict[str, float] = {}
    total_raw = 0.0
    for m in metrics:
        cost = _num(m.get("cost_usd"))
        if cost:
            spend_raw[_key(m)] = spend_raw.get(_key(m), 0.0) + cost
            total_raw += cost
    spend = {k: round(v, 2) for k, v in spend_raw.items()}

    questions = {}
    for row in journal:
        if row.get("kind") == "question":
            # Журнал читаем как данные, а не как контракт: запись без qid
            # (старая версия, ручная правка) не должна ронять доску.
            if not row.get("qid"):
                continue
            questions[row["qid"]] = dict(
                row, status="open", asked=_epoch(row.get("ts"))
            )
        elif row.get("kind") == "answer" and row.get("qid") in questions:
            questions[row["qid"]].update(status="answered", answer=row.get("text"))

    # Эскалации «исполнитель сдался» (WAV-011): маркер ссылается на вопрос
    # по qid — доска показывает сдачу вместе с тем, ждёт вопрос ответа или
    # оператор его уже закрыл. Запись без qid (чужая версия, правка руками)
    # всё равно показывается: сам факт сдачи ценнее целостности ссылки.
    escalations = [
        dict(
            row,
            asked=_epoch(row.get("ts")),
            status=questions.get(str(row.get("qid") or ""), {}).get("status", ""),
        )
        for row in journal
        if row.get("kind") == "agent_gave_up"
    ]

    suppressed: dict[str, list[dict[str, Any]]] = {}
    for row in journal:
        if row.get("kind") == "policy_suppressed":
            suppressed.setdefault(_key(row), []).extend(row.get("items") or [])

    tasks = []
    for t in data.get("tasks", []):
        tid = t.get("id")
        phases = [
            {
                "ts": (m.get("ts") or "")[11:19],
                "iter": m.get("iter"),
                "phase": PHASE_RU.get(_key(m, "phase"), m.get("phase")),
                "result": (
                    m.get("verdict")
                    or m.get("reason")
                    or (
                        "ок"
                        if m.get("ok")
                        else "провал"
                        if m.get("ok") is False
                        else ""
                    )
                ),
                "dur": _num(m.get("dur_s")) or _num(m.get("wall_s")),
                "cost": _num(m.get("cost_usd")),
            }
            for m in metrics
            if m.get("task") == tid and m.get("phase")
        ]
        diff = ""
        # Источников коммита два и они расходятся: step_done в журнале и поле
        # `commit` в задаче (его мог проставить оператор). Берём поле задачи —
        # иначе принятая вручную работа выглядит как «в код ничего не вошло».
        if t.get("commit"):
            diff = _git(root, "show", "--stat", "--format=%s%n", t["commit"])
            full = _git(root, "show", "--format=", t["commit"])
            if full:
                diff += "\n" + (
                    full[:MAX_DIFF_CHARS]
                    + ("\n… дифф обрезан" if len(full) > MAX_DIFF_CHARS else "")
                )
        streams = sorted(p.name for p in (swarm / "log").glob(f"{tid}-*executor.jsonl"))
        tasks.append(
            dict(
                t,
                _phases=phases,
                _cost=spend.get(tid),
                _verdicts=_verdicts(swarm, tid),
                _diff=diff,
                _suppressed=suppressed.get(tid, []),
                _streams=streams,
                _questions=[q for q in questions.values() if q.get("task") == tid],
            )
        )

    rounds: dict[str, list[dict[str, Any]]] = {}
    for r in journal:
        if r.get("kind") == "round":
            rounds.setdefault(_key(r), []).append(
                {
                    "round": r.get("round"),
                    "verdict": r.get("verdict"),
                    "outcome": r.get("outcome"),
                    "findings": r.get("findings"),
                    "intent": r.get("intent"),
                }
            )
    for t in tasks:
        t["_rounds"] = rounds.get(_key(t, "id"), [])

    # Записи без задачи — это события ПРОГОНА, а не чьи-то: сгруппировать их
    # «по задачам» значит потерять ровно то, что объясняет остановку очереди.
    # Фильтр открытый, а не список видов: закрытый перечень молча терял
    # plan_failed — событие, ради которого блок и существует. Наружу не
    # идёт только бухгалтерия (state_written): она сопровождает каждую
    # запись состояния и хоронила бы под собой редкие события.
    run_level = [
        r
        for r in journal
        if not r.get("task") and r.get("kind") not in vocab.BOOKKEEPING_KINDS
    ]

    # Хроника — фразами, а не дампом: `{"kind":"round","round":1,…}` человек
    # разбирает медленнее, чем «раунд 1 → request_changes, находок 3», и
    # ровно так же медленно он разбирал её здесь до появления vocab.
    # `ts: null` — тоже данные: str(… or "") вместо веры в строку.
    events = [
        {
            "ts": str(r.get("ts") or "")[11:19],
            "kind": r.get("kind"),
            "kind_ru": vocab.ru(KIND_RU, r.get("kind")),
            "task": r.get("task"),
            "detail": vocab.narrate(r),
        }
        for r in journal
    ]

    # Незавершённость шага считает state.unfinished_steps(), а не копия
    # формулы: копия закрывала шаг только по step_done, и разобранный
    # step_failed висел на доске «незавершённым» вечно.
    st = state_mod.SwarmState(root)
    unfinished = st.unfinished_steps()

    segments = _timeline(metrics)
    return {
        "goal": data.get("goal", ""),
        "tasks": tasks,
        "questions": list(questions.values()),
        "escalations": escalations,
        "events": events,
        "run_level": run_level,
        "unfinished": unfinished,
        "spend": spend,
        "total": round(total_raw, 2),
        "timeline": segments,
        "totals": _totals(metrics, tasks),
        "run": _run_facts(st, root, journal, metrics, segments),
        "root": str(root),
        "swarm_dir": str(swarm),
        "built": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


CSS = """
:root{--paper:#f7f5f0;--card:#fffefb;--sunk:#f0ede6;--ink:#1b1a17;
--mut:#6c665b;--faint:#a1998a;--line:#e2ddd1;--hair:#ede8dd;
--ok:#2f7d4f;--warn:#a97706;--bad:#a83a2f;--acc:#2e5fa3;--live:#b9800f;
--okbg:#e7f0e8;--warnbg:#f7edd8;--badbg:#f7e3e0;--accbg:#e4ebf7;
--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
--paper:#131418;--card:#191b20;--sunk:#15171c;--ink:#e7e5e0;--mut:#989286;
--faint:#6d675c;--line:#2b2e35;--hair:#23262c;--ok:#79c48d;--warn:#dfa94f;
--bad:#e5786c;--acc:#82a9ea;--live:#e0a836;--okbg:#1a2a20;--warnbg:#2d2617;
--badbg:#2e1d1b;--accbg:#1b2436}}
:root[data-theme=dark]{--paper:#131418;--card:#191b20;--sunk:#15171c;
--ink:#e7e5e0;--mut:#989286;--faint:#6d675c;--line:#2b2e35;--hair:#23262c;
--ok:#79c48d;--warn:#dfa94f;--bad:#e5786c;--acc:#82a9ea;--live:#e0a836;
--okbg:#1a2a20;--warnbg:#2d2617;--badbg:#2e1d1b;--accbg:#1b2436}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:14.5px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:20px 22px 90px}
.mono{font-family:var(--mono)}
.num{font-variant-numeric:tabular-nums}

/* --- шапка --- */
.rail{display:flex;gap:12px;align-items:center;flex-wrap:wrap;
font:11px/1.4 var(--mono);letter-spacing:.1em;text-transform:uppercase;
color:var(--faint)}
.rail .brand{color:var(--ink);letter-spacing:.22em}
.grow{flex:1}
.tag{border:1px solid var(--line);border-radius:3px;padding:1px 6px}
.pill{display:inline-flex;gap:6px;align-items:center;border-radius:3px;
padding:1px 7px;border:1px solid var(--line)}
.pill.on{color:var(--live);border-color:var(--live)}
.pill .dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.on .dot{animation:pulse 2.2s ease-in-out infinite}
@keyframes pulse{50%{opacity:.2}}
@media(prefers-reduced-motion:reduce){.pill.on .dot{animation:none}}
h1{font:600 23px/1.28 var(--sans);letter-spacing:-.015em;margin:12px 0 5px;
max-width:72ch}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
button{font:inherit;font-size:13px;padding:5px 11px;border-radius:6px;
border:1px solid var(--line);background:var(--card);color:var(--ink);
cursor:pointer}
button:hover{border-color:var(--mut)}
button.on{border-color:var(--acc);color:var(--acc)}
#theme{padding:2px 8px;font-size:12px;line-height:1.4}

/* --- заголовки разделов --- */
h2{font:600 11px/1 var(--mono);letter-spacing:.13em;text-transform:uppercase;
color:var(--faint);margin:30px 0 11px;display:flex;gap:11px;align-items:center}
h2::after{content:"";flex:1;height:1px;background:var(--hair)}
h2 .cnt{color:var(--mut);letter-spacing:.05em}

/* --- показатели --- */
.vitals{display:grid;gap:10px;
grid-template-columns:repeat(auto-fit,minmax(202px,1fr))}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:11px 13px 12px}
.cap{font:11px/1 var(--mono);letter-spacing:.11em;text-transform:uppercase;
color:var(--faint)}
.tile .big{font:600 21px/1.1 var(--mono);font-variant-numeric:tabular-nums;
margin-top:7px}
.tile .big small{font-size:12px;font-weight:400;color:var(--mut);
letter-spacing:0}
.split{display:flex;height:6px;border-radius:3px;overflow:hidden;
background:var(--sunk);margin-top:9px}
.split i{display:block;height:100%}
.legend{display:flex;gap:9px;flex-wrap:wrap;font-size:12px;color:var(--mut);
margin-top:7px}
.legend .sw{display:inline-block;width:8px;height:8px;border-radius:2px;
margin-right:4px;vertical-align:baseline}
.legend b{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}

/* --- сейчас --- */
.now{border:1px solid var(--live);background:var(--warnbg);border-radius:8px;
padding:12px 14px;display:flex;gap:13px;align-items:flex-start}
.now.dead{border-color:var(--bad);background:var(--badbg)}
.now .beat{width:9px;height:9px;border-radius:50%;background:var(--live);
margin-top:6px;flex:none;animation:pulse 2.2s ease-in-out infinite}
.now.dead .beat{background:var(--bad);animation:none}
@media(prefers-reduced-motion:reduce){.now .beat{animation:none}}
.now .what{font-weight:600}
.now .meta{color:var(--mut);font-size:13px;margin-top:2px}
.now .grow{flex:1}
.now .el{font:600 19px var(--mono);font-variant-numeric:tabular-nums;
color:var(--live)}
.now.dead .el{color:var(--bad)}

/* --- карточки решений --- */
.ask{background:var(--card);border:1px solid var(--line);border-left:3px solid
var(--live);border-radius:7px;padding:11px 14px;margin-bottom:8px}
.ask.blocked{border-left-color:var(--bad)}
.ask .top{font:11px/1.4 var(--mono);letter-spacing:.06em;color:var(--faint);
text-transform:uppercase;margin-bottom:4px;display:flex;gap:9px;
flex-wrap:wrap;align-items:baseline}
.ask .q{margin-bottom:8px}
.cmd{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.cmd code{flex:1;min-width:220px;padding:6px 9px;overflow-x:auto;
white-space:nowrap}
.hint{color:var(--mut);font-size:12.5px;margin-top:6px}
code{background:var(--sunk);border:1px solid var(--hair);border-radius:5px;
padding:1px 6px;font-family:var(--mono);font-size:12.5px}
pre{background:var(--sunk);border:1px solid var(--hair);border-radius:7px;
padding:11px;overflow-x:auto;font-size:12px;line-height:1.45;margin:7px 0 0;
font-family:var(--mono)}

/* --- шкала времени --- */
.tl{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:12px 14px}
.tlrow{display:grid;grid-template-columns:118px 1fr;gap:11px;
align-items:center;margin-bottom:5px}
.tlrow .who{font:12px/1.3 var(--mono);color:var(--mut);overflow:hidden;
text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
.tlrow .who:hover{color:var(--acc)}
.track{position:relative;height:15px;background:var(--sunk);border-radius:3px}
.seg{position:absolute;top:0;bottom:0;border-radius:2px;min-width:2px}
.seg.mark{width:3px;border-radius:1px;top:-2px;bottom:-2px}
.tlaxis{display:flex;justify-content:space-between;color:var(--faint);
font:11px var(--mono);margin-top:7px;border-top:1px solid var(--hair);
padding-top:5px}

/* --- задачи --- */
.bar{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin:0 0 10px}
input[type=search]{font:inherit;font-size:13.5px;padding:6px 10px;
border-radius:6px;border:1px solid var(--line);background:var(--card);
color:var(--ink);flex:1;min-width:170px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
margin-bottom:7px;overflow:hidden}
.head{display:grid;grid-template-columns:auto 1fr auto auto;gap:11px;
align-items:baseline;padding:10px 13px;cursor:pointer}
.head:hover{background:var(--sunk)}
.head .id{font:12px var(--mono);color:var(--faint)}
.head .t{font-weight:600}
.head .spark{display:flex;gap:2px;align-items:flex-end;height:14px}
.head .spark i{width:4px;background:var(--acc);border-radius:1px;display:block}
.head .spark i.zero{background:var(--ok);height:2px}
.badge{font:11px/1.5 var(--mono);letter-spacing:.05em;padding:1px 8px;
border-radius:3px;border:1px solid var(--line);color:var(--mut);
white-space:nowrap}
.b-done{color:var(--ok);border-color:var(--ok)}
.b-blocked{color:var(--bad);border-color:var(--bad);background:var(--badbg)}
.b-pending{color:var(--mut)}
.b-progress{color:var(--live);border-color:var(--live);background:var(--warnbg)}
.meta{color:var(--mut);font-size:12.5px;padding:0 13px 10px}
.body{border-top:1px solid var(--hair);padding:13px;display:none}
.card.open .body{display:block}
.card.open .head{background:var(--sunk)}
.sec{margin-bottom:15px}
.sec:last-child{margin-bottom:0}
.sec h3{font:600 11px var(--mono);letter-spacing:.1em;text-transform:uppercase;
margin:0 0 6px;color:var(--faint)}
.spec{white-space:pre-wrap;font-size:13.5px;background:var(--sunk);
border:1px solid var(--hair);border-radius:7px;padding:10px 12px}
ul.acc{margin:0;padding-left:19px}
ul.acc li{margin:3px 0}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--hair)}
th{color:var(--faint);font-weight:500;font-family:var(--mono);
font-size:11px;letter-spacing:.06em;text-transform:uppercase}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{overflow-x:auto}
.find{border-left:3px solid var(--line);padding:5px 0 5px 11px;margin:8px 0}
.find.blocker{border-color:var(--bad)}
.find.major{border-color:var(--warn)}
.find.minor{border-color:var(--line)}
.find .top{font:11px var(--mono);color:var(--faint);margin-bottom:2px;
letter-spacing:.04em}
.find .sug{color:var(--acc);font-size:13px;margin-top:3px}
.qa{border-left:3px solid var(--live);padding:7px 0 7px 12px;margin:10px 0}
.qa.answered{border-color:var(--ok)}
.qa .ans{color:var(--mut);font-size:13px;margin-top:4px}
details{margin:7px 0}
summary.det{cursor:pointer;color:var(--acc);font-size:13px;list-style:none}
summary.det::-webkit-details-marker{display:none}
summary.det::before{content:"▸ "}
details[open] summary.det::before{content:"▾ "}

/* --- события и хроника --- */
.log{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:4px 13px}
.ev{font-size:13px;padding:5px 0;border-bottom:1px solid var(--hair);
display:grid;grid-template-columns:62px 168px 1fr;gap:10px}
.ev:last-child{border-bottom:none}
.ev .tm{color:var(--faint);font-family:var(--mono);font-size:12px;
font-variant-numeric:tabular-nums}
.ev .kd{font-family:var(--mono);font-size:12px}
.ev .dt{color:var(--mut);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.ev.hot .kd{color:var(--bad)}
@media(max-width:640px){.ev{grid-template-columns:56px 1fr}.ev .dt{grid-column:1/-1;white-space:normal}}
.empty{color:var(--mut);background:var(--card);border:1px dashed var(--line);
border-radius:8px;padding:16px}
.foot{color:var(--mut);font-size:12px;margin-top:34px;
border-top:1px solid var(--hair);padding-top:12px}
.hidden{display:none!important}
"""

JS = """
// `let`, не `const`: живое обновление перепривязывает D к свежему payload
// (см. apply()) — переменную, объявленную const, переприсвоить нельзя.
let D = JSON.parse(document.getElementById('data').textContent);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// Имена — из vocab через payload, а не третьей копией здесь: копия уже
// разошлась (исход раунда шёл по-английски, пока why говорил по-русски).
const V = D.vocab || {};
const SEV = V.severity || {};
const CAT = V.category || {};
const ST = V.status || {};
const OUT = V.outcome || {};
const PH = V.phase || {};
const CLS = {done:'b-done', blocked:'b-blocked', pending:'b-pending',
             in_progress:'b-progress', in_review:'b-progress'};
// Цвет фазы — один и тот же на шкале и в легенде денег.
const PCOL = {implement:'var(--acc)', review:'var(--live)', gate:'var(--ok)',
              scope:'var(--faint)', verification:'var(--mut)',
              policy:'var(--mut)', integrity:'var(--bad)'};

// Длительность словами: секунды нужны только пока их мало.
function dur(s) {
  if (s === null || s === undefined || isNaN(s)) return '';
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + ' с';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? m + ' мин ' + r + ' с' : m + ' мин';
  const h = Math.floor(m / 60);
  return h + ' ч ' + (m % 60) + ' мин';
}
function clock(t) {
  return t ? new Date(t * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '';
}

function copy(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    const was = btn.textContent; btn.textContent = 'скопировано';
    setTimeout(() => btn.textContent = was, 1200);
  });
}
// Команда для копирования лежит в data-атрибуте: inline-onclick с JSON
// внутри одинарных кавычек разрывался апострофом в пути корня.
document.addEventListener('click', ev => {
  const btn = ev.target.closest('button.copy');
  if (btn) copy(btn.dataset.cmd, btn);
});

// Тема: выбор человека сильнее системной, поэтому живёт в localStorage.
// Хранилище бывает недоступно (приватное окно) — молча остаёмся на
// системной теме, страница обязана работать и так.
function theme(next) {
  const root = document.documentElement;
  const cur = root.getAttribute('data-theme');
  const val = next || (cur === 'dark' ? 'light' : 'dark');
  root.setAttribute('data-theme', val);
  try { localStorage.setItem('swarm-board-theme', val); } catch (e) {}
}
try {
  const saved = localStorage.getItem('swarm-board-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
} catch (e) {}

// Счётчики, которые обязаны идти сами: «идёт 4 мин 12 с» — единственное
// на странице, что меняется между перерисовками, и замерев, оно врёт.
function tick() {
  const now = Date.now() / 1000;
  document.querySelectorAll('[data-since]').forEach(el => {
    el.textContent = dur(now - Number(el.dataset.since));
  });
  document.querySelectorAll('[data-ago]').forEach(el => {
    el.textContent = dur(now - Number(el.dataset.ago)) + ' назад';
  });
}
setInterval(tick, 1000);

// --- шкала времени --------------------------------------------------------
// Не украшение: топтание, ретраи и дорогое ревью видно формой быстрее,
// чем чтением таблиц. Строим из тех же метрик, что и суммы.
function timeline() {
  const box = document.getElementById('timeline');
  if (!box) return;
  const segs = D.timeline || [];
  if (!segs.length) { box.innerHTML = ''; return; }
  const t0 = Math.min(...segs.map(s => s.t0));
  const live = D.run && D.run.live;
  const t1 = Math.max(...segs.map(s => s.t1), live ? Date.now() / 1000 : 0);
  const span = Math.max(1, t1 - t0);
  const byTask = new Map();
  segs.forEach(s => {
    if (!byTask.has(s.task)) byTask.set(s.task, []);
    byTask.get(s.task).push(s);
  });
  const title = D.tasks.reduce((m, t) => (m[t.id] = t.title || '', m), {});
  let h = '';
  byTask.forEach((list, task) => {
    const bars = list.map(s => {
      const left = (s.t0 - t0) / span * 100;
      const w = Math.max((s.t1 - s.t0) / span * 100, 0);
      const col = s.phase === 'gate' && s.ok === false ? 'var(--bad)'
                : (PCOL[s.phase] || 'var(--mut)');
      const bits = [PH[s.phase] || s.phase];
      if (s.iter) bits.push('раунд ' + s.iter);
      if (s.dur) bits.push(dur(s.dur));
      if (s.cost) bits.push('$' + s.cost.toFixed(2));
      if (s.verdict) bits.push(s.verdict);
      if (s.ok === false) bits.push('провал');
      if (s.note) bits.push(s.note);
      return `<i class="seg ${w < 0.4 ? 'mark' : ''}" style="left:${left}%;
        width:${w < 0.4 ? '' : w + '%'};background:${col}"
        title="${esc(bits.join(' · '))}"></i>`;
    }).join('');
    h += `<div class="tlrow"><div class="who" data-goto="${esc(task)}"
      title="${esc(title[task] || task)}">${esc(task || 'прогон')}</div>
      <div class="track">${bars}</div></div>`;
  });
  h += `<div class="tlaxis"><span>${clock(t0)}</span>
    <span>${dur(span)}</span><span>${clock(t1)}</span></div>`;
  box.innerHTML = h;
}

function findings(list) {
  if (!list.length) return '';
  return list.map(f => `<div class="find ${esc(f.severity)}">
    <div class="top">${esc(SEV[f.severity] || f.severity)} · ${esc(CAT[f.category] || f.category)}
    ${f.file ? '· ' + esc(f.file) + (f.line ? ':' + f.line : '') : ''}</div>
    <div>${esc(f.issue)}</div>
    ${f.suggestion ? `<div class="sug">→ ${esc(f.suggestion)}</div>` : ''}
  </div>`).join('');
}

function taskBody(t) {
  let h = '';
  if (t.spec) h += `<div class="sec"><h3>Что просили сделать</h3>
    <div class="spec">${esc(t.spec)}</div></div>`;
  if (t.acceptance?.length) h += `<div class="sec"><h3>Критерии приёмки</h3>
    <ul class="acc">${t.acceptance.map(a => `<li>${esc(a)}</li>`).join('')}</ul></div>`;
  if (t.human_decisions?.length) h += `<div class="sec"><h3>Ваши решения по задаче</h3>
    <ul class="acc">${t.human_decisions.map(d => `<li>${esc(d)}</li>`).join('')}</ul></div>`;

  if (t._rounds?.length) {
    h += `<div class="sec"><h3>Траектория схождения</h3><div class="scroll">
      <table><tr><th>раунд</th><th>вердикт</th><th>исход</th>
      <th class="n">находок</th><th class="n">о замысле</th></tr>` +
      t._rounds.map(r => `<tr><td>${esc(r.round)}</td><td>${esc(r.verdict)}</td>
        <td>${esc(OUT[r.outcome] || r.outcome)}</td><td class="n">${esc(r.findings ?? '')}</td>
        <td class="n">${esc(r.intent ?? '')}</td></tr>`).join('') +
      `</table></div><div class="hint">${esc(trend(t._rounds))}</div></div>`;
  }

  if (t._phases?.length) h += `<div class="sec"><h3>Как шла работа</h3><div class="scroll">
    <table><tr><th>время</th><th>раунд</th><th>фаза</th><th>итог</th>
    <th class="n">сек</th><th class="n">$</th></tr>` +
    t._phases.map(p => `<tr><td>${esc(p.ts)}</td><td>${esc(p.iter ?? '')}</td>
      <td>${esc(p.phase)}</td><td>${esc(p.result)}</td>
      <td class="n">${p.dur ? p.dur.toFixed(1) : ''}</td>
      <td class="n">${p.cost ? p.cost.toFixed(2) : ''}</td></tr>`).join('') +
    `</table></div></div>`;

  (t._verdicts || []).forEach(v => {
    if (v.failed) {
      h += `<div class="sec"><h3>Ревьюер, ${esc(v.round)} — ответа нет</h3>
        <div class="hint">${esc(v.why)}</div></div>`;
      return;
    }
    h += `<div class="sec"><h3>Ревьюер, ${esc(v.round)} → ${esc(v.verdict)}</h3>
      <div>${esc(v.summary)}</div>` +
      (v.analysis ? `<details><summary class="det">рассуждение ревьюера</summary>
        <div class="spec">${esc(v.analysis)}</div></details>` : '') +
      findings(v.findings) +
      (v.requests?.length ? `<div class="hint">запрошены проверки: ` +
        v.requests.map(r => esc(r.kind)).join(', ') + `</div>` : '') +
      (v.notes?.length ? `<div class="sec"><h3>Замечено вне рамок задачи</h3>
        <ul class="acc">${v.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>
        <div class="hint">готовый бэклог: ревьюер это увидел, но чинить не просил</div>
        </div>` : '') + `</div>`;
  });

  if (t._suppressed?.length) h += `<div class="sec"><h3>Подавлено политиками прогона</h3>
    ${t._suppressed.map(s => `<div class="find minor"><div class="top">${esc(s.policy || '')}
    ${s.severity ? '· ' + esc(SEV[s.severity] || s.severity) : ''}</div>
    <div>${esc(s.issue)}</div></div>`).join('')}</div>`;

  (t._questions || []).forEach(q => {
    const cmd = `swarm --root ${D.root} answer ${q.qid} "…"`;
    h += `<div class="sec"><div class="qa ${q.status === 'answered' ? 'answered' : ''}">
      <div><b>${esc(q.qid)}</b> · ${esc(q.qkind || '')}</div>
      <div>${esc(q.question)}</div>
      ${q.answer ? `<div class="ans">→ ${esc(q.answer)}</div>` :
        `<div class="cmd"><code>${esc(cmd)}</code>
         <button class="copy" data-cmd="${esc(cmd)}">скопировать</button></div>`}
    </div></div>`;
  });

  if (t._diff) h += `<div class="sec"><h3>Что вошло в код</h3>
    <pre>${esc(t._diff)}</pre></div>`;

  if (t._streams?.length) h += `<div class="sec"><h3>Сырые логи</h3>
    <div class="hint">потоки исполнителя не встроены (сотни КБ):
    <code>${esc(D.swarm_dir)}/log/</code> — ${t._streams.map(esc).join(', ')}</div></div>`;
  return h;
}

// Ряд находок читается строго (§9.3): убывание — только строгое, ровный
// ряд называется топтанием. «Работа сходится» на ряде 3, 3, 3 не просто
// бесполезно — оно противоречит диагнозу петли и отправляет чинить не то.
function trend(rounds) {
  const n = rounds.map(r => r.findings).filter(x => typeof x === 'number');
  if (n.length < 2) return 'один раунд — о схождении говорить нечего';
  const down = n.every((v, i) => i === 0 || v < n[i - 1]);
  const flat = n.every(v => v === n[0]);
  if (down) return 'находок меньше с каждым раундом — работа сходится';
  if (flat) return 'число находок не меняется — петля топчется';
  return 'ряд находок неровный — сходимость не подтверждается';
}

function tasks() {
  const q = document.getElementById('search').value.toLowerCase();
  const filter = document.querySelector('.bar button.on')?.dataset.f || 'all';
  const box = document.getElementById('tasks');
  const order = ['in_progress', 'blocked', 'pending', 'in_review', 'done'];
  const sorted = [...D.tasks].sort((a, b) =>
    order.indexOf(a.status) - order.indexOf(b.status));
  let shown = 0;
  box.innerHTML = sorted.map(t => {
    const hay = JSON.stringify(t).toLowerCase();
    const okF = filter === 'all' || t.status === filter;
    const okQ = !q || hay.includes(q);
    if (!(okF && okQ)) return '';
    shown++;
    const bits = [`тип ${esc(t.type || '—')}`];
    if (t.paths?.length) bits.push('файлы: ' + esc(t.paths.join(', ')));
    if (t._cost) bits.push(`$${t._cost}`);
    if (t.commit) bits.push(`коммит ${esc(t.commit)}`);
    // Поля прошлого исхода не чистятся при закрытии задачи: показывать
    // «причина: invalid_verdict» рядом с «закрыта» — вводить в заблуждение.
    if (t.status === 'blocked') {
      if (t.reason) bits.push(`причина: ${esc(t.reason)}`);
      if (t.stash) bits.push(`работа сохранена: ${esc(t.stash)}`);
      if (t.diagnosis) bits.push(`диагноз: ${esc(t.diagnosis)}`);
    }
    if (t.deps?.length && t.status === 'pending')
      bits.push(`ждёт: ${esc(t.deps.join(', '))}`);
    if (t.iterations) bits.push(`раундов ${esc(t.iterations)}`);
    const nf = (t._verdicts || []).reduce((n, v) => n + (v.findings?.length || 0), 0);
    if (nf) bits.push(`находок ${nf}`);
    // Спарклайн — та же траектория схождения, но читаемая не открывая
    // карточку: столбики по раундам, зелёная риска — раунд без находок.
    const series = (t._rounds || []).map(r => r.findings)
      .filter(x => typeof x === 'number');
    const top = Math.max(1, ...series);
    // Один раунд — не ряд: столбик из одного значения формы не несёт, а
    // выглядит соринкой у каждой закрытой с первого раза задачи.
    const spark = series.length > 1 ? `<span class="spark">` + series.map(v =>
      `<i class="${v ? '' : 'zero'}" style="height:${v ? Math.max(3, v / top * 14) : 2}px"
        title="находок ${v}"></i>`).join('') + `</span>` : '<span></span>';
    return `<div class="card" data-id="${esc(t.id)}" onclick="if(!event.target.closest('button'))
        this.classList.toggle('open')">
      <div class="head"><span class="id">${esc(t.id)}</span>
      <span class="t">${esc(t.title)}</span>${spark}
      <span class="badge ${CLS[t.status] || ''}">${esc(ST[t.status] || t.status)}</span></div>
      <div class="meta">${bits.join(' · ')}</div>
      <div class="body">${taskBody(t)}</div></div>`;
  }).join('');
  if (!shown) box.innerHTML = '<div class="empty">ничего не найдено</div>';
}

function render() { tasks(); timeline(); tick(); }

// Привязка обработчиков — отдельной функцией, а не разовым кодом при
// загрузке: после живой подмены .wrap (см. apply()) старые кнопки и
// поле поиска уже не те DOM-узлы, к которым что-то привязано, и без
// повторного вызова фильтр с поиском переставали бы отвечать на клики.
function bind() {
  document.querySelectorAll('.bar button[data-f]').forEach(b => b.onclick = () => {
    document.querySelectorAll('.bar button[data-f]').forEach(x => x.classList.remove('on'));
    b.classList.add('on'); tasks();
  });
  document.getElementById('search').oninput = tasks;
  const th = document.getElementById('theme');
  if (th) th.onclick = () => theme();
  const ev = document.getElementById('toggle-ev');
  if (ev) ev.onclick = e => {
    const el = document.getElementById('events');
    el.classList.toggle('hidden');
    e.target.textContent = el.classList.contains('hidden')
      ? 'показать хронику прогона' : 'скрыть хронику';
  };
  // Клик по имени задачи на шкале открывает её карточку: шкала
  // показывает форму, а объяснение формы лежит в карточке.
  document.querySelectorAll('.tlrow .who').forEach(w => w.onclick = () => {
    const card = document.querySelector(`#tasks .card[data-id="${CSS.escape(w.dataset.goto)}"]`);
    if (!card) return;
    card.classList.add('open');
    card.scrollIntoView({block: 'center', behavior: 'smooth'});
  });
}
bind();
render();
bind();   // строки шкалы появились только что — им тоже нужны обработчики

// Живое обновление: раньше страница целиком перезагружала саму себя
// каждые 15 с — рабочий приём, но раскрытая карточка схлопывалась и
// прокрутка прыгала ровно тогда, когда её читали. Сервер
// (boardserve.BoardServer) отдаёт по тому же адресу свежий HTML, а
// сюда — только подмена DOM: страница жива, вкладка не мигает, никакой
// навигации не происходит вовсе.
if (location.protocol !== 'http:' && location.protocol !== 'https:') {
  // Открыт как файл (file://) — сервера за ним нет и быть не может:
  // честнее сказать это прямо, чем гонять fetch в никуда.
  document.getElementById('live').textContent =
    'это снимок на диске: живая доска — live_board = true в swarm.toml, ' +
    'её адрес печатает команда запуска — здесь не обновится';
} else {
  let etag = null;
  let misses = 0;
  const timer = setInterval(() => {
    const headers = etag ? {'If-None-Match': etag} : {};
    fetch(location.href, {cache: 'no-store', headers})
      .then(res => {
        misses = 0;
        if (res.status === 304) return null;   // ETag совпал — контент тот же
        etag = res.headers.get('ETag') || etag;
        return res.text();
      })
      .then(txt => { if (txt) apply(txt); })
      .catch(() => {
        // Три подряд неудачи — не сбой сети, а конец прогона: сервер
        // живёт, пока жива петля, и его исчезновение — единственный
        // надёжный признак того, что смотреть дальше некуда.
        if (++misses >= 3) {
          clearInterval(timer);
          document.getElementById('live').textContent =
            'сервер прогона остановлен — прогон завершён, ' +
            'доска замерла на финальном состоянии';
        }
      });
  }, 5000);
}

function apply(txt) {
  const fresh = new DOMParser().parseFromString(txt, 'text/html');
  const freshData = fresh.getElementById('data');
  const curData = document.getElementById('data');
  // Тот же payload — эхо собственного запроса (или пересборка без
  // изменений на стороне сервера): перерисовывать нечего.
  if (!freshData || freshData.textContent === curData.textContent) return;
  const freshWrap = fresh.querySelector('.wrap');
  const curWrap = document.querySelector('.wrap');
  if (!freshWrap || !curWrap) return;
  // Состояние интерфейса живёт в DOM, а не в D — подмена .wrap его
  // сотрёт, поэтому снимается ДО подмены и возвращается после.
  const openIds = [...document.querySelectorAll('#tasks .card.open')]
    .map(c => c.dataset.id);
  const searchVal = document.getElementById('search').value;
  const activeFilter = document.querySelector('.bar button.on')?.dataset.f || 'all';
  const evEl0 = document.getElementById('events');
  const evHidden = !evEl0 || evEl0.classList.contains('hidden');

  curWrap.replaceWith(document.adoptNode(freshWrap));
  curData.textContent = freshData.textContent;
  D = JSON.parse(curData.textContent);
  bind();   // свежий .wrap принёс новые кнопки и поле поиска

  document.getElementById('search').value = searchVal;
  const btn = document.querySelector(`.bar button[data-f="${activeFilter}"]`);
  if (btn) {
    document.querySelectorAll('.bar button[data-f]').forEach(x => x.classList.remove('on'));
    btn.classList.add('on');
  }
  const evEl = document.getElementById('events');
  const evBtn = document.getElementById('toggle-ev');
  if (evEl && !evHidden) evEl.classList.remove('hidden');
  if (evEl && evHidden) evEl.classList.add('hidden');
  if (evBtn) evBtn.textContent =
    evHidden ? 'показать хронику прогона' : 'скрыть хронику';

  render();
  bind();   // шкала перерисована — её строки снова новые узлы
  // CSS.escape: id задачи попадает в селектор атрибута буквально, а не
  // как текст — без экранирования свои же скобки/точки в id ломали бы
  // запрос вместо того, чтобы найти карточку.
  openIds.forEach(id => {
    const card = document.querySelector(`#tasks .card[data-id="${CSS.escape(id)}"]`);
    if (card) card.classList.add('open');
  });
  document.getElementById('live').textContent =
    'обновлено ' + new Date().toLocaleTimeString();
}
"""


def _k(n: float) -> str:
    """Тысячи — только там, где они есть: 480 токенов не «0k»."""
    return f"{n / 1000:.0f}k" if n >= 1000 else f"{n:.0f}"


def _bar(parts: list[tuple[float, str]], total: float) -> str:
    """Полоса-раскладка: доли одного целого, а не отдельные числа."""
    if total <= 0:
        return ""
    out = ['<div class="split">']
    for value, color in parts:
        if value > 0:
            out.append(
                f'<i style="width:{value / total * 100:.4g}%;background:{color}"></i>'
            )
    out.append("</div>")
    return "".join(out)


def _legend(items: list[tuple[str, str, str]]) -> str:
    """Подписи к полосе: цвет, имя, значение — иначе полоса нечитаема."""
    e = html.escape
    if not items:
        return ""
    cells = "".join(
        f'<span><b class="sw" style="background:{c}"></b>'
        f"{e(name)} <b>{e(val)}</b></span>"
        for c, name, val in items
    )
    return f'<div class="legend">{cells}</div>'


def _vitals(
    board: dict[str, Any], tasks: list[dict[str, Any]], by_status: dict[str, int]
) -> str:
    """Показатели прогона: четыре ответа, ради которых доску открывают —
    сколько сделано, сколько стоило и кому, сходится ли ревью, зелен ли гейт.
    """
    e = html.escape
    run = board.get("run") or {}
    totals = board.get("totals") or {}
    phases = totals.get("phases") or {}
    tiles = []

    # 1. Задачи.
    n = len(tasks)
    done, blocked = by_status.get("done", 0), by_status.get("blocked", 0)
    active = by_status.get("in_progress", 0) + by_status.get("in_review", 0)
    pending = by_status.get("pending", 0)
    bar = _bar(
        [
            (done, "var(--ok)"),
            (blocked, "var(--bad)"),
            (active, "var(--live)"),
            (pending, "var(--faint)"),
        ],
        n,
    )
    marks: list[tuple[str, str, str]] = [("var(--ok)", "закрыто", str(done))]
    if blocked:
        marks.append(("var(--bad)", "заблокировано", str(blocked)))
    if active:
        marks.append(("var(--live)", "в работе", str(active)))
    if pending:
        marks.append(("var(--faint)", "в очереди", str(pending)))
    legend = _legend(marks)
    tiles.append(
        f'<div class="tile"><div class="cap">задачи</div>'
        f'<div class="big">{done}<small> из {n}</small></div>'
        f"{bar}{legend}</div>"
    )

    # 2. Деньги — по ролям. Сумма не отвечает на вопрос «дорого — это кто».
    total = board.get("total", 0)
    budget = run.get("budget")
    paid = [
        (p, phases[p]["cost"])
        for p in PHASE_ORDER
        if p in phases and phases[p]["cost"] > 0
    ]
    paid += [
        (p, row["cost"])
        for p, row in phases.items()
        if p not in PHASE_ORDER and row["cost"] > 0
    ]
    if budget:
        share = min(1.0, float(total) / budget) if budget else 0.0
        money_bar = _bar([(share, "var(--live)"), (1 - share, "var(--sunk)")], 1.0)
        cap = f"<small> из ${budget:g}</small>"
    else:
        money_bar = _bar(
            [(c, PHASE_COLOR.get(p, "var(--mut)")) for p, c in paid],
            sum(c for _, c in paid),
        )
        cap = ""
    legend = _legend(
        [
            (PHASE_COLOR.get(p, "var(--mut)"), PHASE_RU.get(p, p), f"${c:.2f}")
            for p, c in paid
        ]
    )
    tiles.append(
        f'<div class="tile"><div class="cap">{e(vocab.SPEND_LABEL)}</div>'
        f'<div class="big">${total}{cap}</div>{money_bar}{legend}</div>'
    )

    # 3. Ревью: вердикты и находки по весу.
    verdicts = totals.get("verdicts") or {}
    severity = totals.get("severity") or {}
    rounds = sum(verdicts.values())
    ok = verdicts.get("approve", 0)
    again = verdicts.get("request_changes", 0)
    lost = verdicts.get("нет вердикта", 0)
    bar = _bar(
        [(ok, "var(--ok)"), (again, "var(--live)"), (lost, "var(--bad)")], rounds
    )
    marks = []
    if ok:
        marks.append(("var(--ok)", "approve", str(ok)))
    if again:
        marks.append(("var(--live)", "правки", str(again)))
    if lost:
        marks.append(("var(--bad)", "без вердикта", str(lost)))
    legend = _legend(marks)
    finds = sum(severity.values())
    sev_line = " · ".join(
        f"{SEVERITY_RU.get(s, s)} {severity[s]}"
        for s in ("blocker", "major", "minor")
        if severity.get(s)
    )
    tiles.append(
        f'<div class="tile"><div class="cap">ревью</div>'
        f'<div class="big">{rounds}<small> вердиктов, находок '
        f"{finds}</small></div>{bar}{legend}"
        + (f'<div class="hint">{e(sev_line)}</div>' if sev_line else "")
        + "</div>"
    )

    # 4. Гейт и контекст: зелен ли прогон тестов и сколько стоил контекст.
    gate = totals.get("gate") or {}
    g_ok, g_bad = gate.get("ok", 0), gate.get("fail", 0)
    bar = _bar([(g_ok, "var(--ok)"), (g_bad, "var(--bad)")], g_ok + g_bad)
    tokens = totals.get("tokens") or {}
    read, write = tokens.get("cache_read", 0), tokens.get("cache_write", 0)
    cached = read / (read + write) * 100 if (read + write) else 0
    ctx = ""
    if tokens.get("in") or read:
        # Округление до тысяч превращало 480 токенов в «0k» — цифра,
        # которая называет не масштаб, а ноль там, где его нет.
        ctx = (
            f'<div class="hint">контекст: вход {_k(tokens.get("in", 0))}, '
            f"выход {_k(tokens.get('out', 0))}"
            + (f", из кэша {cached:.0f}%" if read + write else "")
            + "</div>"
        )
    tiles.append(
        f'<div class="tile"><div class="cap">гейт</div>'
        f'<div class="big">{g_ok}<small> зелёных из {g_ok + g_bad}'
        f"</small></div>{bar}{ctx}</div>"
    )
    return f'<div class="vitals">{"".join(tiles)}</div>'


def _now_panel(board: dict[str, Any]) -> str:
    """Что идёт ПРЯМО СЕЙЧАС — или где прогон оборвался.

    Одна и та же запись `now.json` значит разное у живой и мёртвой петли,
    поэтому панель никогда не называет фазу, не назвав состояния петли.
    """
    e = html.escape
    run = board.get("run") or {}
    now, broke = run.get("now"), run.get("broke_at")
    row = now or broke
    if not isinstance(row, dict):
        return ""
    phase = PHASE_RU.get(str(row.get("phase")), str(row.get("phase") or "фаза"))
    since = _epoch(row.get("since"))
    bits = []
    if row.get("task"):
        bits.append(f"задача {row['task']}")
    if row.get("iter"):
        bits.append(f"раунд {row['iter']}")
    bits.extend(
        f"{field} {row[field]}"
        for field in ("engine", "model", "attempt")
        if row.get(field)
    )
    meta = " · ".join(str(b) for b in bits)
    if now:
        elapsed = f'<span class="el" data-since="{since:.0f}"></span>' if since else ""
        return (
            f'<section class="now"><span class="beat"></span>'
            f'<div class="grow"><div class="what">сейчас: {e(phase)}</div>'
            f'<div class="meta">{e(meta)}</div></div>{elapsed}</section>'
        )
    cmd = f"swarm --root {board.get('root', '.')} resume"
    return (
        f'<section class="now dead"><span class="beat"></span>'
        f'<div class="grow"><div class="what">прогон оборвался на фазе '
        f'«{e(phase)}»</div><div class="meta">{e(meta)} · петля не '
        f"запущена, а отметка о работе осталась — процесс убит, "
        f"а не завершён</div>"
        f'<div class="cmd"><code>{e(cmd)}</code>'
        f'<button class="copy" data-cmd="{e(cmd)}">скопировать</button>'
        f"</div></div></section>"
    )


def _decisions(
    board: dict[str, Any], open_q: list[dict[str, Any]], blocked: list[dict[str, Any]]
) -> str:
    """Очередь человека: вопросы петли и заблокированные задачи вместе.

    Раньше вопрос лежал в одном месте страницы, блокировка — в другом, а
    команда, которой её снимают, не называлась вовсе. Ждут человека — и
    то и другое; список должен быть один и с готовой командой.
    """
    e = html.escape
    root = board.get("root", ".")
    out = []
    for q in open_q:
        cmd = f'swarm --root {root} answer {q["qid"]} "…"'
        asked = q.get("asked")
        age = f'<span data-ago="{asked:.0f}"></span>' if asked else ""
        out.append(
            f'<div class="ask"><div class="top"><b>{e(q["qid"])}</b>'
            f"<span>задача {e(str(q.get('task') or '—'))}</span>"
            f"<span>{e(str(q.get('qkind', '')))}</span>"
            f'<span class="grow"></span>{age}</div>'
            f'<div class="q">{e(str(q.get("question", "")))}</div>'
            # Команда — в data-атрибуте, а не в inline-onclick: JSON
            # экранирует кавычки, но не апостроф, и путь корня с «'»
            # разрывал одинарно-кавыченный атрибут (инъекция разметки).
            f'<div class="cmd"><code>{e(cmd)}</code>'
            f'<button class="copy" data-cmd="{e(cmd)}">скопировать</button></div>'
            f'<div class="hint">если решение требует тронуть файл вне границ '
            f"задачи — добавьте <code>--add-path путь</code></div></div>"
        )
    for t in blocked:
        cmd = f"swarm --root {root} retry {t.get('id')}"
        why = " · ".join(str(x) for x in (t.get("reason"), t.get("diagnosis")) if x)
        out.append(
            f'<div class="ask blocked"><div class="top">'
            f"<b>{e(str(t.get('id')))}</b><span>задача заблокирована</span></div>"
            f'<div class="q">{e(str(t.get("title") or ""))}</div>'
            + (f'<div class="hint">{e(why)}</div>' if why else "")
            + (
                f'<div class="hint">работа сохранена: '
                f"<code>{e(str(t['stash']))}</code></div>"
                if t.get("stash")
                else ""
            )
            + f'<div class="cmd"><code>{e(cmd)}</code>'
            f'<button class="copy" data-cmd="{e(cmd)}">скопировать</button>'
            f'</div><div class="hint">разбор причины: '
            f"<code>swarm --root {e(root)} why {e(str(t.get('id')))}</code>"
            f"</div></div>"
        )
    return "".join(out)


def render(board: dict[str, Any]) -> str:
    e = html.escape
    tasks = board.get("tasks") or []
    open_q = [q for q in board.get("questions") or [] if q.get("status") == "open"]
    blocked = [t for t in tasks if t.get("status") == "blocked"]
    by_status: dict[str, int] = {}
    for t in tasks:
        by_status[str(t.get("status"))] = by_status.get(str(t.get("status")), 0) + 1

    unfinished = board.get("unfinished") or []
    run_level = board.get("run_level") or []
    run = board.get("run") or {}

    # Шапка: чей это прогон, жив ли он и когда о нём в последний раз
    # что-то знали. «Живой» здесь — про ПЕТЛЮ (блокировка состояния), а
    # `#live` ниже — про страницу (сервер за ней); это разные факты, и
    # смешивать их в одну надпись значит врать об одном из двух.
    live = bool(run.get("live"))
    pill = (
        '<span class="pill on"><span class="dot"></span>петля идёт</span>'
        if live
        else '<span class="pill"><span class="dot"></span>петля не запущена</span>'
    )
    ident = []
    if run.get("id"):
        ident.append(f'<span class="mono">прогон {e(str(run["id"]))}</span>')
    if run.get("sha"):
        ident.append(f'<span class="tag">код {e(str(run["sha"]))}</span>')

    sub = [f"{len(tasks)} задач"]
    if run.get("t0") and run.get("t1"):
        span = float(run["t1"]) - float(run["t0"])
        if span >= 3600:
            sub.append(f"прогон {int(span // 3600)} ч {int(span % 3600 // 60)} мин")
        elif span >= 60:
            sub.append(f"прогон {int(span // 60)} мин")
        else:
            sub.append(f"прогон {int(span)} с")
    if run.get("t1"):
        sub.append(f'последнее событие <span data-ago="{float(run["t1"]):.0f}"></span>')

    rail = (
        f'<div class="rail"><span class="brand">рой</span>'
        f'{"".join(ident)}<span class="grow"></span>{pill}'
        f'<button id="theme" title="светлая или тёмная">◐ тема</button>'
        f"</div>"
    )
    # Кодировка объявляется в самой странице, а не только заголовком
    # сервера: файл `.swarm/board.html` открывают и напрямую (file://),
    # где заголовка нет вовсе — и весь русский текст превращался в
    # мусор, хотя руководство оператора велит открывать именно файл.
    parts = [
        '<meta charset="utf-8">',
        f"<title>Доска прогона</title><style>{CSS}</style>",
        '<div class="wrap">',
        rail,
        f"<h1>{e(board.get('goal') or 'цель не задана')}</h1>",
        f'<div class="sub">{" · ".join(sub)}</div>',
    ]

    parts.append(_vitals(board, tasks, by_status))

    now_panel = _now_panel(board)
    if now_panel:
        parts.append("<h2>Настоящее</h2>")
        parts.append(now_panel)

    if open_q or blocked:
        parts.append(
            f'<h2>Ждут вас <span class="cnt">{len(open_q) + len(blocked)}</span></h2>'
        )
        parts.append(_decisions(board, open_q, blocked))

    # Эскалации «исполнитель сдался»: сдача — это факт журнала, а не только
    # открытый вопрос; уже закрытые вопросы остаются здесь историей — счёт
    # сдач за прогон оператор обязан видеть, не раскрывая хронику.
    escalations = board.get("escalations") or []
    if escalations:
        parts.append(
            f'<h2>Эскалации <span class="cnt">{len(escalations)}</span></h2>'
        )
        parts.append('<div class="log">')
        parts.extend(
            f'<div class="ev"><span class="tm">'
            f"{e(str(r.get('ts') or '')[11:19])}</span>"
            f'<span class="kd">{e(str(r.get("task") or ""))}</span>'
            f'<span class="dt">раунд {e(str(r.get("round") or ""))}'
            f" — {e(str(r.get('summary') or 'сдался без объяснения'))}"
            + (f" · вопрос {e(str(r.get('qid')))}" if r.get("qid") else "")
            + (f" · {e(_Q_STATUS_RU[str(r.get('status'))])}"
               if r.get("status") in _Q_STATUS_RU else "")
            + "</span></div>"
            for r in escalations
        )
        parts.append("</div>")

    # События уровня прогона и шаги без исхода — не хроника: остановку по
    # бюджету, сорванное планирование и падение посреди коммита доска
    # прятала за кнопкой «показать хронику», и прогон, встав, выглядел
    # спокойным. То, что объясняет тишину очереди, обязано быть видно сразу.
    if run_level:
        parts.append("<h2>События прогона</h2>")
        parts.append('<div class="log">')
        parts.extend(
            f'<div class="ev"><span class="tm">'
            f"{e(str(r.get('ts') or '')[11:19])}</span>"
            f'<span class="kd">{e(vocab.ru(KIND_RU, r.get("kind")))}</span>'
            f'<span class="dt">{e(vocab.narrate(r))}</span></div>'
            for r in run_level
        )
        parts.append("</div>")
    if unfinished:
        parts.append("<h2>Шаги без исхода — прогон падал</h2>")
        parts.append('<div class="log">')
        parts.extend(
            f'<div class="ev hot"><span class="tm"></span>'
            f'<span class="kd">{e(str(r.get("task") or ""))}'
            f'</span><span class="dt">{e(vocab.narrate(r))}</span></div>'
            for r in unfinished
        )
        parts.append("</div>")
        parts.append(
            f'<div class="hint">интент без записи о завершении: '
            f"<code>swarm --root {e(str(board.get('root', '.')))} "
            f"resume</code> разберётся</div>"
        )

    if board.get("timeline"):
        parts.append('<h2>Ход прогона <span class="cnt">по фазам</span></h2>')
        parts.append('<div class="tl" id="timeline"></div>')
        parts.append(
            _legend(
                [
                    (PHASE_COLOR[p], PHASE_RU.get(p, p), "")
                    for p in ("implement", "review", "gate", "scope")
                    if (board.get("totals") or {}).get("phases", {}).get(p)
                ]
            )
        )
        parts.append(
            '<div class="hint">строка — задача, отрезок — фаза; '
            "засечка вместо отрезка значит, что у фазы нет "
            "измеренной длительности (гейт, границы). Наведите "
            "на отрезок — время, деньги и исход; щёлкните по имени "
            "задачи слева — откроется её карточка.</div>"
        )

    parts.append(f'<h2>Задачи <span class="cnt">{len(tasks)}</span></h2>')
    parts.append(
        '<div class="bar">'
        '<input type="search" id="search" placeholder="поиск по задачам, '
        'находкам, файлам…">'
        '<button data-f="all" class="on">все</button>'
        '<button data-f="pending">в очереди</button>'
        '<button data-f="blocked">заблокированы</button>'
        '<button data-f="done">закрыты</button></div>'
    )
    parts.append('<div id="tasks"></div>')
    if not tasks:
        parts.append(
            '<div class="empty">Очередь пуста: задач ещё нет. '
            "Их приносит <code>swarm plan</code> — или "
            "<code>swarm go</code>, который планирует и исполняет "
            "сам. Страница наполнится, как только в "
            "<code>.swarm/tasks.json</code> появится очередь.</div>"
        )

    events = board.get("events") or []
    parts.append("<h2>Хроника</h2>")
    parts.append('<button id="toggle-ev">показать хронику прогона</button>')
    parts.append('<div id="events" class="log hidden" style="margin-top:9px">')
    parts.extend(
        f'<div class="ev"><span class="tm">{e(str(ev.get("ts", "")))}</span>'
        f'<span class="kd">{e(str(ev.get("kind_ru", "")))}</span>'
        f'<span class="dt">{e(str(ev.get("task") or ""))} '
        f"{e(str(ev.get('detail', '')))}</span></div>"
        for ev in events
    )
    parts.append("</div>")

    # Формулировка точная намеренно: этот файл — снимок на момент сборки,
    # а не то, что видит человек во время прогона. Живьём страницу отдаёт
    # boardserve.BoardServer (см. cli._board_open), и обновляется она на
    # месте, без перезагрузки (§ подмена .wrap в JS) — врать про F5 или
    # про самоперезагрузку значило бы обещать то, чего у статичного файла
    # нет и не может быть.
    root = e(str(board.get("root", ".")))
    parts.append(
        f'<div class="foot">Этот файл — снимок на момент сборки; во время '
        f"прогона доска живая (адрес печатает команда запуска) и "
        f"обновляется на месте без перезагрузки — петля переписывает файл "
        f"после каждого раунда, живой сервер каждый раз собирает страницу "
        f"заново. Посмотреть живьём вне прогона: "
        f"<code>swarm --root {root} board --serve</code>; "
        f"разовый снимок: <code>swarm --root {root} board</code>.<br>"
        f"Собрано: {e(str(board.get('built', '')))} "
        f'<span id="live"></span></div></div>'
    )

    # Словари имён едут на страницу ИЗ vocab, а не живут третьей копией в
    # JS: копия уже разошлась — исход раунда доска показывала по-английски,
    # пока `why` говорил по-русски.
    payload = json.dumps(
        dict(
            board,
            vocab={
                "severity": vocab.SEVERITY_RU,
                "category": vocab.CATEGORY_RU,
                "status": vocab.STATUS_RU,
                "outcome": vocab.OUTCOME_RU,
                "phase": vocab.PHASE_RU,
            },
        ),
        ensure_ascii=False,
    ).replace("</", "<\\/")
    parts.append(f'<script type="application/json" id="data">{payload}</script>')
    parts.append(f"<script>{JS}</script>")
    return "\n".join(parts)


def build(
    root: str | pathlib.Path,
    out: str | pathlib.Path | None = None,
) -> tuple[pathlib.Path, dict[str, Any]]:
    board = collect(root)
    page = render(board)
    out = pathlib.Path(out) if out else pathlib.Path(root) / ".swarm" / "board.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out, board
