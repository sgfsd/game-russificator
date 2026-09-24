"""Окно программы: нативное окно (pywebview/WebView2) с интерфейсом на HTML/CSS.

Python-часть — объект :class:`Api`, методы которого вызываются из
JavaScript (``window.pywebview.api.<метод>``). Долгие операции (загрузка
моделей, русификация) идут в фоновых потоках, а прогресс уходит в окно
событиями ``window.onBackendEvent({...})``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

import russificator
from .. import paths, settings
from ..translation import ORDER_CONTACT, ORDER_TELEGRAM

log = logging.getLogger("russificator.ui")

WEB = Path(__file__).resolve().parent / "web"


def _open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _dir_size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    if not p.is_dir():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


class Api:
    """Методы, доступные интерфейсу. Всё, что возвращается, — JSON-совместимое."""

    def __init__(self) -> None:
        self._window = None
        self._cfg = settings.load()
        settings.apply_data_dir(self._cfg)
        self._session_keys: Dict[str, str] = {}   # ключи, которые не просили запоминать
        self._task_lock = threading.Lock()
        self._task: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._last_progress = 0.0
        self._live = None                         # LiveServer живого перевода
        self._live_cancel = threading.Event()

    # ---------- связь с окном ----------

    def _bind(self, window) -> None:
        self._window = window

    def _emit(self, event: Dict[str, Any]) -> None:
        if self._window is None:
            return
        if event.get("type") in ("progress", "status", "download") and not event.get("final"):
            now = time.monotonic()
            if now - self._last_progress < 0.1:
                return
            self._last_progress = now
        try:
            self._window.evaluate_js(f"window.onBackendEvent({json.dumps(event, ensure_ascii=False)})")
        except Exception:  # noqa: BLE001
            log.debug("окно не приняло событие", exc_info=True)

    def _run_task(self, name: str, fn) -> Dict[str, Any]:
        with self._task_lock:
            if self._task is not None and self._task.is_alive():
                return {"ok": False, "error": "Дождитесь окончания текущей операции."}
            self._cancel = threading.Event()

            def wrapper():
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    log.exception("Задача %s упала", name)
                    self._emit({"type": "task_error", "task": name, "message": str(exc), "final": True})

            self._task = threading.Thread(target=wrapper, name=name, daemon=True)
            self._task.start()
        return {"ok": True}

    # ---------- состояние ----------

    def init(self) -> Dict[str, Any]:
        from ..translation import local_llm, machine
        from ..translation.cloud import PROVIDERS
        cfg = dict(self._cfg)
        keys = cfg.pop("cloud_keys", {}) or {}
        return {
            "version": russificator.__version__,
            "settings": cfg,
            "saved_keys": {p: bool(v) for p, v in keys.items()},
            "providers": [{
                "id": p.id, "title": p.title, "base_url": p.base_url, "model": p.default_model,
                "key_url": p.key_url, "note": p.note, "needs_key": p.needs_key,
            } for p in PROVIDERS.values()],
            "presets": self._presets(),
            "machine": {"installed": machine.is_installed(), "size_mb": machine.installed_size_mb(),
                        "download_mb": machine.APPROX_SIZE_MB},
            "ram_gb": round(local_llm.total_ram_gb(), 1),
            "runtime_installed": local_llm.server_exe() is not None,
            "order": {"telegram": ORDER_TELEGRAM, "url": ORDER_CONTACT},
            "data_dir": str(paths.app_home()),
        }

    def _presets(self) -> List[Dict[str, Any]]:
        from ..translation import local_llm
        return [{
            "id": p.id, "title": p.title, "description": p.description, "size_gb": p.size_gb,
            "min_ram_gb": p.min_ram_gb, "good_vram_gb": p.good_vram_gb,
            "installed": local_llm.model_installed(p),
        } for p in local_llm.PRESETS]

    def hardware(self) -> Dict[str, Any]:
        """Железо и рекомендация модели (видеокарты видны после установки llama.cpp)."""
        from ..translation import local_llm
        gpus = local_llm.gpu_devices()
        vram = max((g["vram_gb"] for g in gpus), default=None)
        return {"ram_gb": round(local_llm.total_ram_gb(), 1), "gpus": gpus,
                "gpu_known": local_llm.server_exe() is not None,
                "recommended": local_llm.recommend_preset(vram_gb=vram if vram is not None else 0)}

    def save_settings(self, partial: Dict[str, Any]) -> Dict[str, Any]:
        partial = {k: v for k, v in (partial or {}).items() if k in settings.DEFAULTS and k != "cloud_keys"}
        data_dir_changed = "data_dir" in partial and partial["data_dir"] != self._cfg.get("data_dir")
        self._cfg.update(partial)
        settings.save(self._cfg)
        if data_dir_changed:
            settings.apply_data_dir(self._cfg)
        return {"ok": True, "data_dir": str(paths.app_home())}

    # ---------- ключи API ----------

    def set_api_key(self, provider: str, key: str, remember: bool) -> Dict[str, Any]:
        key = (key or "").strip()
        keys = dict(self._cfg.get("cloud_keys") or {})
        if remember and key:
            keys[provider] = settings.protect(key)
            self._session_keys.pop(provider, None)
        else:
            keys.pop(provider, None)
            if key:
                self._session_keys[provider] = key
            else:
                self._session_keys.pop(provider, None)
        self._cfg["cloud_keys"] = keys
        settings.save(self._cfg)
        return {"ok": True, "saved": bool(remember and key)}

    def _key(self, provider: str, explicit: str = "") -> str:
        if explicit and explicit.strip():
            return explicit.strip()
        if provider in self._session_keys:
            return self._session_keys[provider]
        return settings.unprotect((self._cfg.get("cloud_keys") or {}).get(provider, ""))

    def list_models(self, provider: str, base_url: str = "", key: str = "") -> Dict[str, Any]:
        from ..translation.cloud import list_models
        try:
            return {"ok": True, "models": list_models(provider, self._key(provider, key), base_url)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def test_cloud(self, provider: str, base_url: str, model: str, key: str = "") -> Dict[str, Any]:
        from ..translation.cloud import CloudTranslator
        from ..core.universal import Entry
        try:
            tr = CloudTranslator(provider, self._key(provider, key), base_url=base_url, model=model)
            out = tr.translate([Entry(id="t", source="Welcome back, traveler! Your journey continues.")], {})
            text = out.get("t")
            if not text:
                return {"ok": False, "error": "Модель ответила, но без перевода — попробуйте другую модель."}
            return {"ok": True, "sample": text}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ---------- диалоги ----------

    def pick_folder(self) -> Optional[str]:
        import webview
        res = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return res[0] if res else None

    def pick_file(self, kind: str) -> Optional[str]:
        import webview
        types = {"font": ("Шрифты (*.ttf;*.otf)",), "gguf": ("Модели GGUF (*.gguf)",)}.get(kind, ("Все файлы (*.*)",))
        res = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=types)
        return res[0] if res else None

    def open_url(self, url: str) -> None:
        webbrowser.open(url)

    def open_folder(self, path: str) -> None:
        p = Path(path)
        if p.exists():
            _open_path(p)

    def open_data_folder(self) -> None:
        _open_path(paths.app_home())

    # ---------- игра ----------

    def detect(self, path: str) -> Dict[str, Any]:
        from ..core.backup import GameBackup
        from ..core.pipeline import project_dir
        from ..core.universal import TranslationProject
        p = Path(path or "")
        if not p.is_dir():
            return {"ok": False, "error": "Папка не найдена."}
        try:
            plugin, det = russificator.detect_engine(p)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        progress = None
        try:
            proj = TranslationProject.load(project_dir(p))
            st = proj.stats()
            progress = {"total": st["total"], "done": st["translated"] + st["approved"]}
        except Exception:  # noqa: BLE001
            pass
        self._cfg["game_dir"] = str(p)
        settings.save(self._cfg)
        return {"ok": True, "engine": det.engine_name or plugin.title, "confidence": det.confidence,
                "details": det.details, "notes": det.notes, "name": p.name,
                "russified": GameBackup(p).exists, "progress": progress,
                "engine_id": plugin.engine_id, "live": bool(plugin.supports_live)}

    # ---------- загрузки ----------

    def download_machine(self) -> Dict[str, Any]:
        from ..translation import machine

        def work():
            try:
                machine.install(lambda m, f=None: self._emit({"type": "download", "task": "machine",
                                                              "message": m, "fraction": f}), self._cancel)
                self._emit({"type": "download", "task": "machine", "done": True, "final": True,
                            "message": "Модель установлена"})
            except Exception as exc:  # noqa: BLE001
                self._emit({"type": "download", "task": "machine", "error": _err(exc), "final": True})
        return self._run_task("download", work)

    def download_local(self, preset_id: str) -> Dict[str, Any]:
        from ..translation import local_llm

        def work():
            task = f"local:{preset_id}"
            report = (lambda m, f=None: self._emit({"type": "download", "task": task, "message": m, "fraction": f}))
            try:
                local_llm.install_runtime(report, self._cancel)
                local_llm.install_model(local_llm.PRESET_BY_ID[preset_id], report, self._cancel)
                self._emit({"type": "download", "task": task, "done": True, "final": True,
                            "message": "Модель установлена"})
            except Exception as exc:  # noqa: BLE001
                self._emit({"type": "download", "task": task, "error": _err(exc), "final": True})
        return self._run_task("download", work)

    def cancel_task(self) -> None:
        self._cancel.set()

    def models_overview(self) -> Dict[str, Any]:
        from ..translation import local_llm, machine
        items = [{"kind": "machine", "id": "machine", "title": "Машинный переводчик (Argos en→ru)",
                  "installed": machine.is_installed(), "bytes": _dir_size(machine.model_dir())}]
        for p in local_llm.PRESETS:
            items.append({"kind": "local", "id": p.id, "title": p.title, "installed": local_llm.model_installed(p),
                          "bytes": _dir_size(local_llm.model_path(p)) if local_llm.model_installed(p) else 0,
                          "download_gb": p.size_gb})
        items.append({"kind": "runtime", "id": "llama.cpp", "title": "llama.cpp (движок нейросети)",
                      "installed": local_llm.server_exe() is not None, "bytes": _dir_size(local_llm.runtime_dir())})
        cache = paths.cache_dir() / "translations.db"
        return {"items": items, "memory_bytes": _dir_size(cache), "data_dir": str(paths.app_home())}

    def delete_model(self, kind: str, model_id: str) -> Dict[str, Any]:
        import shutil
        from ..translation import local_llm, machine
        if self._task is not None and self._task.is_alive():
            return {"ok": False, "error": "Сначала дождитесь окончания текущей операции."}
        if kind == "machine":
            machine.remove()
        elif kind == "local" and model_id in local_llm.PRESET_BY_ID:
            local_llm.remove_model(local_llm.PRESET_BY_ID[model_id])
        elif kind == "runtime":
            shutil.rmtree(local_llm.runtime_dir(), ignore_errors=True)
        return {"ok": True}

    def clear_memory(self) -> Dict[str, Any]:
        db = paths.cache_dir() / "translations.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        return {"ok": True}

    # ---------- русификация ----------

    def start(self, options: Dict[str, Any]) -> Dict[str, Any]:
        from ..core.pipeline import Pipeline, PipelineOptions
        from ..translation import create_translator

        game = (options or {}).get("game_dir") or self._cfg.get("game_dir", "")
        if not game or not Path(game).is_dir():
            return {"ok": False, "error": "Выберите папку с игрой."}
        mode = options.get("mode") or self._cfg.get("mode", "machine")
        opts = dict(self._cfg)
        opts.update(options or {})
        if mode == "cloud":
            opts["api_key"] = self._key(opts.get("cloud_provider", ""), options.get("api_key", ""))
        font = opts.get("font_path") or ""

        def work():
            self._emit({"type": "run_started", "game": game, "mode": mode})
            try:
                translator = create_translator(mode, opts, Path(game).name)
                pipeline = Pipeline(translator, PipelineOptions(font_path=Path(font) if font else None),
                                    on_event=self._emit, cancel=self._cancel)
                result = pipeline.run(Path(game))
            except Exception as exc:  # noqa: BLE001
                log.exception("Русификация упала")
                self._emit({"type": "run_finished", "final": True, "success": False, "errors": [_err(exc)],
                            "warnings": [], "instructions": [], "stats": {}})
                return
            self._emit({
                "type": "run_finished", "final": True, "success": result.success, "cancelled": result.cancelled,
                "engine": result.engine_title, "errors": result.errors, "warnings": result.warnings,
                "instructions": result.instructions, "injected": result.injected, "game": game,
                "live": result.live,
                "stats": {"total": result.total_lines, "translated": result.translated_lines,
                          "failed": result.failed_lines, "skipped": result.skipped_lines,
                          "memory": result.from_memory},
            })
        return self._run_task("run", work)

    # ---------- живой перевод (Unity) ----------

    def live_state(self) -> Dict[str, Any]:
        srv = self._live
        return {"running": bool(srv and srv.running), "count": srv.count if srv else 0}

    def start_live(self) -> Dict[str, Any]:
        """Запустить сервер живого перевода выбранным способом (для XUnity в игре)."""
        from ..engines.unity.xunity import LIVE_PORT
        from ..translation import create_translator
        from ..translation.live_server import LiveServer
        from ..translation.memory import TranslationMemory

        if self._live is not None and self._live.running:
            return {"ok": True}
        mode = self._cfg.get("mode", "machine")
        opts = dict(self._cfg)
        if mode == "cloud":
            opts["api_key"] = self._key(opts.get("cloud_provider", ""))
        game = self._cfg.get("game_dir", "")

        def work():
            status = (lambda m, f=None: self._emit({"type": "live_status", "message": m, "fraction": f}))
            try:
                translator = create_translator(mode, opts, Path(game).name if game else "")
                translator.prepare(status, self._live_cancel)
                memory = TranslationMemory(paths.cache_dir() / "translations.db")
                srv = LiveServer(translator, memory, on_event=self._emit)
                try:
                    srv.start(LIVE_PORT)
                except OSError:
                    srv.stop()
                    raise RuntimeError(f"Порт {LIVE_PORT} занят — живой перевод уже запущен "
                                       "в другой копии программы.")
                self._live = srv
                self._emit({"type": "live_status", "running": True, "final": True,
                            "message": "Живой перевод включён — можно играть"})
            except Exception as exc:  # noqa: BLE001
                log.exception("Живой перевод не запустился")
                self._emit({"type": "live_status", "running": False, "final": True, "error": _err(exc)})

        self._live_cancel = threading.Event()
        threading.Thread(target=work, name="live", daemon=True).start()
        return {"ok": True}

    def stop_live(self) -> Dict[str, Any]:
        self._live_cancel.set()
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._emit({"type": "live_status", "running": False, "final": True, "message": "Живой перевод выключен"})
        return {"ok": True}

    def launch_game(self, path: str = "") -> Dict[str, Any]:
        game = Path(path or self._cfg.get("game_dir", ""))
        exe = self._game_exe(game) if game.is_dir() else None
        if exe is None:
            return {"ok": False, "error": "Не найден .exe игры — запустите её вручную."}
        try:
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
        except OSError as exc:
            return {"ok": False, "error": f"Не удалось запустить игру: {exc}"}
        return {"ok": True}

    def desktop_shortcut(self, path: str = "") -> Dict[str, Any]:
        """Ярлык на рабочем столе: игра сразу с живым переводом (режим --play)."""
        from ..core import launcher
        game = Path(path or self._cfg.get("game_dir", ""))
        if not game.is_dir():
            return {"ok": False, "error": "Сначала выберите папку с игрой."}
        exe = self._game_exe(game)
        title = exe.stem if exe else game.name
        lnk = launcher.desktop_shortcut(game, exe, title)
        if lnk is None:
            return {"ok": False, "error": "Не удалось создать ярлык на рабочем столе."}
        return {"ok": True, "name": lnk.stem}

    @staticmethod
    def _game_exe(game: Path) -> Optional[Path]:
        from .. import play
        return play._game_exe(game)

    def restore(self, path: str) -> Dict[str, Any]:
        from ..core.restore import restore_backups
        if self._task is not None and self._task.is_alive():
            return {"ok": False, "error": "Дождитесь окончания текущей операции."}
        try:
            count, notes = restore_backups(Path(path))
            return {"ok": bool(count), "notes": notes}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "notes": [f"Откат не удался: {exc}"]}


def _err(exc: Exception) -> str:
    if exc.__class__.__name__ == "Cancelled":
        return "Отменено."
    return str(exc) or exc.__class__.__name__


def _setup_logging() -> None:
    log_file = paths.logs_dir() / "russificator.log"
    handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _selftest() -> int:
    """Проверка сборки без окна: все компоненты импортируются, ресурсы на месте.

    Результат пишется в logs/selftest.txt (у оконного exe нет консоли).
    """
    lines = [f"version {russificator.__version__}"]
    ok = True
    for mod in ("webview", "ctranslate2", "sentencepiece", "UnityPy", "numpy", "PIL",
                "UnityPy.helpers.TypeTreeGenerator", "russificator.engines.unity.plugin",
                "russificator.engines.renpy.plugin", "russificator.engines.rpgmaker.plugin"):
        try:
            __import__(mod)
            lines.append(f"ok   {mod}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"FAIL {mod}: {exc}")
    from ..fonts.library import ensure_font
    from ..translation.glossary import ui_phrases
    resources = Path(__file__).resolve().parents[1] / "resources"
    for name, good in (("web/index.html", (WEB / "index.html").is_file()), ("font", ensure_font() is not None),
                       ("glossary", len(ui_phrases()) > 100),
                       ("unity plugin", (resources / "unity" / "Russificator.Unity.dll").is_file()),
                       ("rpgmaker plugin", (resources / "rpgmaker" / "Russificator.js").is_file())):
        ok &= good
        lines.append(("ok   " if good else "FAIL ") + name)
    lines.append("RESULT " + ("OK" if ok else "FAIL"))
    (paths.logs_dir() / "selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    if sys.stdout is not None:
        print("\n".join(lines))
    return 0 if ok else 1


def run(argv=None) -> int:
    """Точка входа: окно программы.

    ``--selftest`` — проверка сборки без окна; ``--play <папка игры>`` — запустить
    игру с живым переводом без окна (так работает ярлык «Играть на русском»);
    ``--serve <папка игры> --pid <процесс>`` — живой перевод для уже запущенной
    игры (его запускает плагин русификатора внутри игры).
    """
    argv = list(argv or [])
    if "--selftest" in argv:
        return _selftest()
    for flag in ("--play", "--serve"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                from .. import play
                pid = 0
                if flag == "--serve":
                    j = argv.index("--pid") if "--pid" in argv else -1
                    pid = int(argv[j + 1]) if 0 <= j < len(argv) - 1 and argv[j + 1].isdigit() else 0
                    if not pid:
                        return 2
                return play.run(argv[i + 1], pid)
    _setup_logging()
    import webview
    api = Api()
    window = webview.create_window(
        f"Русификатор игр {russificator.__version__}", url=str(WEB / "index.html"), js_api=api,
        width=1180, height=800, min_size=(960, 660), background_color="#0d0f14")
    api._bind(window)

    def on_closing():
        api.cancel_task()  # остановить перевод/загрузку; локальный сервер нейросети умрёт вместе с программой
        api.stop_live()

    window.events.closing += on_closing
    webview.start(debug=bool(os.environ.get("RUSSIFICATOR_DEBUG")), http_server=True,
                  storage_path=str(paths.sub("webview")), private_mode=False)
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
