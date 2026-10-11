"""Бандлы Addressables: проверка контрольной суммы после правки бандла.

Игры на Addressables кладут ресурсы в ``StreamingAssets/aa/<платформа>/*.bundle``, а в каталоге
``StreamingAssets/aa/catalog.json`` для каждого бандла может быть записан CRC. Если он не ноль,
Unity сверяет его при загрузке и отказывается грузить изменённый бандл — после дорисовки
кириллицы в шрифт из бандла игра осталась бы без этого шрифта (или без всего бандла).

CRC хранится в ``m_ExtraDataString`` (base64 от двоичных записей с JSON внутри, строки — UTF-16
или UTF-8). Значение заменяется нулём той же длины (``"m_Crc":0`` и пробелы), поэтому длины
записей не меняются и каталог остаётся корректным; ноль значит «не проверять».
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Iterable, Optional, Set

log = logging.getLogger("russificator.unity.addressables")


def _u16(text: str) -> bytes:
    return re.escape(text.encode("utf-16-le"))


#: «"m_Crc":<цифры>» в UTF-16 и в UTF-8 — ищется прямо в байтах: строки в двоичных записях
#: не обязательно выровнены по двум байтам
_SP16 = b"(?:" + _u16(" ") + b")*"
_CRC16 = re.compile(_u16('"m_Crc"') + _SP16 + _u16(":") + _SP16 + b"((?:[0-9]\x00)+)")
_CRC8 = re.compile(rb'"m_Crc" *: *([0-9]+)')


def catalog_for(bundle: Path) -> Optional[Path]:
    """Каталог Addressables, к которому относится бандл (или None, если бандл не из Addressables)."""
    for parent in Path(bundle).parents:
        cat = parent / "catalog.json"
        if parent.name == "aa" and cat.is_file():
            return cat
        if parent.name == "StreamingAssets":
            break
    return None


def zero_crc_bytes(raw: bytes) -> bytes:
    """Записи каталога с нулевыми CRC (длина не меняется)."""
    def fix(m: "re.Match", zero: bytes, space: bytes) -> bytes:
        n = len(m.group(1)) // len(zero)
        return m.group(0)[:m.start(1) - m.start(0)] + zero + space * (n - 1)
    raw = _CRC16.sub(lambda m: fix(m, "0".encode("utf-16-le"), " ".encode("utf-16-le")), raw)
    return _CRC8.sub(lambda m: fix(m, b"0", b" "), raw)


def zero_crc(text: str) -> str:
    """JSON-каталог с нулевыми CRC во всех записях ``m_ExtraDataString``."""
    def blob(match: "re.Match") -> str:
        raw = base64.b64decode(match.group(2))
        new = zero_crc_bytes(raw)
        if new == raw or len(new) != len(raw):
            return match.group(0)
        return match.group(1) + base64.b64encode(new).decode("ascii") + match.group(3)
    return re.sub(r'("m_ExtraDataString"\s*:\s*")([A-Za-z0-9+/=]*)(")', blob, text)


def disable_crc(bundles: Iterable[Path], backup) -> int:
    """Выключить проверку CRC в каталогах Addressables изменённых бандлов. Возвращает число каталогов."""
    done: Set[Path] = set()
    for b in bundles:
        cat = catalog_for(b)
        if cat is None or cat in done:
            continue
        done.add(cat)
        try:
            text = cat.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("каталог Addressables не прочитан: %s", exc)
            continue
        new = zero_crc(text)
        if new != text:
            backup.write_bytes(cat, new.encode("utf-8"))   # как было, без перевода строк в CRLF
            log.info("Addressables: проверка CRC выключена (%s)", cat)
    return len(done)
