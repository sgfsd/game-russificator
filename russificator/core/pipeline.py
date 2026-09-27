"""Пайплайн русификации: движок → извлечение → перевод → внедрение → шрифт.

Пайплайн ничего не знает об устройстве конкретных движков (только
интерфейс плагина) и о том, как переводится текст (только интерфейс
переводчика). Интерфейс программы получает события через ``on_event``:

  {"type": "stage", "id": "translate", "title": "Перевод"}
  {"type": "progress", "done": 120, "total": 840, "message": "..."}
  {"type": "status", "message": "Скачивание модели…", "fraction": 0.4}
  {"type": "log", "level": "info|warning|error", "message": "..."}

Гарантии:
  * игра не трогается, пока текст не извлечён и не переведён;
  * если русификация уже была — сначала возвращаются оригиналы, поэтому
    текст всегда берётся из исходных файлов;
  * каждый изменённый файл попадает в бэкап с манифестом — откат одной кнопкой;
  * прерванный перевод продолжается с места остановки (проект + память переводов).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import paths
from .backup import GameBackup
from .plugin_api import EngineNotDetectedError, EnginePlugin
from .registry import detect_engine
from .universal import TranslationProject
from ..translation.base import Translator, TranslatorError
from ..translation.memory import TranslationMemory
from ..translation.service import TranslationCancelled, TranslationService

log = logging.getLogger("russificator")

EventFn = Callable[[Dict[str, Any]], None]

STAGES = {
    "detect": "Определение движка",
    "prepare": "Подготовка игры",
    "extract": "Извлечение текста",
    "translator": "Подготовка переводчика",
    "translate": "Перевод",
    "inject": "Внедрение перевода",
    "font": "Шрифт с кириллицей",
}


@dataclass
class PipelineOptions:
    font_path: Optional[Path] = None      # свой шрифт; None — из библиотеки
    use_memory: bool = True               # общая память переводов


@dataclass
class PipelineResult:
    engine_title: str = ""
    detection_details: str = ""
    game_title: str = ""
    total_lines: int = 0
    translated_lines: int = 0
    failed_lines: int = 0
    skipped_lines: int = 0
    from_memory: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    instructions: List[str] = field(default_factory=list)
    success: bool = False
    cancelled: bool = False
    injected: bool = False
    live: bool = False            # можно включить «живой» перевод (Unity)
    game_exe: str = ""

    def summary(self) -> str:
        lines = [f"Движок: {self.engine_title}" + (f" ({self.detection_details})" if self.detection_details else "")]
        if self.total_lines:
            lines.append(f"Строк: всего {self.total_lines}, переведено {self.translated_lines}, "
                         f"не переведено {self.failed_lines}, пропущено {self.skipped_lines}")
        if self.errors:
            lines.append("Ошибки:")
            lines += [f"  - {e}" for e in self.errors]
        if self.warnings:
            lines.append("Предупреждения:")
            lines += [f"  - {w}" for w in self.warnings]
        if self.instructions:
            lines.append("Что дальше:")
            lines += [f"  - {i}" for i in self.instructions]
        if self.cancelled:
            lines.append("Остановлено. Прогресс сохранён — при следующем запуске перевод продолжится.")
        elif self.success:
            lines.append("Готово: игра русифицирована, можно запускать.")
        return "\n".join(lines)


def _fallback_translator(primary):
    """Запасной переводчик для строк, которые не дались основному: машинный, если он уже скачан."""
    from ..translation import machine
    if isinstance(primary, machine.MachineTranslator) or not machine.is_installed():
        return None
    return machine.MachineTranslator()


class _NothingToTranslate(Exception):
    """Заранее переводить нечего (движок переводит в рантайме)."""


def project_dir(game_dir: Path) -> Path:
    """Папка проекта перевода игры (в данных программы, а не рядом с игрой)."""
    game_dir = Path(game_dir).resolve()
    slug = re.sub(r"[^\w\-]+", "_", game_dir.name, flags=re.UNICODE).strip("_")[:40] or "game"
    digest = hashlib.sha1(str(game_dir).lower().encode("utf-8")).hexdigest()[:8]
    return paths.sub("projects") / f"{slug}-{digest}"


class Pipeline:
    """Оркестратор всего процесса русификации."""

    def __init__(self, translator: Translator, options: Optional[PipelineOptions] = None,
                 on_event: Optional[EventFn] = None, cancel: Optional[threading.Event] = None):
        self.translator = translator
        self.options = options or PipelineOptions()
        self._on_event = on_event or (lambda ev: None)
        self.cancel = cancel or threading.Event()

    # ---------- события ----------

    def emit(self, **event: Any) -> None:
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001
            log.debug("обработчик событий упал", exc_info=True)

    def stage(self, sid: str) -> None:
        self.emit(type="stage", id=sid, title=STAGES[sid])
        log.info("== %s", STAGES[sid])

    def info(self, message: str, level: str = "info") -> None:
        self.emit(type="log", level=level, message=message)
        getattr(log, "warning" if level == "warning" else "error" if level == "error" else "info")(message)

    # ---------- полный прогон ----------

    def run(self, game_dir: Path, work_dir: Optional[Path] = None) -> PipelineResult:
        result = PipelineResult()
        game_dir = Path(game_dir).resolve()
        work_dir = Path(work_dir) if work_dir else project_dir(game_dir)

        # 1. движок
        self.stage("detect")
        try:
            plugin, det = detect_engine(game_dir)
        except EngineNotDetectedError as exc:
            result.errors.append(str(exc))
            return result
        result.engine_title = det.engine_name or plugin.title
        result.detection_details = det.details
        result.warnings.extend(det.notes)
        self.info(f"Движок: {result.engine_title}" + (f" — {det.details}" if det.details else ""))
        plugin.status = lambda msg, frac=None: self.emit(type="status", message=msg, fraction=frac)
        plugin.cancel = self.cancel

        # 2. вернуть оригиналы, если игру уже русифицировали
        self.stage("prepare")
        if GameBackup(game_dir).exists:
            from .restore import restore_backups
            self.info("Игра уже русифицирована — возвращаю оригинальные файлы перед новым проходом")
            restore_backups(game_dir)
        project = TranslationProject(game_dir, plugin.engine_id, work_dir)
        project.backup = GameBackup(game_dir)
        try:
            prepared = plugin.pre_extract(game_dir, project)
        except Exception as exc:  # noqa: BLE001
            log.warning("pre_extract упал: %s", exc)
            prepared = None
        effective_dir = Path(prepared) if prepared else game_dir
        project.meta["effective_game_dir"] = str(effective_dir)
        project.meta["detection_details"] = det.details

        # 3. текст
        self.stage("extract")
        try:
            extraction = plugin.extract(effective_dir, project)
        except Exception as exc:  # noqa: BLE001
            log.exception("Извлечение упало")
            result.errors.append(f"Не удалось извлечь текст из игры: {exc}")
            return self._finish(result, project, plugin)
        project.warnings.extend(extraction.warnings)
        result.game_title = str(project.meta.get("game_title") or game_dir.name)
        if not project.entries and not plugin.runtime_translation:
            result.errors.append("В игре не найден текст для перевода — ничего не изменено.")
            result.warnings.extend(project.warnings)
            return self._finish(result, project, plugin, save=False)
        self.info(f"Найдено строк: {len(project.entries)}")
        self._merge_previous(project, work_dir)
        project.save()

        # 4-5. перевод
        memory = None
        try:
            if not project.entries:
                raise _NothingToTranslate()
            self.stage("translator")
            self.translator.prepare(lambda msg, frac=None: self.emit(type="status", message=msg, fraction=frac),
                                    self.cancel)
            if self.options.use_memory:
                try:
                    memory = TranslationMemory(paths.cache_dir() / "translations.db")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Память переводов недоступна: %s", exc)
            self.stage("translate")
            service = TranslationService(
                self.translator, memory, cancel=self.cancel, fallback=_fallback_translator(self.translator),
                on_progress=lambda d, t, m: self.emit(type="progress", done=d, total=t, message=m))
            stats = service.run(project)
            result.from_memory = stats.from_memory
            if stats.failed:
                project.warnings.append(
                    f"Не удалось перевести {stats.failed} строк даже повторно — в игре они останутся "
                    "на английском. Запустите русификацию ещё раз (переведутся только они) или скачайте "
                    "машинный переводчик — он подстрахует. Примеры: "
                    + "; ".join(f"«{s}»" for s in stats.failed_examples[:5]))
            if stats.shortened:
                project.warnings.append(f"{stats.shortened} строк длиннее места в интерфейсе — сокращены.")
        except _NothingToTranslate:
            pass  # движок переводит в рантайме — сразу внедряем
        except TranslationCancelled:
            result.cancelled = True
            return self._finish(result, project, plugin)
        except TranslatorError as exc:
            result.errors.append(str(exc))
            return self._finish(result, project, plugin)
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "Cancelled":
                result.cancelled = True
            else:
                log.exception("Перевод упал")
                result.errors.append(f"Перевод прерван ошибкой: {exc}")
            return self._finish(result, project, plugin)
        finally:
            try:
                self.translator.close()
            except Exception:  # noqa: BLE001
                pass
            if memory is not None:
                memory.close()

        # 6. внедрение
        self.stage("inject")
        try:
            plugin.inject(effective_dir, project)
            result.injected = True
        except Exception as exc:  # noqa: BLE001
            log.exception("Внедрение перевода упало")
            project.errors.append(f"Внедрение перевода не удалось: {exc}. Игра возвращена к оригиналу.")
            from .restore import restore_backups
            restore_backups(game_dir)
            return self._finish(result, project, plugin)

        # 7. шрифт
        self.stage("font")
        self._install_font(plugin, effective_dir, project)
        try:   # состояние после русификации — «Мои игры» по нему видят, что обновление затёрло перевод
            st = project.stats()
            project.backup.snapshot({"translator": project.meta.get("translator", ""),
                                     "date": time.strftime("%Y-%m-%d %H:%M"),
                                     "translated": st["translated"] + st["approved"], "total": st["total"]})
        except Exception as exc:  # noqa: BLE001
            log.warning("Снимок состояния не записан: %s", exc)
        return self._finish(result, project, plugin)

    # ---------- помощники ----------

    def _merge_previous(self, project: TranslationProject, work_dir: Path) -> None:
        # переводы прошлого прохода берём, только если переводил тот же способ и та же модель:
        # сменили машинный перевод на нейросеть — значит, хотят перевести заново и лучше
        translator_id = f"{type(self.translator).__name__}:{self.translator.cache_id}"
        project.meta["translator"] = translator_id
        if not (work_dir / TranslationProject.PROJECT_FILE).exists():
            return
        try:
            old = TranslationProject.load(work_dir)
        except Exception as exc:  # noqa: BLE001
            log.warning("Старый проект не прочитан: %s", exc)
            return
        if old.engine != project.engine:
            return
        if old.meta.get("translator", translator_id) != translator_id:
            self.info("Способ перевода изменился — текст переводится заново")
            return
        n = project.merge_translations(old)
        from ..translation.markup import normalize_layout
        for e in project.entries:        # переводы прошлых версий программы — к нынешним правилам
            if e.translation:
                e.translation = normalize_layout(e.source, e.translation)
        if n:
            self.info(f"Продолжаю прошлую работу: {n} строк уже переведено")

    def _install_font(self, plugin: EnginePlugin, game_dir: Path, project: TranslationProject) -> None:
        font = self.options.font_path
        if font is None:
            from ..fonts.library import ensure_font
            try:
                font = ensure_font()
            except Exception as exc:  # noqa: BLE001
                log.info("Шрифт из библиотеки недоступен: %s", exc)
        try:
            ok = plugin.install_font(game_dir, project, Path(font) if font else None)
            if ok is False:
                project.warnings.append("Кириллический шрифт подключить автоматически не удалось — "
                                        "если вместо букв квадратики, см. раздел «Шрифты» в README.")
        except Exception as exc:  # noqa: BLE001
            log.exception("Шрифт")
            project.warnings.append(f"Не удалось подключить шрифт: {exc}")

    def _finish(self, result: PipelineResult, project: TranslationProject, plugin: EnginePlugin,
                save: bool = True) -> PipelineResult:
        st = project.stats()
        result.total_lines = st["total"]
        result.translated_lines = st["translated"] + st["approved"]
        result.skipped_lines = st["skipped"]
        result.failed_lines = st["failed"] + (st["new"] if result.injected else 0)
        result.warnings.extend(w for w in project.warnings if w not in result.warnings)
        result.errors.extend(project.errors)
        if result.injected:
            result.instructions = plugin.post_inject_instructions()
            result.live = plugin.supports_live
            result.game_exe = str((project.meta.get("unity") or {}).get("exe") or "")
        result.success = result.injected and not result.errors and (
            result.translated_lines > 0 or plugin.runtime_translation)
        project.meta["last_result"] = {"success": result.success, "translated": result.translated_lines,
                                       "failed": result.failed_lines}
        if save:
            try:
                project.save()
            except OSError as exc:
                log.warning("Проект не сохранён: %s", exc)
        return result
