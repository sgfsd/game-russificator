"""Варианты кадра для глубокого прохода распознавания.

Распознавание Windows хорошо читает обычный текст интерфейса, но пропускает
надписи на картинках и текстурах: цветной текст на пёстром фоне, обводку,
«штампы», мелкий или пиксельный шрифт. Разные преобразования кадра вытаскивают
разные надписи (проверено на играх): серый с растянутым контрастом, отдельный
цветовой канал, насыщенность (цветной текст на сером фоне становится тёмным на
светлом), увеличение для мелкого шрифта. Результаты вариантов потом сводятся
голосованием (:func:`russificator.overlay.text.merge_deep`).
"""

from __future__ import annotations

from typing import List, Tuple

#: (название, во сколько раз увеличен, пиксели BGRA, ширина, высота)
Variant = Tuple[str, float, bytes, int, int]

#: мелкий кадр (окно игры) увеличиваем: распознавание Windows плохо читает строки ниже ~12 пикселей
UPSCALE_BELOW = 1000


def variants(frame: bytes, w: int, h: int, max_dim: int = 4096) -> List[Variant]:
    """Преобразованные копии кадра, самые полезные — первыми."""
    try:
        import numpy as np
    except ImportError:          # без numpy — только серый и увеличение средствами PIL
        return _variants_pil(frame, w, h, max_dim)
    a = np.frombuffer(frame, dtype=np.uint8, count=w * h * 4).reshape(h, w, 4)
    b, g, r = a[..., 0].astype(np.int16), a[..., 1].astype(np.int16), a[..., 2].astype(np.int16)
    out: List[Variant] = []

    def gray_variant(name: str, ch) -> None:
        ch = _stretch(np, ch)
        if ch is None:
            return
        bgra = np.empty((h, w, 4), dtype=np.uint8)
        bgra[..., 0] = bgra[..., 1] = bgra[..., 2] = ch
        bgra[..., 3] = 255
        out.append((name, 1.0, bgra.tobytes(), w, h))

    lum = (r * 299 + g * 587 + b * 114) // 1000
    gray_variant("контраст", lum)
    sat = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    gray_variant("насыщенность", 255 - sat)
    gray_variant("синий", b)
    gray_variant("красный", r)
    if max(w, h) <= UPSCALE_BELOW and max(w, h) * 2 <= max_dim:
        from PIL import Image
        img = Image.frombuffer("RGBA", (w, h), frame, "raw", "BGRA", 0, 1)
        big = img.resize((w * 2, h * 2), Image.LANCZOS)
        out.insert(1, ("увеличение", 2.0, big.tobytes("raw", "BGRA"), w * 2, h * 2))
    return out


def _stretch(np, ch):
    """Растянуть яркость на весь диапазон (по 1-му и 99-му процентилю); однотонный канал — None."""
    lo, hi = np.percentile(ch[::4, ::4], (1, 99))
    if hi - lo < 24:
        return None
    x = (ch.astype(np.float32) - lo) * (255.0 / (hi - lo))
    return np.clip(x, 0, 255).astype(np.uint8)


def _variants_pil(frame: bytes, w: int, h: int, max_dim: int) -> List[Variant]:
    from PIL import Image, ImageOps
    img = Image.frombuffer("RGBA", (w, h), frame, "raw", "BGRA", 0, 1)
    out: List[Variant] = []
    gray = ImageOps.autocontrast(img.convert("L"), cutoff=1).convert("RGBA")
    out.append(("контраст", 1.0, gray.tobytes("raw", "BGRA"), w, h))
    if max(w, h) <= UPSCALE_BELOW and max(w, h) * 2 <= max_dim:
        big = img.resize((w * 2, h * 2), Image.LANCZOS)
        out.append(("увеличение", 2.0, big.tobytes("raw", "BGRA"), w * 2, h * 2))
    return out


def blank(frame: bytes, w: int, h: int, threshold: int = 8) -> bool:
    """Кадр чёрный (или почти): окно так не снимается, либо в игре тёмный экран загрузки."""
    stride = w * 4
    step = max(1, h // 24)
    for row in range(0, h, step):
        line = frame[row * stride:(row + 1) * stride]
        for c in (0, 1, 2):
            if max(line[c::64], default=0) > threshold:
                return False
    return True


def luma_grid(frame: bytes, w: int, h: int, cols: int = 32, rows: int = 18) -> List[int]:
    """Яркость в узлах сетки — чтобы понять, что картинка сменилась целиком (новая сцена)."""
    stride = w * 4
    out = []
    for gy in range(rows):
        y = min(h - 1, int((gy + 0.5) * h / rows))
        base = y * stride
        for gx in range(cols):
            x = min(w - 1, int((gx + 0.5) * w / cols))
            i = base + x * 4
            out.append((frame[i] * 114 + frame[i + 1] * 587 + frame[i + 2] * 299) // 1000)
    return out


def changed_share(a: List[int], b: List[int], delta: int = 28) -> float:
    """Какая доля узлов сетки заметно поменяла яркость."""
    if not a or len(a) != len(b):
        return 1.0
    return sum(1 for x, y in zip(a, b) if abs(x - y) > delta) / len(a)
