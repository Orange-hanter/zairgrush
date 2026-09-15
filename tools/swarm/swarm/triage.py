"""P1: детерминированный триаж ревью — измеренная бэнда дешёвого контура
(ADR-027).

Бэнда отобрана распределением самого пилота (REV-002,
experiments/reviewarm/report-rev002.md): cheap = (single_file AND ≤100
изменённых строк) OR docs_only; guard = файл не защищён, новых
зависимостей нет, гейт зелёный. Всё механическое, без LLM (§7.1: если
решается без LLM — LLM не нужна); любой сомнительный случай — полное
ревью (fail-open).

Маршруты: docs_only + guard → пропуск ревью с отметкой в журнале; код в
бэнде + guard → дешёвая рука (`triage_arm`) с полным контрактом вердикта.
Код триаж не пропускает и не одобряет сам НИКОГДА — вердикт остаётся за
рукой ревьюера (§7.2, ADR-026), triage лишь выбирает, какой рукой.

Триаж ЗАПРЕЩЁН на канареечном и adversarial стендах: их swarm.toml обязан
нести `triage = false`. Иначе полоса поймала бы 7/9 посеянных багов
канареек (замер REV-002 §3), и измеритель ревьюера перестал бы мерить.
Test-only диффы бэнда не трогает: при protected_paths пилота предикат
недостижим, и данные говорят держать test-only полным ревью (REV-002 §2).

«Гейт зелёный» в guard структурен, а не проверяем: петля зовёт ревью
только после зелёного гейта (loop._run_task), отдельной проверки нет.
"""
from __future__ import annotations

import fnmatch
import pathlib
import sys
from typing import Any

_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import boundarynote  # noqa: E402

# Порог не калиброван пилотом: любое N в [64, 369) даёт то же
# маршрутизированное множество (p1fn=64 внутри, следующий однофайловый —
# e7in-close=370 снаружи). Взято круглым, с запасом под микро-диффы E13
# (≤29) и с запретом на ползание: 370 — уже медиана пилота.
BAND_MAX_LINES = 100

# Только суффиксы ДОКУМЕНТАЦИИ. `.txt` сознательно исключён: golden-
# эталоны пилота — .txt-файлы данных по 16К строк (g1nt), и docs_only,
# съедающий данные, пропустил бы ровно те диффы, ради которых размер
# и считают. Пропуск размера у docs_only нет — ошибаться надо в сторону
# полного ревью.
DOC_SUFFIXES = (".md", ".rst", ".adoc")

# Манифесты и lock-файлы пакетных менеджеров: правка такого файла —
# потенциально НОВАЯ зависимость, а дешёвая рука зависимостный риск по
# замеру REV-001 не видит (0/6 endorsed claims) — такой дифф остаётся
# полным ревью.
DEP_MANIFESTS = frozenset({
    "cargo.toml", "cargo.lock",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pyproject.toml", "poetry.lock", "uv.lock", "pipfile", "pipfile.lock",
    "go.mod", "go.sum",
    "gemfile", "gemfile.lock",
    "composer.json", "composer.lock",
})

# Пороги гигантского диффа (NXT-012) выведены из распределения корпуса
# пилота (experiments/reviewarm/rev001/ground/*.raw.diff, 18 диффов,
# замер 2026-09-15): нормальные диффы ≤ 692 изменённых строк и
# ≤ 46_072 символов (z8ck-close и s2ky-i1), гиганты — g1nt-close
# (16_086 строк / 371_519 символов) и c4rp-close (54_870 / 3_755_327).
# Между «всем нормальным» и «обоими гигантами» — разрыв в 23 раза по
# строкам и в 8 раз по символам; дефолты встали внутри разрыва с запасом
# ≥ ~2x от каждой стороны: 2_000 строк — в 2.9 раза выше максимума
# нормального и в 8 раз ниже минимального гиганта, 200_000 символов —
# 4.3x и 1.9x. Дифф за ЛЮБЫМ из порогов не отправляется в LLM-ревью
# вообще: g1nt и c4rp уже ломали прогоны (rev003/rev004 — таймауты,
# пустой вывод, rubber-stamping), и порог — защита вызова, а не выбор
# руки. Стратегический split-and-process — отдельная будущая задача.
GIANT_MAX_LINES = 2_000
GIANT_MAX_CHARS = 200_000


def giant_reason(diff_lines: int, diff_chars: int,
                 config: dict[str, Any]) -> str | None:
    """«lines» | «chars», если дифф гигантский (NXT-012), иначе None.

    Правило стоит ПЕРЕД классификацией бэнды decide(): гигант не
    маршрутизируется, а исключается из LLM-ревью детерминированно — бэнда
    выбирает руку для вызова, а здесь вызова нет. Размер приходит тот же,
    что пишется в метрику ревью (diff_lines по СЫРОМУ диффу), символы —
    длина того же сырого диффа: второй редакции размера нет и здесь.

    Пороги настраиваемы (`giant_max_lines`/`giant_max_chars`);
    `giant_exclusion = false` выключает правило целиком (стенд, который
    измеряет поведение на гигантах). Некорректное значение порога —
    fail-open на дефолт: исключение по мусорной настройке объяснить
    невозможно.
    """
    if config.get("giant_exclusion") is False:
        return None
    max_lines = config.get("giant_max_lines", GIANT_MAX_LINES)
    max_chars = config.get("giant_max_chars", GIANT_MAX_CHARS)
    if isinstance(max_lines, bool) or not isinstance(max_lines, int):
        max_lines = GIANT_MAX_LINES
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        max_chars = GIANT_MAX_CHARS
    if diff_lines > max_lines:
        return "lines"
    if diff_chars > max_chars:
        return "chars"
    return None


def is_doc(path: str) -> bool:
    """Документация — по суффиксу: README.md да, docs/schema.sql нет."""
    return path.lower().endswith(DOC_SUFFIXES)


def is_test(path: str) -> bool:
    """Тестовый файл: каталог tests/test или имя test_*, *_test, *.test.*."""
    parts = path.split("/")
    name = parts[-1]
    return ("tests" in parts[:-1] or "test" in parts[:-1]
            or name.startswith("test_") or name.endswith("_test.py")
            or ".test." in name or ".spec." in name)


def is_dep_manifest(path: str) -> bool:
    """Манифест/lock зависимостей — по имени файла, каталог не важен."""
    name = path.rsplit("/", 1)[-1].lower()
    return (name in DEP_MANIFESTS
            or (name.startswith("requirements") and name.endswith(".txt")))


def decide(diff: str, diff_files: int, diff_lines: int,
           config: dict[str, Any]) -> dict[str, Any]:
    """Маршрут ревью по бэнде: full | cheap | skip плюс факты для журнала.

    Размер (diff_files/diff_lines) приходит снаружи — тот же, что пишется
    в строку метрик ревью (parsing.diff_size по СЫРОМУ диффу): бэнда
    обязана читать те числа, которыми замерялась, а не вторую редакцию
    размера. Пути файлов — из заголовков диффа (boundarynote.diff_files).
    """
    if config.get("triage") is False:
        # Канареечный/adversarial стенд: полоса запрещена ADR-027, полное
        # ревью идёт молча — ровно как до введения триажа.
        return {"route": "full"}
    paths = boundarynote.diff_files(diff)
    docs_only = bool(paths) and all(is_doc(p) for p in paths)
    band = docs_only or (diff_files == 1 and diff_lines <= BAND_MAX_LINES)
    if not band:
        return {"route": "full"}
    if not docs_only and (not paths or all(is_test(p) for p in paths)):
        # test-only — полное ревью: исключение явное (ADR-027), а не
        # следствие guard'а. Пустой список путей при непустом диффе —
        # сомнительный случай, и он тоже уходит на полное ревью.
        return {"route": "full", "excluded": "test_only"}
    protected = boundarynote.normalize_protected(config.get("protected_paths"))
    if any(fnmatch.fnmatch(p, pat) for p in paths for pat in protected):
        return {"route": "full", "guard_block": "protected"}
    if any(is_dep_manifest(p) for p in paths):
        return {"route": "full", "guard_block": "new_dependency"}
    if docs_only:
        return {"route": "skip", "docs_only": True}
    return {"route": "cheap"}


def cheap_arm(config: dict[str, Any]) -> tuple[Any, Any] | None:
    """Дешёвая рука триажа (`triage_arm`): те же формы, что у draw_arm.

    Без явной руки кодовый дифф бэнды уходит на полное ревью (fail-open):
    skip для кода запрещён, а одобрять триаж не вправе. Формы значения —
    как у элемента пула рук: "модель", ["модель", "усилие"] или
    {"model": ..., "effort": ...}; пустое усилие — «флаг не передавать».
    """
    arm = config.get("triage_arm")
    if isinstance(arm, str):
        return arm, None
    if isinstance(arm, dict):
        return arm.get("model"), arm.get("effort")
    if isinstance(arm, list | tuple) and arm:
        pair = [*list(arm), None]
        return pair[0], pair[1]
    return None
