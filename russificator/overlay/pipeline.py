"""Живой перевод по событиям кадра: что видно, что изменилось, что показывать.

Кадр снимается часто (10–20 раз в секунду) и сравнивается с прошлым по клеткам — это доли
миллисекунды. Распознавание (дорогое) запускается, только когда на кадре что-то сменилось и
успокоилось, и только для полосы кадра, где сменилось. Неподвижный экран распознаётся один раз:
тот же текст не читается, не группируется и не переводится заново, поэтому перевод на нём не
«мигает» и не меняется.

Надпись (:class:`LiveBlock`) живёт, пока на кадре её отпечаток (:class:`~.pixels.Probe` — точки
букв): реплика пропала, сцена сменилась — перевод убирается на следующем же кадре, без ожидания
распознавания. Промах распознавания надпись не убирает: отпечаток на месте — надпись тоже.

Перевод — по предложениям (:func:`~.text.sentences`): у реплики, которая допечатывается,
готовые предложения переведены и больше не меняются. Показывается надпись целиком, когда
подтверждено, что текст не меняется (картинка вокруг неё замерла или два прочтения совпали), и
переведены все её предложения; до тех пор остаётся прежний перевод этого места (``prev``) — так
перевод меняется только тогда, когда поменялся сам текст.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from . import pixels, text
from .text import Line, Rect

log = logging.getLogger("russificator.overlay.pipeline")

#: клетка сравнения кадров, пикселей
CELL = 8
#: на сколько должна смениться средняя яркость клетки, чтобы считать её изменившейся
CELL_DELTA = 6.0
#: резкая смена клетки (появилась или пропала буква, мигнула надпись); плавная анимация фона слабее
CELL_STRONG = 18.0
#: изменившаяся область не менялась столько — распознаём (буква допечатана, анимация прошла)
SETTLE = 0.07
#: область меняется всё время (анимация, игра) — всё равно распознаём не реже
MAX_WAIT = 0.45
#: плавные изменения (анимация фона, текст проявляется постепенно) проверяются не чаще
WEAK_PERIOD = 1.5
#: сколько окрестность надписи должна простоять неизменной, чтобы показать перевод
CONFIRM = 0.18
#: то же для реплики, которая допечатывается и ещё не дошла до конца предложения
CONFIRM_TYPING = 0.6
#: место меняется, а распознавание раз за разом не находит там нового (анимация, мигающий значок) —
#: оно проверяется всё реже: пауза удваивается до NOISE_MAX с
NOISE_FIRST = 0.5
NOISE_MAX = 3.0
#: …а если «шумит» полэкрана и больше (экшен: меняется всё) — не дольше, иначе новые субтитры ждали бы
NOISE_MAX_WIDE = 1.0
#: доля кадра, сменившаяся разом, — новая сцена
SCENE_CUT = 0.45
#: доля точек отпечатка, сменивших цвет, — надпись пропала, сменилась другой или «шевелится»
GONE = 0.4
#: …а пропала она, если цвета букв в её рамке осталось меньше этой доли
PRESENT = 0.5
#: и так — не меньше стольких секунд: анимированная надпись (глитч) на кадр-другой почти исчезает
GONE_HOLD = 0.15
#: перевод предложения, которое переводить не нужно (имя, код): показывается оригинал
SKIP = "\x00"


@dataclass(eq=False)
class LiveBlock:
    """Надпись на экране: абзац распознанного текста."""
    lines: List[Line]
    text: str                                   # текст надписи (без значка продолжения)
    rect: Rect
    parts: List[str]                            # предложения
    keys: List[str]                             # их ключи (:func:`text.normalize`)
    probe: Optional[pixels.Probe] = None
    frame: object = None                        # кадр, с которого прочитана (для перевода «на месте»)
    read_at: float = 0.0                        # когда снят этот кадр
    typing: bool = False                        # текст рос между прочтениями (печатная машинка)
    agree: int = 0                              # сколько прочтений подряд дали тот же текст
    confirmed: bool = False
    #: не переводится (имя, русский текст, название, мусор) — но место запоминается
    skip: bool = False
    #: на её месте только что было другое прочтение (анимированная надпись, логотип): показывается,
    #: только когда два прочтения подряд совпадут — иначе на экран попал бы мусор распознавания
    volatile: bool = False
    #: надпись анимирована (дрожь, глитч, волна): её пиксели меняются, а буквы на месте
    restless: bool = False
    #: другое прочтение на месте «беспокойной» надписи — заменит её, если следующее прочтение совпадёт
    challenger: Optional["LiveBlock"] = None
    gone_since: Optional[float] = None          # с какого момента её не видно
    deep: bool = False                          # найдена глубоким проходом
    prev: Optional["LiveBlock"] = None          # что показывать на этом месте, пока надпись не готова
    source: str = ""                            # текст для перевода целиком (с поправками распознавания)
    #: кэш картинки «перевод на месте» (overlay.inline) и для какого перевода она нарисована
    patch: object = None
    patch_text: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return text.normalize(self.text)

    @property
    def line_height(self) -> float:
        return sum(ln.h for ln in self.lines) / max(1, len(self.lines))


def make_block(lines: List[Line], img, read_at: float, source: Optional[str] = None) -> Optional[LiveBlock]:
    """Надпись из строк абзаца: текст, предложения, отпечаток на кадре."""
    body = text.strip_marker(text.join_lines(ln.text for ln in lines))
    src = text.strip_marker(source) if source else body
    parts = text.sentences(src) or [src]
    keys = [text.normalize(p) for p in parts]
    if not any(keys):
        return None
    x0, y0 = min(ln.x for ln in lines), min(ln.y for ln in lines)
    x1, y1 = max(ln.x + ln.w for ln in lines), max(ln.y + ln.h for ln in lines)
    probe = pixels.make_probe(img, [(ln.x, ln.y, ln.w, ln.h) for ln in lines]) if img is not None else None
    return LiveBlock(lines=list(lines), text=body, rect=(x0, y0, x1 - x0, y1 - y0), parts=parts, keys=keys,
                     probe=probe, frame=img, read_at=read_at, source=src)


@dataclass
class Job:
    """Задание распознавания: кадр (копия) и полоса, которую прочитать."""
    img: object
    rect: Rect
    at: float
    full: bool = False
    force: bool = False
    deep: bool = False
    cells: object = None                        # клетки, изменения в которых вызвали распознавание


def grows(old: str, new: str) -> bool:
    """Печатная машинка: новый текст продолжает старый."""
    return bool(old) and len(new) > len(old) and new.startswith(old[: max(1, len(old) - 2)])


def _overlap(a: Rect, b: Rect) -> int:
    return text.intersect_area(a, b)


class Pipeline:
    """Состояние живого перевода одного окна. Вызывается из одного потока (главного цикла):
    :meth:`frame` на каждый кадр, :meth:`ocr_done` с результатом распознавания."""

    def __init__(self, tr_get: Callable[[str], Optional[str]], watch: bool = True,
                 clock: Callable[[], float] = time.monotonic):
        self.tr_get = tr_get                    # перевод предложения по ключу: строка, SKIP или None
        self.watch = watch                      # следить за отпечатками (кадр без наших плашек)
        self.clock = clock
        self.blocks: List[LiveBlock] = []
        self.size: Optional[Tuple[int, int]] = None
        self.cells = None
        self.changed_at = None                  # когда в последний раз менялась каждая клетка
        self.strong_at = None                   # когда в последний раз клетка менялась резко
        self.dirty = None                       # клетки, менявшиеся с прошлого распознавания
        self.strong_dirty = None                # …из них — резко
        self.strong_since = 0.0
        self.strong_changed = 0.0
        self.cells_at_job = None                # клетки кадра прошлого распознавания
        self.last_job = -1e9
        self.busy = False                       # распознавание идёт
        self.noise = None                       # клетки, где меняется, но текста нового нет
        self.noise_until = 0.0
        self.noise_pause = 0.0
        self.static_since = 0.0                 # с какого момента кадр не меняется вовсе
        self.deep_done = False                  # глубокий проход для этого неподвижного экрана был
        self.version = 0                        # растёт, когда меняется то, что нужно показать
        self.scene_cuts = 0

    # ------------------------------------------------------------ кадр

    def frame(self, img, now: Optional[float] = None, force: bool = False) -> Optional[Job]:
        """Новый кадр (массив высота × ширина × BGRA). Возвращает задание распознавания или None.

        Распознавание запускают резкие изменения (появилась, пропала или допечаталась буква,
        сменилась реплика) — когда они успокоились, а если не успокаиваются (экшен) — не реже
        MAX_WAIT. Плавная анимация фона сама его не запускает: раз в WEAK_PERIOD проверяется,
        не изменилось ли что-то заметно с прошлого распознавания (текст, проявившийся плавно).
        Места, где меняется, но нового текста раз за разом нет (глитч, мигающий значок,
        переливы логотипа), проверяются всё реже (:meth:`_noise`)."""
        import numpy as np
        now = self.clock() if now is None else now
        c = pixels.cells(img, CELL)
        size = img.shape[:2]
        if self.cells is None or size != self.size or c.shape != self.cells.shape:
            self.size, self.cells = size, c
            self.changed_at = np.full(c.shape, now, np.float64)
            self.strong_at = np.full(c.shape, -1e9, np.float64)
            self.dirty = np.ones(c.shape, bool)
            self.strong_dirty = np.ones(c.shape, bool)
            self.noise = np.zeros(c.shape, bool)
            self.cells_at_job = c
            self.strong_since = self.strong_changed = now - SETTLE
            self.static_since = now
            self._drop_all()
        else:
            delta = np.abs(c - self.cells)
            changed = delta > CELL_DELTA
            self.cells = c
            if changed.any():
                if changed.mean() >= SCENE_CUT:
                    log.debug("новая сцена: сменилось %.0f%% кадра", changed.mean() * 100)
                    self._drop_all()            # сменилась вся картинка — прежние надписи не ждём
                    self.scene_cuts += 1
                self.dirty |= changed
                self.changed_at[changed] = now
                self.static_since = now
                self.deep_done = False
                strong = delta > CELL_STRONG
                if strong.any():
                    if not self.strong_dirty.any():
                        self.strong_since = now
                    self.strong_dirty |= strong
                    self.strong_changed = now
                    self.strong_at[strong] = now
        if self.watch:
            self._watch(img, now)
        self._confirm(now)
        if self.busy:
            return None
        noisy = now < self.noise_until
        live = self.strong_dirty & ~self.noise if noisy else self.strong_dirty
        cause = None
        if force:
            cause = np.ones(c.shape, bool)
        elif live.any() and (now - self.strong_changed >= SETTLE or now - self.strong_since >= MAX_WAIT):
            cause = live.copy()                 # strong_dirty сейчас обнулится
        elif self.dirty.any() and now - self.last_job >= WEAK_PERIOD:
            drift = np.abs(self.cells - self.cells_at_job) > CELL_STRONG
            if noisy:
                drift &= ~self.noise
            if drift.any():
                cause = drift
        if cause is None:
            return None
        rect = self._band(cause)
        self.dirty[:] = False
        self.strong_dirty[:] = False
        self.cells_at_job = self.cells
        self.last_job = now
        self.busy = True
        h, w = size
        return Job(img.copy(), rect, now, full=rect == (0, 0, w, h), force=force, cells=cause)

    def _band(self, cause) -> Rect:
        """Полоса кадра (во всю ширину), где что-то сменилось, с полями; надписи, которые она
        задевает, входят в неё целиком — надпись читается или вся, или никак."""
        import numpy as np
        h, w = self.size
        rows = np.flatnonzero(cause.any(axis=1))
        margin = max(2 * CELL, int(0.03 * h))
        y0 = max(0, int(rows[0]) * CELL - margin)
        y1 = min(h, (int(rows[-1]) + 1) * CELL + margin)
        grown = True
        while grown:
            grown = False
            for b in self.blocks:
                by0, by1 = b.rect[1], b.rect[1] + b.rect[3]
                if by1 > y0 and by0 < y1 and (by0 < y0 or by1 > y1):
                    y0, y1 = min(y0, max(0, by0 - 4)), max(y1, min(h, by1 + 4))
                    grown = True
        if y1 - y0 > 0.6 * h:
            return 0, 0, w, h
        return 0, y0, w, y1 - y0

    def _cells_of(self, r: Rect, grid):
        x, y, w, h = r
        rows, cols = grid.shape
        x0, y0 = max(0, x // CELL), max(0, y // CELL)
        x1, y1 = min(cols, -(-(x + w) // CELL)), min(rows, -(-(y + h) // CELL))
        return grid[y0:max(y0 + 1, y1), x0:max(x0 + 1, x1)]

    def _last_change(self, b: LiveBlock, now: float) -> float:
        """Когда в последний раз резко менялась окрестность надписи: она сама, продолжение строки
        справа и строка под ней (туда допечатывается текст). Плавная анимация фона не в счёт —
        иначе реплика на «живом» фоне не подтверждалась бы никогда."""
        x, y, w, h = b.rect
        lh = max(8, int(b.line_height))
        r = (x, y, w + 3 * lh, h + int(1.5 * lh))
        area = self._cells_of(r, self.strong_at)
        if area.size and now < self.noise_until:
            area = area[~self._cells_of(r, self.noise)]     # мигающий значок рядом — не «печатается»
        return float(area.max()) if area.size else 0.0

    # ------------------------------------------------------------ надписи

    def _drop_all(self) -> None:
        if self.blocks:
            self.blocks = []
            self.version += 1

    @staticmethod
    def _gone(b: LiveBlock, img, now: float) -> bool:
        """Надписи на кадре больше нет: её точки сменились, и цвета букв в рамке почти не осталось —
        дольше GONE_HOLD. Точки сменились, а цвет на месте — надпись анимирована или её сменил
        другой текст того же цвета: это решит распознавание (место изменилось — оно запустится)."""
        if b.probe is None or b.probe.changed(img) < GONE:
            b.gone_since = None
            return False
        if b.probe.present(img) >= PRESENT:
            b.restless = True
            b.gone_since = None
            return False
        if b.gone_since is None:
            b.gone_since = now
        return now - b.gone_since >= GONE_HOLD

    def _watch(self, img, now: float) -> None:
        """Убрать надписи, которых на кадре больше нет."""
        keep: List[LiveBlock] = []
        for b in self.blocks:
            if b.prev is not None and self._gone(b.prev, img, now):
                b.prev = None
                self.version += 1
            if self._gone(b, img, now):
                log.debug("пропала: %s", b.text[:50])
                self.version += 1
                continue
            keep.append(b)
        self.blocks = keep

    def _confirm(self, now: float) -> None:
        for b in self.blocks:
            if b.confirmed:
                continue
            last = self._last_change(b, now)
            wait = CONFIRM_TYPING if b.typing and not text.complete(b.parts[-1]) else CONFIRM
            steady = not b.volatile and b.read_at >= last - 1e-6 and now - last >= wait
            if b.agree >= 1 or steady:
                b.confirmed = True
                self.version += 1

    def ocr_done(self, job: Job, found: List[LiveBlock], now: Optional[float] = None) -> None:
        """Результат распознавания полосы ``job.rect``: новые и изменившиеся надписи."""
        now = self.clock() if now is None else now
        self.busy = False
        if job.img is None or job.img.shape[:2] != self.size:
            return
        band = job.rect
        inside = [b for b in self.blocks if _overlap(b.rect, band) >= 0.5 * text.area(b.rect)]
        result = [b for b in self.blocks if b not in inside]
        taken = set()
        for nb in found:
            if job.force:
                nb.confirmed = True
            over = [ob for ob in inside if _overlap(ob.rect, nb.rect) > 0.2 * min(text.area(ob.rect),
                                                                                    text.area(nb.rect))]
            same = next((ob for ob in over if id(ob) not in taken and _same_text(ob, nb)), None)
            if same is not None:
                # тот же текст: надпись остаётся как была (перевод, картинка, отпечаток)
                taken.add(id(same))
                same.agree += 1
                if job.force:
                    same.confirmed = True
                result.append(same)
                log.debug("та же: %s", same.text[:50])
                for ob in over:
                    if ob is not same:
                        taken.add(id(ob))       # обрывок того же места — заменён целой надписью
                continue
            live = next((ob for ob in over if id(ob) not in taken and not ob.skip and ob.confirmed
                         and ob.probe is not None and ob.probe.present(job.img) >= PRESENT), None)
            if nb.skip and live is not None:
                # «непереводимое» прочтение на месте живой переведённой надписи — это искажённое
                # прочтение той же надписи (глитч, помеха), а не новый текст
                taken.add(id(live))
                result.append(live)
                continue
            growing = [ob for ob in over if grows(ob.key, nb.key) or ob.typing and text.similar(ob.key, nb.key, 0.6)]
            # похожее прочтение (глитч исказил пару букв) — ждём подтверждения; совсем другой текст
            # (следующая реплика того же цвета) заменяет сразу
            steady = next((ob for ob in over if id(ob) not in taken and ob.restless and ob.confirmed
                           and ob not in growing and text.similar(ob.key, nb.key, 0.5)), None)
            if steady is not None and not (steady.challenger is not None and _same_text(steady.challenger, nb)):
                # «беспокойная» надпись прочитана иначе (глитч исказил буквы) — одного прочтения мало
                steady.challenger = nb
                taken.add(id(steady))
                result.append(steady)
                log.debug("другое прочтение анимированной надписи ждёт подтверждения: %s", nb.text[:50])
                continue
            # прежний перевод держится, только пока текст допечатывается; другой текст на том же
            # месте — прежний перевод убирается сразу
            shown = [ob for ob in growing if self.ready(ob)] + [ob.prev for ob in growing if ob.prev is not None]
            nb.prev = max(shown, key=lambda ob: _overlap(ob.rect, nb.rect)) if shown else None
            nb.typing = bool(growing)
            nb.volatile = bool(over) and not nb.typing
            if steady is not None:
                nb.agree = 1                    # два прочтения подряд совпали
            for ob in over:
                taken.add(id(ob))
            result.append(nb)
            log.debug("новая%s%s: %s (заменила %d)", " (печатается)" if nb.typing else "",
                      " (прежний перевод остаётся)" if nb.prev is not None else "", nb.text[:50], len(over))
        for ob in inside:
            if id(ob) in taken:
                continue
            # распознавание её не нашло: пропала — или промах (отпечаток или цвет букв на месте — остаётся)
            if ob.probe is not None and (ob.probe.changed(job.img) < GONE or ob.probe.present(job.img) >= PRESENT):
                result.append(ob)
            else:
                log.debug("не найдена и отпечаток сменился: %s", ob.text[:50])
        # новое — настоящая новая надпись (не непереводимое и не прочтение, ждущее подтверждения) или пропажа
        fresh = [b for b in result if b not in self.blocks and not b.skip and not b.volatile] or \
            len(result) < len(self.blocks)
        self.blocks = result
        self.version += 1
        self._noise(job, bool(fresh), now)

    def _noise(self, job: Job, fresh: bool, now: float) -> None:
        """Распознавание не нашло нового — клетки, из-за которых оно запускалось, «шумные»: пока
        меняются только они, следующее распознавание откладывается (всё дольше). Нашло —
        шума нет."""
        if job.cells is None or job.force:
            return
        if fresh:
            self.noise[:] = False
            self.noise_until = 0.0
            self.noise_pause = 0.0
            return
        self.noise |= job.cells
        cap = NOISE_MAX if self.noise.mean() < 0.5 else NOISE_MAX_WIDE
        self.noise_pause = min(cap, self.noise_pause * 2 if self.noise_pause else NOISE_FIRST)
        self.noise_until = now + self.noise_pause

    def add_deep(self, found: List[LiveBlock]) -> List[LiveBlock]:
        """Надписи глубокого прохода: только новые места (готовое не трогается); экран неподвижен —
        текст подтверждён."""
        self.deep_done = True
        added = []
        for nb in found:
            if any(_overlap(ob.rect, nb.rect) > 0 for ob in self.blocks):
                continue
            nb.confirmed = True
            nb.deep = True
            self.blocks.append(nb)
            added.append(nb)
        if added:
            self.version += 1
        return added

    def idle_for(self, now: Optional[float] = None) -> float:
        """Сколько секунд кадр неподвижен и распознавать нечего (для глубокого прохода)."""
        now = self.clock() if now is None else now
        if self.busy or self.dirty is None or self.strong_dirty.any():
            return 0.0
        return now - self.static_since

    # ------------------------------------------------------------ перевод и показ

    def needed(self) -> List[Tuple[str, str, bool]]:
        """Предложения без перевода: (ключ, текст, срочно). Законченные предложения переводятся
        сразу (они уже не изменятся), незаконченное — когда надпись подтверждена."""
        out: List[Tuple[str, str, bool]] = []
        seen = set()
        for b in sorted(self.blocks, key=lambda b: not b.confirmed):
            if b.skip:
                continue
            for i, (part, key) in enumerate(zip(b.parts, b.keys)):
                if not key or key in seen or self.tr_get(key) is not None:
                    continue
                last = i == len(b.parts) - 1
                if not b.confirmed and last and not text.complete(part):
                    continue
                seen.add(key)
                out.append((key, part, b.confirmed))
        return out

    def ready(self, b: LiveBlock) -> bool:
        return b.confirmed and not b.skip and all(not k or self.tr_get(k) is not None for k in b.keys)

    def translation(self, b: LiveBlock) -> Optional[str]:
        """Перевод надписи (None — не готов или переводить нечего)."""
        if not self.ready(b):
            return None
        out, useful = [], False
        for part, key in zip(b.parts, b.keys):
            tr = self.tr_get(key) if key else SKIP
            if tr == SKIP or not tr:
                out.append(part)                # имя, код — остаётся как было
            else:
                out.append(tr)
                useful = True
        return " ".join(out) if useful else None

    def visible(self) -> List[Tuple[LiveBlock, str]]:
        """Что показывать: готовые надписи, а на месте неготовых — их прежний перевод."""
        out: List[Tuple[LiveBlock, str]] = []
        seen = set()
        for b in self.blocks:
            tr = self.translation(b)
            if tr is not None:
                if b.prev is not None:
                    b.prev = None               # готова — прежний перевод больше не нужен
                if id(b) not in seen:
                    seen.add(id(b))
                    out.append((b, tr))
                continue
            if self.ready(b):
                b.prev = None                   # готова, но переводить нечего — прежнее убираем
                continue
            p = b.prev
            if p is not None and id(p) not in seen:
                ptr = self.translation(p)
                if ptr is not None:
                    seen.add(id(p))
                    out.append((p, ptr))
        return out


def _same_text(old: LiveBlock, new: LiveBlock) -> bool:
    """То же самое прочтение с поправкой на шум распознавания (но не дописанный текст)."""
    a, b = old.key, new.key
    if a == b:
        return True
    if grows(a, b):
        return False                            # допечатано
    if grows(b, a) and len(a) - len(b) > 2:
        return False                            # заметно короче — другой текст
    return abs(len(a) - len(b)) <= max(2, len(b) // 20) and text.similar(a, b, 0.9)
