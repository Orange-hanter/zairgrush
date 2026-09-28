"""Реестр стендов: какие корни `swarm` видел на этой машине.

Прогоны не регистрируются нигде, кроме журнала своего стенда, — и
вопрос «где сейчас что-то бежит» без реестра решается обходом диска.
Реестр отвечает на него списком корней, а живость каждого проверяет
флок (`lockprobe`), не сам реестр: запись здесь говорит только «этот
корень существовал», и умереть с ней вместе прогону ничто не мешает.

Файл — `${XDG_STATE_HOME:-~/.local/state}/swarm/roots.json`, форма
`{"<абсолютный корень>": {"seen": "<iso utc>"}}`. `SWARM_REGISTRY`
подменяет путь целиком — для тестов, чтобы прогон набора не писал
в домашний каталог оператора.

Модуль не импортирует ничего из роя: его читает `clitab` на каждый Tab.
"""
import json
import os
import pathlib
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

ENV_PATH = "SWARM_REGISTRY"
# Отметка «видел» чаще раза в минуту ничего не добавляет, а запись на
# каждый вызов CLI — лишний fsync на горячем пути `swarm status`.
FRESH = timedelta(seconds=60)


def path() -> pathlib.Path:
    explicit = os.environ.get(ENV_PATH)
    if explicit:
        return pathlib.Path(explicit)
    base = os.environ.get("XDG_STATE_HOME") or str(
        pathlib.Path.home() / ".local" / "state")
    return pathlib.Path(base) / "swarm" / "roots.json"


def is_stand(root: pathlib.Path) -> bool:
    """Стенд — корень, где петля уже была или настроена.

    Голый каталог, в котором кто-то набрал `swarm --help`, реестр не
    засоряет: подсказка `--root` состоит из мест, где есть что смотреть.
    """
    return (root / "swarm.toml").is_file() or (root / ".swarm").is_dir()


def load() -> dict[str, dict[str, Any]]:
    """Реестр как данные: битый файл — пустой реестр, а не отказ."""
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _save(data: dict[str, dict[str, Any]]) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Атомарно: два CLI в соседних окнах не должны оставить полфайла.
    # Потерянное обновление при гонке безвредно — следующий вызов допишет.
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".roots-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        pathlib.Path(tmp).replace(target)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def touch(root: str | pathlib.Path) -> bool:
    """Отметить корень как увиденный. True — запись была.

    Исключения не глотает: решать, что делать с поломкой реестра, —
    вызывающему (CLI пишет её в диагностику и продолжает команду).
    """
    resolved = pathlib.Path(root).resolve()
    if not is_stand(resolved):
        return False
    now = datetime.now(UTC)
    data = load()
    key = str(resolved)
    seen = data.get(key, {}).get("seen")
    try:
        if seen and now - datetime.fromisoformat(str(seen)) < FRESH:
            return False
    except ValueError:
        pass                   # битая метка — перепишем свежей
    # Исчезнувшие каталоги выметаются попутно: запись всё равно идёт.
    data = {k: v for k, v in data.items() if is_stand(pathlib.Path(k))}
    data[key] = {"seen": now.isoformat(timespec="seconds")}
    _save(data)
    return True


def roots() -> list[tuple[pathlib.Path, str]]:
    """Живые на диске корни с отметкой «видел», свежие первыми.

    Исчезнувший каталог выпадает при чтении; из файла он уходит при
    следующей записи (`touch`), а не здесь — Tab не пишет на диск.
    """
    out = [(pathlib.Path(k), str(v.get("seen") or ""))
           for k, v in load().items()]
    out = [(p, seen) for p, seen in out if is_stand(p)]
    return sorted(out, key=lambda r: r[1], reverse=True)


def scan(base: str | pathlib.Path, depth: int = 3) -> list[pathlib.Path]:
    """Разовый досыл: найти стенды под каталогом и внести в реестр.

    Реестр наполняется вызовами CLI, и стенд, где `swarm` не запускали
    после установки автодополнения, в нём отсутствует. Скан закрывает
    этот разрыв один раз. Глубина ограничена: обход домашнего каталога
    целиком на каждую установку — не то, чего ждут от подсказки.
    """
    found: list[pathlib.Path] = []
    start = pathlib.Path(base).resolve()

    def walk(d: pathlib.Path, left: int) -> None:
        if is_stand(d):
            found.append(d)
        if left == 0:
            return
        try:
            kids = sorted(p for p in d.iterdir()
                          if p.is_dir() and not p.name.startswith(".")
                          and p.name not in {"node_modules", "__pycache__"})
        except OSError:
            return
        for k in kids:
            walk(k, left - 1)

    walk(start, depth)
    if found:
        data = load()
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        for f in found:
            data.setdefault(str(f), {"seen": stamp})
        _save(data)
    return found
