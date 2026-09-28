"""`swarm completion zsh`: скрипт автодополнения, выведенный из парсера.

Скрипт не пишется руками: его строит обход дерева argparse, поэтому
новая команда или флаг попадают в Tab тем же коммитом, которым попали в
`--help`. Рукописный `_swarm` разошёлся бы с парсером на первой же
правке — и молча, потому что неподсказанный флаг выглядит как отсутствие
флага, а не как ошибка.

Статика (команды, флаги, choices) — здесь. Значения, которые живут в
состоянии стенда (id задач, вопросов, прогонов, корни стендов), скрипт
спрашивает у `swarm __complete <вид>` на каждый Tab; отвечает `clitab`,
минуя тяжёлый `cli`.
"""

import argparse
import pathlib
import sys

_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import registry  # noqa: E402

# Какой аргумент какими значениями дополнять: (путь подкоманд, dest) ->
# вид кандидатов `clitab` или готовое zsh-действие (начинается с `{`
# или `_`). Путь — кортеж имён от корня; () — глобальные флаги.
DYNAMIC: dict[tuple[tuple[str, ...], str], str] = {
    ((), "root"): "_swarm_roots",
    (("pair",), "task"): "tasks",
    (("replan",), "task"): "tasks",
    (("why",), "task"): "tasks",
    (("report",), "task"): "tasks",
    # retry возвращает в очередь только заблокированное (cmd_retry).
    (("retry",), "task"): "tasks:blocked",
    (("task", "split"), "id"): "tasks",
    # close закрывает лишь то, что ещё не в работе (clitask).
    (("task", "close"), "id"): "tasks:pending,blocked",
    (("task", "add"), "dep"): "tasks",
    (("answer",), "qid"): "questions",
    # `policy add "текст"` — свободный текст; id политики нужен только
    # для remove, и подсказывать pid при add значит подсовывать мусор.
    # После `*::` words начинается с имени подкоманды: words[2] — действие.
    (("policy",), "text"):
        "{ [[ $words[2] == remove ]] && _swarm_dyn policies 'политика' }",
    (("memory", "show"), "id"): "lessons",
    (("memory", "forget"), "id"): "lessons",
    (("ab",), "run"): "runs",
}

# dest, у которых значение — путь в файловой системе.
FILES = {"dispute", "out", "changed", "add_path", "path", "anchor"}

# dest, которые ОБЯЗАНЫ иметь запись в DYNAMIC: это id из состояния, и
# без подсказки новый такой аргумент молча дополнялся бы ничем.
ID_DESTS = {"task", "id", "qid", "dep", "run", "root"}

KIND_LABEL = {
    "tasks": "задача", "questions": "вопрос", "policies": "политика",
    "lessons": "урок", "runs": "прогон",
}

PRELUDE = r"""#compdef swarm
# Сгенерировано `swarm completion zsh` из парсера cli.py — руками не править:
# перегенерировать после обновления swarm.

# Меню выбора стрелками — с ПЕРВОГО Tab и только в контексте swarm: без
# второго Tab и без правки глобального стиля оболочки. Стиль ставится при
# автозагрузке файла, то есть до разбора первого же Tab. Шаблон — полные
# шесть полей контекста: zstyle предпочитает шаблон с БОЛЬШИМ числом полей,
# и пятипольный `:completion:*:*:swarm:*` проигрывал стилю oh-my-zsh
# `:completion:*:*:*:*:*` (menu select) — меню снова ждало второго Tab.
zstyle ':completion:*:*:swarm:*:*' menu yes select

# Значения из состояния стенда: `swarm __complete <вид> --root <корень>`,
# строки `значение<TAB>описание`. Порядок задаёт swarm (живое и открытое —
# первым), -V его хранит. Не `_describe`: тот склеивает кандидатов с
# одинаковым описанием в одну строку и пересортировывает весь список.
_swarm_dyn() {
  local -a lines vals disp expl
  local root=${_swarm_root/#\~/$HOME} ln tab=$'\t'
  integer w=0
  lines=(${(f)"$($_swarm_cmd __complete $1 --root "$root" 2>/dev/null)"})
  (( $#lines )) || return 1
  for ln in $lines; do
    vals+=("${ln%%$tab*}")
    (( ${#vals[-1]} > w )) && w=${#vals[-1]}
  done
  for ln in $lines; do
    disp+=("${(r:w:)${ln%%$tab*}}  -- ${ln#*$tab}")
  done
  _wanted -V "swarm-${1%%:*}" expl "$2" compadd -l -d disp -a vals
}

# --root: сначала известные стенды (живые — первыми), потом любой каталог.
_swarm_roots() {
  _swarm_dyn roots 'стенд'
  _directories
}
"""

EPILOGUE = r"""
_swarm() {
  # --root ищется по ИСХОДНОЙ строке: после разбора подкоманды `words`
  # сдвигается, и флаг перед командой становится невидим.
  local _swarm_cmd=$words[1] _swarm_root=. i
  for (( i = 2; i < $#words; i++ )); do
    case $words[i] in
      --root) _swarm_root=$words[i+1] ;;
      --root=*) _swarm_root=${words[i]#--root=} ;;
    esac
  done
  _swarm__
}

_swarm "$@"
"""


def _q(text: str) -> str:
    """Строка в одинарных кавычках zsh."""
    return "'" + text.replace("'", "'\\''") + "'"


def _desc(text: str | None) -> str:
    """Пояснение для `[...]` в спецификации _arguments."""
    one = " ".join(str(text or "").split())
    one = one.replace("\\", "\\\\").replace("]", "\\]").replace(":", "\\:")
    return one.replace("[", "(").replace("%", "%%")


def _subparsers(p: argparse.ArgumentParser) -> argparse._SubParsersAction | None:  # type: ignore[type-arg]
    for a in p._actions:  # noqa: SLF001 — у argparse нет публичного обхода
        if isinstance(a, argparse._SubParsersAction):  # noqa: SLF001
            return a
    return None


def _value_action(path: tuple[str, ...], a: argparse.Action) -> str:
    dyn = DYNAMIC.get((path, a.dest))
    if dyn is not None:
        if dyn.startswith(("{", "_")):
            return dyn
        label = KIND_LABEL.get(dyn.partition(":")[0], a.dest)
        # В фигурных скобках: простое действие-функцию _arguments зовёт
        # со своими опциями compadd (-J, -X …) ПЕРЕД нашими словами, и
        # вид кандидатов уезжал из $1 — Tab внутри подкоманд молчал.
        # Скобки исполняются как есть, без вставок.
        return "{_swarm_dyn " + dyn + " " + _q(label) + "}"
    if a.choices:
        return "(" + " ".join(str(c) for c in a.choices) + ")"
    if a.dest in FILES:
        return "_files"
    return " "


def _spec_for(path: tuple[str, ...], a: argparse.Action) -> list[str]:
    """Строки спецификации _arguments для одного аргумента."""
    # Двоеточия в действии не экранируются: _arguments считает действием
    # весь остаток после второго двоеточия (`tasks:blocked` доходит целым).
    act = _value_action(path, a)
    msg = _desc(a.metavar if isinstance(a.metavar, str) else a.dest)
    if not a.option_strings:
        if a.nargs in ("*", "+", argparse.REMAINDER):
            return [_q(f"*:{msg}:{act}")]
        opt = ":" if a.nargs == "?" else ""
        return [_q(f"{opt}:{msg}:{act}")]
    takes_value = a.nargs != 0
    repeat = "*" if isinstance(a, argparse._AppendAction) else ""  # noqa: SLF001
    helptext = _desc(a.help if a.help != argparse.SUPPRESS else "")
    out = []
    for opt in a.option_strings:
        if isinstance(a, argparse._HelpAction):  # noqa: SLF001
            out.append(_q(f"(- *){opt}[{helptext}]"))
            continue
        head = f"{repeat}{opt}" + ("=" if takes_value and opt.startswith("--") else "")
        tail = f":{msg}:{act}" if takes_value else ""
        out.append(_q(f"{head}[{helptext}]{tail}"))
    return out


def _fname(path: tuple[str, ...]) -> str:
    return "_swarm__" + "__".join(p.replace("-", "_") for p in path)


def _emit(p: argparse.ArgumentParser, path: tuple[str, ...], out: list[str]) -> None:
    subs = _subparsers(p)
    specs: list[str] = []
    for a in p._actions:  # noqa: SLF001
        if a is subs:
            continue
        specs.extend(_spec_for(path, a))
    body = [f"{_fname(path)}() {{"]
    if subs is None:
        body.append("  _arguments -s -S \\")
        body += [f"    {s} \\" for s in specs]
        body[-1] = body[-1].removesuffix(" \\")
        body.append("}")
        out.append("\n".join(body))
        return
    helps = {ca.dest: ca.help or "" for ca in subs._choices_actions}  # noqa: SLF001
    names = list(subs.choices)
    body += [
        "  local curcontext=$curcontext state state_descr line",
        "  typeset -A opt_args",
        "  _arguments -C -s -S \\",
        *[f"    {s} \\" for s in specs],
        "    '1:команда:->cmd' \\",
        "    '*::аргумент:->args'",
        "  case $state in",
        "    cmd)",
        "      local -a cmds",
        "      cmds=(",
        *[f"        {_q(n + ':' + ' '.join(helps.get(n, '').split()))}" for n in names],
        "      )",
        "      _describe -V -t commands 'команда' cmds ;;",
        "    args)",
        "      case $line[1] in",
        *[f"        {n}) {_fname((*path, n))} ;;" for n in names],
        "      esac ;;",
        "  esac",
        "}",
    ]
    out.append("\n".join(body))
    for n, child in subs.choices.items():
        _emit(child, (*path, n), out)


def zsh_script(ap: argparse.ArgumentParser) -> str:
    parts: list[str] = [PRELUDE]
    _emit(ap, (), parts)
    parts.append(EPILOGUE)
    return "\n\n".join(parts)


def cmd_completion(args: argparse.Namespace) -> int:
    """Автодополнение оболочки: команды, флаги и значения из состояния.

    Tab подсказывает id задач, вопросов, политик, уроков и прогонов
    текущего стенда (или указанного --root), а `--root <Tab>` — стенды
    этой машины, где петля уже бывала: живые прогоны первыми.

    Установка (один раз; .zshrc команда не трогает):
      swarm completion zsh --install      # пишет ~/.zfunc/_swarm
      # в ~/.zshrc, ДО compinit:
      fpath=(~/.zfunc $fpath)

    Реестр стендов пополняется любым вызовом swarm в стенде; разово
    досыпать уже существующие — `--scan <каталог>`.
    """
    if args.scan:
        found = registry.scan(args.scan)
        print(f"стендов найдено и внесено в реестр: {len(found)} ({registry.path()})",
              file=sys.stderr)
        for f in found:
            print(f"  {f}", file=sys.stderr)
    script = zsh_script(args.parser)
    if not args.install:
        print(script)
        return 0
    target = pathlib.Path(args.install).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script + "\n", encoding="utf-8")
    print(f"записано: {target}")
    print(f"в ~/.zshrc до compinit: fpath=({target.parent} $fpath)")
    print("затем: rm -f ~/.zcompdump*; exec zsh")
    return 0
