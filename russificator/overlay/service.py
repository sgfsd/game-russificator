"""Служба живого перевода: главный цикл оверлея, перевод в фоне, трей, состояние.

Цикл (≈2 раза в секунду, только пока на экране игра):
  окно переднего плана → игра ли (detect) → захват области → распознавание →
  иностранные строки → блоки → устойчивые блоки на перевод → плашки поверх игры.

Перевод идёт в отдельном потоке пачками; готовое берётся из памяти переводов
(общей с программой) и из кэша сессии, поэтому повторяющийся текст (меню,
подсказки) появляется мгновенно.

Окно программы узнаёт состояние и управляет службой по HTTP на 127.0.0.1
(порт :data:`~russificator.overlay.STATUS_PORT`): /status, /toggle, /reload, /quit…
Порт заодно не даёт запустить вторую копию.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .. import paths, settings
from . import STATUS_PORT
from . import detect, render, text, win32
from .ocr import Ocr, OcrUnavailable
from .text import Block, Line, Tracker, TranslationCache

log = logging.getLogger("russificator.overlay")

VK = {"T": 0x54, "Y": 0x59, "R": 0x52, "G": 0x47}
HOTKEYS = {
    "toggle": win32.Hotkey(1, win32.MOD_ALT, VK["T"], "toggle"),
    "now": win32.Hotkey(2, win32.MOD_ALT, VK["Y"], "now"),
    "region": win32.Hotkey(3, win32.MOD_ALT, VK["R"], "region"),
    "mark": win32.Hotkey(4, win32.MOD_ALT, VK["G"], "mark"),
}

DEFAULTS = {
    "live_mode": "auto",          # auto — как на вкладке «Русификация» (запасной — машинный), или machine|cloud|local
    "live_font_scale": 1.0,
    "live_opacity": 0.86,
    "live_interval": 0.6,         # пауза между кадрами, с
    "live_always": [],
    "live_never": [],
    "live_profiles": {},          # exe -> {"region": [x, y, w, h] доли клиентской области}
}


def _cfg(cfg: Dict[str, Any], key: str) -> Any:
    v = cfg.get(key)
    return DEFAULTS[key] if v is None else v


def translator_options(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Способ перевода для живого перевода: выбранный (с ключом/моделью) или машинный как запасной."""
    from .. import play
    opts = dict(cfg)
    mode = _cfg(cfg, "live_mode")
    if mode in ("machine", "cloud", "local"):
        opts["mode"] = mode
    return play._options(opts)


class LiveService:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.last_game: Optional[win32.WindowInfo] = None   # последняя игра (для кнопок в окне программы)
        self._region_target: Optional[win32.WindowInfo] = None
        self.cfg = settings.load()
        settings.apply_data_dir(self.cfg)
        self.stop_event = threading.Event()
        self.paused = False
        self.force_now = False
        self.target: Optional[win32.WindowInfo] = None
        self.target_reason = ""
        self.region: Tuple[int, int, int, int] = (0, 0, 0, 0)
        self.tracker = Tracker()
        self.cache = TranslationCache()
        self.recent: Deque[Dict[str, str]] = deque(maxlen=40)
        self.count = 0
        self.ocr: Optional[Ocr] = None
        self.ocr_error: Optional[OcrUnavailable] = None
        self._ocr_failures = 0
        self.translator = None
        self.translator_mode = ""
        self.translator_error = ""
        self.memory = None
        self._queue: Deque[Block] = deque()
        self._pending: set = set()
        self._cond = threading.Condition()
        self._shown_sig = ""
        self._frame_sig = ""
        self._decider = detect.Decider()
        self._decider_at = 0.0
        self._announced: set = set()
        self._http: Optional[ThreadingHTTPServer] = None
        self.gui: Optional[win32.Gui] = None
        self._reload_translator = True
        self._last_error_log = 0.0

    # ================================================================ запуск

    def run(self) -> int:
        try:
            self._start_http()
        except OSError:
            log.info("живой перевод уже запущен — вторая копия не нужна")
            return 0
        icon = Path(__file__).resolve().parents[1] / "resources" / "icon.ico"
        self.gui = win32.Gui(str(icon), self._on_hotkey, self._on_menu, self._on_region, self._menu_items)
        self.gui.start()
        self.gui.set_hotkeys([HOTKEYS["mark"]])
        if not self.quiet:
            self.gui.balloon("Живой перевод включён", "Запустите игру — перевод появится поверх неё. "
                                                       "Alt+T — показать/скрыть, Alt+R — выбрать область.")
        threading.Thread(target=self._translate_loop, name="live-translate", daemon=True).start()
        threading.Thread(target=self._init_ocr, name="live-ocr-init", daemon=True).start()
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    self._tick()
                except Exception:  # noqa: BLE001
                    if time.monotonic() - self._last_error_log > 30:
                        log.exception("кадр живого перевода")
                        self._last_error_log = time.monotonic()
                interval = float(_cfg(self.cfg, "live_interval"))
                self.stop_event.wait(max(0.15, interval - (time.monotonic() - started)))
        finally:
            self._shutdown()
        return 0

    def _shutdown(self) -> None:
        with self._cond:
            self._cond.notify_all()
        if self.gui:
            self.gui.stop()
        if self.ocr:
            self.ocr.close()
        if self._http:
            self._http.shutdown()
        try:
            if self.translator is not None:
                self.translator.close()
        except Exception:  # noqa: BLE001
            pass
        if self.memory is not None:
            self.memory.close()

    def _init_ocr(self) -> None:
        from . import ocr as ocr_mod
        try:
            self.ocr = ocr_mod.create("en", paths.sub("tmp"))
            self.ocr_error = None
        except OcrUnavailable as exc:
            self.ocr_error = exc
            if exc.code == "no_language":
                self.gui.balloon("Нужно распознавание английского",
                                 "Откройте «Русификатор игр» → «Живой перевод» и нажмите «Установить».")

    # ================================================================ главный цикл

    def _refresh_decider(self) -> None:
        if time.monotonic() - self._decider_at < 60 and self._decider.game_dirs:
            return
        dirs: List[str] = []
        try:
            data = json.loads((paths.cache_dir() / "library.json").read_text(encoding="utf-8"))
            dirs = [g["path"] for g in data.get("games", []) if g.get("path")]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self._decider = detect.Decider(dirs, _cfg(self.cfg, "live_always"), _cfg(self.cfg, "live_never"),
                                       detect.gamebar_exes())
        self._decider_at = time.monotonic()

    def _tick(self) -> None:
        self._refresh_decider()
        fg = win32.foreground()
        if fg is None or fg.pid == os.getpid():
            return                              # своё меню трея/выбор области — оставляем как есть
        if fg.minimized or fg.client[2] < 120 or fg.client[3] < 90:
            self._drop_target()
            return
        is_game, reason = self._decider.decide(fg.exe, fg.fullscreen)
        if not is_game:
            self._drop_target()
            return
        if self.target is None or self.target.hwnd != fg.hwnd:
            self._set_target(fg, reason)
        self.target = self.last_game = fg
        if self.paused or self.ocr is None:
            self._hide()
            return
        x, y, w, h = self._region(fg)
        if w < 40 or h < 20:
            return
        frame = win32.capture(x, y, w, h)
        if frame is None:
            return
        sig = self._sample(frame, w, h)
        if sig == self._frame_sig and not self.force_now and self.region == (x, y, w, h):
            self._show(x, y, w, h)
            return
        self._frame_sig = sig
        self.region = (x, y, w, h)
        lines = self._recognize(w, h, frame)
        if lines is None:
            return
        lines = [ln for ln in lines if text.is_foreign(ln.text)]
        blocks = text.group_lines(lines)
        self.tracker.need = 1 if self.force_now else 2
        self.force_now = False
        self.tracker.update(blocks)
        for b in self.tracker.ready():
            hit = self.cache.get(b.key)
            if hit:
                b.translation = hit
            else:
                self._enqueue(b)
        self._show(x, y, w, h)

    def _recognize(self, w: int, h: int, frame: bytes) -> Optional[List[Line]]:
        try:
            lines = self.ocr.recognize(w, h, frame)
            self._ocr_failures = 0
            return lines
        except Exception as exc:  # noqa: BLE001
            self._ocr_failures += 1
            log.warning("распознавание кадра (%s): %s", getattr(self.ocr, "name", "?"), exc)
            if self._ocr_failures >= 3 and getattr(self.ocr, "name", "") == "pythonnet":
                # pythonnet сломался уже в работе — переходим на PowerShell
                from .ocr import PowerShellOcr
                try:
                    self.ocr.close()
                    self.ocr = PowerShellOcr("en", paths.sub("tmp"))
                    self._ocr_failures = 0
                    log.info("распознавание переключено на PowerShell")
                except Exception as exc2:  # noqa: BLE001
                    self.ocr_error = OcrUnavailable("failed", str(exc2))
                    self.ocr = None
            return None

    @staticmethod
    def _sample(frame: bytes, w: int, h: int) -> str:
        """Быстрый отпечаток кадра: каждая 16-я строка, каждый 4-й пиксель."""
        stride = w * 4
        rows = [frame[r * stride:(r + 1) * stride:16] for r in range(0, h, 16)]
        return hashlib.blake2b(b"".join(rows), digest_size=12).hexdigest()

    def _region(self, fg: win32.WindowInfo) -> Tuple[int, int, int, int]:
        x, y, w, h = fg.client
        prof = (_cfg(self.cfg, "live_profiles") or {}).get(detect._norm(fg.exe)) or {}
        r = prof.get("region")
        if isinstance(r, list) and len(r) == 4:
            rx, ry, rw, rh = (float(v) for v in r)
            return (int(x + rx * w), int(y + ry * h), max(1, int(rw * w)), max(1, int(rh * h)))
        return x, y, w, h

    def _set_target(self, fg: win32.WindowInfo, reason: str) -> None:
        self.target_reason = reason
        self.tracker = Tracker()
        self._shown_sig = self._frame_sig = ""
        # в играх с XUnity (русифицированы файлами) Alt+T уже занят — там он переключает перевод игры
        game_dir = self._decider.game_dir(fg.exe) or str(Path(fg.exe).parent)
        xunity = (Path(game_dir) / "BepInEx" / "plugins" / "XUnity.AutoTranslator").is_dir()
        keys = [HOTKEYS["now"], HOTKEYS["region"], HOTKEYS["mark"]] + ([] if xunity else [HOTKEYS["toggle"]])
        self.gui.set_hotkeys(keys)
        name = fg.title or Path(fg.exe).stem
        self.gui.set_tip(f"Живой перевод: {name}")
        if fg.exe not in self._announced:
            self._announced.add(fg.exe)
            log.info("игра: %s (%s) — %s", name, fg.exe, reason)

    def _drop_target(self) -> None:
        if self.target is not None:
            self.target = None
            self.gui.set_hotkeys([HOTKEYS["mark"]])
            self.gui.set_tip("Русификатор — живой перевод (ждёт игру)")
        self._hide()

    def _hide(self) -> None:
        if self._shown_sig:
            self._shown_sig = ""
            self.gui.hide()

    def _show(self, x: int, y: int, w: int, h: int) -> None:
        visible = [b for b in self.tracker.blocks if b.translation]
        if not visible:
            self._hide()
            return
        sig = f"{x},{y},{w},{h}|" + "|".join(f"{b.rect}{b.translation}" for b in visible)
        if sig == self._shown_sig:
            return
        style = render.Style(font_scale=float(_cfg(self.cfg, "live_font_scale")),
                             opacity=float(_cfg(self.cfg, "live_opacity")))
        items = [render.Item(rect=b.rect, text=b.translation or "", line_h=b.line_height) for b in visible]
        img = render.render((w, h), items, style)
        self.gui.show_frame(x, y, w, h, render.to_bgra_premultiplied(img))
        self._shown_sig = sig

    # ================================================================ перевод

    def _enqueue(self, b: Block) -> None:
        with self._cond:
            if b.key in self._pending:
                return
            self._pending.add(b.key)
            self._queue.append(b)
            self._cond.notify()

    def _make_translator(self) -> None:
        from ..translation import create_translator
        from ..translation.memory import TranslationMemory
        opts = translator_options(self.cfg)
        if opts is None:
            raise RuntimeError("ни один переводчик не установлен — скачайте машинный переводчик в программе")
        tr = create_translator(opts["mode"], opts, "")
        tr.prepare(lambda m, f=None: None, self.stop_event)
        old = self.translator
        self.translator, self.translator_mode = tr, opts["mode"]
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        if self.memory is None:
            try:
                self.memory = TranslationMemory(paths.cache_dir() / "translations.db")
            except Exception as exc:  # noqa: BLE001
                log.warning("память переводов недоступна: %s", exc)
        log.info("живой перевод: переводчик %s", self.translator_mode)

    def _translate_loop(self) -> None:
        from ..core.universal import Entry, TextKind
        while not self.stop_event.is_set():
            if self._reload_translator:
                try:
                    self._make_translator()
                    self.translator_error = ""
                    self._reload_translator = False
                except Exception as exc:  # noqa: BLE001
                    self.translator_error = str(exc)
                    log.warning("переводчик для живого перевода: %s", exc)
                    self.stop_event.wait(15)
                    continue
            with self._cond:
                while not self._queue and not self.stop_event.is_set() and not self._reload_translator:
                    self._cond.wait(1.0)
                batch: List[Block] = []
                while self._queue and len(batch) < 8:
                    batch.append(self._queue.popleft())
            if not batch:
                continue
            todo: List[Block] = []
            ns = self.translator.cache_id
            if self.memory is not None:
                try:
                    hits = self.memory.get_many(ns, [(b.text, None) for b in batch])
                except Exception:  # noqa: BLE001
                    hits = {}
                for b in batch:
                    tr = hits.get((b.text, None))
                    if tr:
                        self._done(b, tr, from_memory=True)
                    else:
                        todo.append(b)
            else:
                todo = batch
            if not todo:
                continue
            entries = [Entry(id=str(i), source=b.text, kind=TextKind.OTHER) for i, b in enumerate(todo)]
            try:
                out = self.translator.translate(entries, {})
            except Exception as exc:  # noqa: BLE001
                log.warning("живой перевод не удался: %s", exc)
                self.translator_error = str(exc)
                with self._cond:
                    for b in todo:
                        self._pending.discard(b.key)
                self.stop_event.wait(3)
                continue
            self.translator_error = ""
            fresh: Dict[Tuple[str, Optional[str]], str] = {}
            for i, b in enumerate(todo):
                tr = (out.get(str(i)) or "").strip()
                if tr:
                    self._done(b, tr)
                    fresh[(b.text, None)] = tr
                else:
                    with self._cond:
                        self._pending.discard(b.key)
            if fresh and self.memory is not None:
                try:
                    self.memory.put_many(ns, fresh)
                except Exception:  # noqa: BLE001
                    pass

    def _done(self, b: Block, tr: str, from_memory: bool = False) -> None:
        b.translation = tr
        self.cache.put(b.key, tr)
        with self._cond:
            self._pending.discard(b.key)
        # тот же текст мог появиться новым блоком, пока шёл перевод
        for other in self.tracker.blocks:
            if not other.translation and text.similar(other.key, b.key, 0.93):
                other.translation = tr
        self.count += 1
        self.recent.appendleft({"src": b.text[:160], "tr": tr[:160]})
        self._shown_sig = ""                    # перерисовать на следующем кадре

    # ================================================================ клавиши, трей, область

    def _on_hotkey(self, name: str) -> None:
        if name == "toggle":
            self.toggle()
        elif name == "now":
            self.force_now = True
            self._frame_sig = ""
        elif name == "region":
            self.select_region()
        elif name == "mark":
            self.mark_current("always")

    def toggle(self) -> None:
        self.paused = not self.paused
        if self.paused:
            self._hide()
        self._shown_sig = ""

    def select_region(self) -> None:
        fg = self.target or win32.foreground()
        if fg is None or fg.pid == os.getpid():
            return
        self._hide()
        self._region_target = fg
        x, y, w, h = fg.client
        self.gui.select_region(x, y, w, h)

    def _on_region(self, rect: Optional[Tuple[int, int, int, int]]) -> None:
        fg = self._region_target
        if fg is None:
            return
        profiles = dict(_cfg(self.cfg, "live_profiles") or {})
        key = detect._norm(fg.exe)
        prof = dict(profiles.get(key) or {})
        if rect is None:
            prof.pop("region", None)
        else:
            _, _, w, h = fg.client
            rx, ry, rw, rh = rect
            prof["region"] = [round(rx / w, 4), round(ry / h, 4), round(rw / w, 4), round(rh / h, 4)]
        profiles[key] = prof
        self._save({"live_profiles": profiles})
        self._frame_sig = self._shown_sig = ""
        self.tracker = Tracker()

    def clear_region(self) -> None:
        game = self.target or self.last_game
        if game is None:
            return
        profiles = dict(_cfg(self.cfg, "live_profiles") or {})
        profiles.pop(detect._norm(game.exe), None)
        self._save({"live_profiles": profiles})
        self._frame_sig = self._shown_sig = ""

    def mark_current(self, which: str, from_ui: bool = False) -> Optional[str]:
        """Добавить окно в «Всегда»/«Никогда». С горячей клавиши — окно переднего плана (игра),
        из окна программы — последняя игра (на переднем плане тогда сама программа)."""
        fg = (self.target or self.last_game) if from_ui else win32.foreground()
        if fg is None or fg.pid == os.getpid() or not fg.exe:
            return None
        key = "live_always" if which == "always" else "live_never"
        other = "live_never" if which == "always" else "live_always"
        lst = [p for p in _cfg(self.cfg, key) if detect._norm(p) != detect._norm(fg.exe)] + [fg.exe]
        rest = [p for p in _cfg(self.cfg, other) if detect._norm(p) != detect._norm(fg.exe)]
        self._save({key: lst, other: rest})
        self._decider_at = 0
        if which == "always":
            self.gui.balloon("Живой перевод", f"Окно «{fg.title or Path(fg.exe).stem}» будет переводиться.")
        return fg.exe

    def _save(self, partial: Dict[str, Any]) -> None:
        cur = settings.load()
        cur.update(partial)
        settings.save(cur)
        self.cfg.update(partial)

    def _menu_items(self) -> List[Tuple[str, str, bool]]:
        return [("toggle", "Показывать перевод  (Alt+T)", not self.paused),
                ("now", "Перевести сейчас  (Alt+Y)", False),
                ("region", "Выбрать область текста  (Alt+R)", False),
                ("mark", "Переводить это окно  (Alt+G)", False),
                ("-", "", False),
                ("open", "Открыть «Русификатор игр»", False),
                ("quit", "Выключить живой перевод", False)]

    def _on_menu(self, cmd: str) -> None:
        if cmd == "toggle":
            self.toggle()
        elif cmd == "now":
            self.force_now = True
        elif cmd == "region":
            self.select_region()
        elif cmd == "mark":
            self.mark_current("always")
        elif cmd == "open":
            open_main_window()
        elif cmd == "quit":
            self._save({"live_enabled": False})
            self.stop_event.set()

    # ================================================================ состояние для окна программы

    def status(self) -> Dict[str, Any]:
        t = self.target
        last = self.last_game
        prof = (_cfg(self.cfg, "live_profiles") or {}).get(detect._norm(t.exe)) if t else None
        err = self.ocr_error
        return {
            "running": True, "paused": self.paused, "pid": os.getpid(),
            "game": {"title": t.title or Path(t.exe).stem, "exe": t.exe, "reason": self.target_reason,
                     "region": bool(prof and prof.get("region"))} if t else None,
            "ocr": {"ok": self.ocr is not None, "name": getattr(self.ocr, "name", ""),
                    "lang": getattr(self.ocr, "lang", ""), "code": err.code if err else "",
                    "error": str(err) if err else "", "starting": self.ocr is None and err is None},
            "translator": {"mode": self.translator_mode, "ready": self.translator is not None,
                           "error": self.translator_error},
            "last_game": {"title": last.title or Path(last.exe).stem, "exe": last.exe} if last else None,
            "count": self.count, "recent": list(self.recent)[:12],
            "capture_excluded": bool(self.gui and self.gui.excluded_from_capture),
        }

    def _start_http(self) -> None:
        svc = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self._route()

            def do_POST(self):  # noqa: N802
                self._route()

            def _route(self):
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                path = url.path.rstrip("/")
                body: Any = {"ok": True}
                if path == "/status":
                    body = svc.status()
                elif path == "/toggle":
                    svc.toggle()
                elif path == "/pause":
                    svc.paused = q.get("on", "1") == "1"
                    svc._shown_sig = ""
                elif path == "/now":
                    svc.force_now = True
                elif path == "/region":
                    svc.select_region()
                elif path == "/region_clear":
                    svc.clear_region()
                elif path == "/mark":
                    body = {"ok": True, "exe": svc.mark_current(q.get("list", "always"), from_ui=True)}
                elif path == "/reload":
                    old = (svc.cfg.get("live_mode"), svc.cfg.get("mode"), svc.cfg.get("cloud_model"),
                           svc.cfg.get("local_model"))
                    svc.cfg = settings.load()
                    new = (svc.cfg.get("live_mode"), svc.cfg.get("mode"), svc.cfg.get("cloud_model"),
                           svc.cfg.get("local_model"))
                    if old != new:
                        svc._reload_translator = True
                        with svc._cond:
                            svc._cond.notify_all()
                    svc._decider_at = 0
                    svc._shown_sig = ""
                elif path == "/quit":
                    svc.stop_event.set()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args) -> None:
                pass

        self._http = ThreadingHTTPServer(("127.0.0.1", STATUS_PORT), Handler)
        self._http.daemon_threads = True
        threading.Thread(target=self._http.serve_forever, name="live-status", daemon=True).start()


def open_main_window() -> None:
    """Показать окно программы (если уже открыто) или запустить её."""
    if win32.IS_WINDOWS:
        import ctypes
        from ctypes import wintypes
        found = []
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            buf = ctypes.create_unicode_buffer(256)
            win32.GetWindowTextW(hwnd, buf, 256)
            if buf.value.startswith("Русификатор игр") and win32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
            return True
        ctypes.windll.user32.EnumWindows(proto(cb), 0)
        if found:
            ctypes.windll.user32.ShowWindow(found[0], 9)   # SW_RESTORE
            win32.SetForegroundWindow(found[0])
            return
    from ..core import launcher
    exe, args, workdir = launcher._program()
    try:
        subprocess.Popen([exe, *args], cwd=workdir)
    except OSError as exc:
        log.warning("программа не запущена: %s", exc)


def run(argv: Optional[List[str]] = None) -> int:
    """Точка входа процесса живого перевода (``Russificator.exe --overlay``)."""
    win32.set_dpi_aware()
    handler = logging.FileHandler(paths.logs_dir() / "live.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    if not win32.IS_WINDOWS:
        log.error("живой перевод работает только в Windows")
        return 2
    try:
        return LiveService(quiet="--quiet" in (argv or [])).run()
    except Exception:  # noqa: BLE001
        log.exception("живой перевод упал")
        return 1
