from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import obs  # noqa: E402

log = obs.get_logger("docctx")

# Таймаут по умолчанию: cod-doc — внешний CLI, петля не должна ждать вечно.
DEFAULT_TIMEOUT = 30

# Ключи ожидаемого ответа. Проверяем структуру, чтобы мусорный JSON
# не дошёл до промпта исполнителя.
_EXPECTED_KEYS = ("docs", "links_at_risk", "token_estimate")


def _bin(config: dict[str, Any]) -> str:
    return str(config.get("cod_doc_bin") or "cod-doc")


def enabled_for_doc_context(config: dict[str, Any], role: str) -> bool:
    """Включён ли doc_context для роли."""
    mode = str((config.get("experiments") or {}).get("doc_context", "off"))
    if mode == "all":
        return True
    return mode == role


def codctx(config: dict[str, Any], args: dict[str, Any],
           timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Единственная дверь к cod-doc `ctx docs`. Возвращает (ok, payload).

    При сбое возвращает (False, диагностика); вызывающий решает,
    как рендерить пустой блок. Никакие исключения наружу не пролетают:
    cod-doc — внешний инструмент, и его отсутствие не имеет права
    остановить петлю (RFC 22 §3.4, вариант C).
    """
    paths = args.get("paths") or []
    if not paths:
        return False, "empty paths list"
    budget = args.get("budget_tokens")
    if budget is None:
        budget = config.get("doc_context_budget_tokens", 2000)
    cmd = [
        _bin(config),
        "ctx", "docs",
        "-p", str(args.get("project", "zairgrush")),
        "--paths", ",".join(str(p) for p in paths),
        "--budget-tokens", str(int(budget)),
        "--json",
    ]
    if not shutil.which(cmd[0]):
        return False, f"cod-doc binary not found: {cmd[0]}"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()[:300]
    text = r.stdout.strip()
    if not text:
        return False, "empty response"
    try:
        parsed = json.loads(text)
    except ValueError as e:
        return False, f"invalid json: {e}"
    if not isinstance(parsed, dict):
        return False, "json is not an object"
    missing = [k for k in _EXPECTED_KEYS if k not in parsed]
    if missing:
        return False, f"missing keys: {', '.join(missing)}"
    return True, text
