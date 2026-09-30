"""Служба живого перевода: главный цикл оверлея, распознавание и перевод в фоне, трей, состояние.

Три потока, каждый делает своё и не ждёт остальных:

* главный цикл (10–20 кадров в секунду, пока на экране игра): кадр игры → что на нём
  изменилось и какие надписи пропали (:mod:`~russificator.overlay.pipeline`, доли
  миллисекунды) → показ перевода. Пропавшая надпись убирается на следующем же кадре;
* распознавание: только полоса кадра, где сменился текст, и только когда картинка там
  успокоилась; неподвижный экран распознаётся один раз;
* перевод: по предложениям, одним выбранным переводчиком — перевод показывается один раз и
  больше не подменяется. Готовое берётся из памяти переводов (общей с программой) и из
  кэша сессии, поэтому повторяющийся текст (меню, подсказки) появляется сразу.

Кадр снимается с окна игры (``PrintWindow``), а если окно так не снимается (полноэкранный
режим) — с экрана: окно перевода исключено из захвата, поэтому на снимке игра под переводом.

Глубокий проход — когда экран замер (меню, надпись, диалог ждёт клика) — ищет надписи на
картинках и текстурах, особые шрифты и мелкий текст (:mod:`~russificator.overlay.vision`).

Окно программы узнаёт состояние и управляет службой по HTTP на 127.0.0.1
(порт :data:`~russificator.overlay.STATUS_PORT`): /status, /toggle, /reload, /quit…
Порт заодно не даёт запустить вторую копию.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import numpy as np

from .. import paths, settings
from . import STATUS_PORT
from . import detect, inline, pixels, render, text, vision, win32
from .ocr import OcrUnavailable
from .pipeline import SKIP, Job, LiveBlock, Pipeline, make_block
from .text import Line, Rect, TranslationCache

log = logging.getLogger("russificator.overlay")

MAIN_TITLE = "Русификатор игр"          # начало заголовка окна программы (ui/app.py)

VK = {"T": 0x54, "Y": 0x59, "R": 0x52}
# Клавиши регистрируются только пока на экране игра: в остальных программах Alt+… не перехватываются.
HOTKEYS = {
    "toggle": win32.Hotkey(1, win32.MOD_ALT, VK["T"], "toggle"),
    "now": win32.Hotkey(2, win32.MOD_ALT, VK["Y"], "now"),
    "region": win32.Hotkey(3, win32.MOD_ALT, VK["R"], "region"),
}
# Сколько кадров подряд чёрный экран у полноэкранной игры, чтобы подсказать про режим «окно без рамки».
BLACK_FRAMES = 40
#: пауза между кадрами, пока что-то происходит (перевод на экране, картинка меняется), и в покое
FRAME_ACTIVE = 1 / 15
FRAME_IDLE = 0.15
#: как часто кадр окна игры сверяется со снимком экрана (не застыл ли, не заливка ли), с
CROSSCHECK = 3.0
#: глубокий проход — когда экран неподвижен столько секунд
DEEP_IDLE = 0.6
DEEP_BUDGET = 1.2                 # с на один глубокий проход распознавания Windows (варианты кадра)
#: детектору текста кадр уменьшается так, чтобы мелкие строки этой игры были не ниже стольких пикселей
DET_TEXT_PX = 18
DET_MIN_PX = 240_000
DET_SCALES = (0.5, 0.75, 1.0)

DEFAULTS = {
    "live_mode": "auto",          # auto — как на вкладке «Русификация» (запасной — машинный), или machine|cloud|local
    "live_font_scale": 1.0,
    "live_opacity": 0.86,
    "live_always": [],
    "live_never": [],
    "live_profiles": {},          # exe -> {"region": [x, y, w, h] доли клиентской области}
    "live_deep": True,            # глубокий проход: надписи на картинках, особые шрифты, мелкий текст
    "live_style": "inline",       # inline — русский текст на месте английского в его стиле; plate — плашки
    "live_ocr": "auto",           # auto — нейросетевое распознавание (PaddleOCR), если есть; windows
    "live_gpu": True,             # распознавание на видеокарте (DirectML), если там быстрее
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


class _Avg:
    """Скользящее среднее времени (мс) — для состояния в окне программы."""

    def __init__(self) -> None:
        self.value = 0.0
        self.n = 0

    def add(self, seconds: float) -> None:
        ms = seconds * 1000
        self.n += 1
        self.value = ms if self.n == 1 else self.value * 0.8 + ms * 0.2

    def __int__(self) -> int:
        return int(round(self.value))


class LiveService:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.last_game: Optional[win32.WindowInfo] = None   # последняя игра (для кнопок в окне программы)
        self.candidate: Optional[win32.WindowInfo] = None   # последнее окно, не похожее на игру («Переводить его»)
        self.hint = ""                                        # подсказка игроку (чёрный кадр и т.п.)
        self._black = 0
        self._picture = False
        self._hinted: set = set()
        self._region_target: Optional[win32.WindowInfo] = None
        self.cfg = settings.load()
        settings.apply_data_dir(self.cfg)
        self.stop_event = threading.Event()
        self._wake = threading.Event()                       # в главный цикл пришёл результат
        self.paused = False
        self.force_now = False
        self.target: Optional[win32.WindowInfo] = None
        self.target_reason = ""
        self.region: Tuple[int, int, int, int] = (0, 0, 0, 0)
        self.cache = TranslationCache()                      # переводы предложений в этой сессии
        self.book = inline.StyleBook()
        self._gen = 0                                        # номер состояния (смена игры/области)
        self.pipe = self._new_pipe()
        self.recent: Deque[Dict[str, str]] = deque(maxlen=40)
        self.count = 0
        self.ocr = None
        self.ocr_ru = None                                   # русское распознавание: отличить русский текст
        self.words = None                                    # английский словарь (исправление ошибок распознавания)
        self.ocr_error: Optional[OcrUnavailable] = None
        self._ocr_failures = 0
        self._capture_mode = "window"                        # window — кадр с окна игры; screen — область экрана
        self._window_misses = 0
        self.captured_by = ""
        self._plates: List[Rect] = []                        # где наши плашки (в координатах кадра)
        self._excluded_at = 0.0                              # когда окно перевода исключили из захвата
        self._crosscheck_at = 0.0                            # когда кадр окна сверялся с экраном
        self._window_wrong = False
        self._jobs: "queue.Queue" = queue.Queue()
        self._results: Deque[tuple] = deque()
        self._refined: Dict[str, str] = {}                   # перечитанное крупнее (распознавание Windows)
        self._heights: Deque[int] = deque(maxlen=200)        # высоты прочитанных строк этой игры
        self.deep_found = 0
        self.translator = None
        self.translator_mode = ""
        self.translator_error = ""
        self.memory = None
        self._queue: Deque[Tuple[str, str]] = deque()        # предложения на перевод: (ключ, текст)
        self._pending: Set[str] = set()
        self._wanted: Set[str] = set()
        self._cond = threading.Condition()
        self._shown_sig: Any = None
        self._shown_ids: Set[int] = set()
        self._on = False                                     # перевод сейчас на экране
        self._redraw = False
        self._decider = detect.Decider()
        self._decider_at = 0.0
        self._announced: set = set()
        self._http: Optional[ThreadingHTTPServer] = None
        self.gui: Optional[win32.Gui] = None
        self._reload_translator = True
        self._last_error_log = 0.0
        self.speed = {"ocr": _Avg(), "translate": _Avg(), "frame": _Avg(), "shown": _Avg()}

    def _new_pipe(self) -> Pipeline:
        self._gen += 1
        return Pipeline(self._tr_get)

    def _tr_get(self, key: str) -> Optional[str]:
        return self.cache.get(key)

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
        if not self.quiet:
            self.gui.balloon("Живой перевод включён", "Запустите игру — перевод появится поверх неё. "
                                                       "Alt+T — показать/скрыть, Alt+R — выбрать область.")
        threading.Thread(target=self._translate_loop, name="live-translate", daemon=True).start()
        threading.Thread(target=self._ocr_loop, name="live-ocr", daemon=True).start()
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
                spent = time.monotonic() - started
                if self.target is not None:
                    self.speed["frame"].add(spent)
                interval = FRAME_ACTIVE if self._active() else FRAME_IDLE
                self._wake.wait(max(0.005, interval - spent))
                self._wake.clear()
        finally:
            self._shutdown()
        return 0

    def _active(self) -> bool:
        """Что-то происходит: на экране перевод или ждём распознавания/перевода — кадры чаще."""
        p = self.pipe
        return self.target is not None and (self._on or p.busy or bool(p.blocks) or
                                            (p.dirty is not None and bool(p.dirty.any())))

    def _shutdown(self) -> None:
        with self._cond:
            self._cond.notify_all()
        self._jobs.put(None)
        if self.gui:
            self.gui.stop()
        if self.ocr:
            self.ocr.close()
        if self.ocr_ru:
            self.ocr_ru.close()
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
        from . import words
        try:
            self.words = words.load()
        except Exception as exc:  # noqa: BLE001
            log.warning("словарь английских слов: %s", exc)
        if _cfg(self.cfg, "live_ocr") in ("auto", "paddle"):
            try:
                from . import paddle
                if paddle.available():
                    self.ocr = paddle.PaddleOcr(gpu=bool(_cfg(self.cfg, "live_gpu")))
                    self.ocr.doubt = self._doubtful
                    self.ocr_error = None
                    return                      # русский текст оно распознаёт само — проверка не нужна
            except Exception as exc:  # noqa: BLE001
                log.warning("нейросетевое распознавание не запустилось (%s) — распознавание Windows", exc)
        try:
            self.ocr = ocr_mod.create("en", paths.sub("tmp"))
            self.ocr_error = None
        except OcrUnavailable as exc:
            self.ocr_error = exc
            if exc.code == "no_language":
                self.gui.balloon("Нужно распознавание английского",
                                 "Откройте «Русификатор игр» → «Живой перевод» и нажмите «Установить».")
            return
        try:
            self.ocr_ru = ocr_mod.create_verifier(paths.sub("tmp"), prefer=getattr(self.ocr, "name", ""))
        except Exception as exc:  # noqa: BLE001
            log.warning("русское распознавание: %s", exc)

    def _doubtful(self, s: str) -> bool:
        """Английское прочтение сомнительно — цифры вперемешку с буквами или слова не из словаря:
        так английская модель читает русский текст («Об игре» → «06 urpe»)."""
        tokens = [t for t in re.findall(r"[A-Za-z0-9']+", s) if re.search(r"[A-Za-z]", t)]
        if any(re.search(r"[0-9]", t) for t in tokens):
            return True
        if self.words is None:
            return text.misread_cyrillic(s)
        return bool(text.unknown_words(s, self.words.ranks))

    @property
    def _paddle(self) -> bool:
        return getattr(self.ocr, "name", "") == "paddle"

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
            if reason == detect.REASON_WINDOW:
                self.candidate = fg             # обычное окно — можно предложить «Переводить это окно»
            self._drop_target()
            return
        if self.candidate is not None and self.candidate.hwnd == fg.hwnd:
            self.candidate = None
        if self.target is None or self.target.hwnd != fg.hwnd:
            self._set_target(fg, reason)
        self.target = self.last_game = fg
        if self.paused or self.ocr is None:
            self._hide()
            return
        x, y, w, h = self._region(fg)
        if w < 40 or h < 20:
            return
        img = self._capture(fg, x, y, w, h)
        if img is None:
            return
        if (x, y, w, h) != self.region:
            self.region = (x, y, w, h)
            self.pipe = self._new_pipe()        # другая область (окно двигали, меняли размер)
            self._hide()
        if (x, y, w, h) == tuple(fg.client):
            self._check_black(fg, img)          # в выбранной области чёрный фон — это нормально
        self._drain()
        # на снимке экрана без исключения из захвата поверх текста наши же плашки — отпечаткам не верим
        self.pipe.watch = not (self.captured_by == "screen" and not self._excluded())
        job = self.pipe.frame(img, force=self.force_now)
        if job is not None:
            self.force_now = False
            self._jobs.put((self._gen, job, fg))
        elif self._deep_due(w, h):
            self.pipe.busy = True
            self.pipe.deep_done = True
            self._jobs.put((self._gen, Job(img.copy(), (0, 0, w, h), time.monotonic(), full=True, deep=True), fg))
        self._dispatch()
        self._show(x, y, w, h, img)

    def _excluded(self) -> bool:
        """Окно перевода исключено из захвата (и Windows успела это применить)."""
        return bool(self.gui is not None and getattr(self.gui, "excluded_from_capture", False)
                    and time.monotonic() - self._excluded_at > 0.2)

    def _exclude(self, on: bool) -> None:
        """Исключать окно перевода из захвата, только пока игра снимается с экрана: при съёмке
        самого окна игры исключать незачем, а так перевод виден на скриншотах и в записи экрана."""
        gui = self.gui
        if gui is None or not getattr(gui, "can_exclude", False) or gui.excluded_from_capture == on:
            return
        gui.set_excluded(on)
        self._excluded_at = time.monotonic()

    def _drain(self) -> None:
        """Результаты распознавания — в состояние (только из главного цикла)."""
        while self._results:
            gen, job, found = self._results.popleft()
            if gen != self._gen:
                continue                        # от прошлой игры/области
            if job.deep:
                self.pipe.busy = False
                added = self.pipe.add_deep(found)
                self.deep_found += len(added)
                if added:
                    log.info("глубокий проход: %s", " | ".join(b.text[:40] for b in added))
            else:
                self.pipe.ocr_done(job, found)

    # ---------------------------------------------------------------- кадр

    def _capture(self, fg: win32.WindowInfo, x: int, y: int, w: int, h: int):
        """Кадр игры массивом (высота, ширина, BGRA): с самого окна (наших плашек и чужих окон в нём
        нет), а если окно так не снимается — область экрана. Окно перевода тогда исключено из
        захвата, а если Windows этого не умеет — место плашек закрашивается.

        Полноэкранная игра («окно без рамки», эксклюзивный режим с оптимизацией Windows) часто отдаёт
        с окна не свою картинку, а заливку цветом фона или застывший кадр — поэтому кадр окна
        время от времени сверяется со снимком экрана (:meth:`_window_lies`)."""
        cx, cy, cw, ch = fg.client
        win_img = None
        if self._capture_mode == "window":
            win_img = win32.grab_window(fg.hwnd, cw, ch, (x - cx, y - cy, w, h))
            if win_img is not None and win_img.shape[:2] != (h, w):
                win_img = None
            if win_img is not None and not _uniform(win_img) and not self._window_lies(win_img, x, y, w, h):
                self._window_misses = 0
                self.captured_by = "window"
                self._exclude(False)
                return win_img
        self._exclude(True)
        screen = win32.grab_screen(x, y, w, h)
        if screen is None or screen.shape[:2] != (h, w):
            return win_img
        if not self._excluded():
            screen = self._mask_plates(screen)
        if self._capture_mode == "window":
            if win_img is None or not _uniform(screen):
                # окно снимается пустым, заливкой или не тем, а на экране картинка есть
                self._window_misses += 1
                if win_img is None or self._window_misses >= 3 or self._window_wrong:
                    self._capture_mode = "screen"
                    log.info("кадр снимается с экрана: окно игры не отдаёт свою картинку")
            else:
                self.captured_by = "window"
                self._exclude(False)
                return win_img                  # однотонный экран в самой игре (загрузка, затемнение)
        self.captured_by = "screen"
        return screen

    def _window_lies(self, win_img, x: int, y: int, w: int, h: int) -> bool:
        """Кадр окна не совпадает с тем, что на экране (застывший или чужой кадр): раз в
        CROSSCHECK с сверяется со снимком экрана того же места — там, где нет наших плашек."""
        now = time.monotonic()
        if now - self._crosscheck_at < CROSSCHECK:
            return self._window_wrong
        self._crosscheck_at = now
        screen = win32.grab_screen(x, y, w, h)
        if screen is None or screen.shape[:2] != win_img.shape[:2] or _uniform(screen):
            self._window_wrong = False
            return False
        a, b = pixels.cells(win_img, 16), pixels.cells(screen, 16)
        free = np.ones(a.shape, bool)
        for bx, by, bw, bh in self._plates:        # наши плашки на экране — не в счёт
            free[max(0, by // 16):(by + bh) // 16 + 1, max(0, bx // 16):(bx + bw) // 16 + 1] = False
        if free.sum() < 0.3 * free.size:
            return self._window_wrong
        differs = float((np.abs(a - b)[free] > 30).mean())
        self._window_wrong = differs >= 0.3
        if self._window_wrong:
            log.info("кадр окна игры не совпадает с экраном (%.0f%% кадра) — снимаем экран", differs * 100)
        return self._window_wrong

    def _mask_plates(self, img):
        """Снимок экрана со старой Windows (окно перевода не исключить из захвата): закрасить наши
        плашки, иначе распознавание читало бы собственный перевод."""
        if not self._plates:
            return img
        img = img.copy()
        h, w = img.shape[:2]
        for bx, by, bw, bh in self._plates:
            x0, y0, x1, y1 = max(0, bx), max(0, by), min(w, bx + bw), min(h, by + bh)
            if x1 > x0 and y1 > y0:
                img[y0:y1, x0:x1] = 0
        return img

    def _check_black(self, fg: win32.WindowInfo, img) -> None:
        """Эксклюзивный полноэкранный режим: вместо игры Windows отдаёт чёрный кадр — подсказываем, что делать.

        Подсказка — только если у этой игры ещё ни разу не было видно картинки (не чёрный экран загрузки)."""
        if self._picture:
            return
        if not _blank(img):
            self._picture = True
            self._black = 0
            self.hint = ""
            return
        self._black += 1
        if self._black < BLACK_FRAMES or not fg.fullscreen:
            return
        self.hint = ("Игра не даёт снять изображение (эксклюзивный полноэкранный режим). Переключите в её "
                     "настройках экран на «Окно без рамки» (Borderless) или «В окне».")
        if fg.exe not in self._hinted:
            self._hinted.add(fg.exe)
            log.info("чёрный кадр у %s — вероятно, эксклюзивный полноэкранный режим", fg.exe)
            self.gui.balloon("Живой перевод не видит игру", "Переключите в игре экран на «Окно без рамки» "
                                                          "(Borderless) — тогда перевод появится.")

    def _region(self, fg: win32.WindowInfo) -> Tuple[int, int, int, int]:
        x, y, w, h = fg.client
        prof = (_cfg(self.cfg, "live_profiles") or {}).get(detect._norm(fg.exe)) or {}
        r = prof.get("region")
        if isinstance(r, list) and len(r) == 4:
            rx, ry, rw, rh = (float(v) for v in r)
            return (int(x + rx * w), int(y + ry * h), max(1, int(rw * w)), max(1, int(rh * h)))
        return x, y, w, h

    # ---------------------------------------------------------------- распознавание (свой поток)

    def _ocr_loop(self) -> None:
        while not self.stop_event.is_set():
            item = self._jobs.get()
            if item is None:
                return
            gen, job, fg = item
            started = time.monotonic()
            found: List[LiveBlock] = []
            try:
                if self.ocr is not None and gen == self._gen:
                    found = self._read(job, fg)
            except Exception as exc:  # noqa: BLE001
                log.warning("распознавание (%s): %s", getattr(self.ocr, "name", "?"), exc)
                self._ocr_failed()
            if not job.deep:
                self.speed["ocr"].add(time.monotonic() - started)
            log.debug("распознавание%s %s: %d мс, надписей %d", " (глубокое)" if job.deep else "", job.rect,
                      (time.monotonic() - started) * 1000, len(found))
            self._results.append((gen, job, found))
            self._wake.set()

    def _ocr_failed(self) -> None:
        self._ocr_failures += 1
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

    def _read(self, job: Job, fg: win32.WindowInfo) -> List[LiveBlock]:
        """Распознать полосу кадра задания и собрать из строк надписи для перевода."""
        img = job.img
        h, w = img.shape[:2]
        if job.deep:
            lines = self._deep_lines(img)
        else:
            lines = self._recognize(img, job.rect)
            self._ocr_failures = 0
        blocks = self._analyze(lines, img, fg, job.at)
        if job.deep:
            # крупная надпись (логотип), прочитанная лишь частично, выглядит хуже непереведённой
            ranks = self.words.ranks if self.words is not None else None
            blocks = [b for b in blocks if b.line_height < 0.07 * h or text.known_words(b.text, ranks) >= 3]
        return blocks

    def _det_budget(self, rect: Rect) -> Optional[int]:
        """Сколько пикселей дать детектору текста. Кадр уменьшается так, чтобы самые мелкие строки,
        уже встречавшиеся в этой игре, остались не ниже DET_TEXT_PX: в игре с крупным текстом
        детектор работает в разы быстрее, а мелкий текст (подсказки) не теряется — его находит
        глубокий проход в полном разрешении, и тогда уменьшение становится меньше."""
        try:
            heights = sorted(self._heights)
        except RuntimeError:                    # как раз сменилась игра (главный цикл очистил список)
            return None
        if len(heights) < 3:
            return None
        small = heights[len(heights) // 10]
        area = rect[2] * rect[3]
        want = DET_TEXT_PX / max(1, small)
        # ступенями: один и тот же размер входа детектора (видеокарта готовится под каждый новый
        # размер заново) и те же рамки строк (прочитанное узнаётся из кэша)
        scale = min((k for k in DET_SCALES if k >= want), default=1.0)
        return int(min(area, max(DET_MIN_PX, area * scale * scale)))

    def _recognize(self, img, rect: Rect, budget: Optional[int] = None) -> List[Line]:
        if self._paddle:
            lines = self.ocr.recognize_array(img, rect, budget=budget if budget is not None else self._det_budget(rect))
            self._heights.extend(ln.h for ln in lines if ln.text.strip() and ln.rot is None)
            return lines
        x, y, w, h = rect
        crop = img[y:y + h, x:x + w]
        limit = int(getattr(self.ocr, "max_dim", 0) or 0)
        f = 1.0
        if limit and max(w, h) > limit:
            from PIL import Image
            f = limit / float(max(w, h))
            small = Image.frombuffer("RGBA", (w, h), crop.tobytes(), "raw", "BGRA", 0, 1).resize(
                (max(1, int(w * f)), max(1, int(h * f))), Image.BILINEAR)
            lines = self.ocr.recognize(small.width, small.height, small.tobytes("raw", "BGRA"))
        else:
            lines = self.ocr.recognize(w, h, crop.tobytes())
        return [Line(ln.text, int(ln.x / f) + x, int(ln.y / f) + y, max(1, int(ln.w / f)), max(1, int(ln.h / f)))
                for ln in lines]

    def _deep_lines(self, img) -> List[Line]:
        """Надписи на картинках, особые шрифты, мелкий текст. Нейросетевое распознавание — кадр в
        полном разрешении; распознавание Windows — несколько вариантов кадра, берётся то, что
        подтвердили хотя бы два (:func:`text.merge_deep`)."""
        h, w = img.shape[:2]
        if self._paddle:
            lines = self.ocr.recognize_array(img, budget=min(2560 * 1440, w * h))
            self._heights.extend(ln.h for ln in lines if ln.text.strip() and ln.rot is None)
            return lines
        started = time.monotonic()
        base = self._recognize(img, (0, 0, w, h))
        results: List[List[Line]] = []
        frame = img.tobytes()
        for name, scale, data, vw, vh in vision.variants(frame, w, h, int(getattr(self.ocr, "max_dim", 4096))):
            if time.monotonic() - started > DEEP_BUDGET:
                break
            try:
                lines = self.ocr.recognize(vw, vh, data)
            except Exception as exc:  # noqa: BLE001
                log.debug("глубокий проход (%s): %s", name, exc)
                continue
            if scale != 1:
                lines = [Line(ln.text, int(ln.x / scale), int(ln.y / scale), max(1, int(ln.w / scale)),
                              max(1, int(ln.h / scale))) for ln in lines]
            results.append(lines)
        return text.merge_deep(base, [base] + results)

    def _deep_due(self, w: int, h: int) -> bool:
        if self.ocr is None or not _cfg(self.cfg, "live_deep") or self.pipe.busy or self.pipe.deep_done:
            return False
        if self._paddle and max(w, h) <= 1100:
            return False                        # кадр и так распознаётся в полном разрешении
        return self.pipe.idle_for() >= DEEP_IDLE

    # ---------------------------------------------------------------- что переводить

    def _analyze(self, lines: List[Line], img, fg: win32.WindowInfo, read_at: float) -> List[LiveBlock]:
        """Строки → надписи для перевода.

        Строки сначала собираются в абзацы (строки разного цвета и разделённые чертой рамки —
        разные надписи: имя героя над репликой, заголовок, кнопки), и только потом решается,
        переводить ли абзац, — целиком: слово, которое распознавание прочитало неуверенно, или
        обрывок строки не остаются непереведёнными посреди русского текста."""
        h = img.shape[0]
        rgb = img[..., :3]
        top = self.region[1]
        lines = text.merge_rows(lines, apart=lambda a, b: pixels.rule_beside(rgb, a, b))
        cands: List[Line] = []
        other: List[Line] = []
        for ln in lines:
            if not ln.text.strip() or pixels.cut_side(ln, rgb) is not None:
                continue                        # обрывок надписи на краю кадра
            if ln.ru or self._is_caption(ln, fg, top) or text.is_brand(ln.text) or self._title_fragment(ln, fg, h):
                other.append(ln)                # русский текст, заголовок окна, название
            else:
                cands.append(ln)
        colors: Dict[int, Any] = {}

        def color(ln: Line):
            if id(ln) not in colors:
                colors[id(ln)] = pixels.line_colors(img, (ln.x, ln.y, ln.w, ln.h))
            return colors[id(ln)]

        def apart(a: Line, b: Line) -> bool:
            if pixels.rule_between(rgb, a, b):
                return True                     # между строками черта рамки
            ca, cb = color(a), color(b)
            return bool(ca and cb and pixels.color_distance(ca[0], cb[0]) > 90)

        ranks = self.words.ranks if self.words is not None else None
        out: List[LiveBlock] = []
        skip: List[List[Line]] = [g.lines for g in text.group_lines(other, apart=apart)] if other else []
        groups = [g for g in text.group_lines(cands, apart=apart)]
        ok = [self._translatable(img, g.lines, ranks) for g in groups]
        for i, g in enumerate(groups):
            if ok[i] and ranks is not None and text.is_name(g.text, ranks) and _above_reply(g, groups, ok):
                ok[i] = False                   # имя героя табличкой над репликой — не переводим
        for g, good in zip(groups, ok):
            if not good:
                skip.append(g.lines)
                continue
            body = text.strip_marker(text.join_lines(ln.text for ln in g.lines if getattr(ln, "conf", 1.0) >= 0.4))
            if not self._paddle:
                body = self._refine(img, g.lines, body, ranks)
            b = make_block(g.lines, img, read_at, source=self._source(body))
            if b is not None:
                out.append(b)
        # непереводимое (имена, русский текст, названия, мусор) тоже запоминается: другое прочтение
        # того же места (глитч, анимация) узнаётся как то же место, а не как новая надпись
        for lines_ in skip:
            b = make_block(lines_, img, read_at)
            if b is not None:
                b.skip = b.confirmed = True
                out.append(b)
        return out

    def _translatable(self, img, lines: List[Line], ranks) -> bool:
        """Абзац надо переводить: в нём есть уверенно прочитанный иностранный текст, и это не
        русский текст, не мусор распознавания, не логотип и не имя."""
        if not any(getattr(ln, "conf", 1.0) >= 0.6 and text.is_foreign(ln.text) for ln in lines):
            return False
        body = text.strip_marker(text.join_lines(ln.text for ln in lines if getattr(ln, "conf", 1.0) >= 0.4))
        if not text.is_foreign(body) or self._russian(img, lines, body, ranks):
            return False
        junk = text.junk_tokens(body)
        if junk and (ranks is None or text.known_words(body, ranks) < 2 * len(junk)):
            return False                        # мусор распознавания (узор, логотип)
        return not (ranks is not None and (text.gibberish(body, ranks) or text.caps_junk(body, ranks)))

    def _russian(self, img, lines: List[Line], body: str, ranks) -> bool:
        """Русский текст, прочитанный английским распознаванием как абракадабра («HaqaTb»). Нейросеть
        читает кириллицу сама (``Line.ru``); распознаванию Windows помогает русское распознавание."""
        if self._paddle:
            return False
        if self.ocr_ru is not None:
            h, w = img.shape[:2]
            x0 = max(0, min(ln.x for ln in lines) - 12)
            y0 = max(0, min(ln.y for ln in lines) - 12)
            x1 = min(w, max(ln.x + ln.w for ln in lines) + 12)
            y1 = min(h, max(ln.y + ln.h for ln in lines) + 12)
            if x1 - x0 >= 8 and y1 - y0 >= 8:
                try:
                    ru = self.ocr_ru.recognize(x1 - x0, y1 - y0, img[y0:y1, x0:x1].tobytes())
                    joined = " ".join(m.text for m in ru)
                    if joined.strip():
                        return text.looks_russian(joined)
                except Exception as exc:  # noqa: BLE001
                    log.warning("русское распознавание: %s", exc)
        return text.misread_cyrillic(body, ranks)

    def _refine(self, img, lines: List[Line], body: str, ranks) -> str:
        """Распознавание Windows: мелкий и особый шрифт перечитывается увеличенным (x2–x3) — читается
        заметно точнее («unutd be an qeratbn» → «would be an operation»). Берётся, если лучше."""
        key = text.normalize(body)
        if key in self._refined:
            return self._refined[key]
        self._refined[key] = body
        if len(self._refined) > 2000:
            self._refined.clear()
        lh = sum(ln.h for ln in lines) / max(1, len(lines))
        if lh >= 34:
            return body                         # крупный текст и так читается хорошо
        scale = 3 if lh < 15 else 2
        h, w = img.shape[:2]
        m = int(max(6, lh * 0.6))
        x0, y0 = max(0, min(ln.x for ln in lines) - m), max(0, min(ln.y for ln in lines) - m)
        x1, y1 = min(w, max(ln.x + ln.w for ln in lines) + m), min(h, max(ln.y + ln.h for ln in lines) + m)
        cw, ch = x1 - x0, y1 - y0
        if cw < 8 or ch < 8 or max(cw, ch) * scale > int(getattr(self.ocr, "max_dim", 4096)):
            return body
        try:
            from PIL import Image
            crop = img[y0:y1, x0:x1].tobytes()
            big = Image.frombuffer("RGBA", (cw, ch), crop, "raw", "BGRA", 0, 1).resize((cw * scale, ch * scale),
                                                                                       Image.LANCZOS)
            got = self.ocr.recognize(cw * scale, ch * scale, big.tobytes("raw", "BGRA"))
        except Exception as exc:  # noqa: BLE001
            log.debug("перечитывание: %s", exc)
            return body
        got = sorted((ln for ln in got if ln.text.strip()), key=lambda ln: (ln.y, ln.x))
        refined = text.strip_marker(text.join_lines(ln.text for ln in got))
        if not got or text.cyrillic_share(refined) > 0.3 or not text.is_foreign(refined):
            return body
        if text.letters(refined) > 1.4 * text.letters(body) or not text.similar(text.normalize(refined), key, 0.55):
            return body                         # в поля попала соседняя надпись — это уже другой текст
        old_known, new_known = text.known_words(body, ranks), text.known_words(refined, ranks)
        if new_known > old_known or (new_known == old_known and text.letters(refined) > text.letters(body) * 1.1):
            self._refined[key] = refined
            return refined
        return body

    def _is_caption(self, ln: Line, fg: win32.WindowInfo, y: int) -> bool:
        """Заголовок окна внутри клиентской области (так рисуют окна NW.js, Electron) — не переводим."""
        top = y - fg.client[1] + ln.y
        if top > 48 or not fg.title:
            return False
        a, b = text.normalize(ln.text), text.normalize(fg.title)
        return bool(a) and (text.similar(a, b, 0.7) or (len(a) >= 4 and a in b))

    def _title_fragment(self, ln: Line, fg: win32.WindowInfo, h: int) -> bool:
        """Надпись только из слов названия игры (заголовка окна или имени exe) — это логотип.
        Имя («BLOODMONEY!») и обрывок названия не переводятся (вышла бы транслитерация или
        бессмыслица); полное описательное название из обычных слов («A Game About Literally
        Doing Your Taxes») переводится, как любой текст. Одно слово названия считается логотипом,
        только если оно крупное (иначе это может быть пункт меню)."""
        name = f"{fg.title} {Path(fg.exe).stem if fg.exe else ''}"
        title = {t for t in text.normalize(name).split() if len(t) >= 2}
        words = [t for t in text.normalize(ln.text).split() if len(t) >= 2]
        if not words or (len(words) < 2 and ln.h < 0.045 * h):
            return False
        # логотип нарисован особым шрифтом — распознавание может ошибиться в букве («SLOODMONEY»)
        if not all(t in title or (len(t) >= 5 and any(text.similar(t, tt, 0.75) for tt in title)) for t in words):
            return False
        own_title = {t for t in text.normalize(fg.title).split() if len(t) >= 2} or title
        ranks = self.words.ranks if self.words is not None else None
        whole = len(set(words) & own_title) >= 0.8 * len(own_title)
        return not (whole and text.known_words(ln.text, ranks) >= 3)

    def _source(self, body: str) -> str:
        """Текст надписи для переводчика: без мусора и с исправленными ошибками распознавания
        («sraphic» → «graphic»)."""
        src = text.without_junk(body) or body
        if self.words is None:
            return src
        try:
            return self.words.fix(src)
        except Exception:  # noqa: BLE001
            return src

    # ---------------------------------------------------------------- показ

    def _set_target(self, fg: win32.WindowInfo, reason: str) -> None:
        self.target_reason = reason
        self._hide()                            # перевод прошлого окна не должен остаться на экране
        self.pipe = self._new_pipe()
        self.book = inline.StyleBook()
        self.region = (0, 0, 0, 0)
        self._heights.clear()
        self._capture_mode = "window"
        self._window_misses = 0
        self._crosscheck_at = 0.0
        self._window_wrong = False
        self._plates = []
        # в играх, русифицированных файлами, Alt+T уже занят самой игрой (переключает перевод/оригинал) —
        # глобальная клавиша оверлея его бы перехватила
        own = self._russified(fg.exe)
        keys = [HOTKEYS["now"], HOTKEYS["region"]] + ([] if own else [HOTKEYS["toggle"]])
        self._black = 0
        self._picture = False
        self.hint = ""
        self.gui.set_hotkeys(keys)
        name = fg.title or Path(fg.exe).stem
        self.gui.set_tip(f"Живой перевод: {name}")
        if fg.exe not in self._announced:
            self._announced.add(fg.exe)
            log.info("игра: %s (%s) — %s", name, fg.exe, reason)

    def _russified(self, exe: str) -> bool:
        """Игра русифицирована программой (или в ней XUnity): папка игры — выше exe на 0–3 уровня."""
        from ..core.backup import BACKUP_DIR
        dirs = [self._decider.game_dir(exe)] + [str(p) for p in list(Path(exe).parents)[:4]]
        for d in dirs:
            if not d:
                continue
            root = Path(d)
            if (root / BACKUP_DIR).is_dir() or (root / "BepInEx" / "plugins" / "XUnity.AutoTranslator").is_dir():
                return True
        return False

    def _drop_target(self) -> None:
        if self.target is not None:
            self.target = None
            self.gui.set_hotkeys([])
            self.gui.set_tip("Русификатор — живой перевод (ждёт игру)")
        self._hide()

    def _hide(self) -> None:
        """Убрать перевод с экрана. Прячем по признаку «показано», а не по подписи кадра: подпись
        сбрасывается и ради перерисовки — иначе перевод оставался висеть поверх других окон."""
        self._plates = []
        if self._on or self._shown_sig is not None:
            self._shown_sig = None
            self._on = False
            self.gui.hide()

    def _show(self, x: int, y: int, w: int, h: int, img) -> None:
        visible = self.pipe.visible()
        if not visible:
            self._hide()
            return
        sig = (x, y, w, h, tuple((id(b), tr) for b, tr in visible))
        if sig == self._shown_sig and not self._redraw:
            return
        self._redraw = False
        style = render.Style(font_scale=float(_cfg(self.cfg, "live_font_scale")),
                             opacity=float(_cfg(self.cfg, "live_opacity")))
        use_inline = _cfg(self.cfg, "live_style") != "plate"
        patches: List[inline.Patch] = []
        plates: List[render.Item] = []
        for b, tr in visible:
            patch = self._patch(b, tr, w, h) if use_inline else None
            if patch is not None:
                patches.append(patch)
            else:
                plates.append(render.Item(rect=b.rect, text=tr, line_h=b.line_height))
            if id(b) not in self._shown_ids:
                log.debug("показана через %d мс после кадра: %s", (time.monotonic() - b.read_at) * 1000, tr[:50])
                self._shown_ids.add(id(b))
                self.count += 1
                self.recent.appendleft({"src": b.source[:160], "tr": tr[:160]})
                if b.read_at:
                    self.speed["shown"].add(time.monotonic() - b.read_at)
        if len(self._shown_ids) > 5000:
            self._shown_ids = {id(b) for b, _ in visible}
        picture = inline.compose((w, h), patches)
        if plates:
            picture.alpha_composite(render.render((w, h), plates, style))
        self._plates = [(p.x, p.y, p.image.width, p.image.height) for p in patches] + \
            render.plate_rects((w, h), plates, style)
        if getattr(self.gui, "colorkey", False):
            data = render.to_colorkey(picture, under=img if img.shape[:2] == (h, w) else None)
        else:
            data = render.to_bgra_premultiplied(picture)
        self.gui.show_frame(x, y, w, h, data)
        self._shown_sig = sig
        self._on = True

    def _patch(self, b: LiveBlock, tr: str, w: int, h: int) -> Optional[inline.Patch]:
        """Русский текст на месте оригинала: считается один раз на перевод надписи. None — нарисовать
        плашкой (стиль не определить или кадр с нашими же плашками)."""
        if b.patch_text == tr:
            return b.patch
        b.patch_text = tr
        b.patch = None
        if b.frame is None or b.frame.shape[:2] != (h, w) or (self.captured_by == "screen" and not self._excluded()):
            return None
        rot = text.block_rot(b.lines)
        try:
            b.patch = inline.render_item(b.frame, w, h, inline.Item(
                rect=b.rect, lines=[(ln.x, ln.y, ln.w, ln.h) for ln in b.lines], text=tr, source=b.text,
                rot=rot, line_rots=[ln.rot for ln in b.lines] if rot else None), self.book)
        except Exception:  # noqa: BLE001
            log.exception("перевод на месте не нарисован")
        return b.patch

    # ================================================================ перевод

    def _dispatch(self) -> None:
        """Предложения без перевода — переводчику; срочные (надпись уже подтверждена) — вперёд.
        То, что пропало с экрана, переводчик пропустит (``_wanted``)."""
        need = self.pipe.needed()
        wanted = {k for k, _, _ in need}
        with self._cond:
            self._wanted = wanted
            fresh = False
            for key, src, urgent in need:
                if key in self._pending:
                    continue
                self._pending.add(key)
                if urgent:
                    self._queue.appendleft((key, src))
                else:
                    self._queue.append((key, src))
                fresh = True
            if fresh:
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
        self.speed["translate"] = _Avg()
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
                    self.cache = TranslationCache()     # переводы прошлого переводчика не смешиваем
                except Exception as exc:  # noqa: BLE001
                    self.translator_error = str(exc)
                    log.warning("переводчик для живого перевода: %s", exc)
                    self.stop_event.wait(15)
                    continue
            with self._cond:
                while not self._queue and not self.stop_event.is_set() and not self._reload_translator:
                    self._cond.wait(1.0)
                batch: List[Tuple[str, str]] = []
                while self._queue and len(batch) < 8:
                    key, src = self._queue.popleft()
                    if key in self._wanted:
                        batch.append((key, src))
                    else:
                        self._pending.discard(key)      # пропало с экрана — переводить незачем
            if not batch:
                continue
            ns = self.translator.cache_id
            todo: List[Tuple[str, str]] = []
            hits: Dict[Tuple[str, Optional[str]], str] = {}
            if self.memory is not None:
                try:
                    hits = self.memory.get_many(ns, [(src, None) for _, src in batch])
                except Exception:  # noqa: BLE001
                    hits = {}
            for key, src in batch:
                tr = hits.get((src, None))
                if tr:
                    self._done(key, src, tr)
                else:
                    todo.append((key, src))
            if not todo:
                self._wake.set()
                continue
            entries = [Entry(id=str(i), source=src, kind=TextKind.OTHER) for i, (_, src) in enumerate(todo)]
            started = time.monotonic()
            try:
                out = self.translator.translate(entries, {})
            except Exception as exc:  # noqa: BLE001
                log.warning("живой перевод не удался: %s", exc)
                self.translator_error = str(exc)
                with self._cond:
                    for key, _ in todo:
                        self._pending.discard(key)
                self.stop_event.wait(3)
                continue
            self.speed["translate"].add((time.monotonic() - started) / len(todo))
            log.debug("перевод %d предл.: %d мс", len(todo), (time.monotonic() - started) * 1000)
            self.translator_error = ""
            fresh: Dict[Tuple[str, Optional[str]], str] = {}
            for i, (key, src) in enumerate(todo):
                tr = (out.get(str(i)) or "").strip()
                if tr:
                    self._done(key, src, tr)
                    fresh[(src, None)] = tr
                else:
                    with self._cond:
                        self._pending.discard(key)
            if fresh and self.memory is not None:
                try:
                    self.memory.put_many(ns, fresh)
                except Exception:  # noqa: BLE001
                    pass
            self._wake.set()

    def _done(self, key: str, src: str, tr: str) -> None:
        # перевод без русских букв или совпавший с оригиналом (имена, коды) — остаётся оригинал
        useless = not text.cyrillic_share(tr) or text.normalize(tr) == text.normalize(src)
        self.cache.put(key, SKIP if useless else tr)
        with self._cond:
            self._pending.discard(key)

    # ================================================================ клавиши, трей, область

    def _on_hotkey(self, name: str) -> None:
        if name == "toggle":
            self.toggle()
        elif name == "now":
            self.force_now = True
            self._wake.set()
        elif name == "region":
            self.select_region()

    def toggle(self) -> None:
        self.paused = not self.paused
        if self.paused:
            self._hide()
        self._redraw = True

    def _alive(self, w: Optional[win32.WindowInfo]) -> Optional[win32.WindowInfo]:
        """Свежие координаты окна, если оно ещё открыто и не свёрнуто."""
        if w is None:
            return None
        cur = win32.window_info(w.hwnd)
        if cur is None or cur.pid != w.pid or cur.minimized or cur.client[2] < 120 or cur.client[3] < 90:
            return None
        return cur

    def select_region(self) -> None:
        # с горячей клавиши — игра на экране; из меню трея — последняя игра (фокус тогда у трея)
        fg = self.target or self._alive(self.last_game)
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
        self._redraw = True
        self.region = (0, 0, 0, 0)              # следующий кадр начнёт с новой областью

    def clear_region(self) -> None:
        game = self.target or self.last_game
        if game is None:
            return
        profiles = dict(_cfg(self.cfg, "live_profiles") or {})
        profiles.pop(detect._norm(game.exe), None)
        self._save({"live_profiles": profiles})
        self._redraw = True
        self.region = (0, 0, 0, 0)

    def mark_current(self, which: str, what: str = "game") -> Optional[str]:
        """Добавить окно в «Всегда»/«Никогда»: ``game`` — текущая или последняя игра,
        ``candidate`` — последнее обычное окно, которое не распозналось как игра."""
        fg = self.candidate if what == "candidate" else (self.target or self.last_game)
        if fg is None or fg.pid == os.getpid() or not fg.exe:
            return None
        key = "live_always" if which == "always" else "live_never"
        other = "live_never" if which == "always" else "live_always"
        lst = [p for p in _cfg(self.cfg, key) if detect._norm(p) != detect._norm(fg.exe)] + [fg.exe]
        rest = [p for p in _cfg(self.cfg, other) if detect._norm(p) != detect._norm(fg.exe)]
        self._save({key: lst, other: rest})
        self._decider_at = 0
        if which == "always":
            if self.candidate is not None and detect._norm(self.candidate.exe) == detect._norm(fg.exe):
                self.candidate = None
            self.gui.balloon("Живой перевод", f"Окно «{fg.title or Path(fg.exe).stem}» будет переводиться.")
        else:
            if self.target is not None and detect._norm(self.target.exe) == detect._norm(fg.exe):
                self._drop_target()
            if self.last_game is not None and detect._norm(self.last_game.exe) == detect._norm(fg.exe):
                self.last_game = None
        return fg.exe

    def _save(self, partial: Dict[str, Any]) -> None:
        self.cfg.update(partial)
        settings.save(self.cfg)             # подхватит и то, что поменяли в окне программы

    def _menu_items(self) -> List[Tuple[str, str, bool]]:
        items = [("toggle", "Показывать перевод  (Alt+T)", not self.paused),
                 ("now", "Перевести сейчас  (Alt+Y)", False)]
        if self.target or self.last_game:
            items.append(("region", "Выбрать область текста  (Alt+R)", False))
        c = self.candidate
        if c is not None:
            name = (c.title or Path(c.exe).stem).strip()
            name = name if len(name) <= 36 else name[:35] + "…"
            items.append(("mark", f"Переводить окно «{name}»", False))
        return items + [("-", "", False),
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
            self.mark_current("always", "candidate")
        elif cmd == "open":
            open_main_window()
        elif cmd == "quit":
            self._save({"live_enabled": False})
            self.stop_event.set()

    # ================================================================ состояние для окна программы

    def status(self) -> Dict[str, Any]:
        t = self.target
        last = self.last_game
        profiles = _cfg(self.cfg, "live_profiles") or {}

        def has_region(w: win32.WindowInfo) -> bool:
            return bool((profiles.get(detect._norm(w.exe)) or {}).get("region"))
        err = self.ocr_error
        c = self.candidate
        return {
            "running": True, "paused": self.paused, "pid": os.getpid(),
            "game": {"title": t.title or Path(t.exe).stem, "exe": t.exe, "reason": self.target_reason,
                     "region": has_region(t)} if t else None,
            "candidate": {"title": c.title or Path(c.exe).stem, "exe": c.exe} if c else None,
            "hint": self.hint,
            "hint_title": "Живой перевод не видит игру" if self.hint else "",
            "ocr": {"ok": self.ocr is not None, "name": getattr(self.ocr, "name", ""),
                    "lang": getattr(self.ocr, "lang", ""), "code": err.code if err else "",
                    "error": str(err) if err else "", "starting": self.ocr is None and err is None,
                    "device": getattr(self.ocr, "device", "")},
            "translator": {"mode": self.translator_mode, "ready": self.translator is not None,
                           "error": self.translator_error},
            "speed": {"ocr_ms": int(self.speed["ocr"]), "translate_ms": int(self.speed["translate"]),
                      "frame_ms": int(self.speed["frame"]), "shown_ms": int(self.speed["shown"])},
            "last_game": {"title": last.title or Path(last.exe).stem, "exe": last.exe, "reason": self.target_reason,
                          "region": has_region(last)} if last else None,
            "count": self.count, "recent": list(self.recent)[:12],
            "capture_excluded": self._excluded(),
            "capture": self.captured_by,
            "verify_ru": self.ocr_ru is not None or self._paddle,
            "deep": {"on": bool(_cfg(self.cfg, "live_deep")), "found": self.deep_found},
            "busy_hotkeys": list(getattr(self.gui, "busy_hotkeys", []) or []),
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
                    svc._redraw = True
                elif path == "/now":
                    svc.force_now = True
                elif path == "/region":
                    svc.select_region()
                elif path == "/region_clear":
                    svc.clear_region()
                elif path == "/mark":
                    exe = svc.mark_current(q.get("list", "always"), q.get("what", "game"))
                    body = {"ok": bool(exe), "exe": exe, "error": "" if exe else "Нет окна, которое можно отметить."}
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
                    svc._redraw = True
                    for b in svc.pipe.blocks:     # вид перевода мог смениться — перерисовать заплатки
                        b.patch_text = None
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


def _above_reply(g, groups, ok) -> bool:
    """Надпись стоит табличкой над репликой: прямо под ней (до двух с половиной строк) начинается
    переводимый абзац из нескольких слов, и они перекрываются по горизонтали. Так выглядит имя
    говорящего; такое же слово в меню или на кнопке — обычный текст."""
    x, y, w, h = g.rect
    lh = g.line_height
    for other, good in zip(groups, ok):
        if other is g or not good or len(other.text.split()) < 3:
            continue
        ox, oy, ow, oh = other.rect
        if 0 <= oy - (y + h) <= 2.5 * lh and min(x + w, ox + ow) - max(x, ox) > -lh:
            return True
    return False


def _blank(img, threshold: int = 8) -> bool:
    """Кадр чёрный (или почти): эксклюзивный полноэкранный режим, либо в игре тёмный экран загрузки."""
    h, w = img.shape[:2]
    return int(img[::max(1, h // 24), ::16, :3].max(initial=0)) <= threshold


def _uniform(img, spread: int = 12) -> bool:
    """Кадр однотонный: чёрный или залитый одним цветом (так полноэкранные игры отдают кадр с окна)."""
    h, w = img.shape[:2]
    px = img[::max(1, h // 24), ::16, :3]
    if px.size == 0:
        return True
    return int((px.max(axis=(0, 1)).astype(np.int16) - px.min(axis=(0, 1))).max()) <= spread


_last_open = 0.0


def open_main_window() -> None:
    """Показать окно программы (если уже открыто) или запустить её."""
    global _last_open
    hwnd = win32.find_program_window(MAIN_TITLE)
    if hwnd:
        win32.activate(hwnd)
        return
    if time.monotonic() - _last_open < 8:     # только что запускали — окно ещё открывается
        return
    _last_open = time.monotonic()
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
