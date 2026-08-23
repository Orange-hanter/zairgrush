#!/usr/bin/env python3
"""Выживаемость мутаций: чего НЕ ловит набор тестов стенда.

Зелёный сьют говорит «код делает то, что делает». Вопрос E11 другой:
поймает ли набор ПОДМЕНУ поведения — и зависит ли ответ от того, кто
тесты писал. Мера одна и механическая: портим исходник по одному узлу
AST, гоняем сьют, считаем, сколько подмен он заметил.

Отличие от `tools/swarm/mutate.py`: тот держит рукописные якоря к
инвариантам самой петли, здесь мутанты порождаются автоматически из
дерева разбора — по чужому коду якорей не напасёшься.

ЧЕСТНО О МЕТРИКЕ. Выживший мутант — не обязательно дыра: часть подмен
поведения не меняет (эквивалентные мутанты), и отличить их может только
человек. Поэтому скрипт печатает КАЖДОГО выжившего с местом и подменой,
а не одно число: «выживаемость 12 %» без списка — цифра, которой нельзя
воспользоваться.

Запуск:
    python3 mutation-survival.py <стенд> [--module wordstat/rank.py]
    python3 mutation-survival.py <стенд> --json out.jsonl

Дерево стенда обязано быть чистым: скрипт правит исходники и возвращает
их обратно, а прерывание на середине оставит мутанта на диске.
"""
import argparse
import ast
import copy
import json
import pathlib
import subprocess
import sys

# Подмены, каждая — правдоподобная ошибка живого кода, а не случайный шум.
CMP_SWAP = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}
BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add,
            ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult}
BOOL_SWAP = {ast.And: ast.Or, ast.Or: ast.And}


class Mutant:
    __slots__ = ("kind", "lineno", "before", "after", "tree")

    def __init__(self, kind, lineno, before, after, tree):
        self.kind = kind
        self.lineno = lineno
        self.before = before
        self.after = after
        self.tree = tree

    def label(self):
        return (f"{self.kind} строка {self.lineno}: "
                f"{self.before} -> {self.after}")


def _mutants_of(tree):
    """Все мутанты модуля: по одной подмене на копию дерева.

    Копия дерева на каждого мутанта — не расточительство, а условие
    независимости: правка на месте склеила бы подмены в одну.
    """
    out = []
    nodes = list(ast.walk(tree))
    for idx, node in enumerate(nodes):
        def make(kind, before, after, apply):
            clone = copy.deepcopy(tree)
            target = list(ast.walk(clone))[idx]
            apply(target)
            out.append(Mutant(kind, getattr(node, "lineno", 0),
                              before, after, clone))

        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op = type(node.ops[0])
            if op in CMP_SWAP:
                new = CMP_SWAP[op]
                make("сравнение", op.__name__, new.__name__,
                     lambda t, new=new: t.ops.__setitem__(0, new()))
        elif isinstance(node, ast.BinOp) and type(node.op) in BIN_SWAP:
            new = BIN_SWAP[type(node.op)]
            make("арифметика", type(node.op).__name__, new.__name__,
                 lambda t, new=new: setattr(t, "op", new()))
        elif isinstance(node, ast.BoolOp) and type(node.op) in BOOL_SWAP:
            new = BOOL_SWAP[type(node.op)]
            make("логика", type(node.op).__name__, new.__name__,
                 lambda t, new=new: setattr(t, "op", new()))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            # `not X` -> `X`: снятие отрицания меняет ветку целиком.
            make("отрицание", "not X", "X",
                 lambda t: None)   # заменяется ниже, см. _strip_not
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            make("константа", str(node.value), str(not node.value),
                 lambda t: setattr(t, "value", not t.value))
        elif (isinstance(node, ast.Constant)
              and isinstance(node.value, int)
              and not isinstance(node.value, bool)
              and abs(node.value) <= 1000):
            make("число", str(node.value), str(node.value + 1),
                 lambda t: setattr(t, "value", t.value + 1))
        elif isinstance(node, ast.Raise):
            # Снятый raise — самый показательный класс: тест, не
            # проверяющий отказ, этого не заметит.
            make("отказ", "raise", "pass",
                 lambda t: None)   # см. _drop_raise
    return out


def _strip_not(tree):
    """`not X` -> `X` там, где мутант это объявил (post-обработка)."""
    class T(ast.NodeTransformer):
        def visit_UnaryOp(self, node):  # noqa: N802
            self.generic_visit(node)
            return node.operand if isinstance(node.op, ast.Not) else node
    return T().visit(tree)


def build(path: pathlib.Path):
    """Мутанты одного файла как список (метка, исходный текст)."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for m in _mutants_of(tree):
        clone = m.tree
        if m.kind == "отрицание":
            clone = _strip_not(clone)
        elif m.kind == "отказ":
            clone = _drop_raise(clone, m.lineno)
        try:
            text = ast.unparse(ast.fix_missing_locations(clone))
        except (ValueError, RecursionError):
            continue
        if text.strip() == ast.unparse(ast.parse(src)).strip():
            continue        # подмена ничего не изменила — не мутант
        out.append((m, text))
    return out


def _drop_raise(tree, lineno):
    class T(ast.NodeTransformer):
        def visit_Raise(self, node):  # noqa: N802
            if getattr(node, "lineno", 0) == lineno:
                return ast.Pass()
            return node
    return T().visit(tree)


def run_suite(stand: pathlib.Path, timeout: int = 120) -> bool:
    """True — сьют зелёный (мутант ВЫЖИЛ)."""
    r = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests",
         "-t", "."], cwd=stand, capture_output=True, text=True, check=False,
        timeout=timeout, env={"PYTHONDONTWRITEBYTECODE": "1",
                              "PATH": "/usr/bin:/bin"})
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stand")
    ap.add_argument("--module", action="append", default=[],
                    help="ограничить одним файлом (можно повторять)")
    ap.add_argument("--json", help="куда сложить построчный отчёт")
    ap.add_argument("--limit", type=int, default=400)
    args = ap.parse_args()

    stand = pathlib.Path(args.stand).resolve()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=stand,
                           capture_output=True, text=True, check=False).stdout
    if dirty.strip():
        print("дерево стенда грязное — мутант мог бы остаться на диске:",
              file=sys.stderr)
        print(dirty, file=sys.stderr)
        return 2
    if not run_suite(stand):
        print("сьют КРАСНЫЙ до мутаций: мерить нечего", file=sys.stderr)
        return 2

    targets = ([stand / m for m in args.module] if args.module
               else sorted(p for p in (stand / "wordstat").glob("*.py")
                           if p.name != "__init__.py"))
    rows, survived = [], []
    killed = 0
    for path in targets:
        original = path.read_text(encoding="utf-8")
        mutants = build(path)[:args.limit]
        rel = str(path.relative_to(stand))
        print(f"\n{rel}: мутантов {len(mutants)}")
        for m, text in mutants:
            path.write_text(text, encoding="utf-8")
            try:
                alive = run_suite(stand)
            except subprocess.TimeoutExpired:
                alive = False       # зависание — тоже пойманная подмена
            finally:
                path.write_text(original, encoding="utf-8")
            rows.append({"file": rel, "kind": m.kind, "line": m.lineno,
                         "before": m.before, "after": m.after,
                         "survived": alive})
            if alive:
                survived.append((rel, m.label()))
            else:
                killed += 1
        print(f"  поймано {sum(1 for r in rows if r['file'] == rel and not r['survived'])}"
              f" из {len(mutants)}")

    total = len(rows)
    print(f"\n=== итог: поймано {killed} из {total}"
          + (f", выживаемость {100 * (total - killed) / total:.0f} %" if total
             else "") + " ===")
    if survived:
        print("ВЫЖИВШИЕ (каждый — либо дыра в тестах, либо эквивалентная "
              "подмена; различает человек):")
        for rel, label in survived:
            print(f"  {rel}  {label}")
    if args.json:
        with pathlib.Path(args.json).open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"построчно: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
