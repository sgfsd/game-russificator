"""Встроенный браузер ZenFulcrum (ZFBrowser) в Unity-играх: HTML-страницы внутри игры.

Игры со «встроенным интернетом» (Welcome to the Game и др.) показывают
страницы через Chromium, а сами страницы лежат в пакете ресурсов
``*_Data/Resources/browser_assets`` (формат ``zfbRes_v1``: строка-заголовок,
число файлов, оглавление «имя / смещение int64 / длина int32», данные).
XUnity такой текст не видит — он рисуется браузером, а не Unity, — поэтому
страницы переводятся прямо в пакете.

Переводится только видимый текст: теги, атрибуты, комментарии, скрипты и
стили не трогаются (в них игровые механики — скрытые хэши, кликабельные
метки). Внутристрочные теги (<a>, <b>, <em>…) остаются внутри фразы как
разметка, которую переводчик обязан сохранить. Русские буквы записываются
числовыми HTML-сущностями — страница отобразится правильно при любой
кодировке.
"""

from __future__ import annotations

import html
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MAGIC = b"\tzfbRes_v1"

BLOCK_TAGS = {"html", "head", "body", "title", "div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol",
              "table", "tr", "td", "th", "tbody", "thead", "tfoot", "pre", "br", "hr", "section", "article",
              "header", "footer", "nav", "form", "button", "label", "option", "select", "textarea", "blockquote",
              "dd", "dt", "dl", "img", "input", "center", "aside", "main", "figure", "figcaption", "iframe",
              "caption", "fieldset", "legend", "meta", "link"}
_TOKEN_RE = re.compile(r"<!--.*?-->|<(script|style)\b[^>]*>.*?</\1\s*>|<[^>]+>|[^<]+", re.S | re.I)
_TAG_NAME_RE = re.compile(r"</?\s*([a-zA-Z0-9]+)")


# ---------- пакет ----------

def _read_str(data: bytes, pos: int) -> Tuple[str, int]:
    n = shift = 0
    while True:
        b = data[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            break
    return data[pos:pos + n].decode("utf-8"), pos + n


def _write_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    n = len(raw)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + raw


def is_pack(path: Path) -> bool:
    try:
        with Path(path).open("rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def find_packs(data_dir: Path) -> List[Path]:
    res = Path(data_dir) / "Resources"
    candidates = list(res.glob("*")) if res.is_dir() else []
    return [p for p in candidates if p.is_file() and is_pack(p)]


def read_pack(path: Path) -> Tuple[str, List[Tuple[str, bytes]]]:
    data = Path(path).read_bytes()
    header, pos = _read_str(data, 0)
    count = struct.unpack_from("<i", data, pos)[0]
    pos += 4
    files = []
    for _ in range(count):
        name, pos = _read_str(data, pos)
        off, length = struct.unpack_from("<qi", data, pos)
        pos += 12
        files.append((name, data[off:off + length]))
    return header, files


def write_pack(header: str, files: List[Tuple[str, bytes]]) -> bytes:
    index_size = len(_write_str(header)) + 4 + sum(len(_write_str(n)) + 12 for n, _ in files)
    out = bytearray(_write_str(header) + struct.pack("<i", len(files)))
    off = index_size
    for name, blob in files:
        out += _write_str(name) + struct.pack("<qi", off, len(blob))
        off += len(blob)
    for _, blob in files:
        out += blob
    return bytes(out)


# ---------- текст страниц ----------

@dataclass
class Unit:
    start: int   # границы фразы в исходном HTML
    end: int
    source: str  # текст для перевода: сущности раскрыты, пробелы схлопнуты, внутристрочные теги как есть


def units(page: str) -> List[Unit]:
    """Фразы страницы в порядке появления (детерминированно — внедрение опирается на порядок)."""
    out: List[Unit] = []
    cur: List[Tuple[int, int, str, bool]] = []   # (start, end, текст, это_тег)

    def flush() -> None:
        # по краям убираем только пустой текст; теги остаются во фразе — разметка сбалансирована
        while cur and not cur[-1][3] and not cur[-1][2].strip():
            cur.pop()
        while cur and not cur[0][3] and not cur[0][2].strip():
            cur.pop(0)
        if cur and any(not t[3] and t[2].strip() for t in cur):
            parts = []
            for _, _, text, is_tag in cur:
                parts.append(text if is_tag else html.unescape(text))
            src = re.sub(r"\s+", " ", "".join(parts)).strip()
            first = next(t for t in cur if t[3] or t[2].strip())
            lead = len(first[2]) - len(first[2].lstrip()) if not first[3] else 0
            last = cur[-1]
            trail = len(last[2]) - len(last[2].rstrip()) if not last[3] else 0
            out.append(Unit(first[0] + lead, last[1] - trail, src))
        cur.clear()

    for m in _TOKEN_RE.finditer(page):
        tok = m.group(0)
        if tok.startswith("<"):
            if tok.startswith("<!--") or m.group(1):
                flush()
                continue
            tm = _TAG_NAME_RE.match(tok)
            name = tm.group(1).lower() if tm else ""
            if name in BLOCK_TAGS or tok.startswith("<!"):
                flush()
            else:
                cur.append((m.start(), m.end(), tok, True))
        else:
            cur.append((m.start(), m.end(), tok, False))
    flush()
    return out


def encode_text(translation: str) -> str:
    """Перевод -> HTML: текст экранирован, не-ASCII — числовыми сущностями, внутристрочные теги как есть."""
    out = []
    for m in re.finditer(r"<[^>]+>|[^<]+", translation):
        tok = m.group(0)
        if tok.startswith("<"):
            out.append(tok)
        else:
            esc = html.escape(tok, quote=False)
            out.append("".join(ch if ord(ch) < 128 else f"&#{ord(ch)};" for ch in esc))
    return "".join(out)


def apply_page(page: str, translations: Dict[int, str]) -> str:
    """Подставить переводы фраз {номер фразы: перевод} в страницу."""
    parts = []
    pos = 0
    for i, u in enumerate(units(page)):
        tr = translations.get(i)
        if tr is None:
            continue
        parts.append(page[pos:u.start])
        parts.append(encode_text(tr))
        pos = u.end
    parts.append(page[pos:])
    return "".join(parts)


def decode_page_enc(blob: bytes) -> Optional[Tuple[str, str]]:
    """(текст страницы, кодировка) — чтобы записать страницу обратно в той же кодировке."""
    for enc in ("utf-8", "cp1252"):
        try:
            return blob.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return None


def decode_page(blob: bytes) -> Optional[str]:
    res = decode_page_enc(blob)
    return res[0] if res else None
