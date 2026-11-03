"""Прямая русификация Unity: перевод фраз вписывается в данные игры, а не подменяется на лету.

XUnity переводит текст в момент, когда игра кладёт его в текстовый компонент. Это не работает,
когда игра рисует текст своим путём: движки визуальных новелл (UTAGE — свой ``UguiNovelText`` с
печатной машинкой), свои генераторы текста, текст, который переписывается каждый кадр. Тогда
перевод нужен в самих данных: игра берёт из базы уже русскую фразу и показывает её как есть.

Что пишется: только фразы (от трёх слов, с пробелами — реплики, описания, подсказки). Короткие
подписи и имена часто служат ключами (персонаж «Yunxin» в команде сценария, термин таблицы), их
перевод сломал бы игру — их по-прежнему подменяет XUnity на лету.

Где:
  * MonoBehaviour в файлах ассетов и обычных бандлах — строки меняются прямо в байтах объекта
    (длина + байты + выравнивание до 4): так можно и без типового дерева (база I2 Localization,
    таблицы UTAGE). Фраза, встречающаяся в одном объекте дважды, пропускается — одна из копий
    может быть ключом;
  * CSV и JSON — файлы в StreamingAssets и TextAsset'ы: меняются ячейки/значения, равные фразе;
  * бандлы Addressables (``StreamingAssets/aa``) — как обычные, а проверка их CRC в каталоге
    выключается (``addressables.disable_crc``). Там таблицы строк пакета Unity Localization: игра
    подставляет в них числа («{0:stat(per|0.#|%|s)} Power Luck» → «+15% Power Luck»), и XUnity с готовой
    строкой не справляется — перевод должен быть в самой таблице (Death Must Die).
Каждый изменённый файл перечитывается и сверяется (те же объекты, перевод на месте) — иначе он
не записывается. Оригиналы — в бэкапе, откат возвращает их.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from ...translation.filters import looks_technical, looks_translatable
from ...translation.lang import cjk_letters, cjk_text
from . import text_assets, unitypy_io

log = logging.getLogger("russificator.unity")

StatusFn = Callable[..., None]


@dataclass
class DirectReport:
    files: int = 0
    strings: int = 0
    skipped_ambiguous: int = 0
    #: записи таблиц Unity Localization других языков, получившие русский
    other_locales: int = 0
    errors: List[str] = field(default_factory=list)


def is_phrase(source: str) -> bool:
    """Фраза для игрока, а не ключ: от трёх слов, с пробелами, не код и не путь. Иероглифами и каной
    (слова без пробелов) — от шести знаков со знаком препинания: ключи и имена так не пишут."""
    s = source.strip()
    if "\x00" not in s and cjk_text(s):
        return (cjk_letters(s) >= 6 and bool(re.search(r"[。、，！？…「」『』!?]", s))
                and looks_translatable(s, strict=True) and not looks_technical(s))
    if len(s) < 12 or "\x00" in s or not re.search(r"\s", s):
        return False
    if len(re.findall(r"[A-Za-z]{2,}", s)) < 3:
        return False
    return looks_translatable(s, strict=True) and not looks_technical(s)


def phrase_map(pairs: Dict[str, str]) -> Dict[str, str]:
    return {s: t for s, t in pairs.items() if t and t != s and is_phrase(s)}


# ---------- строки внутри объекта (без типового дерева) ----------

def _align4(n: int) -> int:
    return (n + 3) & ~3


def patch_raw(data: bytes, mapping: Dict[bytes, bytes],
              only: Optional[Set[int]] = None) -> Tuple[bytes, int, int]:
    """Заменить строки Unity (int32 длина + UTF-8 + нули до кратного 4) по точному совпадению.

    Возвращает (новые данные, заменено, пропущено как неоднозначное). Длина строки после замены
    другая — всё, что дальше, сдвигается на кратное 4, выравнивание полей сохраняется. ``only`` —
    менять только строки по этим смещениям (английский столбец базы I2).
    """
    if not mapping or len(data) < 8:
        return data, 0, 0
    import numpy as np
    lens = np.array(sorted({len(k) for k in mapping}), dtype="<i4")
    found: List[Tuple[int, int, bytes]] = []          # (позиция, длина, ключ)
    for shift in range(4):
        usable = (len(data) - shift) // 4 * 4
        if usable <= 0:
            continue
        ints = np.frombuffer(data, dtype="<i4", count=usable // 4, offset=shift)
        for idx in np.nonzero(np.isin(ints, lens))[0]:
            pos = shift + int(idx) * 4
            n = int(ints[idx])
            body = data[pos + 4:pos + 4 + n]
            if len(body) == n and body in mapping and (only is None or pos in only):
                found.append((pos, n, body))
    if not found:
        return data, 0, 0
    counts: Dict[bytes, int] = {}
    for _, _, body in found:
        counts[body] = counts.get(body, 0) + 1
    out = bytearray()
    last = 0
    done = ambiguous = 0
    for pos, n, body in sorted(found):
        if counts[body] > 1:
            ambiguous += 1
            continue
        end_old = _align4(pos + 4 + n)
        if pos < last or end_old > len(data) or any(data[pos + 4 + n:end_old]):
            continue                                   # не строка: за ней не нули выравнивания
        new = mapping[body]
        out += data[last:pos]
        seg = len(new).to_bytes(4, "little", signed=True) + new
        cur = len(out) + len(seg)
        # выравнивание — от начала объекта: в выходе позиции сдвинуты на кратное 4, как и в исходнике
        out += seg + b"\0" * (_align4(cur) - cur)
        last = end_old
        done += 1
    out += data[last:]
    return bytes(out), done, ambiguous


# ---------- CSV и JSON ----------

def patch_document(text: str, mapping: Dict[str, str]) -> Tuple[str, int]:
    """Перевод фраз в тексте CSV/JSON (Ink и прочие форматы — не трогаются)."""
    head = text.lstrip()[:200]
    if head[:1] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            return text, 0
        if isinstance(data, dict) and "inkVersion" in data:
            return text, 0                             # Ink: реплики собираются из кусков — их переводит XUnity
        n = [0]

        def walk(v):
            if isinstance(v, str):
                t = mapping.get(v)
                if t is not None:
                    n[0] += 1
                    return t
                return v
            if isinstance(v, list):
                return [walk(x) for x in v]
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            return v
        new = walk(data)
        if not n[0]:
            return text, 0
        bom = "﻿" if text.startswith("﻿") else ""
        indent = 2 if "\n" in text.strip()[:2000] else None
        return bom + json.dumps(new, ensure_ascii=False, indent=indent), n[0]
    return _patch_csv(text, mapping)


def _patch_csv(text: str, mapping: Dict[str, str]) -> Tuple[str, int]:
    lines = text.splitlines(keepends=True)
    if len(lines) < 2:
        return text, 0
    first = lines[0]
    delim = "\t" if "\t" in first else "," if "," in first else ";" if ";" in first else None
    if delim is None:
        return text, 0
    out: List[str] = []
    count = 0
    # построчно: если строка CSV многострочная (кавычки с переводом строки) — не трогаем её
    for line in lines:
        body = line.rstrip("\r\n")
        nl = line[len(body):]
        if body.count('"') % 2:
            out.append(line)
            continue
        try:
            cells = next(csv.reader([body], delimiter=delim))
        except (csv.Error, StopIteration):
            out.append(line)
            continue
        raw_cells = _split_raw(body, delim)
        if len(raw_cells) != len(cells):
            out.append(line)
            continue
        changed = False
        for i, cell in enumerate(cells):
            tr = mapping.get(cell)
            if tr is None:
                continue
            quoted = raw_cells[i].startswith('"')
            if quoted:
                raw_cells[i] = '"' + tr.replace('"', '""') + '"'
            elif delim in tr or '"' in tr or "\n" in tr:
                continue                               # без кавычек игра режет по разделителю — не ломаем
            else:
                raw_cells[i] = tr
            changed = True
            count += 1
        out.append(delim.join(raw_cells) + nl if changed else line)
    return "".join(out), count


def _split_raw(body: str, delim: str) -> List[str]:
    """Ячейки строки CSV как они записаны (с кавычками)."""
    cells, cur, quoted = [], [], False
    for ch in body:
        if ch == '"':
            quoted = not quoted
            cur.append(ch)
        elif ch == delim and not quoted:
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    cells.append("".join(cur))
    return cells


# ---------- объекты со структурой (типовое дерево) ----------

#: последнее слово имени поля, по которому значение ищут, а не показывают (questId, voice_key, sceneName)
_KEY_WORDS = {"id", "ids", "key", "keys", "term", "terms", "guid", "name", "names", "path", "paths",
              "file", "files", "tag", "tags", "symbol", "code", "event", "anim", "animation", "sound",
              "scene", "prefab", "asset", "sprite", "icon", "voice", "label", "class", "type", "url",
              "method", "func", "function", "trigger", "state", "layer", "variable", "var", "flag", "param"}


def _key_field(name: str) -> bool:
    if name == "m_Name":
        return True
    words = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name or "")
    return bool(words) and words[-1].lower() in _KEY_WORDS


#: колонка таблицы UTAGE, которую игра показывает при английском языке
_DISPLAY_COLUMNS = ("english", "en", "eng", "english(us)", "en-us")
_LANG_COLUMNS = {"japanese", "chinese", "chinese (simplified)", "chinese (traditional)", "korean", "korea",
                 "turkish", "thai", "french", "german", "spanish", "russian", "portuguese", "italian"}


def _translated(value: str, full: Dict[str, str]) -> Optional[str]:
    t = full.get(value)
    if t is None and value.strip() != value:
        core = full.get(value.strip())
        if core is not None:                            # «текст » — пробелы по краям как в оригинале
            lead = value[: len(value) - len(value.lstrip())]
            t = lead + core + value[len(value.rstrip()):]
    return t


def utage_display_column(header_cells: List[str]) -> Optional[int]:
    """Номер колонки таблицы UTAGE, которую игра показывает при английском языке: English, а если
    колонок языков нет — Text (сценарий на одном языке) или NameText (таблица персонажей). None —
    таблица без текста для экрана."""
    header = [str(h).strip().lower() for h in header_cells]
    col = next((i for i, h in enumerate(header) if h in _DISPLAY_COLUMNS), None)
    if col is None and not _LANG_COLUMNS & set(header):
        col = next((header.index(h) for h in ("text", "nametext") if h in header), None)
    return col


def utage_grid_lists(tree) -> List[list]:
    """Списки таблиц UTAGE в дереве объекта: ``importGridList`` книги (AdvImportBook) и таблицы настроек
    главы (``settingList`` в AdvChapterData: Localize, Character…) — список элементов с ``rows``."""
    out, stack = [], [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for v in node.values():
                if isinstance(v, list) and v and all(isinstance(g, dict) and "rows" in g and "headerRow" in g
                                                     for g in v):
                    out.append(v)
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return out


def utage_grids(grids: list):
    """(строки после заголовка, колонка для экрана) каждой таблицы из списка таблиц UTAGE."""
    for grid in grids:
        rows = (grid.get("rows") or []) if isinstance(grid, dict) else []
        if not rows:
            continue
        head_i = int(grid.get("headerRow") or 0)
        if not 0 <= head_i < len(rows):
            head_i = 0
        col = utage_display_column(rows[head_i].get("strings") or [])
        if col is not None:
            yield rows[head_i + 1:], col


def patch_utage_grids(grids: list, full: Dict[str, str]) -> int:
    """Таблицы сценария UTAGE: перевод — в колонку, которую игра показывает (English; если колонок
    языков нет — Text/NameText). Команды, аргументы (ключи персонажей), голос и метки не трогаются."""
    count = 0
    for rows, col in utage_grids(grids):
        for r in rows:
            cells = r.get("strings")
            if not isinstance(cells, list) or col >= len(cells) or not isinstance(cells[col], str):
                continue
            t = _translated(cells[col], full)
            if t is not None and t != cells[col]:
                cells[col] = t
                count += 1
    return count


def patch_utage_book(tree: dict, full: Dict[str, str]) -> int:
    return patch_utage_grids(tree.get("importGridList") or [], full)


def patch_i2_source(tree: dict, full: Dict[str, str]) -> int:
    """База I2 Localization: перевод — в значение английского языка; ключ термина не трогается."""
    count = 0
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            terms, langs = node.get("mTerms"), node.get("mLanguages")
            if isinstance(terms, list) and isinstance(langs, list):
                names = [str((l or {}).get("Name", "")).strip().lower() if isinstance(l, dict) else ""
                         for l in langs]
                idx = next((i for i, n in enumerate(names) if n.split(" ")[0] in ("english", "en")), None)
                if idx is not None:
                    for term in terms:
                        vals = term.get("Languages") if isinstance(term, dict) else None
                        if isinstance(vals, list) and idx < len(vals) and isinstance(vals[idx], str):
                            t = _translated(vals[idx], full)
                            if t is not None and t != vals[idx]:
                                vals[idx] = t
                                count += 1
                continue
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return count


@dataclass
class I2Table:
    """Столбцы базы I2 Localization: английский — (смещение строки в сырых байтах объекта или -1,
    текст) — и официальный русский игры, если он есть: английский текст -> русский."""
    english: List[Tuple[int, str]]
    russian: Dict[str, str]


def _lang_index(names: List[str], codes: List[str], lang: str, code: str) -> Optional[int]:
    for i, (n, c) in enumerate(zip(names, codes)):
        if n.strip().lower().split(" ")[0] == lang or c.strip().lower().split("-")[0] == code:
            return i
    return None


def _i2_rows(english: Optional[int], russian: Optional[int], rows) -> Optional[I2Table]:
    """rows — (смещения, значения) терминов. Русский берётся, только если в нём есть кириллица."""
    if english is None:
        return None
    out = I2Table([], {})
    for offsets, values in rows:
        if english >= len(values) or not isinstance(values[english], str) or not values[english].strip():
            continue
        en = values[english]
        out.english.append((offsets[english] if offsets else -1, en))
        ru = values[russian] if russian is not None and russian < len(values) else None
        if isinstance(ru, str) and _CYRILLIC_RE.search(ru):
            out.russian.setdefault(en, ru)
    return out if out.english else None


_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def i2_table(tree: dict) -> Optional[I2Table]:
    """База I2 по типовому дереву (mTerms + mLanguages)."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            terms, langs = node.get("mTerms"), node.get("mLanguages")
            if isinstance(terms, list) and isinstance(langs, list):
                names = [str(l.get("Name", "")) if isinstance(l, dict) else "" for l in langs]
                codes = [str(l.get("Code", "")) if isinstance(l, dict) else "" for l in langs]
                rows = [((), t.get("Languages")) for t in terms
                        if isinstance(t, dict) and isinstance(t.get("Languages"), list)]
                return _i2_rows(_lang_index(names, codes, "english", "en"),
                                _lang_index(names, codes, "russian", "ru"), rows)
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def _raw_str(data: bytes, pos: int, limit: int = 1 << 16) -> Optional[Tuple[str, int]]:
    """Строка Unity по смещению: (текст, конец с выравниванием) или None."""
    if pos < 0 or pos + 4 > len(data):
        return None
    n = int.from_bytes(data[pos:pos + 4], "little", signed=True)
    end = _align4(pos + 4 + n)
    if n < 0 or n > limit or end > len(data) or any(data[pos + 4 + n:end]):
        return None
    try:
        return data[pos + 4:pos + 4 + n].decode("utf-8"), end
    except UnicodeDecodeError:
        return None


_LANG_NAME_RE = re.compile(r"[A-Z][^\x00-\x1f]{1,48}")
_LANG_CODE_RE = re.compile(r"(?:[a-z]{2,3}(?:[-_][A-Za-z0-9]{2,4})*)?")


def _raw_i2_languages(data: bytes) -> Optional[Tuple[List[str], List[str]]]:
    """mLanguages в сырых байтах: число языков, затем (Name, Code, Flags — байт с выравниванием)."""
    for m in re.finditer(rb"\x07\x00\x00\x00English", data):
        for back in range(4, 4096, 4):            # число языков — перед первой записью списка
            start = m.start() - back
            if start < 0:
                break
            count = int.from_bytes(data[start:start + 4], "little", signed=True)
            if not 2 <= count <= 200:
                continue
            for flags in (True, False):
                names: List[str] = []
                codes: List[str] = []
                p = start + 4
                for _ in range(count):
                    name = _raw_str(data, p, 64)
                    code = _raw_str(data, name[1], 16) if name else None
                    if not (name and code and _LANG_NAME_RE.fullmatch(name[0])
                            and _LANG_CODE_RE.fullmatch(code[0])):
                        break
                    p = code[1]
                    if flags:
                        if p + 4 > len(data) or int.from_bytes(data[p:p + 4], "little") > 255:
                            break
                        p += 4
                    names.append(name[0])
                    codes.append(code[0])
                if len(names) == count and "English" in names:
                    return names, codes
    return None


def raw_i2(data: bytes) -> Optional[I2Table]:
    """База I2 Localization без типового дерева (Unity 6.3+: генератор typetree не читает их
    метаданные). Раньше такую базу читали сплошняком — и в перевод уходили все её языки (Fractal
    Space: 25 тыс. строк на 22 языках вместо одного английского). Термин в байтах: число языков N,
    N строк (по языку на строку), затем N флагов."""
    if len(data) < 64 or b"English" not in data:
        return None
    langs = _raw_i2_languages(data)
    if langs is None:
        return None
    names, codes = langs
    n = len(names)
    import numpy as np
    ints = np.frombuffer(data, dtype="<i4", count=len(data) // 4)
    rows = []
    resume = 0
    for idx in np.nonzero(ints == n)[0]:
        pos = int(idx) * 4
        if pos < resume:
            continue
        offsets, values, p = [], [], pos + 4
        for _ in range(n):
            s = _raw_str(data, p)
            if s is None:
                break
            offsets.append(p)
            values.append(s[0])
            p = s[1]
        if len(values) == n and p + 4 <= len(data) and int.from_bytes(data[p:p + 4], "little", signed=True) == n:
            rows.append((offsets, values))
            resume = p
    if len(rows) < 3:
        return None
    return _i2_rows(_lang_index(names, codes, "english", "en"), _lang_index(names, codes, "russian", "ru"), rows)


def _string_leaves(tree) -> Iterator[Tuple[object, object, str, str]]:
    """(контейнер, ключ или индекс, путь поля, строка) для строк дерева в полях, имя которых не похоже
    на ключ. Путь — имена полей через «/», без номеров в списках: у всех элементов списка он общий."""
    stack = [(tree, "", "")]
    while stack:
        node, field, path = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                sub = f"{path}/{k}" if path else str(k)
                if isinstance(v, str):
                    if not _key_field(k):
                        yield node, k, sub, v
                elif isinstance(v, (dict, list)):
                    stack.append((v, k, sub))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str):
                    if not _key_field(field):
                        yield node, i, path, v
                elif isinstance(v, (dict, list)):
                    stack.append((v, field, path))


def tree_signature(tree) -> tuple:
    """Опознавательный знак класса объекта без чтения его скрипта — набор и порядок его полей."""
    return tuple(tree) if isinstance(tree, dict) else ()


def text_fields(tree, phrases: Dict[str, str]) -> Set[str]:
    """Поля объекта, где лежит хотя бы одна фраза с переводом, — поля для текста игрока."""
    return {path for _, _, path, v in _string_leaves(tree) if _translated(v, phrases) is not None}


def patch_fields(tree, phrases: Dict[str, str], full: Optional[Dict[str, str]] = None,
                 fields: Iterable[str] = ()) -> int:
    """Прочие объекты: фразы в полях, имя которых не похоже на ключ, а в текстовых полях ``fields``
    (см. :func:`text_fields`) — любая строка с переводом из ``full``.

    Короткая строка в поле скрипта может быть ключом, поэтому обычно переводятся только фразы. Но поле,
    в котором у того же класса встречаются фразы, — поле текста для игрока: у вариантов ответа Fungus
    (MenuOption.text) рядом с «Go back to work.» лежат «Yes», «No», «Thank you», и без BepInEx их больше
    некому перевести."""
    fields = set(fields)
    count = 0
    for node, key, path, v in list(_string_leaves(tree)):
        t = _translated(v, full if full is not None and path in fields else phrases)
        if t is not None and t != v:
            node[key] = t
            count += 1
    return count


def text_component_field(tree) -> Optional[str]:
    """Поле с надписью, если объект — текстовый компонент UI: TextMeshPro/TextMeshProUGUI (``m_text``
    рядом с ``m_fontAsset``), UGUI Text (``m_Text`` рядом с ``m_FontData``) или NGUI UILabel (``mText``
    рядом с ``mFont``/``mTrueTypeFont``)."""
    if not isinstance(tree, dict):
        return None
    if isinstance(tree.get("m_text"), str) and "m_fontAsset" in tree:
        return "m_text"
    if isinstance(tree.get("m_Text"), str) and isinstance(tree.get("m_FontData"), dict):
        return "m_Text"
    if isinstance(tree.get("mText"), str) and ("mFont" in tree or "mTrueTypeFont" in tree):
        return "mText"
    return None


def patch_text_component(tree, full: Dict[str, str]) -> int:
    """Надпись текстового компонента — то, что видит игрок, поэтому переводится любая строка с
    переводом, а не только фразы от трёх слов: короткие надписи («Start», «Settings», «Music volume»)
    иначе ждали бы XUnity в игре, а в игре без BepInEx (урезанные библиотеки, missed messages.) их
    больше некому перевести."""
    field = text_component_field(tree)
    if field is None:
        return 0
    value = tree[field]
    t = _translated(value, full)
    if t is None or t == value:
        return 0
    tree[field] = t
    return 1


def is_string_table(tree) -> bool:
    """Таблица строк пакета Unity Localization (StringTable): ``m_TableData`` — записи ``{m_Id, m_Localized}``,
    рядом ссылка на общие ключи ``m_SharedData``."""
    rows = tree.get("m_TableData") if isinstance(tree, dict) else None
    return (isinstance(rows, list) and "m_SharedData" in tree
            and all(isinstance(r, dict) and "m_Localized" in r for r in rows[:5]))


def patch_string_table(tree: dict, full: Dict[str, str], record: Optional[Dict[int, str]] = None) -> int:
    """Таблица строк Unity Localization: каждая запись — текст для игрока (ключи — в общей таблице), так что
    переводится любая строка с переводом, и короткая. Игра подставляет в запись числа при показе («{0:stat(per|
    0.#|%|s)} Power Luck» → «+15% Power Luck»), и XUnity готовую строку в словаре не найдёт: перевод должен
    быть в самой таблице. ``record`` получает переводы по номеру записи (``m_Id``) — для таблиц других языков
    (:func:`fill_string_table`)."""
    n = 0
    for row in tree["m_TableData"]:
        value = row.get("m_Localized")
        t = _translated(value, full) if isinstance(value, str) else None
        if t is not None and t != value:
            row["m_Localized"] = t
            n += 1
            if record is not None:
                record[int(row.get("m_Id", 0))] = t
    return n


def fill_string_table(tree: dict, by_id: Dict[int, str]) -> int:
    """Таблица того же набора строк на другом языке: русский — по номеру записи. Игра выбирает язык сама
    (Mad Island на русской Windows стартует по-японски — это её язык по умолчанию), и меню должно быть
    русским на любом из её языков."""
    n = 0
    for row in tree["m_TableData"]:
        t = by_id.get(int(row.get("m_Id", 0)))
        if t is not None and row.get("m_Localized") != t:
            row["m_Localized"] = t
            n += 1
    return n


def string_table_key(obj, tree: dict) -> Optional[Tuple[str, int]]:
    """Набор строк, к которому относится таблица: её общая таблица ключей (``m_SharedData``) — одна у всех
    языков, даже из разных бандлов (файл ссылки — по имени CAB, номер объекта — path_id)."""
    ptr = tree.get("m_SharedData") or {}
    fid, pid = int(ptr.get("m_FileID", 0) or 0), int(ptr.get("m_PathID", 0) or 0)
    if not pid:
        return None
    af = getattr(obj, "assets_file", None)
    if fid == 0:
        name = getattr(af, "name", "") or ""
    else:
        ext = getattr(af, "externals", None) or []
        if fid - 1 >= len(ext):
            return None
        name = getattr(ext[fid - 1], "path", "") or ""
    return re.split(r"[\\/]", name)[-1].lower(), pid


def patch_tree(tree: dict, full: Dict[str, str], phrases: Dict[str, str], fields: Iterable[str] = ()) -> int:
    """Перевести строки дерева объекта. ``fields`` — его текстовые поля, узнанные по фразам в объектах
    того же класса (:func:`text_fields`)."""
    grid_lists = utage_grid_lists(tree)
    if grid_lists:
        return sum(patch_utage_grids(grids, full) for grids in grid_lists)
    if is_string_table(tree):
        return patch_string_table(tree, full)
    n = patch_i2_source(tree, full)
    if n:
        return n
    return patch_text_component(tree, full) + patch_fields(tree, phrases, full, fields)


def typetree_donors(files: List[Path]) -> Dict[Tuple[str, str], object]:
    """Точные структуры классов из файлов, где они записаны (бандлы хранят их всегда): по хешу скрипта
    и хешу раскладки полей. Генератор типов по сборкам может ошибиться (не хватает сборки, особые
    атрибуты) — донор с тем же хешем раскладки не ошибается."""
    donors: Dict[Tuple[str, str], object] = {}
    for path in files:
        try:
            with unitypy_io.load_assets(str(path)) as env:
                sfs = [f for f in getattr(env, "files", {}).values()]
                stack = list(sfs)
                while stack:
                    f = stack.pop()
                    types = getattr(f, "types", None)
                    if types is None:
                        stack.extend(getattr(f, "files", {}).values() if isinstance(getattr(f, "files", None), dict)
                                     else [])
                        continue
                    for st in types:
                        key = _type_key(st)
                        if key and st.node is not None and key not in donors:
                            donors[key] = st.node
        except Exception as exc:  # noqa: BLE001
            log.debug("структуры из %s не прочитаны: %s", path.name, exc)
    return donors


def _type_key(st) -> Optional[Tuple[str, str]]:
    sid, oth = getattr(st, "script_id", None), getattr(st, "old_type_hash", None)
    if not sid or not oth or getattr(st, "class_id", 0) != 114:
        return None
    to_hex = (lambda x: x.hex() if isinstance(x, (bytes, bytearray)) else str(x))
    if to_hex(sid).strip("0") == "" or to_hex(oth).strip("0") == "":
        return None
    return to_hex(sid), to_hex(oth)


#: узлы берутся из генератора типов, подключённого к окружению
_GEN = object()


def _data_of(obj) -> bytes:
    """Текущие байты объекта. UnityPy: set_raw_data кладёт новые в obj.data, а get_raw_data читает
    старые из файла — после изменения верны только obj.data."""
    data = getattr(obj, "data", None)
    return data if isinstance(data, (bytes, bytearray)) else obj.get_raw_data()


def read_structured(obj, donors) -> Optional[dict]:
    """Дерево объекта по структуре из бандла-донора (проверенное записью байт в байт) или None."""
    tree, _ = _read_structured(obj, donors, None)
    return tree


def _read_structured(obj, donors, gen):
    """(дерево, узлы) объекта или (None, None). Узлы чужого происхождения (донор, генератор) годятся,
    только если прочитанное и записанное обратно совпадает с оригиналом байт в байт."""
    raw = obj.get_raw_data()
    candidates = []
    st = obj.serialized_type
    if st is not None and st.node is not None:
        candidates.append(None)                         # структура записана в самом файле
    key = _type_key(st) if st is not None else None
    if key and key in donors:
        candidates.append(donors[key])
    if gen is not None and not candidates:
        candidates.append(_GEN)
    for nodes in candidates:
        used = None if nodes is _GEN else nodes
        try:
            tree = obj.read_typetree(nodes=used)
            if obj.save_typetree(tree, nodes=used) == raw:
                obj.set_raw_data(raw)
                return tree, used
        except Exception:  # noqa: BLE001
            pass
        obj.set_raw_data(raw)
    return None, None


# ---------- применение к игре ----------

#: бандл Addressables больше этого при работающем XUnity не переписывается: его надписи XUnity переведёт
#: в игре, а чтение и запись такого бандла — десятки минут и до 16 ГБ памяти (Demonic Mahjong, 2,6 ГБ)
BIG_BUNDLE = 512 << 20


def _wanted_bundle(path: Path, with_text: Optional[Set[str]], runtime: bool) -> bool:
    """Открывать ли бандл Addressables для прямого перевода: только с найденным текстом; огромный — только
    если перевести его в игре некому (нет BepInEx) или в нём таблицы строк Unity Localization."""
    if with_text is not None and path.name not in with_text:
        return False
    if not runtime or "string-tables" in path.name.lower():
        return True
    try:
        return path.stat().st_size <= BIG_BUNDLE
    except OSError:
        return False


def apply(game, pairs: Dict[str, str], backup, status: StatusFn = lambda *a, **k: None,
          files: Optional[List[Path]] = None, loose: Optional[List[Path]] = None,
          bundles_with_text: Optional[Set[str]] = None, runtime: bool = True) -> DirectReport:
    """``bundles_with_text`` — имена файлов, где извлечение нашло текст: бандлы Addressables без текста
    не открываются (они бывают по нескольку гигабайт). ``runtime`` — в игре работает XUnity."""
    from . import addressables
    from . import extract as _extract
    rep = DirectReport()
    full = {s: t for s, t in pairs.items() if t and t != s and looks_translatable(s)}
    text_map = phrase_map(pairs)
    if not full:
        return rep
    byte_map = {k.encode("utf-8"): v.encode("utf-8") for k, v in text_map.items()}
    every = _extract.serialized_files(game)
    files = files if files is not None else [
        p for p in every if addressables.catalog_for(p) is None or _wanted_bundle(p, bundles_with_text, runtime)]
    loose = loose if loose is not None else _extract.loose_text_files(game)
    written: List[Path] = []

    for i, path in enumerate(loose):
        status(f"Перевод в данных игры: {path.name}", i / max(1, len(loose) + len(files)))
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig")
            new, n = patch_document(text, text_map)
            if n:
                out = new.encode("utf-8")
                if raw.startswith(b"\xef\xbb\xbf") and not out.startswith(b"\xef\xbb\xbf"):
                    out = b"\xef\xbb\xbf" + out
                backup.save_original(path)
                path.write_bytes(out)
                rep.files += 1
                rep.strings += n
        except (OSError, UnicodeDecodeError) as exc:
            log.info("Файл %s не изменён: %s", path.name, exc)

    status("Перевод в данных игры: структуры объектов…", None)
    donors = typetree_donors([p for p in every if "StreamingAssets" in p.parts
                              or p.suffix.lower() in (".bundle", ".unity3d")])
    gen = _extract._typetree_generator(game, [])
    learned: Dict[tuple, Set[str]] = {}         # класс объекта -> его текстовые поля (по всем файлам игры)
    string_ids: Dict[Tuple[str, int], Dict[int, str]] = {}  # таблицы Unity Localization: набор -> m_Id -> перевод
    for i, path in enumerate(files):
        status(f"Перевод в данных игры: {path.name}", (len(loose) + i) / max(1, len(loose) + len(files)))
        try:
            n, amb = _patch_asset_file(path, byte_map, text_map, full, donors, gen, backup, learned, string_ids)
            rep.strings += n
            rep.skipped_ambiguous += amb
            if n:
                rep.files += 1
                written.append(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Перевод в %s не вписан: %s", path.name, exc)
            rep.errors.append(f"{path.name}: {exc}")
    # таблицы строк других языков игры — тем же переводом по номерам записей: игра может выбрать не английский
    if any(string_ids.values()):
        sa = game.data / "StreamingAssets"
        others = [p for p in (sa.rglob("*string-tables*") if sa.is_dir() else [])
                  if p.is_file() and p not in files and addressables.catalog_for(p) is not None]
        for path in others:
            status(f"Перевод в данных игры: {path.name}", None)
            try:
                n, _ = _patch_asset_file(path, {}, {}, {}, donors, gen, backup, None, string_ids, other_locale=True)
                if n:
                    rep.files += 1
                    rep.other_locales += n
                    written.append(path)
            except Exception as exc:  # noqa: BLE001
                log.warning("Перевод в %s не вписан: %s", path.name, exc)
                rep.errors.append(f"{path.name}: {exc}")
    if written:
        addressables.disable_crc(written, backup)   # изменённый бандл Addressables не прошёл бы проверку CRC
    return rep


def _patch_asset_file(path: Path, byte_map: Dict[bytes, bytes], text_map: Dict[str, str],
                      full: Dict[str, str], donors, gen, backup,
                      learned: Optional[Dict[tuple, Set[str]]] = None,
                      string_ids: Optional[Dict[Tuple[str, int], Dict[int, str]]] = None,
                      other_locale: bool = False) -> Tuple[int, int]:
    """``string_ids`` — переводы таблиц строк Unity Localization по набору и номеру записи: собираются по
    английским таблицам, а ``other_locale`` — бандл таблиц другого языка: в нём меняются только они."""
    total = ambiguous = 0
    learned = {} if learned is None else learned
    # ключ — (номер файла в бандле, path_id): в data.unity3d у сцен свои, совпадающие path_id
    patched: Dict[Tuple[int, int], bytes] = {}
    structured = []                         # объекты с деревом: переводятся, когда узнаны текстовые поля
    with unitypy_io.load_assets(str(path)) as env:
        if gen is not None:
            env.typetree_generator = gen
        numbers = unitypy_io.file_numbers(env)
        for obj in env.objects:
            num = numbers.get(id(obj.assets_file))
            if num is None:
                continue                    # соседний файл игры, подгруженный UnityPy: здесь не сохраняется
            key = (num, obj.path_id)
            tname = obj.type.name
            if other_locale:
                if tname == "MonoBehaviour":
                    tree, nodes = _read_structured(obj, donors, gen)
                    skey = string_table_key(obj, tree) if tree is not None and is_string_table(tree) else None
                    n = fill_string_table(tree, string_ids.get(skey) or {}) if skey and string_ids else 0
                    if n:
                        patched[key] = obj.save_typetree(tree, nodes=nodes)
                        total += n
                continue
            if tname == "MonoBehaviour":
                tree, nodes = _read_structured(obj, donors, gen)
                if tree is not None:
                    structured.append((key, obj, tree, nodes))
                    learned.setdefault(tree_signature(tree), set()).update(text_fields(tree, text_map))
                    continue
                data = obj.get_raw_data()
                i2 = raw_i2(data)               # база I2: только английский столбец, языки не трогаются
                new, n, amb = patch_raw(data, byte_map, {off for off, _ in i2.english} if i2 else None)
                ambiguous += amb
                if n:
                    obj.set_raw_data(new)
                    patched[key] = new
                    total += n
            elif tname == "TextAsset":
                try:
                    ta = obj.read()
                except Exception:  # noqa: BLE001
                    continue
                script = getattr(ta, "m_Script", None)
                if not isinstance(script, str) or len(script) > 64 * 1024 * 1024:
                    continue
                # двоичная таблица (Luban): строки с длиной впереди — замена текста сбила бы длины
                if text_assets.decode(script.encode("utf-8", "surrogateescape")) is None:
                    continue
                new_text, n = patch_document(script, text_map)
                if n:
                    ta.m_Script = new_text
                    ta.save()
                    patched[key] = _data_of(obj)
                    total += n
        for key, obj, tree, nodes in structured:
            if string_ids is not None and is_string_table(tree):
                skey = string_table_key(obj, tree)
                n = patch_string_table(tree, full, string_ids.setdefault(skey, {}) if skey else None)
            else:
                n = patch_tree(tree, full, text_map, learned.get(tree_signature(tree), ()))
            if n:
                patched[key] = obj.save_typetree(tree, nodes=nodes)
                total += n

        if not total:
            return 0, ambiguous
        table = unitypy_io.object_table(env)
        out = unitypy_io.save_env(env)
    with unitypy_io.load_assets(out) as env2:
        problem = unitypy_io.same_object_table(table, unitypy_io.object_table(env2))
        if problem:
            raise RuntimeError("проверка: " + problem)
        objs = unitypy_io.own_objects(env2)
        for key, data in patched.items():
            if key not in objs or objs[key].get_raw_data() != data:
                raise RuntimeError(f"проверка: объект {key[1]} после сохранения другой")
    out = unitypy_io.as_stored(path, out)       # защищённый бандл — тем же ключом, что был
    backup.save_original(path)
    path.write_bytes(out)
    return total, ambiguous
