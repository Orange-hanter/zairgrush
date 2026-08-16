"""Доска прогона: одна страница, на которой видно всё происходящее.

Существующие команды (`status`, `report`, `inbox`) отвечают на отдельные
вопросы и требуют знать, какой вопрос задать. Этого мало: человеку нужна
картина целиком и без посредника — что идёт прямо сейчас, сколько
потрачено, где его ждут, что уже сделано и можно ли этому верить.

Страница самодостаточна: ни одного внешнего ресурса, данные встроены в
неё при генерации. Обновляется по F5. Потоки исполнителя (сотни килобайт
на задачу) внутрь не кладутся — на них даётся путь к файлу.
"""
import html
import json
import pathlib
import subprocess
import sys
import time
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import vocab  # noqa: E402 — каталог добавлен строкой выше

# Словарь у доски и у терминала обязан быть ОДИН: пока он лежал здесь,
# `swarm report` до него не доставал и звал те же события кодами.
PHASE_RU = vocab.PHASE_RU
STATUS_RU = vocab.STATUS_RU
SEVERITY_RU = vocab.SEVERITY_RU
CATEGORY_RU = vocab.CATEGORY_RU
KIND_RU = vocab.KIND_RU
MAX_DIFF_CHARS = 12000        # больше человек в браузере всё равно не читает


def _key(row: dict[str, Any], field: str = "task") -> str:
    """Ключ группировки из журнальной записи. Записи без поля попадают в
    пустой ключ: с идентификатором задачи он не совпадёт никогда, поэтому
    такая строка не пристанет к чужой задаче."""
    return str(row.get(field) or "")


def _read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, check=False).stdout


def _verdicts(swarm_dir: pathlib.Path, tid: str) -> list[dict[str, Any]]:
    """Вердикты ревьюера по раундам — то, из-за чего задача идёт по кругу.

    Читаем сырые ответы: имя вида `<id>-i<раунд>-<фаза><попытка>-review.json`,
    где фаза `a` — обычный проход, `v` — повторный после проверок.
    """
    out = []
    for path in sorted((swarm_dir / "log").glob(f"{tid}-i*-review.json")):
        stem = path.stem.replace(f"{tid}-", "").replace("-review", "")
        try:
            env = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        except ValueError:
            # Нечитаемый ответ — САМОЕ интересное для оператора: раунд был,
            # деньги потрачены, вердикта нет. Пропуская такой файл, доска
            # показывала задачу так, будто ревью и не запускалось.
            out.append({"round": stem, "failed": True,
                        "why": "ответ ревьюера не разобран"})
            continue
        v = env.get("structured_output")
        if not isinstance(v, dict):
            out.append({"round": stem, "failed": True,
                        "why": (env.get("terminal_reason")
                                or env.get("subtype") or "ответ не разобран")})
            continue
        out.append({
            "round": stem, "verdict": v.get("verdict"),
            "summary": v.get("summary"), "analysis": v.get("analysis"),
            "findings": v.get("findings") or [],
            "notes": v.get("out_of_scope_notes") or [],
            "requests": v.get("verification_requests") or [],
        })
    return out


def collect(root: str | pathlib.Path) -> dict[str, Any]:
    """Все данные доски. Ничего не додумывает — только факты из файлов."""
    root = pathlib.Path(root)
    swarm = root / ".swarm"
    try:
        data = json.loads((swarm / "tasks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"goal": "", "tasks": []}
    metrics = _read_jsonl(swarm / "metrics.jsonl")
    journal = _read_jsonl(swarm / "log" / "run.jsonl")

    spend: dict[str, float] = {}
    for m in metrics:
        if m.get("cost_usd"):
            spend[_key(m)] = round(spend.get(_key(m), 0) + m["cost_usd"], 2)

    questions = {}
    for row in journal:
        if row.get("kind") == "question":
            # Журнал читаем как данные, а не как контракт: запись без qid
            # (старая версия, ручная правка) не должна ронять доску.
            if not row.get("qid"):
                continue
            questions[row["qid"]] = dict(row, status="open")
        elif row.get("kind") == "answer" and row.get("qid") in questions:
            questions[row["qid"]].update(status="answered",
                                         answer=row.get("text"))

    suppressed: dict[str, list[dict[str, Any]]] = {}
    for row in journal:
        if row.get("kind") == "policy_suppressed":
            suppressed.setdefault(_key(row), []).extend(row.get("items") or [])

    tasks = []
    for t in data.get("tasks", []):
        tid = t.get("id")
        phases = [{
            "ts": (m.get("ts") or "")[11:19], "iter": m.get("iter"),
            "phase": PHASE_RU.get(_key(m, "phase"), m.get("phase")),
            "result": (m.get("verdict") or m.get("reason")
                       or ("ок" if m.get("ok") else "провал" if m.get("ok") is False else "")),
            "dur": m.get("dur_s") or m.get("wall_s"), "cost": m.get("cost_usd"),
        } for m in metrics if m.get("task") == tid and m.get("phase")]
        diff = ""
        # Источников коммита два и они расходятся: step_done в журнале и поле
        # `commit` в задаче (его мог проставить оператор). Берём поле задачи —
        # иначе принятая вручную работа выглядит как «в код ничего не вошло».
        if t.get("commit"):
            diff = _git(root, "show", "--stat", "--format=%s%n", t["commit"])
            full = _git(root, "show", "--format=", t["commit"])
            if full:
                diff += "\n" + (full[:MAX_DIFF_CHARS]
                                + ("\n… дифф обрезан" if len(full) > MAX_DIFF_CHARS else ""))
        streams = sorted(p.name for p in (swarm / "log").glob(f"{tid}-*executor.jsonl"))
        tasks.append(dict(t, _phases=phases, _cost=spend.get(tid),
                          _verdicts=_verdicts(swarm, tid), _diff=diff,
                          _suppressed=suppressed.get(tid, []),
                          _streams=streams,
                          _questions=[q for q in questions.values()
                                      if q.get("task") == tid]))

    rounds: dict[str, list[dict[str, Any]]] = {}
    for r in journal:
        if r.get("kind") == "round":
            rounds.setdefault(_key(r), []).append(
                {"round": r.get("round"), "verdict": r.get("verdict"),
                 "outcome": r.get("outcome"), "findings": r.get("findings"),
                 "intent": r.get("intent")})
    for t in tasks:
        t["_rounds"] = rounds.get(_key(t, "id"), [])

    # Записи без задачи — это события ПРОГОНА, а не чьи-то: сгруппировать их
    # «по задачам» значит потерять ровно то, что объясняет остановку очереди.
    run_level = [r for r in journal
                 if not r.get("task") and r.get("kind") in (
                     "budget_exhausted", "preflight_forced", "plan_applied",
                     "policy", "policy_dropped")]

    # Хроника — фразами, а не дампом: `{"kind":"round","round":1,…}` человек
    # разбирает медленнее, чем «раунд 1 → request_changes, находок 3», и
    # ровно так же медленно он разбирал её здесь до появления vocab.
    events = [{"ts": r.get("ts", "")[11:19], "kind": r.get("kind"),
               "kind_ru": vocab.ru(KIND_RU, r.get("kind")),
               "task": r.get("task"), "detail": vocab.narrate(r)}
              for r in journal]

    unfinished = [r for r in journal if r.get("kind") == "step_intent"
                  and not any(d.get("kind") == "step_done"
                              and d.get("step_id") == r.get("step_id")
                              for d in journal)]

    return {"goal": data.get("goal", ""), "tasks": tasks,
            "questions": list(questions.values()), "events": events,
            "run_level": run_level, "unfinished": unfinished,
            "spend": spend, "total": round(sum(spend.values()), 2),
            "root": str(root), "swarm_dir": str(swarm),
            "built": time.strftime("%Y-%m-%d %H:%M:%S")}


CSS = """
:root{--bg:#fbfbfa;--fg:#1a1a19;--mut:#6b6b68;--line:#e6e6e3;--card:#fff;
--ok:#2f7a3f;--warn:#a86b00;--bad:#a32d2d;--acc:#2a5db0;--hl:#f3f4ef}
@media(prefers-color-scheme:dark){:root{--bg:#141417;--fg:#e9e9e7;--mut:#9a9a96;
--line:#2b2b31;--card:#1c1c21;--ok:#6ec27a;--warn:#e0a84a;--bad:#e57373;
--acc:#7aa5e8;--hl:#232329}}
:root[data-theme=dark]{--bg:#141417;--fg:#e9e9e7;--mut:#9a9a96;--line:#2b2b31;
--card:#1c1c21;--ok:#6ec27a;--warn:#e0a84a;--bad:#e57373;--acc:#7aa5e8;--hl:#232329}
:root[data-theme=light]{--bg:#fbfbfa;--fg:#1a1a19;--mut:#6b6b68;--line:#e6e6e3;
--card:#fff;--ok:#2f7a3f;--warn:#a86b00;--bad:#a32d2d;--acc:#2a5db0;--hl:#f3f4ef}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding:26px 20px 80px}
h1{font-size:21px;margin:0 0 3px;letter-spacing:-.01em}
h2{font-size:15px;margin:30px 0 10px;color:var(--mut);font-weight:600;
text-transform:uppercase;letter-spacing:.05em}
.goal{color:var(--mut);margin-bottom:22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:9px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:9px;
padding:11px 13px}
.kpi .n{font-size:21px;font-weight:600;font-variant-numeric:tabular-nums}
.kpi .l{color:var(--mut);font-size:12px;margin-top:1px}
.kpi.alert .n{color:var(--warn)}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;
margin-bottom:9px;overflow:hidden}
.head{display:flex;justify-content:space-between;gap:12px;align-items:baseline;
flex-wrap:wrap;padding:13px 15px;cursor:pointer;user-select:none}
.head:hover{background:var(--hl)}
.head .t{font-weight:600}
.head .t .id{color:var(--mut);font-weight:400;margin-right:7px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
.badge{font-size:12px;padding:2px 9px;border-radius:20px;border:1px solid var(--line);
color:var(--mut);white-space:nowrap}
.b-done{color:var(--ok);border-color:var(--ok)}
.b-blocked{color:var(--bad);border-color:var(--bad)}
.b-pending{color:var(--acc);border-color:var(--acc)}
.b-progress{color:var(--warn);border-color:var(--warn)}
.meta{color:var(--mut);font-size:13px;padding:0 15px 12px}
.body{border-top:1px solid var(--line);padding:14px 15px;display:none}
.card.open .body{display:block}
.card.open .head{background:var(--hl)}
.sec{margin-bottom:16px}
.sec:last-child{margin-bottom:0}
.sec h3{font-size:13px;margin:0 0 7px;color:var(--mut);font-weight:600}
.spec{white-space:pre-wrap;font-size:14px;background:var(--bg);
border:1px solid var(--line);border-radius:7px;padding:11px 13px}
ul.acc{margin:0;padding-left:20px} ul.acc li{margin:3px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{overflow-x:auto}
.find{border-left:3px solid var(--line);padding:6px 0 6px 11px;margin:9px 0}
.find.blocker{border-color:var(--bad)} .find.major{border-color:var(--warn)}
.find.minor{border-color:var(--line)}
.find .top{font-size:12px;color:var(--mut);margin-bottom:2px}
.find .sug{color:var(--mut);font-size:13px;margin-top:3px}
.q{border-left:3px solid var(--warn);padding:8px 0 8px 12px;margin:11px 0}
.q.answered{border-color:var(--ok)}
.q .ans{color:var(--mut);font-size:13px;margin-top:5px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:7px;
padding:11px;overflow-x:auto;font-size:12px;line-height:1.45;margin:7px 0 0;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
code{background:var(--bg);border:1px solid var(--line);border-radius:5px;
padding:1px 6px;font-size:13px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.cmd{display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap}
.cmd code{flex:1;min-width:220px;padding:7px 10px;overflow-x:auto;white-space:nowrap}
button{font:inherit;font-size:13px;padding:6px 12px;border-radius:7px;
border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button:hover{background:var(--hl)}
button.on{border-color:var(--acc);color:var(--acc)}
.bar{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin:14px 0 4px}
input[type=search]{font:inherit;font-size:14px;padding:7px 11px;border-radius:7px;
border:1px solid var(--line);background:var(--card);color:var(--fg);flex:1;min-width:180px}
.hint{color:var(--mut);font-size:13px;margin-top:6px}
.foot{color:var(--mut);font-size:12px;margin-top:34px;border-top:1px solid var(--line);
padding-top:13px}
.hidden{display:none!important}
details{margin:7px 0}
summary.det{cursor:pointer;color:var(--acc);font-size:13px;list-style:none}
summary.det::-webkit-details-marker{display:none}
summary.det::before{content:"▸ "}
details[open] summary.det::before{content:"▾ "}
.ev{font-size:13px;padding:4px 0;border-bottom:1px solid var(--line);
display:flex;gap:10px}
.ev .tm{color:var(--mut);font-variant-numeric:tabular-nums;white-space:nowrap}
.ev .kd{white-space:nowrap;min-width:170px}
.ev .dt{color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
"""

JS = """
const D = JSON.parse(document.getElementById('data').textContent);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const SEV = {blocker:'блокер', major:'важное', minor:'мелочь'};
const CAT = {correctness:'корректность', tests:'тесты', style:'стиль',
             scope:'границы', architecture:'архитектура'};
const ST = {pending:'в очереди', in_progress:'в работе', in_review:'на ревью',
            done:'закрыта', blocked:'заблокирована'};
const CLS = {done:'b-done', blocked:'b-blocked', pending:'b-pending',
             in_progress:'b-progress', in_review:'b-progress'};

function copy(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    const was = btn.textContent; btn.textContent = 'скопировано';
    setTimeout(() => btn.textContent = was, 1200);
  });
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

  if (t._phases?.length) h += `<div class="sec"><h3>Как шла работа</h3><div class="scroll">
    <table><tr><th>время</th><th>раунд</th><th>фаза</th><th>итог</th>
    <th class="num">сек</th><th class="num">$</th></tr>` +
    t._phases.map(p => `<tr><td>${esc(p.ts)}</td><td>${esc(p.iter ?? '')}</td>
      <td>${esc(p.phase)}</td><td>${esc(p.result)}</td>
      <td class="num">${p.dur ? p.dur.toFixed(1) : ''}</td>
      <td class="num">${p.cost ? p.cost.toFixed(2) : ''}</td></tr>`).join('') +
    `</table></div></div>`;

  if (t._rounds?.length) {
    h += `<div class="sec"><h3>Траектория схождения</h3><div class="scroll">
      <table><tr><th>раунд</th><th>вердикт</th><th>исход</th>
      <th class="num">находок</th><th class="num">о замысле</th></tr>` +
      t._rounds.map(r => `<tr><td>${esc(r.round)}</td><td>${esc(r.verdict)}</td>
        <td>${esc(r.outcome)}</td><td class="num">${esc(r.findings ?? '')}</td>
        <td class="num">${esc(r.intent ?? '')}</td></tr>`).join('') +
      `</table></div><div class="hint">убывает число находок — работа сходится;
       стоит на месте — петля топчется</div></div>`;
  }

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
    h += `<div class="sec"><div class="q ${q.status === 'answered' ? 'answered' : ''}">
      <div><b>${esc(q.qid)}</b> · ${esc(q.qkind || '')}</div>
      <div>${esc(q.question)}</div>
      ${q.answer ? `<div class="ans">→ ${esc(q.answer)}</div>` :
        `<div class="cmd"><code>${esc(cmd)}</code>
         <button onclick='copy(${JSON.stringify(cmd)}, this)'>скопировать</button></div>`}
    </div></div>`;
  });

  if (t._diff) h += `<div class="sec"><h3>Что вошло в код</h3>
    <pre>${esc(t._diff)}</pre></div>`;

  if (t._streams?.length) h += `<div class="sec"><h3>Сырые логи</h3>
    <div class="hint">потоки исполнителя не встроены (сотни КБ):
    <code>${esc(D.swarm_dir)}/log/</code> — ${t._streams.map(esc).join(', ')}</div></div>`;
  return h;
}

function render() {
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
    return `<div class="card" onclick="if(!event.target.closest('button'))
        this.classList.toggle('open')">
      <div class="head"><div class="t"><span class="id">${esc(t.id)}</span>${esc(t.title)}</div>
      <span class="badge ${CLS[t.status] || ''}">${esc(ST[t.status] || t.status)}</span></div>
      <div class="meta">${bits.join(' · ')}</div>
      <div class="body">${taskBody(t)}</div></div>`;
  }).join('');
  if (!shown) box.innerHTML = '<div class="card"><div class="meta">ничего не найдено</div></div>';
}

document.querySelectorAll('.bar button[data-f]').forEach(b => b.onclick = () => {
  document.querySelectorAll('.bar button[data-f]').forEach(x => x.classList.remove('on'));
  b.classList.add('on'); render();
});
document.getElementById('search').oninput = render;
document.getElementById('toggle-ev').onclick = e => {
  const el = document.getElementById('events');
  el.classList.toggle('hidden');
  e.target.textContent = el.classList.contains('hidden')
    ? 'показать хронику прогона' : 'скрыть хронику';
};
render();
"""


def render(board: dict[str, Any]) -> str:
    e = html.escape
    open_q = [q for q in board["questions"] if q["status"] == "open"]
    by: dict[str, int] = {}
    for t in board["tasks"]:
        by[t.get("status")] = by.get(t.get("status"), 0) + 1

    kpis = [(by.get("done", 0), "закрыто", ""),
            (by.get("pending", 0), "в очереди", ""),
            (by.get("blocked", 0), "заблокировано", ""),
            (len(open_q), "ждут вас", "alert" if open_q else ""),
            (f"${board['total']}", "ревьюер стоил", "")]

    parts = [f"<title>Доска прогона</title><style>{CSS}</style>",
             '<div class="wrap">', "<h1>Прогон петли агентов</h1>",
             f'<div class="goal">{e(board["goal"] or "цель не задана")}</div>',
             '<div class="grid">']
    for n, label, cls in kpis:
        parts.append(f'<div class="kpi {cls}"><div class="n">{e(str(n))}</div>'
                     f'<div class="l">{label}</div></div>')
    parts.append("</div>")

    if open_q:
        parts.append("<h2>Ждут вашего решения</h2>")
        for q in open_q:
            cmd = f'swarm --root {board["root"]} answer {q["qid"]} "…"'
            parts.append(
                f'<div class="card"><div class="body" style="display:block">'
                f'<div class="q"><div><b>{e(q["qid"])}</b> · задача '
                f'{e(str(q.get("task")))} · {e(str(q.get("qkind", "")))}</div>'
                f'<div>{e(str(q.get("question", "")))}</div>'
                f'<div class="cmd"><code>{e(cmd)}</code>'
                f"<button onclick='copy({json.dumps(cmd)}, this)'>скопировать</button>"
                f'</div><div class="hint">если решение требует тронуть файл вне '
                f'границ задачи — добавьте <code>--add-path путь</code></div>'
                f"</div></div></div>")

    parts.append("<h2>Задачи</h2>")
    parts.append('<div class="bar">'
                 '<input type="search" id="search" placeholder="поиск по задачам, '
                 'находкам, файлам…">'
                 '<button data-f="all" class="on">все</button>'
                 '<button data-f="pending">в очереди</button>'
                 '<button data-f="blocked">заблокированы</button>'
                 '<button data-f="done">закрыты</button></div>'
                 '<div class="hint">карточка раскрывается по клику: спецификация, '
                 'критерии, находки ревьюера, дифф</div>')
    parts.append('<div id="tasks"></div>')

    parts.append("<h2>Хроника</h2>")
    parts.append('<button id="toggle-ev">показать хронику прогона</button>')
    parts.append('<div id="events" class="card hidden" style="margin-top:9px">'
                 '<div class="body" style="display:block">')
    parts.extend(
        f'<div class="ev"><span class="tm">{e(ev["ts"])}</span>'
        f'<span class="kd">{e(str(ev["kind_ru"]))}</span>'
        f'<span class="dt">{e(str(ev["task"] or ""))} {e(ev["detail"])}</span></div>'
        for ev in board["events"])
    parts.append("</div></div>")

    # Формулировка точная намеренно: прежде подвал обещал «обновляется по
    # F5», а данные вшиты в страницу при генерации. Пока петля не
    # переписывала файл сама, F5 перечитывал тот же снимок прошлого — и
    # человек не имел способа отличить «ничего не происходит» от
    # «страница устарела час назад».
    parts.append(
        f'<div class="foot">Во время прогона петля переписывает эту страницу '
        f'после каждого раунда — обновляйте по F5 и сверяйтесь со временем '
        f'сборки ниже. Вне прогона собрать заново: '
        f'<code>swarm --root {e(board["root"])} board</code>.<br>'
        f'Собрано: {e(board["built"])}</div></div>')

    payload = json.dumps(board, ensure_ascii=False).replace("</", "<\\/")
    parts.append(f'<script type="application/json" id="data">{payload}</script>')
    parts.append(f"<script>{JS}</script>")
    return "\n".join(parts)


def build(root: str | pathlib.Path,
          out: str | pathlib.Path | None = None,
          ) -> tuple[pathlib.Path, dict[str, Any]]:
    board = collect(root)
    page = render(board)
    out = pathlib.Path(out) if out else pathlib.Path(root) / ".swarm" / "board.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out, board
