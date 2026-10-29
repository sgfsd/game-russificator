"""Текст из TextAsset'ов Unity: JSON, CSV/TSV, XML, Ink, Yarn и простой текст.

Многие игры держат диалоги и таблицы локализации не в сценах, а в текстовых
ассетах. Игра читает такой файл и выводит строку как есть — значит, XUnity
найдёт её перевод в словаре по точному совпадению. Отсюда берём только то,
что похоже на текст для игрока: фразы и короткие подписи в полях с «текстовыми»
именами (text, title, description…). Файлы игры не меняются.

Форматы:
  * JSON — все строковые значения; Ink (``inkVersion``) — реплики и варианты выбора (куски одной
    строки склеены, строка со вставкой значения — ещё и шаблоном ``Hello, {0}.``, ``Имя:: текст``
    разделено на имя и текст, из тегов ``#ключ текст`` взят текст), см. :func:`ink_texts`;
  * CSV/TSV — колонка английского текста по заголовку (en, english, text…),
    иначе ячейки, похожие на фразы;
  * XML — текст элементов и значения атрибутов, похожие на фразы;
  * Yarn/простой текст — строки целиком и реплики «Имя: текст».
  * двоичные таблицы (Luban, BinaryWriter, protobuf) — строки с длиной впереди, см. :func:`binary_texts`.
HTML (страницы встроенного браузера) пропускается.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import unicodedata
from typing import Callable, Iterator, List, Optional, Tuple

from ...translation.filters import looks_like_sentence, looks_spoken, looks_technical, looks_translatable

AddFn = Callable[[str], None]
WantFn = Callable[..., bool]

log = logging.getLogger("russificator.unity")

#: больше — не текст, а мусор/видео; диалоги целой игры одним файлом (Ink, JSON) бывают по 30–100 МБ
MAX_SIZE = 512 * 1024 * 1024
#: ключи JSON/атрибуты, в которых короткая строка — скорее всего подпись интерфейса
_LABEL_KEY_RE = re.compile(r"text|title|label|caption|header|desc|message|msg|hint|tip|name|button|option|"
                           r"line|dialog|speech|say|content|question|answer|choice|quest|objective|note|"
                           r"subtitle|tooltip|prompt|body|summary|localiz|english|^en(_|-|$)", re.I)
_EN_COLUMN_RE = re.compile(r"^(en|eng|english|en[-_](us|gb)|text|value|source|original|default|string)$", re.I)
_SPEAKER_RE = re.compile(r"^\s*([A-Z][\w .'-]{0,30}):\s+(.+)$")
#: идентификатор строки в таблице локализации: LBL_CRYO_POD, menu.start, ui/settings/title
_ID_RE = re.compile(r"^[A-Za-z][\w.\-/:]*$")


def _loc_table(pairs) -> bool:
    """JSON — таблица локализации «идентификатор -> текст»: тогда все её значения — текст для игрока,
    и короткие подписи («Cryo pod», «Difficulty») берутся при любом имени ключа."""
    if len(pairs) < 8:
        return False
    ids = sum(1 for k, _ in pairs if _ID_RE.match(k) and not k.isdigit())
    # обычный текст: есть слово со строчной буквы или фраза с точкой — не список «Имя Фамилия» (титры)
    texty = sum(1 for _, v in pairs if re.search(r"\s[a-z]", v) or v.rstrip()[-1:] in ".!?:…")
    return ids >= 0.8 * len(pairs) and texty >= 0.4 * len(pairs)


def _wanted(value: str, key: str = "") -> bool:
    v = value.strip()
    if not v or len(v) > 2500 or looks_technical(v) or not looks_translatable(v, strict=True):
        return False
    if looks_like_sentence(v):
        return True
    words = v.split()
    return bool(key and _LABEL_KEY_RE.search(key)) and 1 <= len(words) <= 5 and v[:1].isupper()


def decode(raw: bytes) -> Optional[str]:
    """Текст ассета или None (двоичные данные, чужая кодировка)."""
    if not raw:
        return None
    if len(raw) > MAX_SIZE:
        log.warning("Текстовый ассет %d МБ пропущен: больше %d МБ", len(raw) >> 20, MAX_SIZE >> 20)
        return None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encoding = "utf-16"
    elif raw.count(b"\0") > len(raw) // 100:
        return None                                   # двоичные данные
    else:
        encoding = "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        return None
    # управляющие байты посреди «текста» — это длины строк двоичной таблицы (Luban): её разбирает
    # binary_texts, а построчно вышли бы обрывки
    controls = sum(1 for c in text[:200000] if c < " " and c not in "\t\n\r")
    return None if controls > max(2, min(len(text), 200000) // 200) else text


# ---------- двоичные таблицы: строки с длиной впереди ----------
#
# Таблицы локализации и конфиги часто лежат двоичным ассетом: Luban (китайские игры: lang_tbtextmapper
# «ключ, китайский, тайваньский, английский» — весь текст CardCultivation), BinaryWriter .NET,
# protobuf, MessagePack. Формат у всех один по сути: длина, затем UTF-8. Строки идут подряд, между ними —
# числа; длина записана коротким числом (1–5 байт) или int32. Текст выбирается дальше теми же
# фильтрами, что и везде.

#: двоичный ассет больше этого — скорее модель, звук или картинка, а не таблица строк
MAX_BINARY = 64 * 1024 * 1024
_MAX_STR = 20000


def _len_compact(raw: bytes, i: int) -> Optional[Tuple[int, int]]:
    """Длина в формате Luban (старшие биты первого байта — сколько байт дальше): (длина, байт заголовка)."""
    h = raw[i]
    if h < 0x80:
        return h, 1
    if h < 0xC0 and i + 1 < len(raw):
        return ((h & 0x3F) << 8) | raw[i + 1], 2
    if h < 0xE0 and i + 2 < len(raw):
        return ((h & 0x1F) << 16) | (raw[i + 1] << 8) | raw[i + 2], 3
    return None


def _len_varint(raw: bytes, i: int) -> Optional[Tuple[int, int]]:
    """Длина 7-битным числом (BinaryWriter .NET, protobuf): младшие 7 бит первыми."""
    value = shift = 0
    for k in range(3):
        if i + k >= len(raw):
            return None
        b = raw[i + k]
        value |= (b & 0x7F) << shift
        if b < 0x80:
            return value, k + 1
        shift += 7
    return None


def _len_int32(raw: bytes, i: int) -> Optional[Tuple[int, int]]:
    if i + 4 > len(raw):
        return None
    return int.from_bytes(raw[i:i + 4], "little"), 4


_PREFIXES = (_len_compact, _len_varint, _len_int32)


def _text_char(c: str) -> bool:
    """Знак, который бывает в тексте: печатный, перевод строки и табуляция, неразрывный и нулевой
    ширины пробелы («Sea\\xa0Embrace», «Skyrend\\u200b» — частые остатки копирования из редакторов)."""
    return c.isprintable() or c in "\n\r\t" or unicodedata.category(c) in ("Zs", "Cf")


class _Scan:
    """Двоичные данные и, для каждого байта, где начинается ближайший управляющий байт (\\0, \\x01…):
    строка с ним внутри — не текст, и такая проверка не требует декодировать кандидата."""

    def __init__(self, raw: bytes):
        import numpy as np
        self.raw = raw
        a = np.frombuffer(raw, dtype=np.uint8)
        bad = ((a < 0x20) & (a != 9) & (a != 10) & (a != 13)) | (a == 0x7F)
        # «\r» без «\n» следом — это длина 13 (ключ «Story_20221_6»), а не конец строки текста
        bad[:-1] |= (a[:-1] == 13) & (a[1:] != 10)
        if len(a):
            bad[-1] |= a[-1] == 13
        idx = np.where(bad, np.arange(len(a)), len(a))
        self.next_bad = np.minimum.accumulate(idx[::-1])[::-1] if len(a) else idx
        self.cache: dict = {}

    def string_at(self, i: int) -> Optional[Tuple[str, int]]:
        """Строка с длиной впереди, начинающаяся с байта ``i``: (строка, конец) или None."""
        if i in self.cache:
            return self.cache[i]
        raw, best = self.raw, None
        for read in _PREFIXES:
            got = read(raw, i)
            if got is None:
                continue
            n, head = got
            start, end = i + head, i + head + n
            if n < 1 or n > _MAX_STR or end > len(raw) or (best is not None and end <= best[1]):
                continue
            if start >= len(raw) or self.next_bad[start] < end:
                continue
            try:
                s = raw[start:end].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if s.strip() and any(c.isalpha() for c in s) and all(_text_char(c) for c in s):
                best = (s, end)
        self.cache[i] = best
        return best

    def chain(self, end: int, depth: int = 4) -> int:
        """Сколько строк подряд начинается сразу за концом ``end`` (до ``depth``)."""
        n = 0
        while n < depth and end < len(self.raw):
            hit = self.string_at(end)
            if hit is None:
                break
            end = hit[1]
            n += 1
        return n


def binary_strings(raw: bytes) -> List[str]:
    """Строки двоичной таблицы по порядку (только похожие на текст: печатные, с буквами).

    Найденная строка пропускается целиком. Разбор бывает неоднозначен: длина 32–126 записана печатным
    байтом, поэтому и байт числа перед строкой («3» и три первые буквы настоящей строки), и первая буква
    ключа («n» = 110 байт, захватывающие три записи) читаются как строка. Из вариантов, начинающихся в
    нескольких байтах друг от друга, верен тот, за которым следующие строки идут цепочкой без разрывов:
    в таблице строки лежат подряд, а у ложного варианта конец приходится на середину чужой строки."""
    if len(raw) > MAX_BINARY:
        return []
    scan = _Scan(raw)
    out: List[str] = []
    i, n = 0, len(raw)
    while i < n:
        hit = scan.string_at(i)
        if hit is None:
            i += 1
            continue
        rivals = [h for h in (scan.string_at(j) for j in range(i + 1, min(i + 5, n)))
                  if h is not None and h[1] > hit[1]]
        if rivals:
            best = scan.chain(hit[1])
            for r in rivals:
                score = scan.chain(r[1])
                if score > best:
                    hit, best = r, score
        out.append(hit[0])
        i = hit[1]
        if len(scan.cache) > 100000:
            scan.cache = {k: v for k, v in scan.cache.items() if k >= i}
    return out


#: письменности, по которым видно, что рядом лежат переводы строки на другие языки
#: (полноширинные знаки «：，» сюда не входят — ими пишут и английский текст китайские разработчики)
_FOREIGN_RE = re.compile(r"[Ѐ-ӿͰ-Ͽ֐-ۿ฀-๿぀-ヿ㐀-鿿가-힯]")


def foreign(text: str) -> bool:
    """Строка на языке с другой письменностью (китайский, японский, корейский, русский, греческий…)."""
    return bool(_FOREIGN_RE.search(text))


def binary_texts(raw: bytes) -> Iterator[Tuple[str, bool]]:
    """Строки двоичной таблицы: (строка, рядом_перевод). ``рядом_перевод`` — соседняя строка на другой
    письменности: это английский вариант строки многоязычной таблицы, и его берут, даже если это одно
    слово («Skip», «Recover») или подпись без точки («Basic Settings»)."""
    strings = binary_strings(raw)
    for k, s in enumerate(strings):
        if foreign(s):
            continue
        near = any(0 <= j < len(strings) and foreign(strings[j]) for j in (k - 1, k + 1))
        yield s, near


def extract(text: str, add: AddFn, seen: Optional[AddFn] = None, tables: bool = True,
            prefix: Optional[AddFn] = None) -> None:
    """Передать в ``add`` строки для перевода из текста ассета. ``seen`` получает все строки-кандидаты
    до отбора (для проверки полноты перевода: что было в файле, но отброшено). ``tables=False`` — не
    считать файл таблицей локализации (титры, списки спонсоров: там имена людей). ``prefix`` получает
    подписи «Имя:: », которые игра может показать вместе с репликой (для правила-разделителя)."""
    def want(value: str, key: str = "", trusted: bool = False) -> bool:
        if seen is not None:
            seen(value)
        if trusted:
            # реплика сценария — текст для игрока по самому месту, где лежит: и «sure», и «Y-You…!»
            return looks_spoken(value)
        return _wanted(value, key)

    head = text.lstrip()[:200].lower()
    if head.startswith(("<!doctype html", "<html")) or "<body" in head:
        return                                        # страница браузера: XUnity её не видит
    if head[:1] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            if isinstance(data, dict) and "inkVersion" in data:
                _ink(data, add, want, prefix)
                return
            pairs = list(_json_strings(data, ""))
            table = tables and _loc_table(pairs)
            for key, value in pairs:
                if table:
                    key = "text"                       # таблица локализации: всё — текст интерфейса
                if want(value, key):
                    add(value)
            return
    if head.startswith("<"):
        _xml(text, add, want)
        return
    if _csv(text, add, want):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("//", "#", "<<", "->", "===", "---")):
            continue
        m = _SPEAKER_RE.match(line)
        if m and want(m.group(2), "text"):
            add(m.group(2).strip())                    # Yarn/скрипты: «Имя: реплика»
        elif want(line):
            add(line)


#: «Narrator:: текст», «Hoove: текст» — имя говорящего перед репликой (игра обычно показывает их отдельно)
_INK_SPEAKER_RE = re.compile(r"^([A-Z][\w.'&-]*(?: [\w.'&-]+){0,3})\s*(::?)\s+(\S[\s\S]*)$")
#: «tinfoilhat:: текст» — «::» в Ink всегда отделяет говорящего, и ник бывает со строчной
_INK_SPEAKER2_RE = re.compile(r"^([\w.'&-]+(?: [\w.'&-]+){0,3})\s*(::)\s+(\S[\s\S]*)$")
#: тег «#ключ текст» / «#Ключ: текст»: ключ — идентификатор (null_hate_phrase, title:, speaker)
_INK_TAG_RE = re.compile(r"^\s*([A-Za-z_][\w.-]*)(?:\s*:\s*|\s+)(\S[\s\S]*)$")
#: элементы Ink, после которых поток текста уходит в другое место: строка кончается
_INK_JUMPS = ("->", "f()", "->t->", "x()")


class _Slot:
    """Место вставки значения (``ev … out /ev``) внутри реплики Ink."""


_SLOT = _Slot()


def ink_texts(story: dict) -> Iterator[Tuple[str, str]]:
    """Текст для игрока из скомпилированной Ink-истории: пары (вид, текст), вид — ``line``, ``choice``,
    ``template`` или ``tag``.

    В JSON Ink текст — строки ``^…``, но не всякая такая строка видна игроку и не всякая видимая
    строка лежит целиком:
      * ``#``, ``^тег``, ``/#`` — теги: чаще команды игре (звук, поза, локация), но бывает и текст для
        экрана («#null_hate_phrase Just a box.» — описание предмета) — отдаются видом ``tag``;
      * ``ev``, ``str``, ``^…``, ``/str``, ``/ev`` — строка-значение: текст варианта выбора, если блок
        кончается точкой выбора ``{"*": …}``, иначе значение для сравнения (``"^love" ==``) — не текст;
      * реплика может состоять из нескольких кусков ``^…`` до ``\n`` (между ними теги, условия):
        игра показывает их одной строкой — она и берётся; если внутри вставка значения (``out``: имя
        игрока, число), целиком строку не собрать — берутся куски и шаблон ``Hello, {0:v}.`` (по нему
        словарь XUnity переводит строку с любым подставленным значением, само значение — как есть).
    """
    stack: List[list] = []
    root = story.get("root")
    if isinstance(root, list):
        stack.append(root)
    while stack:
        cont = stack.pop()
        state = {"buf": []}

        def flush():
            buf = state["buf"]
            texts = [p for p in buf if p is not _SLOT]
            if texts:
                if any(p is _SLOT for p in buf):
                    for piece in texts:
                        yield "line", piece
                    parts, n = [], 0
                    for p in buf:
                        if p is _SLOT:
                            parts.append("{%d:v}" % n)       # «:v» — значение вставляется как есть
                            n += 1
                        else:
                            parts.append(p.replace("{", "{{").replace("}", "}}"))
                    yield "template", "".join(parts)
                elif len(texts) == 1:
                    yield "line", texts[0]
                else:
                    yield "line", "".join(texts)
            state["buf"] = []

        in_tag = in_ev = in_str = False
        tag: List[str] = []
        strs: List[str] = []
        cur: List[str] = []
        for item in cont:
            if isinstance(item, str):
                if in_tag:
                    if item == "/#":
                        in_tag = False
                        if tag:
                            yield "tag", "".join(tag)
                    elif item.startswith("^"):
                        tag.append(item[1:])
                    continue
                if item == "#":
                    in_tag, tag = True, []
                    continue
                if in_ev:
                    if item == "str":
                        in_str, cur = True, []
                    elif item == "/str":
                        in_str = False
                        strs.append("".join(cur))
                    elif in_str and item.startswith("^"):
                        cur.append(item[1:])
                    elif item == "out":
                        state["buf"].append(_SLOT)   # вставка значения в реплику
                    elif item == "/ev":
                        in_ev = False
                    continue
                if item == "ev":
                    in_ev, strs = True, []
                elif item.startswith("^"):
                    state["buf"].append(item[1:])
                elif item == "\n":
                    yield from flush()
                elif item != "<>":                   # «<>» — склейка: строка продолжается
                    yield from flush()
            elif isinstance(item, dict):
                if isinstance(item.get("#"), str):   # тег старого формата: {"#": "текст"}
                    yield "tag", item["#"]
                if "*" in item:                      # точка выбора: строки блока — текст варианта
                    if strs:
                        yield "choice", "".join(strs)
                        if len(strs) > 1:
                            for piece in strs:
                                yield "choice", piece
                    strs = []
                elif any(k in item for k in _INK_JUMPS):
                    yield from flush()
                for k, v in item.items():
                    if isinstance(v, list) and k != "#":
                        stack.append(v)
            elif isinstance(item, list):
                yield from flush()
                stack.append(item)
        yield from flush()


def _speaker(line: str):
    return _INK_SPEAKER_RE.match(line) or _INK_SPEAKER2_RE.match(line)


def _ink(story: dict, add: AddFn, want: WantFn, prefix: Optional[AddFn] = None) -> None:
    for kind, text in ink_texts(story):
        line = text.strip()
        if not line:
            continue
        if kind == "tag":
            # текст тега для экрана — после ключа-идентификатора; сам тег-команда — не текст
            m = _INK_TAG_RE.match(line)
            if m and ("_" in m.group(1) or m.group(1).islower() or ":" in line[:m.start(2)]):
                body = m.group(2).strip()
                if want(body, m.group(1)):
                    add(body)
            elif looks_like_sentence(line) and want(line):
                add(line)
            continue
        m = _speaker(line) if kind in ("line", "template") else None
        if m:
            name, sep, body = m.group(1), m.group(2), m.group(3).strip()
            if want(body, "text", trusted=kind == "line"):
                add(body)                            # игра показывает имя и реплику раздельно
            if kind == "line" and want(name, "name"):
                add(name)
            if prefix is not None:
                prefix(f"{name}{sep} ")              # а если вместе — разделитель XUnity по подписи
            continue
        if want(line, "text", trusted=kind != "template"):
            add(line)


def _json_strings(node, key: str, depth: int = 0) -> Iterator:
    if depth > 40:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                yield str(k), v
            elif isinstance(v, (dict, list)):
                yield from _json_strings(v, str(k), depth + 1)
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, str):
                yield key, v
            elif isinstance(v, (dict, list)):
                yield from _json_strings(v, key, depth + 1)


def _csv(text: str, add: AddFn, want: WantFn = _wanted) -> bool:
    lines = text.splitlines()
    if len(lines) < 2:
        return False
    first = lines[0]
    delim = "\t" if first.count("\t") >= 1 else "," if first.count(",") >= 1 else ";" if first.count(";") >= 1 else None
    if delim is None:
        return False
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    except csv.Error:
        return False
    widths = [len(r) for r in rows[:50] if r]
    if not widths or max(widths) < 2 or widths.count(widths[0]) < len(widths) * 0.8:
        return False                                  # не таблица, а текст с запятыми
    header = [h.strip() for h in rows[0]]
    cols = [i for i, h in enumerate(header) if _EN_COLUMN_RE.match(h)]
    body = rows[1:] if cols else rows
    for row in body:
        for i, cell in enumerate(row):
            if cols and i not in cols:
                continue
            if want(cell, header[i] if cols and i < len(header) else ""):
                add(cell)
    return True


_XML_TEXT_RE = re.compile(r">([^<>]+)<")
_XML_ATTR_RE = re.compile(r"""\s([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _xml(text: str, add: AddFn, want: WantFn = _wanted) -> None:
    import html
    for m in _XML_TEXT_RE.finditer(text):
        value = html.unescape(m.group(1)).strip()
        if want(value, "text"):
            add(value)
    for m in _XML_ATTR_RE.finditer(text):
        value = html.unescape(m.group(2) if m.group(2) is not None else m.group(3) or "")
        if want(value, m.group(1)):
            add(value)
