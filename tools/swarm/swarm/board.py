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
# Потолок диффа на задачу. Прежние 12 000 знаков резали 39 коммитов из 69
# на прогоне cod-doc 2026-09 — дифф нельзя было увидеть целиком. Страница
# показывает его свёрнутым по файлам и рисует строки только по запросу,
# поэтому размер встроенного текста больше не равен размеру DOM.
MAX_DIFF_CHARS = 80000

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
# События прогона, которые сопровождают КАЖДЫЙ запуск и остановку не
# объясняют: версии агентов, пересборка памяти, применённый план. На
# стенде cod-doc их было 98 из 98 — блок «События прогона» над задачами
# стал экраном рутины. Список закрытый в обратную сторону: рутиной
# считается только названное здесь, всё незнакомое (plan_failed,
# остановка по бюджету, новый вид) остаётся наверху.
ROUTINE_KINDS = frozenset({"agent_versions", "memory_reflect", "plan_applied"})
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


def _coddoc_base(root: pathlib.Path) -> str | None:
    """Адрес проекта в веб-интерфейсе cod-doc — куда ведут ID задач и ADR.

    Задачи роя носят ID задачи cod-doc в названии (`ADO-226 — …`), но на
    доске это был мёртвый текст: чтобы открыть задачу, её искали руками.
    Первым спрашивается `cod_doc_url` в `swarm.toml` — стенд часто клон
    проекта (`cod-doc-swarm`), и сопоставить его с проектом cod-doc по
    пути нельзя. Без ключа — реестр `~/.cod-doc/config.yaml`: проект, чей
    `path` совпал с корнем, на `api_host:api_port`. Не нашлось — ссылок
    нет: ссылка наугад хуже текста.
    """
    path = root / "swarm.toml"
    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        cfg = {}
    url = cfg.get("cod_doc_url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url.rstrip("/")
    try:
        text = (pathlib.Path.home() / ".cod-doc" / "config.yaml").read_text(
            encoding="utf-8"
        )
    except OSError:
        return None
    # YAML разбирается узко, без зависимости: плоские ключи верхнего уровня
    # и пары name/path внутри элементов списка `projects`.
    top = dict(re.findall(r"(?m)^(api_host|api_port):\s*(\S+)\s*$", text))
    host, port = top.get("api_host", "127.0.0.1"), top.get("api_port")
    if not port:
        return None
    want = root.resolve()
    for block in re.split(r"(?m)^- ", text)[1:]:
        name = re.search(r"(?m)^\s*name:\s*(\S+)\s*$", block)
        where = re.search(r"(?m)^\s*path:\s*(.+?)\s*$", block)
        if not (name and where):
            continue
        if pathlib.Path(where.group(1)).expanduser() == want:
            return f"http://{host}:{port}/p/{name.group(1)}"
    return None


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


TITLE_MAX = 140


def _goal_parts(goal: str) -> tuple[str, str]:
    """Заголовок доски и остальная постановка.

    Цель прогона — это часто вся постановка волны: на прогоне cod-doc
    2026-09-23 `--goal` занял 2036 знаков с критериями приёмки, и доска
    ставила его целиком в `<h1>` — экран полужирной стены. Заголовком
    идёт первая строка; длинная — режется по концу первой фразы (не
    раньше 20-го знака, чтобы «т. е.» не отрезало заголовок в два
    слова). Остальное уходит в свёрнутый блок под заголовком. Фразы
    нет — заголовок остаётся длинным: резать посреди слова хуже.
    """
    text = goal.strip()
    first, _, tail = text.partition("\n")
    rest = tail.strip()
    if len(first) > TITLE_MAX:
        m = re.search(r"[.!?…](?=\s)", first[20:])
        if m:
            cut = 20 + m.end()
            rest = (first[cut:].strip() + "\n" + rest).strip()
            first = first[:cut].rstrip(".").rstrip()
    return first, rest


def _timeline(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Фазы на общей шкале времени.

    Метрика пишется в конце фазы, поэтому начало — это `ts` минус
    длительность (`wall_s` у исполнителя, `dur_s` у ревью и гейта). У
    границ, у гейта без прогона (`reused`) и у старых метрик гейта
    длительности нет: это отметка момента, а не отрезок, и рисуется она
    засечкой — врать про ширину нельзя.
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
                "reused": bool(m.get("reused")),
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
    gate = {"ok": 0, "fail": 0, "reused": 0}
    for m in metrics:
        phase = str(m.get("phase") or "")
        if phase:
            row = phases.setdefault(phase, {"n": 0.0, "cost": 0.0, "sec": 0.0})
            row["n"] += 1
            row["cost"] += _num(m.get("cost_usd")) or 0.0
            row["sec"] += _num(m.get("wall_s")) or _num(m.get("dur_s")) or 0.0
        if phase == "gate":
            gate["ok" if m.get("ok") else "fail"] += 1
            gate["reused"] += 1 if m.get("reused") else 0
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
        subject, patch, cut = "", "", 0
        # Источников коммита два и они расходятся: step_done в журнале и поле
        # `commit` в задаче (его мог проставить оператор). Берём поле задачи —
        # иначе принятая вручную работа выглядит как «в код ничего не вошло».
        if t.get("commit"):
            subject = _git(root, "show", "-s", "--format=%s", t["commit"]).strip()
            full = _git(root, "show", "--format=", t["commit"])
            patch = full
            if len(full) > MAX_DIFF_CHARS:
                # Режем по границе строки: полстроки в раскраске диффа
                # читается как строка, которой в коммите нет.
                patch = full[: full.rfind("\n", 0, MAX_DIFF_CHARS) + 1]
                cut = len(full)
        streams = sorted(p.name for p in (swarm / "log").glob(f"{tid}-*executor.jsonl"))
        tasks.append(
            dict(
                t,
                _phases=phases,
                _cost=spend.get(tid),
                _verdicts=_verdicts(swarm, tid),
                _subject=subject,
                _patch=patch,
                _patch_cut=cut,
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
    # Развилки пуриста (AUD-002): адресованы человеку, а до сих пор жили
    # только в промпте исполнителя и в сыром потоке. Берётся последняя
    # запись задачи — пурист зовётся один раз до работы, повтор = новый ход.
    unclear: dict[str, dict[str, Any]] = {}
    for r in journal:
        if r.get("kind") == "unclear_found":
            unclear[_key(r)] = {"count": r.get("count"),
                                "summary": r.get("summary"),
                                "items": vocab.unclear_items(r)}
    for t in tasks:
        t["_rounds"] = rounds.get(_key(t, "id"), [])
        t["_unclear"] = unclear.get(_key(t, "id"))

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
        "coddoc": _coddoc_base(root),
        "swarm_dir": str(swarm),
        "built": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


CSS = """
:root{--paper:#f6f4ef;--card:#fffefb;--sunk:#efece5;--ink:#1b1a17;
--mut:#57524a;--faint:#7c7568;--line:#dcd6ca;--hair:#e8e3d8;
--ok:#2c7a4b;--warn:#9a6a00;--bad:#b0372b;--acc:#2a5ca8;--live:#a8720a;
--okbg:#e4f0e6;--warnbg:#f8ecd2;--badbg:#f8e1dd;--accbg:#e2eaf7;
--addbg:#e6f3e8;--delbg:#fbe7e4;--addfg:#1e6b3c;--delfg:#a3322a;
--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
--paper:#131418;--card:#1a1c21;--sunk:#15171b;--ink:#e9e7e2;--mut:#aaa498;
--faint:#857f73;--line:#30333a;--hair:#25282e;--ok:#7cc890;--warn:#e2ad55;
--bad:#ec8175;--acc:#8ab0ee;--live:#e5ad3c;--okbg:#1a2b20;--warnbg:#2e2616;
--badbg:#321e1c;--accbg:#1b2538;--addbg:#16291d;--delbg:#2f1a18;
--addfg:#8fd6a2;--delfg:#f19a8f}}
:root[data-theme=dark]{--paper:#131418;--card:#1a1c21;--sunk:#15171b;
--ink:#e9e7e2;--mut:#aaa498;--faint:#857f73;--line:#30333a;--hair:#25282e;
--ok:#7cc890;--warn:#e2ad55;--bad:#ec8175;--acc:#8ab0ee;--live:#e5ad3c;
--okbg:#1a2b20;--warnbg:#2e2616;--badbg:#321e1c;--accbg:#1b2538;
--addbg:#16291d;--delbg:#2f1a18;--addfg:#8fd6a2;--delfg:#f19a8f}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.6 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:20px 22px 90px}
.mono{font-family:var(--mono)}
.num{font-variant-numeric:tabular-nums}
a{color:var(--acc)}
a.cd{text-decoration:none;border-bottom:1px solid color-mix(in srgb,var(--acc) 40%,transparent);
white-space:nowrap}
a.cd:hover{border-bottom-color:var(--acc)}
a.cd::after{content:"↗";font-size:.75em;margin-left:1px;vertical-align:super}

/* --- шапка --- */
.rail{display:flex;gap:12px;align-items:center;flex-wrap:wrap;
font:12px/1.4 var(--mono);color:var(--faint)}
.rail .brand{color:var(--ink);font-weight:600;letter-spacing:.12em;
text-transform:uppercase}
.grow{flex:1}
.tag{border:1px solid var(--line);border-radius:3px;padding:1px 6px}
.pill{display:inline-flex;gap:6px;align-items:center;border-radius:3px;
padding:1px 7px;border:1px solid var(--line)}
.pill.on{color:var(--live);border-color:var(--live)}
.pill .dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.on .dot{animation:pulse 2.2s ease-in-out infinite}
@keyframes pulse{50%{opacity:.2}}
@media(prefers-reduced-motion:reduce){.pill.on .dot{animation:none}}
h1{font:650 25px/1.3 var(--sans);letter-spacing:-.015em;margin:14px 0 8px;
max-width:70ch}
.sub{color:var(--mut);font-size:13.5px;margin:10px 0 18px}
button{font:inherit;font-size:13px;padding:5px 11px;border-radius:6px;
border:1px solid var(--line);background:var(--card);color:var(--ink);
cursor:pointer}
button:hover{border-color:var(--mut)}
button.on{border-color:var(--acc);color:var(--acc);background:var(--accbg)}
button.link{border:none;background:none;padding:0;color:var(--acc);
font-size:13px}
button.link:hover{text-decoration:underline}
#theme{padding:2px 8px;font-size:12px;line-height:1.4}

/* --- постановка прогона --- */
.brief{max-width:84ch;background:var(--card);border:1px solid var(--line);
border-left:3px solid var(--acc);border-radius:8px;padding:13px 16px 12px}
.brief .rich{font-size:15px;line-height:1.65}
.brief.clamp .rich{max-height:19em;overflow:hidden;
-webkit-mask-image:linear-gradient(#000 70%,transparent);
mask-image:linear-gradient(#000 70%,transparent)}
.brief .more{margin-top:6px}

/* --- заголовки разделов --- */
h2{font:650 14px/1.2 var(--sans);color:var(--mut);margin:34px 0 12px;
display:flex;gap:10px;align-items:center}
h2::after{content:"";flex:1;height:1px;background:var(--hair)}
h2 .cnt{font:12px var(--mono);color:var(--faint);font-weight:400}

/* --- показатели --- */
.vitals{display:grid;gap:10px;
grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:11px 13px 12px}
.cap{font:600 12px/1 var(--sans);color:var(--mut)}
.tile .big{font:600 22px/1.1 var(--mono);font-variant-numeric:tabular-nums;
margin-top:8px}
.tile .big small{font:400 13px var(--sans);color:var(--mut)}
.split{display:flex;height:6px;border-radius:3px;overflow:hidden;
background:var(--sunk);margin-top:9px}
.split i{display:block;height:100%}
.legend{display:flex;gap:4px 11px;flex-wrap:wrap;font-size:13px;
color:var(--mut);margin-top:7px}
.legend .sw{display:inline-block;width:9px;height:9px;border-radius:2px;
margin-right:5px}
.legend b{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}

/* --- сейчас --- */
.now{border:1px solid var(--live);background:var(--warnbg);border-radius:8px;
padding:12px 14px;display:flex;gap:13px;align-items:flex-start}
.now.dead{border-color:var(--bad);background:var(--badbg)}
.now .beat{width:9px;height:9px;border-radius:50%;background:var(--live);
margin-top:7px;flex:none;animation:pulse 2.2s ease-in-out infinite}
.now.dead .beat{background:var(--bad);animation:none}
@media(prefers-reduced-motion:reduce){.now .beat{animation:none}}
.now .what{font-weight:600}
.now .meta{color:var(--mut);font-size:13.5px;margin-top:2px}
.now .grow{flex:1}
.now .el{font:600 19px var(--mono);font-variant-numeric:tabular-nums;
color:var(--live)}
.now.dead .el{color:var(--bad)}

/* --- карточки решений --- */
.ask{background:var(--card);border:1px solid var(--line);border-left:3px solid
var(--live);border-radius:7px;padding:11px 14px;margin-bottom:8px}
.ask.blocked{border-left-color:var(--bad)}
.ask .top{font:12px/1.4 var(--mono);color:var(--faint);margin-bottom:4px;
display:flex;gap:9px;flex-wrap:wrap;align-items:baseline}
.ask .top b{color:var(--ink)}
.ask .q{margin-bottom:8px;font-weight:500}
.acts{display:grid;grid-template-columns:auto 1fr;gap:5px 10px;
align-items:baseline;font-size:13px;color:var(--mut)}
.hint{color:var(--mut);font-size:13px;margin-top:6px}
code{background:var(--sunk);border:1px solid var(--hair);border-radius:5px;
padding:1px 6px;font-family:var(--mono);font-size:.86em}
/* Команда копируется щелчком по ней самой: отдельная кнопка рядом с
   каждой командой занимала строку и дублировала очевидное действие. */
code.copy{cursor:copy;position:relative;overflow-wrap:anywhere}
code.copy:hover{border-color:var(--acc);color:var(--acc)}
code.copy.big{display:inline-block;padding:5px 9px;font-size:12.5px}
code.copy.done::after{content:"скопировано";position:absolute;left:0;
top:-1.9em;font:11px var(--sans);background:var(--ink);color:var(--paper);
padding:2px 6px;border-radius:4px;white-space:nowrap}
pre{background:var(--sunk);border:1px solid var(--hair);border-radius:7px;
padding:11px;overflow-x:auto;font-size:12.5px;line-height:1.5;margin:7px 0 0;
font-family:var(--mono)}

/* --- шкала времени --- */
.tl{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px 14px 12px;overflow:hidden}
.tlbar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;
margin-bottom:10px}
.tlbar button{font-size:12.5px;padding:3px 9px}
.tlbar .sep{width:1px;height:18px;background:var(--line);margin:0 3px}
.tlbar .legend{margin:0 0 0 auto}
.tlbar select{font:inherit;font-size:12.5px;padding:3px 6px;border-radius:6px;
border:1px solid var(--line);background:var(--card);color:var(--ink)}
.seg.now{background-image:repeating-linear-gradient(135deg,rgb(255 255 255 / .35)
0 4px,transparent 4px 8px)!important;animation:pulse 2.2s ease-in-out infinite}
@media(prefers-reduced-motion:reduce){.seg.now{animation:none}}
.tlgrid{position:relative;user-select:none}
.tlrow{display:grid;grid-template-columns:230px 1fr;gap:12px;
align-items:center;height:24px}
.tlrow .who{font-size:13px;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;cursor:pointer;color:var(--mut)}
.tlrow .who .id{font:12px var(--mono);color:var(--faint);margin-right:6px}
.tlrow .who:hover{color:var(--acc)}
.tlrow:hover .track{background:var(--hair)}
.track{position:relative;height:16px;background:var(--sunk);border-radius:3px;
cursor:crosshair}
.seg{position:absolute;top:1px;bottom:1px;border-radius:2px;min-width:3px;
cursor:pointer}
.seg.mark{width:3px;min-width:0;top:-2px;bottom:-2px;border-radius:1px}
.seg:hover{outline:2px solid var(--ink);outline-offset:0;z-index:2}
.seg.sel{outline:2px solid var(--ink);outline-offset:1px;z-index:3}
.brk{position:absolute;top:0;bottom:0;background:repeating-linear-gradient(
135deg,var(--line) 0 2px,transparent 2px 6px);opacity:.8}
.tlaxis{position:relative;height:30px;margin-left:242px;
border-top:1px solid var(--hair);margin-top:4px;font:11.5px var(--mono);
color:var(--faint)}
.tlaxis span{position:absolute;top:5px;transform:translateX(-50%);
white-space:nowrap}
.tlaxis span.gap{color:var(--mut);font-style:italic}
.tlaxis i{position:absolute;top:0;height:4px;width:1px;background:var(--line)}
.brush{position:absolute;top:0;bottom:0;background:var(--accbg);
border:1px solid var(--acc);opacity:.6;pointer-events:none}
.tldet{margin-top:10px;border-top:1px solid var(--hair);padding-top:9px;
font-size:13.5px;min-height:1.6em;color:var(--mut)}
.tldet b{color:var(--ink)}
.tldet .sw{display:inline-block;width:10px;height:10px;border-radius:2px;
margin-right:6px;vertical-align:-1px}
.tldet .kv{display:flex;gap:4px 16px;flex-wrap:wrap;margin-top:3px}

/* --- задачи --- */
.bar{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin:0 0 10px}
input[type=search]{font:inherit;font-size:14px;padding:6px 10px;
border-radius:6px;border:1px solid var(--line);background:var(--card);
color:var(--ink);flex:1;min-width:170px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
margin-bottom:7px}
.card.open{border-color:var(--mut);box-shadow:0 1px 6px rgb(0 0 0 / .06)}
.head{display:grid;grid-template-columns:52px minmax(0,1fr) auto auto;gap:4px 12px;
align-items:baseline;padding:10px 14px;cursor:pointer;border-radius:8px}
.head:hover{background:var(--sunk)}
.card.open .head{border-bottom:1px solid var(--hair);border-radius:8px 8px 0 0}
.head .id{font:12.5px var(--mono);color:var(--faint)}
.head .t{font-weight:600;overflow-wrap:anywhere}
.head .meta{grid-column:2/-1;color:var(--mut);font-size:13px;
display:flex;gap:4px 12px;flex-wrap:wrap;align-items:center}
.spark{display:flex;gap:2px;align-items:flex-end;height:14px}
.spark i{width:4px;background:var(--acc);border-radius:1px;display:block}
.spark i.zero{background:var(--ok);height:2px}
.badge{font:12px/1.5 var(--mono);padding:1px 8px;
border-radius:3px;border:1px solid var(--line);color:var(--mut);
white-space:nowrap}
.b-done{color:var(--ok);border-color:var(--ok)}
.b-blocked{color:var(--bad);border-color:var(--bad);background:var(--badbg)}
.b-pending{color:var(--mut)}
.b-progress{color:var(--live);border-color:var(--live);background:var(--warnbg)}
/* Вес находки — цветом везде одинаково: в шапке карточки, в
   сводке, у самой находки. */
.sev{font:600 11.5px/1.5 var(--mono);padding:0 7px;border-radius:3px;
white-space:nowrap;border:1px solid transparent}
.sev-blocker{color:var(--bad);background:var(--badbg);border-color:var(--bad)}
.sev-major{color:var(--warn);background:var(--warnbg);border-color:var(--warn)}
.sev-minor{color:var(--mut);background:var(--sunk);border-color:var(--line)}
.vd{font:600 12px/1.5 var(--mono);padding:0 7px;border-radius:3px}
.vd-approve{color:var(--ok);background:var(--okbg)}
.vd-request_changes{color:var(--warn);background:var(--warnbg)}
.vd-failed{color:var(--bad);background:var(--badbg)}
.tabs{display:flex;gap:2px;padding:6px 10px 0;border-bottom:1px solid var(--hair);
overflow-x:auto}
.tabs button{border:none;border-bottom:2px solid transparent;border-radius:0;
background:none;padding:7px 11px;color:var(--mut);white-space:nowrap}
.tabs button:hover{color:var(--ink)}
.tabs button.on{color:var(--ink);border-bottom-color:var(--acc);
background:none;font-weight:600}
.tabs .n{font:11.5px var(--mono);color:var(--faint);margin-left:4px}
.pane{padding:14px}
.sec{margin-bottom:18px}
.sec:last-child{margin-bottom:0}
.sec h3{font:650 13px var(--sans);margin:0 0 7px;color:var(--mut);
display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.spec{font-size:14.5px;line-height:1.65;max-width:88ch}
.rich p{margin:0 0 8px}
.rich p.rh{font-weight:650;margin:12px 0 4px}
.rich p:first-child{margin-top:0}
.rich p:last-child,.rich .ri:last-child{margin-bottom:0}
.ri{display:grid;grid-template-columns:2.3em 1fr;margin:0 0 6px}
.ri .rn{color:var(--faint);font:12.5px/1.95 var(--mono)}
.rich code,h1 code,.card .t code,ul.acc code,.find code{overflow-wrap:anywhere}
/* Код в заголовках — моноширинным начертанием без плашки: плашки на
   полужирном 25px превращали название в ряд кнопок. */
h1 code,.head .t code,.ask .q code{background:none;border:none;padding:0;
font-size:.9em;font-weight:inherit}
ul.acc{margin:0;padding-left:20px;max-width:88ch}
ul.acc li{margin:4px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--hair)}
th{color:var(--mut);font-weight:600;font-size:12px}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{overflow-x:auto}
.round{border:1px solid var(--hair);border-radius:7px;margin-bottom:9px}
.round>.rhd{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
padding:8px 11px;cursor:pointer;font-size:13.5px}
.round>.rhd:hover{background:var(--sunk)}
.round>.rhd .rname{font:12.5px var(--mono);color:var(--faint)}
.round>.rhd .sum{flex:1 1 300px;color:var(--ink)}
.round>.rbd{display:none;padding:2px 11px 11px}
.round.open>.rbd{display:block}
.round.open>.rhd{border-bottom:1px solid var(--hair)}
.fold{color:var(--faint);font:12px var(--mono);width:1em}
.round.open>.rhd .fold::before,.dfile.open>.dh .fold::before{content:"▾"}
.fold::before{content:"▸"}
.find{border:1px solid var(--hair);border-left:4px solid var(--line);
border-radius:6px;padding:8px 11px;margin:9px 0;background:var(--card)}
.find.blocker{border-left-color:var(--bad);background:color-mix(in srgb,var(--badbg) 55%,var(--card))}
.find.major{border-left-color:var(--warn);background:color-mix(in srgb,var(--warnbg) 55%,var(--card))}
.find .top{display:flex;gap:8px;flex-wrap:wrap;align-items:center;
font-size:12.5px;color:var(--mut);margin-bottom:4px}
.find .sug{color:var(--acc);font-size:14px;margin-top:5px}
.analysis{color:var(--mut);font-size:14px;line-height:1.6;max-width:88ch}
.qa{border-left:3px solid var(--live);padding:7px 0 7px 12px;margin:10px 0}
.qa.answered{border-color:var(--ok)}
.qa .ans{color:var(--mut);font-size:13.5px;margin-top:4px}
details{margin:7px 0}
summary.det{cursor:pointer;color:var(--acc);font-size:13.5px;list-style:none}
summary.det::-webkit-details-marker{display:none}
summary.det::before{content:"▸ "}
details[open] summary.det::before{content:"▾ "}

/* --- дифф --- */
.dstat{display:flex;gap:12px;align-items:center;flex-wrap:wrap;
font-size:13.5px;margin-bottom:9px}
.add{color:var(--addfg);font-family:var(--mono)}
.del{color:var(--delfg);font-family:var(--mono)}
.dbar{display:inline-flex;gap:1px;vertical-align:middle}
.dbar i{width:7px;height:8px;border-radius:1px;background:var(--line)}
.dbar i.a{background:var(--ok)}
.dbar i.d{background:var(--bad)}
.dfile{border:1px solid var(--hair);border-radius:7px;margin-bottom:6px;
overflow:hidden}
.dfile>.dh{display:flex;gap:10px;align-items:center;padding:6px 10px;
cursor:pointer;font:12.5px var(--mono);background:var(--sunk)}
.dfile>.dh:hover{background:var(--hair)}
.dfile>.dh .p{flex:1;overflow-wrap:anywhere}
.dfile .dcode{display:none;overflow-x:auto}
.dfile.open .dcode{display:block}
table.dl{font:12.5px/1.5 var(--mono);border-collapse:collapse;width:100%}
table.dl td{border:none;padding:0 8px;white-space:pre;vertical-align:top}
table.dl td.ln{width:1%;color:var(--faint);text-align:right;user-select:none;
border-right:1px solid var(--hair)}
table.dl tr.a td{background:var(--addbg)}
table.dl tr.a td.c{color:var(--addfg)}
table.dl tr.d td{background:var(--delbg)}
table.dl tr.d td.c{color:var(--delfg)}
table.dl tr.h td{background:var(--accbg);color:var(--acc)}
.dmore{padding:6px 10px;border-top:1px solid var(--hair)}

/* --- события и хроника --- */
.log{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:4px 13px}
.ev{font-size:13.5px;padding:6px 0;border-bottom:1px solid var(--hair);
display:grid;grid-template-columns:92px 170px 1fr;gap:10px}
.ev:last-child{border-bottom:none}
.log.capped{max-height:15.5em;overflow-y:auto;overscroll-behavior:contain}
.ev .tm{color:var(--faint);font-family:var(--mono);font-size:12px;white-space:nowrap;
font-variant-numeric:tabular-nums}
.ev .kd{font-family:var(--mono);font-size:12.5px}
.ev .dt{color:var(--mut);overflow-wrap:anywhere}
.ev.hot .kd{color:var(--bad)}
@media(max-width:700px){.ev{grid-template-columns:56px 1fr}.ev .dt{grid-column:1/-1}
.tlrow{grid-template-columns:90px 1fr}.tlaxis{margin-left:102px}
.tlrow .who .tt{display:none}.head{grid-template-columns:44px minmax(0,1fr) auto}
.head .spark{display:none}}
.empty{color:var(--mut);background:var(--card);border:1px dashed var(--line);
border-radius:8px;padding:16px}
.foot{color:var(--mut);font-size:12.5px;margin-top:34px;
border-top:1px solid var(--hair);padding-top:12px}
.hidden{display:none!important}
"""

# Отдельной raw-строкой: в регулярках обратные слэши, и обычная строка
# Python съела бы их молча. Тесты гоняют этот кусок в node как есть.
RICH_JS = r"""
// --- постановки: код, пути, пункты ---------------------------------------
// Постановки пишут человек и планировщик в полу-markdown: `код` в
// обратных кавычках, голые пути и идентификаторы, нумерация «(1) … (2) …»
// прямо внутри строки. Сырым текстом это стена, где технические имена
// сливаются с прозой. Разбор намеренно узкий — код и пункты, без
// заголовков, ссылок и HTML: всё нераспознанное остаётся экранированным
// текстом, и ничего из постановки не теряется.
const RICH_CODE = new RegExp([
  '`([^`\n]+)`',
  // путь с расширением: cod_doc/cli/cmd_ctx.py, tests/x.py:118-126
  String.raw`(?<![\w/.:-])(?:[\w.-]+\/)+[\w.-]*\.[A-Za-z]\w{0,5}(?:::\w+)?(?::\d+(?:[-–]\d+)?)?`,
  // точечное или модульное имя: card.drift.issues, x.py::f, meta.counts()
  String.raw`(?<![\w/.:-])[A-Za-z_]\w*(?:(?:\.|::)[A-Za-z_]\w*)+(?:\([^()\n]{0,80}\))?`,
  // snake_case и SCREAMING_CASE: include_skill_bodies, _CURATOR_SKILLS
  String.raw`(?<![\w/.:-])_*[A-Za-z]\w*_\w*(?:\([^()\n]{0,80}\))?`,
  // флаг CLI: --json, --include-skill-bodies
  String.raw`(?<![\w-])--[a-z][\w-]*`,
].join('|'), 'g');

const richEsc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

// ID задач и ADR cod-doc (`ADO-226`, `ADR-028`) — ссылками в веб-интерфейс
// cod-doc. Ссылкой становится только ID с префиксом, который доска видела
// в названии задачи или вехе (`RICH_LINK.prefixes`): `UTF-8` или `TA-1` из
// прозы в ссылку наугад не превратятся. Нет адреса cod-doc — нет ссылок.
let RICH_LINK = null;
const RICH_ID = /\b([A-Z][A-Z0-9]{1,6})-(\d{1,5})\b/g;

function richText(s) {
  const t = richEsc(s);
  if (!RICH_LINK) return t;
  return t.replace(RICH_ID, (id, pre) => {
    if (!RICH_LINK.prefixes.has(pre)) return id;
    const kind = pre === 'ADR' ? 'adr' : 'tasks';
    return '<a class="cd" target="_blank" rel="noopener" href="' +
      richEsc(RICH_LINK.base + '/' + kind + '/' + id) +
      '" title="открыть в cod-doc">' + id + '</a>';
  });
}

function richInline(text) {
  const s = String(text ?? '');
  let out = '', at = 0;
  for (const m of s.matchAll(RICH_CODE)) {
    out += richText(s.slice(at, m.index)) +
      '<code>' + richEsc(m[1] ?? m[0]) + '</code>';
    at = m.index + m[0].length;
  }
  return out + richText(s.slice(at));
}

// Пункты: «(1) … (2) …» и «1. … 2. …» — в том числе внутри одной строки;
// «- …» — в начале строки. Нумерованным пунктом признаётся только ряд
// 1, 2, 3… одного вида длиной от двух: «8 КБ», «PR #102» или случайное
// «1.» в прозе пунктом не станут. Пункт кончается на следующем пункте
// или на переводе строки.
const RICH_MARK = /(^|\s)(?:\((\d{1,2})\)|(\d{1,2})[.)]|([-*•]))(?=\s)/g;

function richMarks(s) {
  const found = [];
  for (const m of s.matchAll(RICH_MARK)) {
    const at = m.index + m[1].length;
    if (m[4] && at > 0 && s[at - 1] !== '\n') continue;  // тире в прозе
    found.push({at, end: m.index + m[0].length,
      n: m[4] ? 0 : +(m[2] || m[3]), kind: m[4] ? 'b' : (m[2] ? 'p' : 'd'),
      label: m[0].slice(m[1].length)});
  }
  const out = [];
  let run = [];
  const flush = () => { if (run.length >= 2) out.push(...run); run = []; };
  for (const k of found) {
    if (k.kind === 'b') { flush(); out.push(k); continue; }
    const last = run[run.length - 1];
    if (last && k.kind === last.kind && k.n === last.n + 1) { run.push(k); continue; }
    if (k.n === 1) { flush(); run = [k]; }
  }
  flush();
  return out.sort((a, b) => a.at - b.at);
}

// Строка, целиком стоящая отдельно и кончающаяся двоеточием («Критерии
// приёмки:»), — заголовок части постановки. Двоеточие посреди строки
// («Две причины: (1) …») заголовком не считается: за ним идёт текст.
function richParas(s) {
  const lines = s.split(/\n+/);
  const closed = /\n\s*$/.test(s);
  return lines.map((x, i) => [x.trim(), i < lines.length - 1 || closed])
    .filter(([x]) => x)
    .map(([x, whole]) => (whole && x.length <= 60 && /:$/.test(x)
      ? '<p class="rh">' : '<p>') + richInline(x) + '</p>').join('');
}

function rich(text) {
  const s = String(text ?? '');
  const marks = richMarks(s);
  let out = richParas(s.slice(0, marks.length ? marks[0].at : s.length));
  marks.forEach((k, i) => {
    const stop = i + 1 < marks.length ? marks[i + 1].at : s.length;
    const body = s.slice(k.end, stop);
    const nl = body.indexOf('\n');
    const item = nl < 0 ? body : body.slice(0, nl);
    out += '<div class="ri"><span class="rn">' + richEsc(k.label) +
      '</span><div>' + richInline(item.trim()) + '</div></div>';
    if (nl >= 0) out += richParas(body.slice(nl));
  });
  return out;
}

// --- дифф: файлы, счётчики, номера строк ------------------------------------
// Дифф был одним <pre> на десятки килобайт без цвета: ни какие файлы
// тронуты, ни где добавлено, а где удалено. Разбор — по заголовкам
// `diff --git` и ханкам; всё до первого ханка (index, ---/+++) —
// служебное и в строки не идёт.
function parseDiff(text) {
  const files = [];
  let f = null, oldN = 0, newN = 0;
  for (const line of String(text ?? '').split('\n')) {
    const h = /^diff --git a\/(.*?) b\/(.*)$/.exec(line);
    if (h) {
      f = {path: h[2], add: 0, del: 0, lines: [], hunks: false,
           bin: false, created: false, gone: false};
      files.push(f);
      continue;
    }
    if (!f) continue;
    if (line.startsWith('@@')) {
      const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(line);
      if (m) { oldN = +m[1]; newN = +m[2]; }
      f.hunks = true;
      f.lines.push(['h', '', '', line]);
      continue;
    }
    if (!f.hunks) {
      if (line.startsWith('Binary files')) f.bin = true;
      if (line.startsWith('new file')) f.created = true;
      if (line.startsWith('deleted file')) f.gone = true;
      continue;
    }
    if (line.startsWith('+')) { f.add++; f.lines.push(['a', '', newN++, line.slice(1)]); }
    else if (line.startsWith('-')) { f.del++; f.lines.push(['d', oldN++, '', line.slice(1)]); }
    else if (line.startsWith(' ')) f.lines.push(['c', oldN++, newN++, line.slice(1)]);
    else if (line.startsWith('\\')) f.lines.push(['m', '', '', line]);
  }
  return files;
}
"""

JS = r"""
// `let`, не `const`: живое обновление перепривязывает D к свежему payload
// (см. apply()) — переменную, объявленную const, переприсвоить нельзя.
let D = JSON.parse(document.getElementById('data').textContent);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// Имена — из vocab через payload, а не третьей копией здесь: копия уже
// разошлась (исход раунда шёл по-английски, пока why говорил по-русски).
let V = D.vocab || {};
let SEV = V.severity || {};
let CAT = V.category || {};
let ST = V.status || {};
let OUT = V.outcome || {};
let PH = V.phase || {};
const CLS = {done:'b-done', blocked:'b-blocked', pending:'b-pending',
             in_progress:'b-progress', in_review:'b-progress'};
const SEV_ORDER = ['blocker', 'major', 'minor'];
// Цвет фазы — один и тот же на шкале и в легенде денег.
const PCOL = {implement:'var(--acc)', review:'var(--live)', gate:'var(--ok)',
              scope:'var(--faint)', verification:'var(--mut)',
              policy:'var(--mut)', integrity:'var(--bad)'};

// Состояние интерфейса живёт здесь, а не в DOM: живое обновление
// подменяет разметку целиком, и раскрытая карточка, выбранная вкладка,
// развёрнутый файл диффа и масштаб шкалы обязаны это пережить.
const OPEN = new Set();        // раскрытые карточки
const TAB = new Map();         // id задачи → вкладка
const FOLD = new Map();        // ключ складки → раскрыта ли
const DFULL = new Set();       // файлы диффа, показанные целиком
const TL = {sess: null, win: null, sel: null};
let BRIEF_OPEN = false;
let DIFFS = new Map();         // id задачи → разобранный дифф
let HAY = new Map();           // id задачи → текст для поиска

function init() {
  V = D.vocab || {}; SEV = V.severity || {}; CAT = V.category || {};
  ST = V.status || {}; OUT = V.outcome || {}; PH = V.phase || {};
  DIFFS = new Map(); HAY = new Map();
  // Префиксы ID cod-doc — только те, что стоят в начале названия задачи,
  // в вехе или в заголовке цели: там ID ставит сам протокол роя.
  if (D.coddoc) {
    const pre = new Set(['ADR']);
    const take = s => { const m = /^([A-Z][A-Z0-9]{1,6})-\d+/.exec(String(s || '').trim());
                        if (m) pre.add(m[1]); };
    (D.tasks || []).forEach(t => { take(t.title); take(t.milestone); });
    take(D.goal);
    RICH_LINK = {base: D.coddoc, prefixes: pre};
  } else RICH_LINK = null;
  document.querySelectorAll('[data-rich]').forEach(el => {
    if (el.dataset.src === undefined) el.dataset.src = el.textContent;
    el.innerHTML = el.dataset.rich === 'block'
      ? rich(el.dataset.src) : richInline(el.dataset.src);
  });
}

// Длительность словами: секунды нужны только пока их мало.
function dur(s) {
  if (s === null || s === undefined || isNaN(s)) return '';
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + ' с';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? m + ' мин ' + r + ' с' : m + ' мин';
  const h = Math.floor(m / 60);
  if (h < 48) return h + ' ч ' + (m % 60) + ' мин';
  return Math.floor(h / 24) + ' д ' + (h % 24) + ' ч';
}
function clock(t, sec) {
  if (!t) return '';
  const o = {hour:'2-digit', minute:'2-digit'};
  if (sec) o.second = '2-digit';
  return new Date(t * 1000).toLocaleTimeString([], o);
}
function day(t) {
  return new Date(t * 1000).toLocaleDateString([], {day:'2-digit', month:'2-digit'});
}

// Команда копируется щелчком по ней самой. Буфер обмена бывает закрыт
// (file:// в части браузеров) — тогда запасной путь через выделение.
function copy(el) {
  const text = el.dataset.cmd || el.textContent;
  const done = () => { el.classList.add('done');
                       setTimeout(() => el.classList.remove('done'), 1100); };
  const fallback = () => {
    const r = document.createRange(); r.selectNodeContents(el);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    try { document.execCommand('copy'); done(); } catch (e) {}
  };
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, fallback);
  else fallback();
}

// Тема: выбор человека сильнее системной, поэтому живёт в localStorage.
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

// --- постановка прогона ----------------------------------------------------
// Видна сразу и размечена: заголовки частей, пункты, код. Длинная — видна
// первыми строками с кнопкой «целиком», а не спрятана за ссылкой.
function brief() {
  const box = document.querySelector('.brief');
  if (!box) return;
  const body = box.querySelector('.rich');
  const btn = box.querySelector('.more');
  box.classList.remove('clamp');
  const tall = body.scrollHeight > 23 * parseFloat(getComputedStyle(body).fontSize);
  if (!tall) { btn.classList.add('hidden'); return; }
  btn.classList.remove('hidden');
  box.classList.toggle('clamp', !BRIEF_OPEN);
  btn.textContent = BRIEF_OPEN ? 'свернуть постановку' : 'показать постановку целиком';
}

// --- шкала времени --------------------------------------------------------
// История прогона растёт: у стенда cod-doc за 98 ч было пять сессий с
// паузами по суткам, и на линейной оси каждая фаза становилась черточкой.
// Поэтому ось кусочная: паузы дольше GAP сжаты в узкие штрихованные
// разрывы, по умолчанию показана последняя сессия, масштаб — протяжкой
// мышью по шкале, ⌘/Ctrl + колесом или кнопками; щелчок по отрезку
// показывает его подробности под шкалой.
const GAP = 20 * 60;
const BRK = 2.5;          // ширина разрыва, % дорожки

function tlNow() { return D.run && D.run.live ? Date.now() / 1000 : 0; }

function sessions() {
  const segs = [...allSegs()].sort((a, b) => a.t0 - b.t0);
  const out = [];
  segs.forEach(s => {
    const last = out[out.length - 1];
    if (last && s.t0 - last.b <= GAP) { last.b = Math.max(last.b, s.t1); last.segs.push(s); }
    else out.push({a: s.t0, b: s.t1, segs: [s]});
  });
  const last = out[out.length - 1];
  if (last) last.b = Math.max(last.b, tlNow());
  out.forEach(x => { x.tasks = new Set(x.segs.map(s => s.task)).size; });
  return out;
}

function tlWindow(ss) {
  if (TL.win) return TL.win;
  const i = TL.sess === null ? ss.length - 1 : TL.sess;
  if (i < 0) return {a: ss[0].a, b: ss[ss.length - 1].b};
  return {a: ss[i].a, b: ss[i].b};
}

// Куски оси: сессии, обрезанные окном; ширина — по длительности, но не
// меньше 6 % — короткая сессия рядом с долгой иначе не видна вовсе.
function layout(ss, win) {
  const parts = ss.map(x => ({a: Math.max(x.a, win.a), b: Math.min(x.b, win.b)}))
    .filter(x => x.b > x.a || (x.b === x.a && x.a >= win.a && x.a <= win.b));
  if (!parts.length) parts.push({a: win.a, b: win.b});
  const avail = 100 - BRK * (parts.length - 1);
  const total = parts.reduce((n, p) => n + Math.max(p.b - p.a, 1), 0);
  let w = parts.map(p => Math.max(Math.max(p.b - p.a, 1) / total * avail, avail * 0.06));
  const k = avail / w.reduce((n, v) => n + v, 0);
  let x = 0;
  parts.forEach((p, i) => { p.x0 = x; p.w = w[i] * k; x += p.w + BRK; });
  return parts;
}
function tx(parts, t) {
  for (const p of parts) {
    if (t <= p.b || p === parts[parts.length - 1]) {
      const c = Math.min(Math.max(t, p.a), p.b);
      return p.x0 + (p.b > p.a ? (c - p.a) / (p.b - p.a) : 0) * p.w;
    }
  }
  return 0;
}
function xt(parts, x) {
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (x <= p.x0 + p.w) return x < p.x0 ? p.a : p.a + (x - p.x0) / p.w * (p.b - p.a);
  }
  return parts[parts.length - 1].b;
}

function ticks(p, px) {
  const span = p.b - p.a;
  const steps = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
  const want = Math.max(1, Math.floor(px / 95));
  const step = steps.find(s => span / s <= want) || 86400;
  const off = new Date().getTimezoneOffset() * 60;
  const out = [];
  for (let t = Math.ceil((p.a - off) / step) * step + off; t <= p.b; t += step) out.push(t);
  return out;
}

function segKey(s) { return s.task + '|' + s.phase + '|' + s.t0; }

// Идущая фаза: метрика пишется в конце фазы, и семь минут работы
// исполнителя на шкале не было видно вовсе. Рисуется из отметки
// «сейчас» штрихованным отрезком до текущего момента.
function liveSeg() {
  const n = D.run && D.run.live && D.run.now;
  const t0 = n && Date.parse(n.since) / 1000;
  if (!n || !t0) return null;
  const t1 = Date.now() / 1000;
  return {task: String(n.task || ''), phase: String(n.phase || ''), iter: n.iter,
          t0, t1, dur: t1 - t0, live: true};
}
function allSegs() {
  const l = liveSeg();
  return l ? [...(D.timeline || []), l] : (D.timeline || []);
}

function timeline() {
  const box = document.getElementById('timeline');
  if (!box) return;
  const all = allSegs();
  if (!all.length) { box.innerHTML = ''; return; }
  const ss = sessions();
  const win = tlWindow(ss);
  const parts = layout(ss, win);
  const inWin = all.filter(s => s.t1 >= win.a && s.t0 <= win.b);
  const title = (D.tasks || []).reduce((m, t) => (m[t.id] = t.title || '', m), {});
  const rows = new Map();
  [...inWin].sort((a, b) => a.t0 - b.t0).forEach(s => {
    if (!rows.has(s.task)) rows.set(s.task, []);
    rows.get(s.task).push(s);
  });

  const cur = TL.win ? null : (TL.sess === null ? ss.length - 1 : TL.sess);
  let h = '<div class="tlbar">';
  if (ss.length > 1) h += `<button data-tl-sess="-1" class="${cur === -1 ? 'on' : ''}"
      title="все сессии, паузы сжаты">вся история · ${esc(dur(ss[ss.length - 1].b - ss[0].a))}</button>`;
  // Кнопками — последние сессии; старые — списком: у стенда cod-doc
  // их было 13, и кнопки занимали три строки над самой шкалой.
  const CHIPS = 5;
  const label = x => day(x.a) + ' ' + clock(x.a) + ' · ' + dur(x.b - x.a);
  if (ss.length > CHIPS) {
    h += `<select data-tl-old><option value="">раньше: ${ss.length - CHIPS} сесс.</option>` +
      ss.slice(0, -CHIPS).map((x, i) => `<option value="${i}" ${cur === i ? 'selected' : ''}>
        ${esc(label(x) + ', задач ' + x.tasks)}</option>`).join('') + `</select>`;
  }
  ss.forEach((x, i) => {
    if (i < ss.length - CHIPS) return;
    h += `<button data-tl-sess="${i}" class="${cur === i ? 'on' : ''}"
      title="${esc(day(x.a) + ' ' + clock(x.a) + ' – ' + clock(x.b) + ', задач ' + x.tasks)}">
      ${esc(label(x))}</button>`;
  });
  h += `<span class="sep"></span><button data-tl-zoom="0.5" title="приблизить">+</button>
    <button data-tl-zoom="2" title="отдалить">−</button>`;
  if (TL.win) h += `<button data-tl-reset>сбросить масштаб</button>`;
  h += `<span class="legend">` + ['implement', 'review', 'gate', 'scope'].map(p =>
    `<span><b class="sw" style="background:${PCOL[p]}"></b>${esc(PH[p] || p)}</span>`).join('') +
    `<span><b class="sw" style="background:var(--bad)"></b>гейт красный</span></span></div>`;

  const brks = parts.slice(1).map(p => `<i class="brk" style="left:${p.x0 - BRK}%;width:${BRK}%"></i>`).join('');
  h += '<div class="tlgrid">';
  rows.forEach((list, task) => {
    const bars = list.map(s => {
      const l = tx(parts, s.t0), r = tx(parts, s.t1);
      const w = r - l;
      const col = s.phase === 'gate' && s.ok === false ? 'var(--bad)'
                : (PCOL[s.phase] || 'var(--mut)');
      const k = segKey(s);
      const mark = w < 0.25 || !s.dur;
      return `<i class="seg ${mark ? 'mark' : ''} ${s.live ? 'now' : ''} ${TL.sel === k ? 'sel' : ''}" data-seg="${esc(k)}"
        style="left:${l}%;${mark ? '' : 'width:' + w + '%;'}background:${col}"
        title="${esc((PH[s.phase] || s.phase) + (s.dur ? ' · ' + dur(s.dur) : '') + (s.cost ? ' · $' + s.cost.toFixed(2) : ''))}"></i>`;
    }).join('');
    h += `<div class="tlrow"><div class="who" data-goto="${esc(task)}" title="${esc(title[task] || task)}">
      <span class="id">${esc(task || 'прогон')}</span><span class="tt">${esc(title[task] || '')}</span></div>
      <div class="track">${brks}${bars}</div></div>`;
  });
  h += '</div><div class="tlaxis">';
  const px = Math.max(200, box.clientWidth - 270);
  // Подписи оси не имеют права лезть друг на друга: у близких разрывов
  // «пауза 39 мин» ложилась на «пауза 21 ч». Подпись ставится, только
  // если её оценочная ширина не задевает предыдущую в том же ряду.
  const lanes = {tick: -1e9, gap: -1e9};
  const place = (lane, pct, text) => {
    const c = pct / 100 * px, half = text.length * 3.6 + 6;
    if (c - half < lanes[lane]) return false;
    lanes[lane] = c + half;
    return true;
  };
  parts.forEach((p, i) => {
    let prevDay = null;
    ticks(p, px * p.w / 100).forEach(t => {
      const d = day(t);
      const lab = d !== prevDay && (prevDay !== null || i > 0 || TL.sess === -1) ? d + ' ' + clock(t) : clock(t);
      prevDay = d;
      const x = tx(parts, t);
      h += `<i style="left:${x}%"></i>`;
      if (place('tick', x, lab)) h += `<span style="left:${x}%">${esc(lab)}</span>`;
    });
    if (i > 0) {
      const lab = 'пауза ' + dur(p.a - parts[i - 1].b);
      const x = p.x0 - BRK / 2;
      if (place('gap', x, lab)) h += `<span class="gap" style="left:${x}%;top:17px">${esc(lab)}</span>`;
    }
  });
  h += `</div><div class="tldet" id="tldet">${tlDetail()}</div>`;
  box.innerHTML = h;
  box._parts = parts;
}

function tlDetail() {
  const s = allSegs().find(x => segKey(x) === TL.sel);
  if (!s) return 'Щёлкните по отрезку — здесь будут его подробности. Протяните мышью ' +
    'по дорожке — приближение выбранного отрезка времени; ⌘/Ctrl + колесо — масштаб. ' +
    'Засечка вместо отрезка — у фазы нет измеренной длительности.';
  const t = (D.tasks || []).find(x => x.id === s.task);
  const kv = [];
  kv.push(`${esc(day(s.t0))} ${esc(clock(s.t0, true))}${s.dur ? ' – ' + esc(clock(s.t1, true)) : ''}`);
  if (s.dur) kv.push('длительность <b>' + esc(dur(s.dur)) + '</b>');
  if (s.iter) kv.push('раунд <b>' + esc(s.iter) + '</b>');
  if (s.cost) kv.push('<b>$' + s.cost.toFixed(2) + '</b>');
  if (s.verdict) kv.push(verdictPill(s.verdict));
  if (s.ok === false) kv.push('<span class="vd vd-failed">провал</span>');
  if (s.reused) kv.push('без прогона: дерево не менялось');
  if (s.live) kv.push('<b>идёт сейчас</b>');
  if (s.note) kv.push(esc(s.note));
  const col = s.phase === 'gate' && s.ok === false ? 'var(--bad)' : (PCOL[s.phase] || 'var(--mut)');
  return `<div><span class="sw" style="background:${col}"></span><b>${esc(PH[s.phase] || s.phase)}</b>
    · задача <b>${esc(s.task || 'прогон')}</b> ${t ? richInline(t.title) : ''}
    ${t ? ` · <button class="link" data-goto="${esc(s.task)}">открыть карточку</button>` : ''}</div>
    <div class="kv">${kv.map(x => '<span>' + x + '</span>').join('')}</div>`;
}

function tlZoom(factor, at) {
  const ss = sessions();
  const w = tlWindow(ss);
  const box = document.getElementById('timeline');
  const parts = box._parts || layout(ss, w);
  const c = at === undefined ? (w.a + w.b) / 2 : xt(parts, at);
  const half = Math.max(30, (w.b - w.a) * factor / 2);
  const lo = ss[0].a, hi = ss[ss.length - 1].b;
  if (half * 2 >= hi - lo) { TL.win = null; TL.sess = -1; }
  else TL.win = {a: Math.max(lo, c - half), b: Math.min(hi, c + half)};
  timeline();
}

// Протяжка по дорожке: выделенный отрезок времени становится окном.
let drag = null;
document.addEventListener('mousedown', ev => {
  const track = ev.target.closest('#timeline .track');
  if (!track || ev.target.closest('.seg') || ev.button !== 0) return;
  const grid = track.closest('.tlgrid');
  const r = track.getBoundingClientRect();
  drag = {r, x0: ev.clientX, el: document.createElement('div'), grid};
  drag.el.className = 'brush';
  drag.el.style.left = (r.left - grid.getBoundingClientRect().left + ev.clientX - r.left) + 'px';
  drag.el.style.width = '0px';
  grid.appendChild(drag.el);
  ev.preventDefault();
});
document.addEventListener('mousemove', ev => {
  if (!drag) return;
  const gl = drag.grid.getBoundingClientRect().left;
  const x = Math.min(Math.max(ev.clientX, drag.r.left), drag.r.right);
  drag.el.style.left = (Math.min(x, drag.x0) - gl) + 'px';
  drag.el.style.width = Math.abs(x - drag.x0) + 'px';
});
document.addEventListener('mouseup', ev => {
  if (!drag) return;
  const d = drag; drag = null; d.el.remove();
  const x = Math.min(Math.max(ev.clientX, d.r.left), d.r.right);
  if (Math.abs(x - d.x0) < 6) return;
  const parts = document.getElementById('timeline')._parts;
  const pct = v => (v - d.r.left) / d.r.width * 100;
  const a = xt(parts, pct(Math.min(x, d.x0))), b = xt(parts, pct(Math.max(x, d.x0)));
  if (b - a < 20) return;
  TL.win = {a, b};
  timeline();
});
document.addEventListener('wheel', ev => {
  const grid = ev.target.closest('#timeline .tlgrid');
  if (!grid || !(ev.ctrlKey || ev.metaKey)) return;
  ev.preventDefault();
  const tr = grid.querySelector('.track').getBoundingClientRect();
  tlZoom(ev.deltaY > 0 ? 1.25 : 0.8, (ev.clientX - tr.left) / tr.width * 100);
}, {passive: false});

// --- задачи ---------------------------------------------------------------
function verdictPill(v) {
  const name = {approve: 'approve', request_changes: 'правки'}[v] || v;
  return `<span class="vd vd-${esc(v)}">${esc(name)}</span>`;
}
function sevChips(counts) {
  return SEV_ORDER.filter(s => counts[s]).map(s =>
    `<span class="sev sev-${s}">${esc(SEV[s] || s)} ${counts[s]}</span>`).join(' ');
}
function sevCount(list) {
  const c = {};
  (list || []).forEach(f => { if (f && typeof f === 'object') c[f.severity] = (c[f.severity] || 0) + 1; });
  return c;
}
function allFindings(t) {
  return (t._verdicts || []).flatMap(v => v.findings || []);
}
function diffOf(t) {
  if (!DIFFS.has(t.id)) DIFFS.set(t.id, t._patch ? parseDiff(t._patch) : []);
  return DIFFS.get(t.id);
}
function diffTotals(files) {
  return files.reduce((n, f) => ({add: n.add + f.add, del: n.del + f.del}), {add: 0, del: 0});
}
function isOpen(key, dflt) { return FOLD.has(key) ? FOLD.get(key) : dflt; }

function findings(list) {
  const rank = f => { const i = SEV_ORDER.indexOf(f.severity); return i < 0 ? 9 : i; };
  return [...list].filter(f => f && typeof f === 'object').sort((a, b) => rank(a) - rank(b))
    .map(f => `<div class="find ${esc(f.severity)}"><div class="top">
      <span class="sev sev-${esc(f.severity)}">${esc(SEV[f.severity] || f.severity)}</span>
      <span>${esc(CAT[f.category] || f.category || '')}</span>
      ${f.file ? `<code class="copy">${esc(f.file)}${f.line ? ':' + esc(f.line) : ''}</code>` : ''}</div>
    <div>${richInline(f.issue)}</div>
    ${f.suggestion ? `<div class="sug">→ ${richInline(f.suggestion)}</div>` : ''}
  </div>`).join('');
}

function paneReview(t) {
  let h = '';
  const vs = t._verdicts || [];
  const total = sevCount(allFindings(t));
  if (t._rounds?.length) {
    h += `<div class="sec"><h3>Траектория схождения ${sevChips(total)}</h3><div class="scroll">
      <table><tr><th>раунд</th><th>вердикт</th><th>исход</th>
      <th class="n">находок</th><th class="n">о замысле</th></tr>` +
      t._rounds.map(r => `<tr><td>${esc(r.round)}</td><td>${r.verdict ? verdictPill(r.verdict) : ''}</td>
        <td>${esc(OUT[r.outcome] || r.outcome)}</td><td class="n">${esc(r.findings ?? '')}</td>
        <td class="n">${esc(r.intent ?? '')}</td></tr>`).join('') +
      `</table></div><div class="hint">${esc(trend(t._rounds))}</div></div>`;
  }
  if (vs.length) h += `<div class="sec"><h3>Вердикты ревьюера по раундам</h3>`;
  vs.forEach((v, i) => {
    const key = t.id + '|r|' + v.round;
    const open = isOpen(key, i === vs.length - 1);
    const c = sevCount(v.findings);
    const pill = v.failed ? '<span class="vd vd-failed">нет ответа</span>' : verdictPill(v.verdict);
    const sum = v.failed ? esc(v.why) : richInline(v.summary);
    let body = '';
    if (!v.failed) {
      body += findings(v.findings || []);
      if (v.requests?.length) body += `<div class="hint">запрошены проверки: ` +
        v.requests.map(r => esc(r.kind)).join(', ') + `</div>`;
      if (v.notes?.length) body += `<div class="sec" style="margin-top:12px"><h3>Замечено вне рамок задачи</h3>
        <ul class="acc">${v.notes.map(n => `<li>${richInline(n)}</li>`).join('')}</ul>
        <div class="hint">готовый бэклог: ревьюер это увидел, но чинить не просил</div></div>`;
      if (v.analysis) {
        const ak = key + '|an';
        body += `<div class="round ${isOpen(ak, false) ? 'open' : ''}" style="margin-top:10px">
          <div class="rhd" data-fold="${esc(ak)}"><span class="fold"></span>рассуждение ревьюера</div>
          <div class="rbd"><div class="analysis rich">${isOpen(ak, false) ? rich(v.analysis) : ''}</div></div></div>`;
      }
      if (!body) body = '<div class="hint">находок нет</div>';
    } else body = `<div class="hint">${esc(v.why)}</div>`;
    h += `<div class="round ${open ? 'open' : ''}">
      <div class="rhd" data-fold="${esc(key)}"><span class="fold"></span>
        <span class="rname">${esc(v.round)}</span>${pill}${sevChips(c)}
        <span class="sum">${open ? '' : sum}</span></div>
      <div class="rbd">${open && !v.failed && v.summary ? `<p class="spec" style="margin:8px 0">${richInline(v.summary)}</p>` : ''}${open ? body : ''}</div></div>`;
  });
  if (vs.length) h += `</div>`;
  if (t._suppressed?.length) h += `<div class="sec"><h3>Подавлено политиками прогона</h3>
    ${t._suppressed.map(s => `<div class="find minor"><div class="top">${esc(s.policy || '')}
    ${s.severity ? `<span class="sev sev-${esc(s.severity)}">${esc(SEV[s.severity] || s.severity)}</span>` : ''}</div>
    <div>${richInline(s.issue)}</div></div>`).join('')}</div>`;
  return h || '<div class="hint">ревью не запускалось</div>';
}

function paneDiff(t) {
  const files = diffOf(t);
  const tot = diffTotals(files);
  let h = `<div class="dstat">`;
  if (t._subject) h += `<b>${richInline(t._subject)}</b>`;
  h += `<span>файлов ${files.length}</span><span class="add">+${tot.add}</span>
    <span class="del">−${tot.del}</span>
    <code class="copy" title="щелчок — скопировать">git show ${esc(t.commit)}</code>
    ${files.length > 1 ? `<button class="link" data-dall="1">развернуть все</button>
      <button class="link" data-dall="0">свернуть все</button>` : ''}</div>`;
  if (t._patch_cut) h += `<div class="hint" style="margin:-3px 0 9px">дифф на странице обрезан:
    показано ${Math.round(t._patch.length / 1000)} из ${Math.round(t._patch_cut / 1000)} тыс. знаков —
    полностью командой выше</div>`;
  // Коротко по умолчанию: маленький дифф открыт целиком, большой —
  // первым файлом с превью в LIM строк; остальное по щелчку.
  const small = files.reduce((n, f) => n + f.lines.length, 0) <= 160;
  files.forEach((f, i) => {
    const key = t.id + '|f|' + f.path;
    const open = isOpen(key, small || i === 0);
    const n = f.add + f.del;
    const blocks = n ? Math.round(f.add / n * 5) : 0;
    const bar = '<span class="dbar">' + [0, 1, 2, 3, 4].map(i =>
      `<i class="${!n ? '' : i < blocks ? 'a' : 'd'}"></i>`).join('') + '</span>';
    const tag = f.created ? ' <span class="sev sev-minor">новый</span>'
              : f.gone ? ' <span class="sev sev-minor">удалён</span>' : '';
    h += `<div class="dfile ${open ? 'open' : ''}"><div class="dh" data-fold="${esc(key)}">
      <span class="fold"></span><span class="p">${esc(f.path)}${tag}</span>
      <span class="add">+${f.add}</span><span class="del">−${f.del}</span>${bar}</div>
      <div class="dcode">${open ? fileCode(f, key) : ''}</div></div>`;
  });
  return h;
}

function fileCode(f, key) {
  if (f.bin) return '<div class="dmore hint">двоичный файл</div>';
  const LIM = 80;
  const full = DFULL.has(key) || f.lines.length <= LIM + 20;
  const rows = full ? f.lines : f.lines.slice(0, LIM);
  let h = '<table class="dl">' + rows.map(([k, o, n, text]) => k === 'h' || k === 'm'
    ? `<tr class="h"><td class="ln"></td><td class="ln"></td><td class="c">${esc(text)}</td></tr>`
    : `<tr class="${k === 'c' ? '' : k}"><td class="ln">${o}</td><td class="ln">${n}</td>
       <td class="c">${k === 'a' ? '+' : k === 'd' ? '−' : ' '}${esc(text)}</td></tr>`).join('') + '</table>';
  if (!full) h += `<div class="dmore"><button class="link" data-full="${esc(key)}">
    показать ещё ${f.lines.length - LIM} строк</button></div>`;
  return h;
}

function paneSpec(t) {
  let h = '';
  if (t.spec) h += `<div class="sec"><h3>Что просили сделать</h3>
    <div class="spec rich">${rich(t.spec)}</div></div>`;
  if (t.acceptance?.length) h += `<div class="sec"><h3>Критерии приёмки</h3>
    <ul class="acc">${t.acceptance.map(a => `<li>${richInline(a)}</li>`).join('')}</ul></div>`;
  if (t.human_decisions?.length) h += `<div class="sec"><h3>Ваши решения по задаче</h3>
    <ul class="acc">${t.human_decisions.map(d => `<li>${richInline(d)}</li>`).join('')}</ul></div>`;
  const u = t._unclear;
  if (u && (u.items || []).length) h += `<div class="sec"><h3>Развилки спецификации
    <span class="sev sev-major">${esc(u.count ?? u.items.length)}</span></h3>
    ${u.summary ? `<div class="hint" style="margin:0 0 6px">${richInline(u.summary)}</div>` : ''}
    ${u.items.map(i => `<div class="find major"><div>${richInline(i.question)}</div>
      ${i.why_it_matters ? `<div class="hint">от чего зависит: ${richInline(i.why_it_matters)}</div>` : ''}
      ${i.where ? `<div class="hint">где молчит спека: ${richInline(i.where)}</div>` : ''}</div>`).join('')}</div>`;
  if (t.paths?.length) h += `<div class="sec"><h3>Границы задачи</h3>
    ${t.paths.map(p => `<code class="copy">${esc(p)}</code>`).join(' ')}</div>`;
  if (t.deps?.length) h += `<div class="sec"><h3>Зависит от</h3>${t.deps.map(esc).join(', ')}</div>`;
  return h || '<div class="hint">постановки нет</div>';
}

function paneWork(t) {
  let h = '';
  if (t._phases?.length) h += `<div class="sec"><h3>Фазы по порядку</h3><div class="scroll">
    <table><tr><th>время</th><th>раунд</th><th>фаза</th><th>итог</th>
    <th class="n">длительность</th><th class="n">$</th></tr>` +
    t._phases.map(p => `<tr><td class="num">${esc(p.ts)}</td><td>${esc(p.iter ?? '')}</td>
      <td>${esc(p.phase)}</td><td>${['approve', 'request_changes'].includes(p.result) ? verdictPill(p.result)
        : p.result === 'провал' ? '<span class="vd vd-failed">провал</span>' : esc(p.result)}</td>
      <td class="n">${p.dur ? esc(dur(p.dur)) : ''}</td>
      <td class="n">${p.cost ? p.cost.toFixed(2) : ''}</td></tr>`).join('') +
    `</table></div></div>`;
  (t._questions || []).forEach(q => {
    const cmd = `swarm --root ${D.root} answer ${q.qid} "…"`;
    h += `<div class="sec"><div class="qa ${q.status === 'answered' ? 'answered' : ''}">
      <div><b>${esc(q.qid)}</b> · ${esc(q.qkind || '')}</div>
      <div>${richInline(q.question)}</div>
      ${q.answer ? `<div class="ans">→ ${esc(q.answer)}</div>` :
        `<div class="hint"><code class="copy big">${esc(cmd)}</code></div>`}
    </div></div>`;
  });
  if (t._streams?.length) h += `<div class="sec"><h3>Сырые логи</h3>
    <div class="hint">потоки исполнителя не встроены (сотни КБ):
    <code class="copy">${esc(D.swarm_dir)}/log/</code> — ${t._streams.map(esc).join(', ')}</div></div>`;
  return h || '<div class="hint">фаз ещё не было</div>';
}

function tabsOf(t) {
  const tabs = [];
  const nf = allFindings(t).length;
  if (t._verdicts?.length || t._rounds?.length)
    tabs.push(['review', 'Ревью', nf ? 'находок ' + nf : 'без находок']);
  if (t._patch) { const d = diffTotals(diffOf(t)); tabs.push(['diff', 'Дифф', `+${d.add} −${d.del}`]); }
  tabs.push(['spec', 'Постановка', '']);
  tabs.push(['work', 'Ход работы', t._phases?.length ? 'фаз ' + t._phases.length : '']);
  return tabs;
}
function defaultTab(t, tabs) {
  const has = k => tabs.some(x => x[0] === k);
  if (allFindings(t).length && has('review')) return 'review';
  if (t.status === 'blocked') return has('review') ? 'review' : 'work';
  if (has('diff')) return 'diff';
  return 'spec';
}

function taskBody(t) {
  const tabs = tabsOf(t);
  let tab = TAB.get(t.id);
  if (!tabs.some(x => x[0] === tab)) tab = defaultTab(t, tabs);
  const pane = {review: paneReview, diff: paneDiff, spec: paneSpec, work: paneWork}[tab];
  return `<div class="tabs">${tabs.map(([k, name, n]) =>
    `<button data-tab="${k}" class="${k === tab ? 'on' : ''}">${name}${n ? `<span class="n">${esc(n)}</span>` : ''}</button>`).join('')}
    </div><div class="pane">${pane(t)}</div>`;
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

function card(t) {
  const bits = [];
  if (t.type) bits.push(`<span>${esc(t.type)}</span>`);
  if (t._cost) bits.push(`<span class="num">$${t._cost}</span>`);
  if (t.iterations) bits.push(`<span>раундов ${esc(t.iterations)}</span>`);
  const chips = sevChips(sevCount(allFindings(t)));
  if (chips) bits.push(`<span>${chips}</span>`);
  if (t._patch) {
    const d = diffTotals(diffOf(t));
    bits.push(`<span><span class="add">+${d.add}</span> <span class="del">−${d.del}</span></span>`);
  }
  // Поля прошлого исхода не чистятся при закрытии задачи: показывать
  // «причина: invalid_verdict» рядом с «закрыта» — вводить в заблуждение.
  if (t.status === 'blocked') {
    if (t.reason) bits.push(`<span>причина: <b>${esc(t.reason)}</b></span>`);
    if (t.diagnosis) bits.push(`<span>диагноз: ${esc(t.diagnosis)}</span>`);
  }
  if (t.deps?.length && t.status === 'pending')
    bits.push(`<span>ждёт: ${esc(t.deps.join(', '))}</span>`);
  // Спарклайн — траектория схождения, читаемая не открывая карточку.
  const series = (t._rounds || []).map(r => r.findings).filter(x => typeof x === 'number');
  const top = Math.max(1, ...series);
  const spark = series.length > 1 ? `<span class="spark">` + series.map(v =>
    `<i class="${v ? '' : 'zero'}" style="height:${v ? Math.max(3, v / top * 14) : 2}px"
      title="находок ${v}"></i>`).join('') + `</span>` : '<span></span>';
  const open = OPEN.has(t.id);
  return `<div class="card ${open ? 'open' : ''}" data-id="${esc(t.id)}">
    <div class="head" title="${open ? 'свернуть' : 'раскрыть'}"><span class="id">${esc(t.id)}</span>
    <span class="t">${richInline(t.title)}</span>${spark}
    <span class="badge ${CLS[t.status] || ''}">${esc(ST[t.status] || t.status)}</span>
    <div class="meta">${bits.join('')}</div></div>
    ${open ? `<div class="body">${taskBody(t)}</div>` : ''}</div>`;
}

function redrawCard(id) {
  const t = (D.tasks || []).find(x => x.id === id);
  const el = document.querySelector(`#tasks .card[data-id="${CSS.escape(id)}"]`);
  if (t && el) el.outerHTML = card(t);
}

function tasks() {
  const q = document.getElementById('search').value.toLowerCase();
  const filter = document.querySelector('.bar button.on')?.dataset.f || 'all';
  const box = document.getElementById('tasks');
  const order = ['in_progress', 'blocked', 'pending', 'in_review', 'done'];
  // Внутри статуса — свежие сверху: история растёт, и вчерашнее не
  // должно заслонять последнее.
  const last = {};
  (D.timeline || []).forEach(s => { last[s.task] = Math.max(last[s.task] || 0, s.t1); });
  const sorted = [...(D.tasks || [])].sort((a, b) =>
    order.indexOf(a.status) - order.indexOf(b.status) || (last[b.id] || 0) - (last[a.id] || 0));
  const shown = sorted.filter(t => {
    if (!HAY.has(t.id)) {
      const {_patch, ...rest} = t;
      HAY.set(t.id, JSON.stringify(rest).toLowerCase());
    }
    return (filter === 'all' || t.status === filter) && (!q || HAY.get(t.id).includes(q));
  });
  box.innerHTML = shown.length ? shown.map(card).join('')
    : '<div class="empty">ничего не найдено</div>';
}

function openCard(id) {
  OPEN.add(id);
  const f = document.querySelector('.bar button.on');
  if (f && f.dataset.f !== 'all') {
    document.querySelectorAll('.bar button[data-f]').forEach(x => x.classList.toggle('on', x.dataset.f === 'all'));
  }
  document.getElementById('search').value = '';
  tasks();
  const el = document.querySelector(`#tasks .card[data-id="${CSS.escape(id)}"]`);
  if (el) el.scrollIntoView({block: 'start', behavior: 'smooth'});
}

function render() { tasks(); timeline(); brief(); tick(); }

// Все щелчки — одним делегатом на документе: он переживает живую подмену
// разметки, и порядок проверок явный. Карточку сворачивает и раскрывает
// ТОЛЬКО её шапка: раньше щелчок в любом месте тела (по тексту, по
// находке) захлопывал карточку посреди чтения.
document.addEventListener('click', ev => {
  const el = ev.target;
  if (el.closest('a')) return;
  const cp = el.closest('code.copy');
  if (cp) { copy(cp); return; }
  const tl = el.closest('[data-tl-sess]');
  if (tl) { TL.sess = +tl.dataset.tlSess; TL.win = null; timeline(); return; }
  const z = el.closest('[data-tl-zoom]');
  if (z) { tlZoom(+z.dataset.tlZoom); return; }
  if (el.closest('[data-tl-reset]')) { TL.win = null; timeline(); return; }
  const seg = el.closest('#timeline .seg');
  if (seg) { TL.sel = TL.sel === seg.dataset.seg ? null : seg.dataset.seg; timeline(); return; }
  const go = el.closest('[data-goto]');
  if (go) { if (go.dataset.goto) openCard(go.dataset.goto); return; }
  if (el.closest('.brief .more')) { BRIEF_OPEN = !BRIEF_OPEN; brief(); return; }
  if (el.closest('#theme')) { theme(); return; }
  const fb = el.closest('.bar button[data-f]');
  if (fb) {
    document.querySelectorAll('.bar button[data-f]').forEach(x => x.classList.remove('on'));
    fb.classList.add('on'); tasks(); return;
  }
  if (el.closest('#toggle-ev')) {
    const box = document.getElementById('events');
    box.classList.toggle('hidden');
    el.closest('#toggle-ev').textContent = box.classList.contains('hidden')
      ? 'показать хронику прогона' : 'скрыть хронику';
    return;
  }
  const cardEl = el.closest('#tasks .card');
  if (!cardEl) return;
  const id = cardEl.dataset.id;
  const tab = el.closest('.tabs button[data-tab]');
  if (tab) { TAB.set(id, tab.dataset.tab); redrawCard(id); return; }
  const fold = el.closest('[data-fold]');
  if (fold) {
    const holder = fold.parentElement;
    FOLD.set(fold.dataset.fold, !holder.classList.contains('open'));
    redrawCard(id); return;
  }
  const dall = el.closest('[data-dall]');
  if (dall) {
    const t = (D.tasks || []).find(x => x.id === id);
    diffOf(t).forEach(f => FOLD.set(id + '|f|' + f.path, dall.dataset.dall === '1'));
    redrawCard(id); return;
  }
  const full = el.closest('[data-full]');
  if (full) { DFULL.add(full.dataset.full); redrawCard(id); return; }
  if (el.closest('.head')) {
    // Выделение текста в шапке — не щелчок: копирующий название не
    // должен захлопывать карточку.
    if (String(getSelection() || '')) return;
    if (OPEN.has(id)) OPEN.delete(id); else OPEN.add(id);
    redrawCard(id);
  }
});
document.addEventListener('input', ev => {
  if (ev.target.id === 'search') tasks();
});
document.addEventListener('change', ev => {
  if (ev.target.matches('[data-tl-old]') && ev.target.value !== '') {
    TL.sess = +ev.target.value; TL.win = null; timeline();
  }
});
let resizeT = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeT); resizeT = setTimeout(() => { timeline(); brief(); }, 150);
});

// Совместимость: обработчики теперь делегированы, привязывать нечего.
function bind() {}

init();
render();

// Живое обновление: сервер (boardserve.BoardServer) отдаёт по тому же
// адресу свежий HTML, а сюда — только подмена DOM: страница жива, вкладка
// не мигает, раскрытое остаётся раскрытым (состояние — в OPEN/TAB/FOLD/TL).
if (location.protocol !== 'http:' && location.protocol !== 'https:') {
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
        // живёт, пока жива петля.
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
  // Тот же payload — эхо собственного запроса: перерисовывать нечего.
  if (!freshData || freshData.textContent === curData.textContent) return;
  const freshWrap = fresh.querySelector('.wrap');
  const curWrap = document.querySelector('.wrap');
  if (!freshWrap || !curWrap) return;
  // Поиск, фильтр и хроника живут в DOM — снимаются ДО подмены.
  const searchVal = document.getElementById('search').value;
  const activeFilter = document.querySelector('.bar button.on')?.dataset.f || 'all';
  const evEl0 = document.getElementById('events');
  const evHidden = !evEl0 || evEl0.classList.contains('hidden');

  curWrap.replaceWith(document.adoptNode(freshWrap));
  curData.textContent = freshData.textContent;
  D = JSON.parse(curData.textContent);

  document.getElementById('search').value = searchVal;
  const btn = document.querySelector(`.bar button[data-f="${activeFilter}"]`);
  if (btn) {
    document.querySelectorAll('.bar button[data-f]').forEach(x => x.classList.remove('on'));
    btn.classList.add('on');
  }
  const evEl = document.getElementById('events');
  const evBtn = document.getElementById('toggle-ev');
  if (evEl) evEl.classList.toggle('hidden', evHidden);
  if (evBtn) evBtn.textContent =
    evHidden ? 'показать хронику прогона' : 'скрыть хронику';

  init();
  render();
  document.getElementById('live').textContent =
    'обновлено ' + new Date().toLocaleTimeString();
}
"""


def _evtime(ts: Any) -> str:
    """Дата и время события: история прогонов тянется днями, и одни
    часы без даты у события трёхдневной давности врут о его свежести."""
    t = str(ts or "")
    return f"{t[8:10]}.{t[5:7]} {t[11:16]}" if len(t) >= 16 else t


def _run_log(rows: list[dict[str, Any]], hot: bool = False) -> str:
    """Лента событий прогона: свежие сверху, высота ограничена прокруткой."""
    e = html.escape
    return (
        '<div class="log capped">'
        + "".join(
            f'<div class="ev{" hot" if hot else ""}"><span class="tm">'
            f"{e(_evtime(r.get('ts')))}</span>"
            f'<span class="kd">{e(vocab.ru(KIND_RU, r.get("kind")))}</span>'
            f'<span class="dt">{e(vocab.narrate(r))}</span></div>'
            for r in reversed(rows)
        )
        + "</div>"
    )


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
    sev_line = " ".join(
        f'<span class="sev sev-{e(s)}">{e(SEVERITY_RU.get(s, s))} {severity[s]}</span>'
        for s in ("blocker", "major", "minor")
        if severity.get(s)
    )
    tiles.append(
        f'<div class="tile"><div class="cap">ревью</div>'
        f'<div class="big">{rounds}<small> вердиктов, находок '
        f"{finds}</small></div>{bar}{legend}"
        + (f'<div class="hint">{sev_line}</div>' if sev_line else "")
        + "</div>"
    )

    # 4. Гейт и контекст: зелен ли прогон тестов и сколько стоил контекст.
    gate = totals.get("gate") or {}
    g_ok, g_bad = gate.get("ok", 0), gate.get("fail", 0)
    g_re = gate.get("reused", 0)
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
        f"</small></div>{bar}"
        + (f'<div class="hint">без прогона (дерево то же): {g_re}</div>'
           if g_re else "")
        + f"{ctx}</div>"
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
        f'<div class="hint"><code class="copy big">{e(cmd)}</code></div>'
        f"</div></section>"
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
            f'<div class="q" data-rich="inline">{e(str(q.get("question", "")))}</div>'
            # Команда копируется щелчком по ней самой: отдельная кнопка
            # «скопировать» рядом с каждой командой была лишней строкой.
            # Текст берётся из узла, а не из inline-onclick: путь корня с
            # «'» разрывал одинарно-кавыченный атрибут (инъекция разметки).
            f'<div class="acts"><span>ответить</span>'
            f'<span><code class="copy big">{e(cmd)}</code></span></div>'
            f'<div class="hint">если решение требует тронуть файл вне границ '
            f"задачи — добавьте <code>--add-path путь</code></div></div>"
        )
    for t in blocked:
        cmd = f"swarm --root {root} retry {t.get('id')}"
        why = " · ".join(str(x) for x in (t.get("reason"), t.get("diagnosis")) if x)
        out.append(
            f'<div class="ask blocked"><div class="top">'
            f"<b>{e(str(t.get('id')))}</b><span>задача заблокирована</span></div>"
            f'<div class="q" data-rich="inline">{e(str(t.get("title") or ""))}</div>'
            + (f'<div class="hint" style="margin:-4px 0 8px">{e(why)}</div>'
               if why else "")
            # Обе команды — щелчком по тексту: «разбор причины» раньше
            # копировался только выделением руками, а у повтора была
            # отдельная кнопка.
            + f'<div class="acts"><span>разбор причины</span><span>'
            f'<code class="copy big">swarm --root {e(root)} why '
            f"{e(str(t.get('id')))}</code></span>"
            f'<span>повторить</span><span><code class="copy big">{e(cmd)}'
            f"</code></span>"
            + (
                f'<span>работа сохранена</span><span><code class="copy">'
                f"{e(str(t['stash']))}</code></span>"
                if t.get("stash")
                else ""
            )
            + "</div></div>"
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
    title, brief = _goal_parts(str(board.get("goal") or ""))
    parts = [
        '<meta charset="utf-8">',
        f"<title>Доска прогона</title><style>{CSS}</style>",
        '<div class="wrap">',
        rail,
        f'<h1 data-rich="inline">{e(title or "цель не задана")}</h1>',
    ]
    if brief:
        # Постановка видна сразу и размечена (заголовки частей, пункты,
        # код), а не спрятана за ссылкой «целиком»: свёрнутый блок никто
        # не раскрывал. Длинную JS показывает первыми строками с кнопкой;
        # без JS она читается целиком экранированным текстом.
        parts.append(
            f'<section class="brief"><div class="rich" data-rich="block">'
            f"{e(brief)}</div>"
            '<button class="link more hidden"></button></section>'
        )
    parts.append(f'<div class="sub">{" · ".join(sub)}</div>')

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
    alarms = [r for r in run_level if r.get("kind") not in ROUTINE_KINDS]
    routine = [r for r in run_level if r.get("kind") in ROUTINE_KINDS]
    if alarms:
        parts.append(
            f'<h2>События прогона <span class="cnt">{len(alarms)}</span></h2>'
        )
        parts.append(_run_log(alarms, hot=True))
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
        # Сессии, масштаб, легенду и панель подробностей рисует JS
        # (timeline()): они зависят от ширины окна и от выбора человека.
        parts.append('<div class="tl" id="timeline"></div>')

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

    # Рутина прогона — внизу, над хроникой: она нужна, когда ищут «какая
    # версия агента была» или «когда пересобрали план», а не при взгляде
    # на доску.
    if routine:
        parts.append(
            f'<h2>Служебные события прогонов <span class="cnt">{len(routine)}'
            "</span></h2>"
        )
        parts.append(_run_log(routine))

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
    parts.append(f"<script>{RICH_JS}{JS}</script>")
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
