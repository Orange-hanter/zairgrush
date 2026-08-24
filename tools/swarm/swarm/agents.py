"""Вызовы дорогих ролей: исполнитель и ревьюер (§4, §6.0).

Собрано из прототипов: извлечение отчёта из последнего assistant-события
(урок SMOKE-1), схема вердикта через `--json-schema`, детект отказа по
квоте, карта символов в handoff по условию (ADR-006), хелпер
коммит-сообщений (§7.2).
"""
import importlib.util
import pathlib
import random
import sys
from types import ModuleType
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import executor  # noqa: E402
import modlock  # noqa: E402
import parsing as parsing_mod  # noqa: E402
import promptbuilder  # noqa: E402
import reviewer  # noqa: E402


def _load(name: str) -> ModuleType:
    # Загрузка модулей — гонка, пока плечи дуэли идут в потоках:
    # модуль публикуется в sys.modules ДО выполнения (иначе не сходятся
    # круговые импорты), и сосед видит пустышку. Замок ОБЩИЙ на все
    # загрузчики петли — см. modlock.py.
    with modlock.LOCK:
        cached = modlock.ready(name)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"не удалось загрузить модуль {name}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod


log = _load("obs").get_logger("agents")

# Парсинг ответов вынесен в parsing.py; здесь shim для потребителей.
condense_diff = parsing_mod.condense_diff
_extract_fenced_code = parsing_mod.extract_fenced_code


class Agents:
    # Классовые дефолты кэшей: часть тестов собирает Agents через
    # __new__ без __init__, и метод, споткнувшийся об отсутствующий
    # атрибут, ломал бы им ревью из-за опционального слоя памяти.
    memory_cache: tuple[str, str] | None = None
    norms_cache: tuple[str, str] | None = None
    # Дерево, В КОТОРОМ работает исполнитель. Почти всегда это корень
    # состояния, и тогда всё как было. Отличается ровно в одном случае —
    # теневое плечо дуэли (duel.py) работает в git-worktree, чтобы два
    # исполнителя на одной задаче не переписывали друг друга. Атрибут, а
    # не аргумент, потому что менять пришлось бы подпись пяти функций
    # ради случая, которого в 99 % прогонов нет.
    work_root: Any = None
    # Блок пуриста (E14): (id задачи, текст). Считается один раз на
    # задачу — см. promptbuilder.unclear_block.
    unclear_cache: tuple[str, str] | None = None
    # Метка в именах файлов сырья: пусто у обычного прогона, `-shadow` у
    # теневого плеча дуэли. Два потока, пишущие один путь, — молчаливая
    # потеря журнала, а не падение.
    log_tag: str = ""

    def __init__(self, state: Any, config: dict[str, Any]) -> None:
        self.state = state
        self.config = config
        self.work_root = state.root
        self.driver = _load("driver")
        self.loop_mod = _load("loop")
        self.helpers: ModuleType | None = None
        self.codemap: ModuleType | None = None
        self.map_cache: tuple[tuple[str, int], str] | None = None
        # Блоки памяти считаются один раз на ЗАДАЧУ: между раундами они
        # обязаны быть байт-стабильны, иначе каждый раунд переписывает
        # префикс-кэш промпта (§8 — экономика порядка блоков).
        self.memory_cache: tuple[str, str] | None = None
        self.norms_cache: tuple[str, str] | None = None
        # Почему ревью не состоялось: оркестратору нужен диагноз, а не
        # голое None. «Кончился бюджет» и «модель ответила мусором» —
        # разные болезни с разным лечением.
        self.last_review_failure: str | None = None
        # Почему не состоялся ПОСЛЕДНИЙ вызов исполнителя: причина, время,
        # события, stderr. None = вызов удался.
        self.last_implement_failure: dict[str, Any] | None = None
        # Жребий для пулов моделей и усилий. Отдельный экземпляр, а не
        # глобальный random: тесты подменяют его сидом, не трогая
        # состояние процесса.
        self.rng = random.Random(config.get("tuning_seed"))  # noqa: S311 — жребий руки замера, не ключи
        self.last_tuning: dict[str, Any] = {}

    # --- контекст ---------------------------------------------------------

    def repo_map(self, task: dict[str, Any]) -> str | None:
        """Делегат к `promptbuilder.repo_map`."""
        return promptbuilder.repo_map(self, task)

    def _tree_fingerprint(self) -> str:
        """Делегат к `promptbuilder.tree_fingerprint`."""
        return promptbuilder.tree_fingerprint(self)

    # --- исполнитель ------------------------------------------------------

    def memory_block(self, task: dict[str, Any]) -> str:
        """Делегат к `promptbuilder.memory_block`."""
        return promptbuilder.memory_block(self, task)

    def norms_for(self, task: dict[str, Any]) -> str:
        """Делегат к `promptbuilder.norms_for`."""
        return promptbuilder.norms_for(self, task)

    def handoff(self, task: dict[str, Any], feedback: str | None,
                repo_map: str | None, memory: str | None = None) -> str:
        """Делегат к `promptbuilder.handoff`."""
        return promptbuilder.handoff(self, task, feedback, repo_map, memory)


    def implement(self, task: dict[str, Any], feedback: str | None,
                  iteration: int) -> dict[str, Any] | None:
        # Поле задачи читается ТОЛЬКО за флагом: с flag=off implement()
        # обязан остаться байт-в-байт сегодняшним (E10, замер против
        # исходного поведения).
        """Делегат к `executor.implement`."""
        return executor.implement(self, task, feedback, iteration)



    def _implement_fill(self, task: dict[str, Any], feedback: str | None,
                        iteration: int) -> dict[str, Any] | None:
        """Делегат к `executor.implement_fill`."""
        return executor.implement_fill(self, task, feedback, iteration)

    @staticmethod
    def _report_in(text: str) -> dict[str, Any] | None:
        """Делегат к `parsing.report_in`."""
        return parsing_mod.report_in(text)

    @classmethod
    def _extract_report(cls, stream: str) -> dict[str, Any] | None:
        """Делегат к `parsing.extract_report`."""
        return parsing_mod.extract_report(stream)

    # --- ревьюер ----------------------------------------------------------

    def _review_prompt_parts(self, task: dict[str, Any], gate_tail: str, diff: str,
                             want_verification: bool = False,
                             verify_results: list[dict[str, Any]] | None = None,
                             memory: str = "", lens: str = "",
                             retry_note: str = "") -> tuple[str, str, str]:
        """Делегат к `promptbuilder.review_prompt_parts`."""
        return promptbuilder.review_prompt_parts(
            task, gate_tail, diff, want_verification, verify_results,
            memory, lens, retry_note)

    def review_prompt(self, task: dict[str, Any], gate_tail: str, diff: str,
                      want_verification: bool = False,
                      verify_results: list[dict[str, Any]] | None = None,
                      memory: str = "", lens: str = "") -> str:
        """Делегат к `promptbuilder.review_prompt`."""
        return promptbuilder.review_prompt(
            task, gate_tail, diff, want_verification,
            verify_results, memory, lens)

    def work_diff(self) -> str:
        """То, что ревьюер обязан увидеть, — включая созданные файлы."""
        diff: str = self.state.work_diff()
        return diff

    def _wants_verification(self, task: dict[str, Any]) -> bool:
        """Делегат к `reviewer.wants_verification`."""
        return reviewer.wants_verification(self, task)

    def _tuning(self, prefix: str, confirming: bool = False) -> list[str]:
        """Делегат к `promptbuilder.tuning`."""
        return promptbuilder.tuning(self, prefix, confirming)

    def _draw(self, key: str, confirming: bool) -> Any:
        """Делегат к `promptbuilder.draw`."""
        return promptbuilder.draw(self, key, confirming)

    def review(self, task: dict[str, Any], gate_tail: str, iteration: int,
               attempt: int = 1,
               verify_results: list[dict[str, Any]] | None = None,
               confirming: bool = False) -> dict[str, Any] | None:
        # Голый `git diff` не показывает созданные файлы: ревьюер получал
        # пустоту и мог одобрить её, а `git add -A` вносил непроверенное
        # в историю. Единый источник — state.work_diff (intent-to-add).
        """Делегат к `reviewer.review`."""
        return reviewer.review(self, task, gate_tail, iteration, attempt,
                               verify_results, confirming)

    # --- хелперы ----------------------------------------------------------

    def commit_message(self, task: dict[str, Any], diff: str) -> str:
        # Хелпер дешёвый, но не бесплатный, и 336 КБ эталона ему так же
        # нечего читать, как и ревьюеру.
        diff = parsing_mod.condense_diff(diff)
        fallback = f"{task['id']}: {task['title']}"
        if self.helpers is None:
            try:
                self.helpers = _load("helpers")
            except Exception:
                log.warning("хелперы недоступны, сообщение коммита по шаблону",
                            exc_info=True)
                return fallback
            # Метрики хелперов — факт ПРОГОНА и живут в .swarm стенда.
            # Без настройки они оседали в каталоге пакета: прогоны разных
            # проектов смешивались в один файл в исходниках инструмента.
            self.helpers.configure(self.state.dir / "helper-metrics.jsonl")
        message, source = self.helpers.commit_message(task, diff, fallback)
        self.state.log("commit_message", task=task["id"], source=source)
        return str(message)
