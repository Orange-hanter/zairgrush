#!/usr/bin/env python3
"""Мутационный аудит: ломаем инвариант — гейт обязан покраснеть.

Зелёные тесты говорят «код делает то, что делает», и ничего не говорят о
том, что они ПРОВЕРЯЮТ. Мутационный аудит спрашивает второе: снимаем по
одной защите за раз и смотрим, заметит ли набор. Мутация, которую никто
не поймал, — дыра в тестах, а не мелочь; на этом же аудите 2026-08-23
нашлись две (спасённый вердикт проходил без валидации; `go` не проверял
движок), и обе закрыты тестами в тот же день.

Файл живой: новая подсистема дописывает сюда свои мутации. Правило то
же, что у гейта, — список только растёт, а строка, которую перестали
ловить, обязана быть либо починена, либо удалена вместе с защитой.

Запуск (дерево должно быть чистым — скрипт правит исходники и
возвращает их обратно):  python3 mutate.py
Код возврата 1 — есть непойманные мутации.
"""
import pathlib
import subprocess
import sys

SW = pathlib.Path("/Users/dakh/Git/_my/ZAIrgRush/tools/swarm")

# (имя, файл, было, стало, чем обязана ловиться)
MUTATIONS = [
    # --- отметка о настоящем и живость петли (§9.3) ---
    ("настоящее: отметка о фазе не снимается после выхода",
     "swarm/state.py",
     """            else:
                self.now_path.unlink(missing_ok=True)""",
     """            else:
                pass""",
     "test_state"),
    ("настоящее: мёртвый прогон объявлен живым",
     "swarm/state.py",
     """        if self._lock is not None:
            return True            # держим сами: доска строится внутри петли""",
     """        if True:
            return True            # держим сами: доска строится внутри петли""",
     "test_state"),
    ("настоящее: доска называет фазой то, что осталось от обрыва",
     "swarm/board.py",
     '        "now": now if live else None,',
     '        "now": now,',
     "test_board"),
    # --- движок исполнителя (ADR-011) ---
    ("движок: незнакомое имя молча становится умолчанием",
     "swarm/engines.py",
     '''    if engine not in ENGINES:
        raise EngineError(''',
     '''    if False:
        raise EngineError(''',
     "test_engines"),
    ("движок: префикс перестал быть старше ключа",
     "swarm/engines.py",
     "    engine = prefix or config.get(\"executor_engine\") or DEFAULT_ENGINE",
     "    engine = config.get(\"executor_engine\") or prefix or DEFAULT_ENGINE",
     "test_engines"),
    ("движок: запрет на запись в git снят",
     "swarm/engines.py",
     '''               "--disallowedTools", CLAUDE_DENIED_TOOLS,''',
     '''               "--disallowedTools", "",''',
     "test_engines"),
    ("движок: форма вызова kimi поехала (модель после -p)",
     "swarm/engines.py",
     "            cmd[1:1] = [\"-m\", model]",
     "            cmd += [\"-m\", model]",
     "test_engines"),
    ("движок: цена конверта подменена нулём вместо отсутствия",
     "swarm/engines.py",
     '''        "cost_usd": env.get("total_cost_usd"),''',
     '''        "cost_usd": env.get("total_cost_usd") or 0.0,''',
     "test_engines"),
    # --- спасение вердикта (§4.2.1) ---
    ("вердикт: неразобравшийся список находок стал пустым",
     "swarm/parsing.py",
     '''            except ValueError:
                # Список, который не разобрался, — не пустой список.
                # Подставить [] значило бы СОЧИНИТЬ отсутствие находок,
                # то есть превратить request_changes в approve.
                return None''',
     '''            except ValueError:
                value = []''',
     "test_verdict_salvage"),
    ("вердикт: спасённое проходит без валидации",
     "swarm/reviewer.py",
     "    valid = agents.loop_mod.validate_verdict(verdict)",
     "    valid = salvaged or agents.loop_mod.validate_verdict(verdict)",
     "test_review_failure"),
    ("вердикт: порог существенности снят",
     "swarm/verdicts.py",
     "    if len((v.get(\"analysis\") or \"\").strip()) < MIN_ANALYSIS:",
     "    if False:",
     "test_verdict_salvage test_verdict"),
    ("вердикт: повтор уходит без причины отказа",
     "swarm/reviewer.py",
     "                           retry_note=str(problem))",
     "                           retry_note=\"\")",
     "test_review_failure"),
    ("вердикт: поток отказа снова затирается повтором",
     "swarm/reviewer.py",
     '''    free_path(agents.state.dir / "log",
              f"{stem}-review-stream", ".jsonl").write_text(run.raw_stream())''',
     '''    (agents.state.dir / "log" / f"{stem}-review-stream.jsonl").write_text(
        run.raw_stream())''',
     "test_review_failure"),
    ("вердикт: спасение невидимо в журнале",
     "swarm/reviewer.py",
     '''    agents.state.log("verdict_salvaged", task=task["id"], round=iteration,''',
     '''    _ = ("verdict_salvaged", task["id"], iteration,''',
     "test_review_failure"),
    # --- инбокс: вопрос, переживший задачу ---
    ("инбокс: ответ снова воскрешает закрытую задачу",
     "swarm/state.py",
     '''                if not closed:
                    t["status"] = "pending"
                    t.pop("reason", None)''',
     '''                t["status"] = "pending"
                t.pop("reason", None)''',
     "test_state test_inbox"),
    ("инбокс: устаревший вопрос снова держит очередь",
     "swarm/state.py",
     '''        if blocking:
            out = [q for q in out if not q["stale"]]''',
     '''        if False:
            out = [q for q in out if not q["stale"]]''',
     "test_inbox"),
    ("инбокс: ответ на закрытую задачу снова теряется",
     "swarm/state.py",
     '''        self.log("answer", qid=qid, task=task_id, text=text,
                 reopened=not closed)''',
     '''        if not closed:
            self.log("answer", qid=qid, task=task_id, text=text,
                     reopened=True)''',
     "test_inbox"),
    # --- предупреждения на старте прогона ---
    ("прогон: неразведённое подтверждение больше не называется",
     "swarm/clirun.py",
     '''    if int(cfg.get("confirmations", 2) or 0) > 1 and not diverged:''',
     '''    if False:''',
     "test_cli"),
    ("прогон: движок не проверяется до трат",
     "swarm/clirun.py",
     '''    if not _engine_preflight(cfg):
        return 2
    st = cli.state_mod.SwarmState(args.root)''',
     '''    st = cli.state_mod.SwarmState(args.root)''',
     "test_cli"),
]


def run_tests(selector: str) -> bool:
    """True — тесты зелёные (мутация НЕ поймана)."""
    files = [f"tests/{name}.py" for name in selector.split()]
    # check=False намеренно: красный прогон здесь — ОЖИДАЕМЫЙ исход, а не
    # авария. Исключение сорвало бы аудит на первой же пойманной мутации,
    # оставив исходник изменённым.
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-x", "-q", *files],
        cwd=SW, capture_output=True, text=True, check=False,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
    return r.returncode == 0


def main() -> int:
    caught, missed = 0, []
    for name, rel, old, new, selector in MUTATIONS:
        path = SW / rel
        src = path.read_text(encoding="utf-8")
        if src.count(old) != 1:
            print(f"[ПРОПУСК] {name}: якорь не найден ({src.count(old)})")
            missed.append(name + " (якорь)")
            continue
        path.write_text(src.replace(old, new), encoding="utf-8")
        try:
            green = run_tests(selector)
        finally:
            path.write_text(src, encoding="utf-8")
        if green:
            print(f"[НЕ ПОЙМАНА] {name}  ({selector})")
            missed.append(name)
        else:
            caught += 1
            print(f"[  поймана ] {name}")
    print(f"\nпоймано {caught} из {len(MUTATIONS)}")
    if missed:
        print("ДЫРЫ В НАБОРЕ:")
        for m in missed:
            print("  -", m)
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
