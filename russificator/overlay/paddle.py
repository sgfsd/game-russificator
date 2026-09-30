"""Нейросетевое распознавание текста: PaddleOCR PP-OCRv5 на onnxruntime (видеокарта через DirectML
или процессор).

Распознавание Windows хорошо читает обычный текст интерфейса, но почти не видит надписи на
картинках, стилизованные, пиксельные и рукописные шрифты. PaddleOCR читает их (проверено на
играх: кнопки-картинки «PLAY»/«CREDITS», рукописный бланк, пиксельные экраны), а
восточнославянская модель распознаёт и латиницу, и кириллицу — поэтому русский текст на экране
(перевод игры, наши же плашки) узнаётся сразу при чтении, без отдельной проверки.

Устройство:
  * детектор DB находит строки текста (карта вероятностей → связные области → прямоугольники;
    без OpenCV — разбор по отрезкам строк);
  * строка читается восточнославянской моделью; если она латинская — ещё и английской
    (она точнее на английском), берётся более уверенное прочтение;
  * прочитанные строки запоминаются по месту и отпечатку картинки — неподвижный текст не
    распознаётся заново, на каждом кадре работает только детектор.

Модели (Apache-2.0) лежат в ``resources/ocr``.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import pixels
from .ocr import Ocr, OcrUnavailable
from .text import Line, looks_russian

log = logging.getLogger("russificator.overlay.paddle")

MODELS = Path(__file__).resolve().parents[1] / "resources" / "ocr"
DET = "det_ppocrv5_mobile.onnx"
REC = {"eslav": "rec_eslav_ppocrv5_mobile", "en": "rec_en_ppocrv5_mobile"}

#: x, y, w, h, уверенность детектора, наклон (None — строка горизонтальная, иначе центр x, y,
#: длина, высота строки и угол в радианах; x, y, w, h — описанный прямоугольник)
Rot = Tuple[float, float, float, float, float]
Box = Tuple[int, int, int, int, float, Optional[Rot]]


def available() -> bool:
    """Есть ли всё для нейросетевого распознавания (onnxruntime и модели)."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return (MODELS / DET).is_file() and all((MODELS / f"{n}.onnx").is_file() for n in REC.values())


GPU, CPU = "видеокарта", "процессор"

#: Сессии на видеокарте не удаляются до конца процесса: onnxruntime-directml делит одно устройство
#: DirectML на все сессии, и удаление одной из них ломает остальные (следующий запуск — нарушение
#: доступа и падение процесса).
_KEEP: list = []


def _session(path: Path, gpu: bool):
    import onnxruntime as ort
    ort.set_default_logger_severity(3)
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if gpu and "DmlExecutionProvider" in ort.get_available_providers():
        so.enable_mem_pattern = False                       # так требует DirectML
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        try:
            s = ort.InferenceSession(str(path), so, providers=["DmlExecutionProvider", "CPUExecutionProvider"])
            _KEEP.append(s)
            if "DmlExecutionProvider" in s.get_providers():
                return s, GPU
        except Exception as exc:  # noqa: BLE001
            log.warning("DirectML недоступен (%s) — распознавание на процессоре", exc)
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"]), CPU


def _fastest(path: Path, gpu: bool, sample: Callable[[object], None]):
    """Сессия на том устройстве, где модель на этом ПК работает быстрее.

    Видеокарта выигрывает не всегда: у слабой (или занятой игрой) каждый запуск стоит заметного
    времени на пересылку и подготовку, и маленькая модель на процессоре оказывается быстрее. Поэтому
    устройство выбирается замером (по два прогона на образце), а не заранее."""
    import time
    first, dev = _session(path, gpu)
    if dev != GPU:
        return first, dev
    cpu, _ = _session(path, False)
    times = {}
    for sess, name in ((first, GPU), (cpu, CPU)):
        try:
            sample(sess)                                    # первый прогон — подготовка, не считается
            t = time.perf_counter()
            sample(sess)
            sample(sess)
            times[name] = time.perf_counter() - t
        except Exception as exc:  # noqa: BLE001
            log.warning("замер %s (%s): %s", path.name, name, exc)
            times[name] = float("inf")
    pick = GPU if times[GPU] < times[CPU] else CPU
    log.info("%s: видеокарта %.0f мс, процессор %.0f мс — %s", path.name, times[GPU] * 500, times[CPU] * 500, pick)
    return (first, GPU) if pick == GPU else (cpu, CPU)


#: ширина строки для модели чтения округляется до кратной — на видеокарте каждый новый размер входа
#: стоит отдельной подготовки, а так размеров немного
REC_STEP = 64
#: детектор на видеокарте получает кадр, дополненный до кратного размера (по той же причине)
DET_STEP = 128


class _Recognizer:
    def __init__(self, name: str, gpu: bool, height: int = 48, device: Optional[str] = None):
        import numpy as np
        path = MODELS / f"{name}.onnx"
        if device is not None:
            self.session, self.device = _session(path, device == GPU)
        else:
            # строки в игре разной длины — каждый замер берёт новые ширины (как в игре), иначе
            # видеокарта выигрывала бы замер на одном размере и проигрывала в игре
            widths: dict = {}

            def sample(sess):
                it = widths.setdefault(id(sess), iter(range(256, 256 + 64 * 40, 64)))
                for _ in range(2):
                    x = np.zeros((2, 3, height, next(it)), np.float32)
                    sess.run(None, {sess.get_inputs()[0].name: x})
            self.session, self.device = _fastest(path, gpu, sample)
        chars = json.loads((MODELS / f"{name}.json").read_text(encoding="utf-8"))
        self.chars = ["<blank>"] + chars + ([" "] if " " not in chars else [])
        self.height = height
        self.input = self.session.get_inputs()[0].name

    def __call__(self, crops: Sequence, batch: int = 8) -> List[Tuple[str, float]]:
        import numpy as np
        res: List[Tuple[str, float]] = [("", 0.0)] * len(crops)
        order = sorted(range(len(crops)), key=lambda i: crops[i].shape[1])
        for b in range(0, len(order), batch):
            idx = order[b:b + batch]
            width = max(crops[i].shape[1] for i in idx)
            if self.device == GPU:
                width = -(-width // REC_STEP) * REC_STEP
            x = np.zeros((len(idx), 3, self.height, width), np.float32)
            for k, i in enumerate(idx):
                c = crops[i]
                x[k, :, :, :c.shape[1]] = ((c[:, :, ::-1] / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)
            prob = self.session.run(None, {self.input: x})[0]
            ids, conf = prob.argmax(2), prob.max(2)
            for k, i in enumerate(idx):
                out, cs, last = [], [], 0
                for t, c in enumerate(ids[k].tolist()):
                    if c and c != last and c < len(self.chars):
                        out.append(self.chars[c])
                        cs.append(float(conf[k, t]))
                    last = c
                res[i] = ("".join(out).strip(), sum(cs) / len(cs) if cs else 0.0)
        return res


class PaddleOcr(Ocr):
    """Распознавание PP-OCRv5 (интерфейс как у распознавания Windows: кадр BGRA → строки)."""

    name = "paddle"
    lang = "en+ru"
    max_dim = 4096
    #: сколько пикселей детектор получает за раз (кадр больше — уменьшается): столько, сколько
    #: 960 × 544, — а полоса кадра (часть экрана, где сменился текст) идёт в полном разрешении
    DET_BUDGET = 960 * 544

    def __init__(self, gpu: bool = True, det_limit: int = 960):
        if not available():
            raise OcrUnavailable("failed", "нейросетевое распознавание не установлено (onnxruntime или модели)")
        import numpy as np

        shapes: dict = {}

        def det_sample(sess):
            # полосы кадра разной высоты — у каждого замера свой размер, как в игре
            it = shapes.setdefault(id(sess), iter([(128, 1280), (256, 1280), (384, 1280), (512, 896), (256, 896),
                                                   (640, 1152), (128, 896), (384, 896), (512, 1280)]))
            sess.run(None, {sess.get_inputs()[0].name: np.zeros((1, 3) + next(it), np.float32)})
        self.det, self.det_device = _fastest(MODELS / DET, gpu, det_sample)
        self.det_input = self.det.get_inputs()[0].name
        en = _Recognizer(REC["en"], gpu)
        self.rec = {"en": en, "eslav": _Recognizer(REC["eslav"], gpu, device=en.device)}
        self.device = self.det_device if self.det_device == en.device else \
            f"детектор — {self.det_device}, чтение — {en.device}"
        self.det_limit = det_limit
        #: «прочтение сомнительное» (служба подставляет проверку по словарю): такая строка
        #: перечитывается ещё и моделью с кириллицей — вдруг это русский текст
        self.doubt: Optional[Callable[[str], bool]] = None
        self._cache: Dict[tuple, Tuple[str, float, bool, str]] = {}
        self._shapes = _ShapeCache()
        self._lock = threading.Lock()
        log.info("нейросетевое распознавание: PP-OCRv5, %s", self.device)

    # ------------------------------------------------------------ кадр → строки

    def recognize(self, w: int, h: int, bgra: bytes, limit: Optional[int] = None) -> List[Line]:
        import numpy as np
        img = np.frombuffer(bgra, dtype=np.uint8, count=w * h * 4).reshape(h, w, 4)
        budget = None if limit is None else int(limit * limit * 544 / 960)
        return self.recognize_array(img, budget=budget)

    def recognize_array(self, img, rect: Optional[Tuple[int, int, int, int]] = None,
                        budget: Optional[int] = None) -> List[Line]:
        """Строки кадра ``img`` (массив высота × ширина × BGRA) или его части ``rect`` (x, y, w, h);
        рамки строк — в координатах всего кадра."""
        import numpy as np
        x0, y0 = 0, 0
        if rect is not None:
            x0, y0 = max(0, rect[0]), max(0, rect[1])
            img = img[y0:y0 + rect[3], x0:x0 + rect[2]]
        if img.shape[0] < 8 or img.shape[1] < 8:
            return []
        with self._lock:
            rgb = np.ascontiguousarray(img[:, :, 2::-1])
            boxes = [part for box in join_fragments(self.detect(rgb, budget=budget), rgb)
                     for part in _split(rgb, box)]
            lines = merge_row(self._read(rgb, boxes), rgb)
        if x0 or y0:
            for ln in lines:
                ln.x += x0
                ln.y += y0
                if ln.rot is not None:
                    cx, cy, length, thick, ang = ln.rot
                    ln.rot = (cx + x0, cy + y0, length, thick, ang)
        return lines

    def detect(self, rgb, limit: Optional[int] = None, budget: Optional[int] = None) -> List[Box]:
        import numpy as np
        from PIL import Image
        h, w = rgb.shape[:2]
        if limit is not None:
            r = min(1.0, limit / max(h, w))
        else:
            r = min(1.0, math.sqrt((budget or self.DET_BUDGET) / float(h * w)))
        nh, nw = max(32, int(round(h * r / 32)) * 32), max(32, int(round(w * r / 32)) * 32)
        im = np.asarray(Image.fromarray(rgb).resize((nw, nh), Image.BILINEAR), dtype=np.float32)[:, :, ::-1]
        im = (im / 255.0 - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        x = im.transpose(2, 0, 1)[None]
        if self.det_device == GPU:
            # дополнить до кратного размера (поля — чёрные): новых размеров входа меньше
            ph, pw = -(-nh // DET_STEP) * DET_STEP, -(-nw // DET_STEP) * DET_STEP
            if (ph, pw) != (nh, nw):
                pad = np.empty((1, 3, ph, pw), np.float32)
                pad[:] = ((0 - np.array([0.485, 0.456, 0.406], np.float32)) /
                          np.array([0.229, 0.224, 0.225], np.float32))[None, :, None, None]
                pad[:, :, :nh, :nw] = x
                x = pad
        x = np.ascontiguousarray(x, dtype=np.float32)
        prob = self.det.run(None, {self.det_input: x})[0][0, 0][:nh, :nw]
        return boxes_from_prob(prob, w / nw, h / nh)

    def _read(self, rgb, boxes: List[Box]) -> List[Line]:
        import numpy as np
        from PIL import Image
        h, w = rgb.shape[:2]
        lines: List[Line] = []
        todo: List[Tuple[tuple, Box, object]] = []
        for box in boxes:
            x, y, bw, bh = box[:4]
            x0, y0, x1, y1 = max(0, x), max(0, y), min(w, x + bw), min(h, y + bh)
            if x1 - x0 < 4 or y1 - y0 < 6:
                continue
            crop = rgb[y0:y1, x0:x1]
            key = (x1 - x0, y1 - y0, _signature(crop))
            hit = self._cache.get(key)
            shape = None
            if hit is None:
                hit, shape = self._shapes.get(crop)
            if hit is not None:
                lines.append(_line(hit, x0, y0, x1 - x0, y1 - y0, box[5]))
                continue
            if box[5] is not None:
                resized = straighten(rgb, box[5], 48)
            else:
                nw = max(8, int(np.ceil(48 * (x1 - x0) / (y1 - y0))))
                resized = np.asarray(Image.fromarray(crop).resize((min(nw, 3200), 48), Image.BILINEAR),
                                     dtype=np.float32)
            todo.append(((key, shape), (x0, y0, x1 - x0, y1 - y0, box[4], box[5]), resized))
        if todo:
            # английская модель точнее на латинице; восточнославянская (латиница + кириллица) —
            # только для сомнительных строк (неуверенно прочитанных или с незнакомыми словами:
            # русское «Об игре» английская модель уверенно читает как «06 urpe»), а обычная
            # английская строка читается одной моделью, а не двумя
            first = self.rec["en"]([t[2] for t in todo])
            doubt = [i for i, (txt, conf) in enumerate(first)
                     if not txt or conf < 0.9 or (self.doubt is not None and self.doubt(txt))]
            second = dict(zip(doubt, self.rec["eslav"]([todo[i][2] for i in doubt]))) if doubt else {}
            for i, (key, (x, y, bw, bh, _, rot), _crop) in enumerate(todo):
                txt, conf = first[i]
                ru, alt = False, ""
                if i in second and second[i][0]:
                    t2, c2 = second[i]
                    # кириллица, и в ней характерные русские буквы (б, г, и…) — русский текст, даже если
                    # английская модель прочитала его уверенно («Об игре» → «06 urpe»)
                    if _cyr_share(t2) >= 0.5 and (c2 >= conf - 0.05 or (c2 >= 0.6 and looks_russian(t2))):
                        # кириллица; английское прочтение — про запас: слово с эффектом (дрожь,
                        # «глитч») посреди английской строки тоже читается как кириллица
                        txt, conf, ru, alt = t2, c2, True, txt
                    elif c2 > conf + 0.05:
                        txt, conf = t2, c2
                res = (txt, conf, ru, alt)
                if len(self._cache) > 4000:
                    self._cache.clear()
                self._cache[key[0]] = res
                self._shapes.put(key[1], res)
                lines.append(_line(res, x, y, bw, bh, rot))
        return [ln for ln in lines if ln.text and ln.conf >= 0.3]

    def close(self) -> None:
        self._cache.clear()
        self._shapes = _ShapeCache()


class _ShapeCache:
    """Прочитанные строки по форме букв. Строка на полупрозрачной плашке над анимированным
    фоном каждый кадр немного другая по пикселям, но форма букв (что темнее, что светлее середины
    её контраста) та же — такую строку незачем читать заново. Совпадением считается разница не
    больше чем в паре точек на ширину буквы: другая буква или цифра (счётчик «HP: 45» → «46»)
    меняет в своём месте десятки точек."""

    def __init__(self, limit: int = 4000):
        self.limit = limit
        self.buckets: Dict[tuple, list] = {}
        self.count = 0

    @staticmethod
    def shape(crop):
        import numpy as np
        g = crop.astype(np.int16).sum(axis=-1)
        h, w = g.shape
        ys = np.linspace(0, h - 1, min(h, 16)).astype(np.intp)
        xs = np.linspace(0, w - 1, min(w, max(64, int(8 * w / max(1, h))))).astype(np.intp)
        small = g[np.ix_(ys, xs)]
        lo, hi = np.percentile(small, 10), np.percentile(small, 90)
        if hi - lo < 90:
            return None                         # контраста мало — по форме не узнать
        return (h // 4, w // 4), small > (lo + hi) / 2

    def get(self, crop):
        import numpy as np
        sh = self.shape(crop)
        if sh is None:
            return None, None
        bucket, mask = sh
        cols = mask.shape[1] // 4 * 4
        for other, res in self.buckets.get(bucket, ()):
            if other.shape != mask.shape:
                continue
            diff = other != mask
            if diff.sum() <= 0.01 * diff.size and (not cols or diff[:, :cols].reshape(diff.shape[0], -1, 4)
                                                   .sum(axis=(0, 2)).max() <= 2):
                return res, None
        return None, sh

    def put(self, sh, res) -> None:
        if sh is None:
            return
        if self.count >= self.limit:
            self.buckets.clear()
            self.count = 0
        lst = self.buckets.setdefault(sh[0], [])
        lst.append((sh[1], res))
        if len(lst) > 40:
            del lst[0]
        self.count += 1


def _line(res: Tuple[str, float, bool, str], x: int, y: int, w: int, h: int, rot: Optional[Rot] = None) -> Line:
    txt, conf, ru, alt = res
    ln = Line(txt, x, y, w, h)
    ln.conf, ln.ru, ln.rot, ln.alt = conf, ru, rot, alt
    return ln


def straighten(rgb, rot: Rot, height: int):
    """Наклонная строка, повёрнутая горизонтально и приведённая к высоте ``height`` (float32, RGB)."""
    import math
    import numpy as np
    from PIL import Image
    cx, cy, length, thick, ang = rot
    h, w = rgb.shape[:2]
    # сначала — вырез вокруг строки, уменьшенный со сглаживанием (поворот сам по себе не сглаживает)
    ca, sa = abs(math.cos(ang)), abs(math.sin(ang))
    hx, hy = (length * ca + thick * sa) / 2 + 2, (length * sa + thick * ca) / 2 + 2
    x0, y0 = max(0, int(cx - hx)), max(0, int(cy - hy))
    x1, y1 = min(w, int(math.ceil(cx + hx))), min(h, int(math.ceil(cy + hy)))
    img = Image.fromarray(np.ascontiguousarray(rgb[y0:y1, x0:x1]))
    k = max(1.0, thick / height)
    if k > 1.2:
        img = img.resize((max(1, round(img.width / k)), max(1, round(img.height / k))), Image.BILINEAR)
    else:
        k = 1.0
    ccx, ccy = (cx - x0) / k, (cy - y0) / k
    s = thick / k / height
    out_w = max(8, min(3200, int(math.ceil(height * length / thick))))
    c, n = math.cos(ang), math.sin(ang)
    data = (s * c, -s * n, ccx - out_w * s / 2 * c + height * s / 2 * n,
            s * n, s * c, ccy - out_w * s / 2 * n - height * s / 2 * c)
    out = img.transform((out_w, height), Image.AFFINE, data, resample=Image.BILINEAR, fillcolor=None)
    return np.asarray(out, dtype=np.float32)


def _cyr_share(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "Ѐ" <= c <= "ӿ") / len(letters)


def _signature(crop) -> int:
    """Отпечаток картинки строки: та же строка (на том же или другом месте) не читается заново.
    Точки берутся густо (по 4 и больше на букву), чтобы строки одного размера, но с разными
    буквами не совпали."""
    import numpy as np
    h, w = crop.shape[:2]
    ys = np.linspace(0, h - 1, min(h, 16)).astype(np.intp)
    xs = np.linspace(0, w - 1, min(w, max(64, int(8 * w / max(1, h))))).astype(np.intp)
    small = crop[np.ix_(ys, xs)].astype(np.int16).sum(axis=-1) if crop.ndim == 3 else crop[np.ix_(ys, xs)]
    return hash((small // 48).astype(np.int16).tobytes())


# ------------------------------------------------------------------ детектор: карта → прямоугольники

def boxes_from_prob(prob, sx: float, sy: float, thresh: float = 0.3, box_thresh: float = 0.6,
                    unclip: float = 1.5) -> List[Box]:
    """Связные области карты вероятностей текста (отрезками по строкам, объединение-поиск) →
    прямоугольники в координатах кадра, расширенные, как в DBPostProcess PaddleOCR."""
    import numpy as np
    bit = prob > thresh
    parent: List[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    runs = []
    prev: List[Tuple[int, int, int]] = []
    for y in range(bit.shape[0]):
        row = bit[y]
        if not row.any():
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        starts, ends = np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()
        cur = []
        j = 0
        for s, e in zip(starts, ends):
            lab = len(parent)
            parent.append(lab)
            while j < len(prev) and prev[j][1] < s:           # восьмисвязность: касание углом — тоже связь
                j += 1
            k = j
            while k < len(prev) and prev[k][0] <= e:
                a, b = find(prev[k][2]), find(lab)
                if a != b:
                    parent[b] = a
                k += 1
            cur.append((s, e, lab))
            runs.append((y, s, e, lab))
        prev = cur
    comps: Dict[int, list] = {}
    for y, s, e, lab in runs:
        r = find(lab)
        c = comps.get(r)
        score = float(prob[y, s:e].sum())
        # моменты области (пиксель — квадрат со стороной 1): по ним — наклон строки
        m = e - s
        mx, mxx = (e * e - s * s) / 2.0, (e ** 3 - s ** 3) / 3.0
        yc = y + 0.5
        mom = (m * yc, m * (y * y + y + 1.0 / 3.0), yc * mx, mx, mxx)
        if c is None:
            comps[r] = [y, y, s, e, score, m, *mom]
        else:
            c[0] = min(c[0], y)
            c[1] = max(c[1], y)
            c[2] = min(c[2], s)
            c[3] = max(c[3], e)
            c[4] += score
            c[5] += m
            for k in range(5):
                c[6 + k] += mom[k]
    tilts: Dict[int, list] = {}
    for root, (y0, y1, x0, x1, score, n, my, myy, mxy, mx, mxx) in comps.items():
        if min(x1 - x0, y1 - y0 + 1) >= 3 and score / n >= box_thresh:
            t = _tilt(n, mx, my, mxx, myy, mxy, sx, sy)
            if t is not None:
                tilts[root] = [*t, math.inf, -math.inf]
    if tilts:
        # длина наклонной строки — по крайним точкам области вдоль её оси (моменты занижают длину:
        # к концам строки область сужается, и крайние буквы оказывались за рамкой)
        for y, s, e, lab in runs:
            t = tilts.get(find(lab))
            if t is None:
                continue
            cx, cy, _, _, ang = t[:5]
            c, sn = math.cos(ang), math.sin(ang)
            py = (y + 0.5) * sy - cy
            for px in (s * sx - cx, e * sx - cx):
                d = px * c + py * sn
                t[5], t[6] = min(t[5], d), max(t[6], d)
    out: List[Box] = []
    for root, (y0, y1, x0, x1, score, n, my, myy, mxy, mx, mxx) in comps.items():
        bw, bh = x1 - x0, y1 - y0 + 1
        if min(bw, bh) < 3 or score / n < box_thresh:
            continue
        t = tilts.get(root)
        if t is not None:
            cx, cy, _, thick, ang, lo, hi = t
            cx, cy = cx + (lo + hi) / 2 * math.cos(ang), cy + (lo + hi) / 2 * math.sin(ang)
            length = hi - lo
            off = length * thick * unclip / (2 * (length + thick))
            length, thick = length + 2 * off, thick + 2 * off
            rot = (cx, cy, length, thick, ang)
            ca, sa = abs(math.cos(ang)), abs(math.sin(ang))
            hx, hy = (length * ca + thick * sa) / 2, (length * sa + thick * ca) / 2
            X0, Y0 = max(0.0, cx - hx), max(0.0, cy - hy)
            out.append((int(X0), int(Y0), int(cx + hx - X0), int(cy + hy - Y0), score / n, rot))
            continue
        off = bw * bh * unclip / (2 * (bw + bh))
        X0, Y0 = max(0.0, (x0 - off) * sx), max(0.0, (y0 - off) * sy)
        X1, Y1 = (x1 + off) * sx, (y1 + 1 + off) * sy
        out.append((int(X0), int(Y0), int(X1 - X0), int(Y1 - Y0), score / n, None))
    return out


def _tilt(n: float, mx: float, my: float, mxx: float, myy: float, mxy: float, sx: float, sy: float
          ) -> Optional[Rot]:
    """Наклон строки по моментам её области на карте детектора. None — строка горизонтальная
    (или слишком короткая, чтобы судить о наклоне): наклон, который почти не раздувает рамку, не
    стоит поворота."""
    ax, ay = mx / n, my / n
    cxx, cyy, cxy = (mxx / n - ax * ax) * sx * sx, (myy / n - ay * ay) * sy * sy, (mxy / n - ax * ay) * sx * sy
    half = (cxx + cyy) / 2
    root = math.sqrt(max(0.0, ((cxx - cyy) / 2) ** 2 + cxy * cxy))
    length, thick = math.sqrt(12 * (half + root)), math.sqrt(12 * max(1e-6, half - root))
    ang = 0.5 * math.atan2(2 * cxy, cxx - cyy)
    tilted = math.radians(3) <= abs(ang) <= math.radians(40)
    if length < 2.0 * thick or not tilted or length * abs(math.sin(ang)) < 0.35 * thick:
        return None
    return (ax * sx, ay * sy, length, thick, ang)


def join_fragments(boxes: List[Box], rgb=None) -> List[Box]:
    """Куски одной строки, на которые детектор её раздробил (рамки на одной высоте перекрываются
    или почти касаются — так бывает у пиксельных и разреженных шрифтов), — в одну рамку: строка
    читается целиком, а не обрывками «isF» + «ly advised». Через черту рамки не склеиваются."""
    flat = sorted((b for b in boxes if b[5] is None), key=lambda b: b[0])
    out = [b for b in boxes if b[5] is not None]
    used = [False] * len(flat)
    for i, a in enumerate(flat):
        if used[i]:
            continue
        x0, y0, x1, y1, score = a[0], a[1], a[0] + a[2], a[1] + a[3], a[4]
        for j in range(i + 1, len(flat)):
            b = flat[j]
            if used[j]:
                continue
            hmin, hmax = min(y1 - y0, b[3]), max(y1 - y0, b[3])
            overlap = min(y1, b[1] + b[3]) - max(y0, b[1])
            if b[0] - x1 > 0.3 * hmin:
                break                       # дальше по строке — только правее
            if overlap < 0.6 * hmin or hmax > 1.7 * hmin:
                continue
            # черта рамки между кусками — по всей высоте обеих рамок (с полями: буква так не тянется)
            top, bottom = min(y0, b[1]), max(y1, b[1] + b[3])
            if rgb is not None and len(pixels.split_columns(
                    rgb, (x1 - hmin, top, max(3 * (bottom - top), b[0] - x1 + 2 * hmin), bottom - top))) > 1:
                continue
            x0, y0, x1, y1 = min(x0, b[0]), min(y0, b[1]), max(x1, b[0] + b[2]), max(y1, b[1] + b[3])
            score = max(score, b[4])
            used[j] = True
        out.append((x0, y0, x1 - x0, y1 - y0, score, None))
    return out


def _split(rgb, box: Box) -> List[Box]:
    """Горизонтальная строка, разрезанная по чертам рамок внутри неё (соседние вкладки, кнопки)."""
    if box[5] is not None:
        return [box]
    return [(*part, box[4], None) for part in pixels.split_columns(rgb, box[:4])]


def _separated(rgb, a: Line, b: Line) -> bool:
    """Между соседними по строке словами — черта рамки: это разные надписи."""
    return pixels.rule_beside(rgb, a, b)


def merge_row(lines: List[Line], rgb=None) -> List[Line]:
    """Слова одной строки, которые детектор разделил (другой шрифт, широкий пробел), — в одну строку:
    «peculiar» + «sight on the road.» → «peculiar sight on the road.»"""
    lines = sorted(lines, key=lambda ln: (ln.x, ln.y))
    used = [False] * len(lines)
    out: List[Line] = []
    for i, a in enumerate(lines):
        if used[i]:
            continue
        group = [a]
        used[i] = True
        changed = True
        while changed:
            changed = False
            gx0, gx1 = min(g.x for g in group), max(g.x + g.w for g in group)
            gy0, gy1 = min(g.y for g in group), max(g.y + g.h for g in group)
            gh = sum(g.h for g in group) / len(group)
            if a.rot is not None:
                break
            for j, b in enumerate(lines):
                if used[j] or b.ru != a.ru or b.rot is not None:
                    continue
                overlap = min(gy1, b.y + b.h) - max(gy0, b.y)
                hmin, hmax = min(gh, b.h), max(gh, b.h)
                gap = max(b.x - gx1, gx0 - (b.x + b.w))
                if overlap >= 0.6 * hmin and hmax <= 1.7 * hmin and -0.3 * hmin <= gap <= 0.6 * hmin and \
                        not any(_separated(rgb, g, b) for g in group):
                    group.append(b)
                    used[j] = True
                    changed = True
                    break
        if len(group) == 1:
            out.append(a)
            continue
        group.sort(key=lambda g: g.x)
        x0, y0 = min(g.x for g in group), min(g.y for g in group)
        x1, y1 = max(g.x + g.w for g in group), max(g.y + g.h for g in group)
        ln = Line(" ".join(g.text for g in group), x0, y0, x1 - x0, y1 - y0)
        ln.conf = min(g.conf for g in group)
        ln.ru = a.ru
        out.append(ln)
    return sorted(out, key=lambda ln: (ln.y, ln.x))
