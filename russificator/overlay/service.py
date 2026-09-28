"""Служба живого перевода: главный цикл оверлея, перевод в фоне, трей, состояние.

Цикл (≈2 раза в секунду, только пока на экране игра):
  окно переднего плана → игра ли (detect) → кадр с окна игры → распознавание →
  иностранные строки (русский текст отсеивается) → блоки → устойчивые блоки на
  перевод → плашки поверх игры.

Быстрый проход распознаёт каждый новый кадр как есть. Глубокий проход — когда
экран замер (меню, надпись, диалог ждёт клика) и раз в несколько секунд — ищет
надписи на картинках и текстурах, особые шрифты и мелкий текст: кадр
распознаётся в нескольких вариантах (контраст, цветовые каналы, увеличение), а
найденное принимается по согласию вариантов (:mod:`~russificator.overlay.vision`).

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
from . import detect, render, text, vision, win32
from .ocr import Ocr, OcrUnavailable
from .text import Block, Line, Rect, Tracker, TranslationCache

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
BLACK_FRAMES = 25
# Глубокий проход: не чаще раза в DEEP_INTERVAL с на меняющемся экране и не больше DEEP_SHARE времени.
DEEP_INTERVAL = 2.5
DEEP_SHARE = 0.2
DEEP_BUDGET = 1.2                 # с на один глубокий проход (дальше варианты не распознаются)
# Доля сетки кадра, сменившая яркость, после которой считаем, что сменилась сцена.
SCENE_CUT = 0.45
# Перевод, совпавший с оригиналом (или без русских букв), не показывается.
SKIP = "\x00"

DEFAULTS = {
    "live_mode": "auto",          # auto — как на вкладке «Русификация» (запасной — машинный), или machine|cloud|local
    "live_font_scale": 1.0,
    "live_opacity": 0.86,
    "live_interval": 0.6,         # пауза между кадрами, с
    "live_always": [],
    "live_never": [],
    "live_profiles": {},          # exe -> {"region": [x, y, w, h] доли клиентской области}
    "live_deep": True,            # глубокий проход: надписи на картинках, особые шрифты, мелкий текст
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
        self.candidate: Optional[win32.WindowInfo] = None   # последнее окно, не похожее на игру («Переводить его»)
        self.hint = ""                                        # подсказка игроку (чёрный кадр и т.п.)
        self._black = 0
        self._picture = False
        self._hinted: set = set()
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
        self.ocr_ru: Optional[Ocr] = None                   # русское распознавание: отличить русский текст
        self.words = None                                    # английский словарь (исправление ошибок распознавания)
        self.ocr_error: Optional[OcrUnavailable] = None
        self._ocr_failures = 0
        self._capture_mode = "window"                        # window — кадр с окна игры; screen — область экрана
        self._window_misses = 0
        self.captured_by = ""
        self._plates: List[Rect] = []                        # где наши плашки (в координатах кадра)
        self._last_lines: List[Line] = []                    # строки быстрого прохода для текущего кадра
        self._grid: List[int] = []
        self._deep_sig = ""
        self._deep_at = 0.0
        self._deep_cost = 0.0
        self.deep_found = 0
        self._frame: Optional[Tuple[bytes, int, int]] = None  # последний кадр (для перечитывания блоков)
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
        frame = self._capture(fg, x, y, w, h)
        if frame is None:
            return
        self._frame = (frame, w, h)
        sig = self._sample(frame, w, h)
        if (x, y, w, h) == tuple(fg.client):
            self._check_black(fg, frame, w, h)     # в выбранной области чёрный фон — это нормально
        same_region = self.region == (x, y, w, h)
        if sig == self._frame_sig and not self.force_now and same_region:
            # картинка та же — значит, и текст тот же: это ещё одно подтверждение, что он допечатан
            # (иначе статичный экран — диалог ждёт клика, меню — так и не дождался бы перевода)
            self._same_frame()
            if self._deep_due(sig, static=True):
                self._deep_scan(fg, frame, x, y, w, h, sig)
            self._show(x, y, w, h)
            return
        grid = vision.luma_grid(frame, w, h)
        scene_cut = same_region and bool(self._grid) and vision.changed_share(self._grid, grid) >= SCENE_CUT
        self._grid = grid
        self._frame_sig = sig
        self.region = (x, y, w, h)
        lines = self._recognize(w, h, frame)
        if lines is None:
            return
        self._last_lines = lines
        lines = self._foreign(lines, fg, frame, x, y, w, h)
        blocks = text.group_lines(lines)
        force = self.force_now
        self.tracker.need = 1 if force else 2
        self.force_now = False
        self.tracker.update(blocks, covered=self._covered(), scene_cut=scene_cut)
        if force or self._deep_due(sig, static=False):
            self._deep_scan(fg, frame, x, y, w, h, sig)
        self._dispatch_ready()
        self._show(x, y, w, h)

    # ---------------------------------------------------------------- кадр

    def _capture(self, fg: win32.WindowInfo, x: int, y: int, w: int, h: int) -> Optional[bytes]:
        """Кадр игры: с самого окна (наших плашек и чужих окон в нём нет), а если окно так не
        снимается — область экрана, где место наших плашек закрашивается."""
        cx, cy, cw, ch = fg.client
        win_frame = None
        if self._capture_mode == "window":
            win_frame = win32.capture_window(fg.hwnd, cw, ch, (x - cx, y - cy, w, h))
            if win_frame is not None and len(win_frame) != w * h * 4:
                win_frame = None
            if win_frame is not None and not vision.blank(win_frame, w, h):
                self._window_misses = 0
                self.captured_by = "window"
                return win_frame
        screen = win32.capture(x, y, w, h)
        if screen is None:
            return win_frame
        screen = self._mask_plates(screen, w, h)
        if self._capture_mode == "window":
            if win_frame is None or not vision.blank(screen, w, h):
                # окно снимается пустым (или не снимается), а на экране картинка есть
                self._window_misses += 1
                if win_frame is None or self._window_misses >= 3:
                    self._capture_mode = "screen"
                    log.info("кадр снимается с экрана: окно игры не отдаёт картинку")
            else:
                self.captured_by = "window"
                return win_frame                    # тёмный экран в самой игре
        self.captured_by = "screen"
        return screen

    def _mask_plates(self, frame: bytes, w: int, h: int) -> bytes:
        """Снимок экрана: закрасить наши плашки (если Windows не прячет их от захвата), иначе
        распознавание читало бы собственный перевод."""
        if not self._plates or (self.gui is not None and getattr(self.gui, "excluded_from_capture", False)):
            return frame
        buf = bytearray(frame)
        stride = w * 4
        for bx, by, bw, bh in self._plates:
            x0, y0, x1, y1 = max(0, bx), max(0, by), min(w, bx + bw), min(h, by + bh)
            if x1 <= x0 or y1 <= y0:
                continue
            blank_row = bytes((x1 - x0) * 4)
            for row in range(y0, y1):
                buf[row * stride + x0 * 4:row * stride + x1 * 4] = blank_row
        return bytes(buf)

    def _covered(self) -> List[Rect]:
        """Области кадра, которые сейчас не видны распознаванию (закрашенные плашки)."""
        if self.captured_by == "screen" and not (self.gui is not None and
                                                 getattr(self.gui, "excluded_from_capture", False)):
            return list(self._plates)
        return []

    # ---------------------------------------------------------------- что переводить

    def _foreign(self, lines: List[Line], fg: win32.WindowInfo, frame: bytes, x: int, y: int, w: int,
                 h: int) -> List[Line]:
        """Строки, которые нужно переводить: иностранные, не заголовок окна и не русский текст,
        прочитанный английским распознаванием как абракадабра."""
        cands = [ln for ln in lines if text.is_foreign(ln.text) and not self._is_caption(ln, fg, y)
                 and not text.is_brand(ln.text) and not self._title_fragment(ln, fg, h)]
        if not cands:
            return []
        ru = self._recognize_ru(frame, w, h, cands)
        ranks = self.words.ranks if self.words is not None else None
        out: List[Line] = []
        for ln in cands:
            if ru is not None:
                r = text.line_rect(ln)
                over = [m for m in ru if text.intersect_area(r, text.line_rect(m)) >=
                        0.3 * min(text.area(r), max(1, text.area(text.line_rect(m))))]
                if over and text.looks_russian(" ".join(m.text for m in over)):
                    continue                        # русское распознавание прочитало тут русский текст
                if not over and text.misread_cyrillic(ln.text, ranks):
                    continue
            elif text.misread_cyrillic(ln.text, ranks):
                continue
            junk = text.junk_tokens(ln.text)
            if junk and (ranks is None or text.known_words(ln.text, ranks) < 2 * len(junk)):
                continue                            # мусор распознавания (узор, логотип) — не переводим
            if ranks is not None and text.gibberish(ln.text, ranks):
                continue
            out.append(ln)
        return out

    def _is_caption(self, ln: Line, fg: win32.WindowInfo, y: int) -> bool:
        """Заголовок окна внутри клиентской области (так рисуют окна NW.js, Electron) — не переводим."""
        top = y - fg.client[1] + ln.y
        if top > 48 or not fg.title:
            return False
        a, b = text.normalize(ln.text), text.normalize(fg.title)
        return bool(a) and (text.similar(a, b, 0.7) or (len(a) >= 4 and a in b))

    @staticmethod
    def _title_fragment(ln: Line, fg: win32.WindowInfo, h: int) -> bool:
        """Надпись только из слов названия игры (заголовка окна или имени exe) — это логотип:
        название не переводится, а обрывок стилизованного логотипа тем более. Одно слово названия
        считается логотипом, только если оно крупное (иначе это может быть пункт меню)."""
        name = f"{fg.title} {Path(fg.exe).stem if fg.exe else ''}"
        title = {t for t in text.normalize(name).split() if len(t) >= 2}
        words = [t for t in text.normalize(ln.text).split() if len(t) >= 2]
        if not words or (len(words) < 2 and ln.h < 0.045 * h):
            return False
        # логотип нарисован особым шрифтом — распознавание может ошибиться в букве («SLOODMONEY»)
        return all(t in title or (len(t) >= 5 and any(text.similar(t, tt, 0.75) for tt in title)) for t in words)

    def _recognize_ru(self, frame: bytes, w: int, h: int, cands: List[Line]) -> Optional[List[Line]]:
        """Русское распознавание области со строками-кандидатами (None — его нет или сбой)."""
        if self.ocr_ru is None:
            return None
        x0 = max(0, min(ln.x for ln in cands) - 12)
        y0 = max(0, min(ln.y for ln in cands) - 12)
        x1 = min(w, max(ln.x + ln.w for ln in cands) + 12)
        y1 = min(h, max(ln.y + ln.h for ln in cands) + 12)
        cw, ch = x1 - x0, y1 - y0
        if cw < 8 or ch < 8:
            return None
        stride = w * 4
        crop = frame if (cw, ch) == (w, h) else b"".join(
            frame[row * stride + x0 * 4:row * stride + x1 * 4] for row in range(y0, y1))
        try:
            lines = self.ocr_ru.recognize(cw, ch, crop)
        except Exception as exc:  # noqa: BLE001
            log.warning("русское распознавание: %s", exc)
            return None
        return [Line(ln.text, ln.x + x0, ln.y + y0, ln.w, ln.h) for ln in lines]

    # ---------------------------------------------------------------- глубокий проход

    def _deep_due(self, sig: str, static: bool) -> bool:
        if self.ocr is None or not _cfg(self.cfg, "live_deep"):
            return False
        now = time.monotonic()
        if static:
            return sig != self._deep_sig and now - self._deep_at >= 0.5
        return now - self._deep_at >= max(DEEP_INTERVAL, self._deep_cost / DEEP_SHARE)

    def _deep_scan(self, fg: win32.WindowInfo, frame: bytes, x: int, y: int, w: int, h: int, sig: str) -> None:
        """Надписи на картинках, особые шрифты, мелкий текст: распознать варианты кадра и взять то,
        что подтвердили хотя бы два варианта."""
        started = time.monotonic()
        results: List[List[Line]] = []
        try:
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
            found = text.merge_deep(self._last_lines, [self._last_lines] + results)
            found = self._foreign(found, fg, frame, x, y, w, h)
            # крупная надпись (логотип), прочитанная лишь частично, выглядит хуже непереведённой
            ranks = self.words.ranks if self.words is not None else None
            found = [ln for ln in found if ln.h < 0.07 * h or text.known_words(ln.text, ranks) >= 3]
            if found:
                hold = max(4.0, DEEP_INTERVAL * 1.6, self._deep_cost / DEEP_SHARE * 1.6)
                added = self.tracker.add(text.group_lines(found), hold=hold)
                self.deep_found += len(added)
                if added:
                    log.info("глубокий проход: %s", " | ".join(b.text[:40] for b in added))
        finally:
            self._deep_at = time.monotonic()
            self._deep_cost = self._deep_at - started
            self._deep_sig = sig

    def _same_frame(self) -> None:
        now = time.monotonic()
        for b in self.tracker.blocks:
            b.last_seen = now
            if not b.translation and b.key:
                b.stable_hits += 1
        self._dispatch_ready()

    def _dispatch_ready(self) -> None:
        """Устойчивые блоки без перевода: из кэша сессии сразу, остальные — перечитать крупнее
        и в очередь переводчику."""
        for b in self.tracker.ready():
            hit = self.cache.get(b.key)
            if hit == SKIP:
                b.skip = True
            elif hit:
                b.translation = hit
            else:
                self._refine(b)
                self._enqueue(b)

    def _refine(self, b: Block) -> None:
        """Перечитать блок в увеличенном виде (x2–x3): мелкий и особый шрифт распознаётся заметно
        точнее («unutd be an qeratbn» → «would be an operation»). Берётся, если прочтение лучше."""
        if b.refined is not None or self.ocr is None or self._frame is None:
            return
        b.refined = ""
        frame, w, h = self._frame
        lh = b.line_height
        if lh >= 34:
            return                                  # крупный текст и так читается хорошо
        scale = 3 if lh < 15 else 2
        bx, by, bw, bh = b.rect
        m = int(max(6, lh * 0.6))
        x0, y0, x1, y1 = max(0, bx - m), max(0, by - m), min(w, bx + bw + m), min(h, by + bh + m)
        cw, ch = x1 - x0, y1 - y0
        limit = int(getattr(self.ocr, "max_dim", 4096))
        if cw < 8 or ch < 8 or max(cw, ch) * scale > limit:
            return
        try:
            from PIL import Image
            stride = w * 4
            crop = b"".join(frame[row * stride + x0 * 4:row * stride + x1 * 4] for row in range(y0, y1))
            img = Image.frombuffer("RGBA", (cw, ch), crop, "raw", "BGRA", 0, 1).resize((cw * scale, ch * scale),
                                                                                         Image.LANCZOS)
            lines = self.ocr.recognize(cw * scale, ch * scale, img.tobytes("raw", "BGRA"))
        except Exception as exc:  # noqa: BLE001
            log.debug("перечитывание блока: %s", exc)
            return
        lines = sorted((ln for ln in lines if ln.text.strip()), key=lambda ln: (ln.y, ln.x))
        if not lines:
            return
        refined = text.join_lines([ln.text for ln in lines])
        if text.cyrillic_share(refined) > 0.3 or not text.is_foreign(refined):
            return
        ranks = self.words.ranks if self.words is not None else None
        old_known, new_known = text.known_words(b.text, ranks), text.known_words(refined, ranks)
        if new_known > old_known or (new_known == old_known and text.letters(refined) > text.letters(b.text) * 1.1):
            b.refined = refined

    def _recognize(self, w: int, h: int, frame: bytes) -> Optional[List[Line]]:
        try:
            limit = int(getattr(self.ocr, "max_dim", 0) or 0)
            if limit and max(w, h) > limit:
                lines = self._recognize_scaled(w, h, frame, limit)
            else:
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

    def _check_black(self, fg: win32.WindowInfo, frame: bytes, w: int, h: int) -> None:
        """Эксклюзивный полноэкранный режим: вместо игры Windows отдаёт чёрный кадр — подсказываем, что делать.

        Подсказка — только если у этой игры ещё ни разу не было видно картинки (не чёрный экран загрузки)."""
        if self._picture:
            return
        stride = w * 4
        step = max(1, h // 24)
        rows = [frame[r * stride:(r + 1) * stride] for r in range(0, h, step)]
        if any(max(row[c::64], default=0) > 8 for row in rows for c in (0, 1, 2)):
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

    def _recognize_scaled(self, w: int, h: int, frame: bytes, limit: int) -> List[Line]:
        """Кадр больше, чем берёт распознавание Windows (очень большие мониторы), — уменьшаем и пересчитываем рамки."""
        from PIL import Image
        f = limit / float(max(w, h))
        sw, sh = max(1, int(w * f)), max(1, int(h * f))
        img = Image.frombuffer("RGBA", (w, h), frame, "raw", "BGRA", 0, 1).resize((sw, sh), Image.BILINEAR)
        lines = self.ocr.recognize(sw, sh, img.tobytes("raw", "BGRA"))
        return [Line(ln.text, int(ln.x / f), int(ln.y / f), int(ln.w / f), int(ln.h / f)) for ln in lines]

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
        self._shown_sig = self._frame_sig = self._deep_sig = ""
        self._capture_mode = "window"
        self._window_misses = 0
        self._plates = []
        self._grid = []
        self._last_lines = []
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
        self._plates = []
        if self._shown_sig:
            self._shown_sig = ""
            self.gui.hide()

    def _show(self, x: int, y: int, w: int, h: int) -> None:
        visible = [b for b in self.tracker.blocks if b.translation and not b.skip]
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
        self._plates = render.plate_rects((w, h), items, style)
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
            src = {id(b): self._source(b) for b in batch}
            if self.memory is not None:
                try:
                    hits = self.memory.get_many(ns, [(src[id(b)], None) for b in batch])
                except Exception:  # noqa: BLE001
                    hits = {}
                for b in batch:
                    tr = hits.get((src[id(b)], None))
                    if tr:
                        self._done(b, tr, from_memory=True)
                    else:
                        todo.append(b)
            else:
                todo = batch
            if not todo:
                continue
            entries = [Entry(id=str(i), source=src[id(b)], kind=TextKind.OTHER) for i, b in enumerate(todo)]
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
                    fresh[(src[id(b)], None)] = tr
                else:
                    with self._cond:
                        self._pending.discard(b.key)
            if fresh and self.memory is not None:
                try:
                    self.memory.put_many(ns, fresh)
                except Exception:  # noqa: BLE001
                    pass

    def _source(self, b: Block) -> str:
        """Текст блока для переводчика: с исправленными ошибками распознавания («sraphic» → «graphic»)."""
        base = b.refined or b.text
        src = text.without_junk(base) or base
        if self.words is None:
            return src
        try:
            return self.words.fix(src)
        except Exception:  # noqa: BLE001
            return src

    def _done(self, b: Block, tr: str, from_memory: bool = False) -> None:
        # перевод без русских букв или совпавший с оригиналом (имена, коды, абракадабра) — плашка не нужна
        useless = not text.cyrillic_share(tr) or text.normalize(tr) == text.normalize(b.text)
        self.cache.put(b.key, SKIP if useless else tr)
        with self._cond:
            self._pending.discard(b.key)
        targets = [b] + [o for o in self.tracker.blocks  # тот же текст мог появиться новым блоком
                         if o is not b and not o.translation and text.similar(o.key, b.key, 0.93)]
        for o in targets:
            if useless:
                o.skip = True
            else:
                o.translation = tr
        if useless:
            return
        self.count += 1
        self.recent.appendleft({"src": (b.refined or b.text)[:160], "tr": tr[:160]})
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

    def toggle(self) -> None:
        self.paused = not self.paused
        if self.paused:
            self._hide()
        self._shown_sig = ""

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
                    "error": str(err) if err else "", "starting": self.ocr is None and err is None},
            "translator": {"mode": self.translator_mode, "ready": self.translator is not None,
                           "error": self.translator_error},
            "last_game": {"title": last.title or Path(last.exe).stem, "exe": last.exe, "reason": self.target_reason,
                          "region": has_region(last)} if last else None,
            "count": self.count, "recent": list(self.recent)[:12],
            "capture_excluded": bool(self.gui and self.gui.excluded_from_capture),
            "capture": self.captured_by,
            "verify_ru": self.ocr_ru is not None,
            "deep": {"on": bool(_cfg(self.cfg, "live_deep")), "found": self.deep_found,
                     "ms": int(self._deep_cost * 1000)},
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
                    svc._shown_sig = ""
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
                    svc._shown_sig = ""
                    svc._deep_sig = ""
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
