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
  * CSV и JSON — файлы в StreamingAssets и TextAsset'ы: меняются ячейки/значения, равные фразе.
    Бандлы Addressables (``StreamingAssets/aa``) не трогаются: игра может проверять их CRC.
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
from typing import Callable, Dict, List, Optional, Tuple

from ...translation.filters import looks_technical, looks_translatable
from . import text_assets, unitypy_io

log = logging.getLogger("russificator.unity")

StatusFn = Callable[..., None]


@dataclass
class DirectReport:
    files: int = 0
    strings: int = 0
    skipped_ambiguous: int = 0
    errors: List[str] = field(default_factory=list)


def is_phrase(source: str) -> bool:
    """Фраза для игрока, а не ключ: от трёх слов, с пробелами, не код и не путь."""
    s = source.strip()
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


def patch_raw(data: bytes, mapping: Dict[bytes, bytes]) -> Tuple[bytes, int, int]:
    """Заменить строки Unity (int32 длина + UTF-8 + нули до кратного 4) по точному совпадению.

    Возвращает (новые данные, заменено, пропущено как неоднозначное). Длина строки после замены
    другая — всё, что дальше, сдвигается на кратное 4, выравнивание полей сохраняется.
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
            if len(body) == n and body in mapping:
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


def patch_fields(tree, phrases: Dict[str, str]) -> int:
    """Прочие объекты: фразы в полях, имя которых не похоже на ключ."""
    count = 0

    def walk(node, field: str):
        nonlocal count
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, str):
                    if not _key_field(k):
                        t = _translated(v, phrases)
                        if t is not None and t != v:
                            node[k] = t
                            count += 1
                elif isinstance(v, (dict, list)):
                    walk(v, k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str):
                    if not _key_field(field):
                        t = _translated(v, phrases)
                        if t is not None and t != v:
                            node[i] = t
                            count += 1
                elif isinstance(v, (dict, list)):
                    walk(v, field)
    walk(tree, "")
    return count


def patch_tree(tree: dict, full: Dict[str, str], phrases: Dict[str, str]) -> int:
    grid_lists = utage_grid_lists(tree)
    if grid_lists:
        return sum(patch_utage_grids(grids, full) for grids in grid_lists)
    n = patch_i2_source(tree, full)
    if n:
        return n
    return patch_fields(tree, phrases)


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

def apply(game, pairs: Dict[str, str], backup, status: StatusFn = lambda *a, **k: None,
          files: Optional[List[Path]] = None, loose: Optional[List[Path]] = None) -> DirectReport:
    from . import extract as _extract
    rep = DirectReport()
    full = {s: t for s, t in pairs.items() if t and t != s and looks_translatable(s)}
    text_map = phrase_map(pairs)
    if not full:
        return rep
    byte_map = {k.encode("utf-8"): v.encode("utf-8") for k, v in text_map.items()}
    sa = game.data / "StreamingAssets"
    every = _extract.serialized_files(game)
    files = files if files is not None else [
        p for p in every if not (sa in p.parents and p.relative_to(sa).parts[:1] == ("aa",))]
    loose = loose if loose is not None else _extract.loose_text_files(game)

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
    for i, path in enumerate(files):
        status(f"Перевод в данных игры: {path.name}", (len(loose) + i) / max(1, len(loose) + len(files)))
        try:
            n, amb = _patch_asset_file(path, byte_map, text_map, full, donors, gen, backup)
            rep.strings += n
            rep.skipped_ambiguous += amb
            if n:
                rep.files += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Перевод в %s не вписан: %s", path.name, exc)
            rep.errors.append(f"{path.name}: {exc}")
    return rep


def _patch_asset_file(path: Path, byte_map: Dict[bytes, bytes], text_map: Dict[str, str],
                      full: Dict[str, str], donors, gen, backup) -> Tuple[int, int]:
    total = ambiguous = 0
    patched: Dict[int, bytes] = {}
    with unitypy_io.load_assets(str(path)) as env:
        if gen is not None:
            env.typetree_generator = gen
        for obj in env.objects:
            tname = obj.type.name
            if tname == "MonoBehaviour":
                tree, nodes = _read_structured(obj, donors, gen)
                if tree is not None:
                    n = patch_tree(tree, full, text_map)
                    if n:
                        patched[obj.path_id] = obj.save_typetree(tree, nodes=nodes)
                        total += n
                    continue
                data = obj.get_raw_data()
                new, n, amb = patch_raw(data, byte_map)
                ambiguous += amb
                if n:
                    obj.set_raw_data(new)
                    patched[obj.path_id] = new
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
                    patched[obj.path_id] = _data_of(obj)
                    total += n

        if not total:
            return 0, ambiguous
        table = unitypy_io.object_table(env)
        out = env.file.save()
    with unitypy_io.load_assets(out) as env2:
        problem = unitypy_io.same_object_table(table, unitypy_io.object_table(env2))
        if problem:
            raise RuntimeError("проверка: " + problem)
        objs = {o.path_id: o for o in env2.objects}
        for pid, data in patched.items():
            if pid not in objs or objs[pid].get_raw_data() != data:
                raise RuntimeError(f"проверка: объект {pid} после сохранения другой")
    out = unitypy_io.as_stored(path, out)       # защищённый бандл — тем же ключом, что был
    backup.save_original(path)
    path.write_bytes(out)
    return total, ambiguous
