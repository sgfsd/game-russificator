"""RPG Maker XP/VX/VX Ace: текст в Data/*.rxdata|rvdata|rvdata2 (Ruby Marshal).

Данные читаются из папки Data/ или прямо из зашифрованного архива
(Game.rgssad/rgss2a/rgss3a) — игра до внедрения не меняется.

Извлекается то же, что и в MV/MZ: сообщения (101/401 — блоком целиком),
прокручиваемый текст (105/405), выборы (102), смена имён (320/324), таблицы
базы данных и термины System. При внедрении архив распаковывается рядом с
игрой (движок читает файлы из архива в первую очередь, поэтому сам архив
уходит в бэкап), изменённые файлы данных записываются, а переведённые
сообщения переносятся по ширине окна.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...core.universal import Entry, EntryStatus, TextKind
from . import ruby_marshal as M
from . import layout, rgss_scripts
from .mv_mz import join_lines
from .rgssad import RgssArchive, find_archive

EXT = {"xp": "rxdata", "vx": "rvdata", "ace": "rvdata2"}
TABLES = {
    "Actors": [("name", TextKind.CHARACTER_NAME), ("nickname", TextKind.CHARACTER_NAME),
               ("description", TextKind.ITEM_DESCRIPTION)],
    "Classes": [("name", TextKind.ITEM_NAME)],
    "Skills": [("name", TextKind.ITEM_NAME), ("description", TextKind.ITEM_DESCRIPTION),
               ("message1", TextKind.SYSTEM), ("message2", TextKind.SYSTEM)],
    "Items": [("name", TextKind.ITEM_NAME), ("description", TextKind.ITEM_DESCRIPTION)],
    "Weapons": [("name", TextKind.ITEM_NAME), ("description", TextKind.ITEM_DESCRIPTION)],
    "Armors": [("name", TextKind.ITEM_NAME), ("description", TextKind.ITEM_DESCRIPTION)],
    "Enemies": [("name", TextKind.CHARACTER_NAME)],
    "States": [("name", TextKind.ITEM_NAME), ("message1", TextKind.SYSTEM), ("message2", TextKind.SYSTEM),
               ("message3", TextKind.SYSTEM), ("message4", TextKind.SYSTEM)],
}
MAX_LENGTHS = {TextKind.CHOICE: 30, TextKind.ITEM_NAME: 22, TextKind.CHARACTER_NAME: 14}


def _eid(key: str) -> str:
    return "rx" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:14]


class DataSource:
    """Файлы Data/ — из папки или из архива."""

    def __init__(self, game_root: Path, version: str):
        self.root = Path(game_root)
        self.ext = EXT.get(version, "rvdata2")
        self.archive: Optional[RgssArchive] = None
        arc = find_archive(self.root)
        if arc is not None:
            self.archive = RgssArchive(arc)

    def names(self) -> List[str]:
        names = {p.name for p in (self.root / "Data").glob(f"*.{self.ext}")} if (self.root / "Data").is_dir() else set()
        if self.archive:
            names |= {Path(n).name for n in self.archive.names() if n.lower().startswith("data/")
                      and n.endswith("." + self.ext)}
        return sorted(names)

    def read(self, name: str) -> Optional[bytes]:
        loose = self.root / "Data" / name
        if self.archive is not None:
            data = self.archive.read(f"Data/{name}")
            if data is not None:
                return data  # движок берёт файл из архива, даже если рядом лежит свой
        return loose.read_bytes() if loose.is_file() else None


# ---------- извлечение ----------

def extract(game_root: Path, version: str) -> Tuple[List[Entry], List[str]]:
    warnings: List[str] = []
    entries: List[Entry] = []
    try:
        src = DataSource(game_root, version)
    except Exception as exc:  # noqa: BLE001
        return [], [f"Архив игры не прочитан: {exc}"]
    ext = src.ext
    for name in src.names():
        stem = name.rsplit(".", 1)[0]
        if stem == "Scripts":
            _scripts(src.read(name), name, entries, warnings)
            continue
        is_map = re.fullmatch(r"Map\d+", stem)
        if not (is_map or stem in ("CommonEvents", "Troops", "System") or stem in TABLES):
            continue
        try:
            data = M.load(src.read(name))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Не удалось разобрать {name}: {exc}")
            continue
        if is_map and isinstance(data, M.RubyObject):
            for ev_id, ev in _events(data.get("events")):
                for p_i, page in enumerate(ev.get("pages") or []):
                    if isinstance(page, M.RubyObject):
                        _commands(page.get("list") or [], f"{name}|ev{ev_id}|p{p_i}", version, entries)
        elif stem == "CommonEvents":
            for ev in data or []:
                if isinstance(ev, M.RubyObject):
                    _commands(ev.get("list") or [], f"{name}|ev{ev.get('id')}|p0", version, entries)
        elif stem == "Troops":
            for tr in data or []:
                if isinstance(tr, M.RubyObject):
                    for p_i, page in enumerate(tr.get("pages") or []):
                        if isinstance(page, M.RubyObject):
                            _commands(page.get("list") or [], f"{name}|ev{tr.get('id')}|p{p_i}", version, entries)
        elif stem == "System" and isinstance(data, M.RubyObject):
            _system(data, name, entries)
        elif stem in TABLES:
            for row in data or []:
                if isinstance(row, M.RubyObject):
                    for field, kind in TABLES[stem]:
                        val = row.get(field)
                        if M.is_text(val) and val.strip():
                            key = f"{name}|{row.get('id')}|{field}"
                            entries.append(Entry(id=_eid(key), source=str(val), kind=kind, target_key=key,
                                                 max_length=MAX_LENGTHS.get(kind)))
    return entries, warnings


def _scripts(raw: Optional[bytes], name: str, out: List[Entry], warnings: List[str]) -> None:
    """Надписи из Ruby-скриптов игры (Vocab, add_command, draw_text…), см. rgss_scripts.py."""
    if not raw:
        return
    try:
        scripts, _ = rgss_scripts.load(raw)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Скрипты игры не разобраны: {exc}")
        return
    for idx, entry in enumerate(scripts):
        code = rgss_scripts.code_of(entry)
        if code is None:
            continue
        script_name = str(entry[1])
        kind = TextKind.SYSTEM if "vocab" in script_name.lower() else TextKind.UI
        for lit in rgss_scripts.literals(code, script_name):
            key = f"{name}|{idx}|{lit.start}"
            out.append(Entry(id=_eid(key), source=lit.value, kind=kind, target_key=key, scene=script_name,
                             notes={"raw": lit.raw}))


def _apply_scripts(scripts: List[Any], name: str, keys: Dict[str, Entry]) -> int:
    by_script: Dict[int, Dict[int, Tuple[str, str]]] = {}
    for key, e in keys.items():
        _, idx, start = key.split("|")
        raw = (e.notes or {}).get("raw")
        if raw:
            by_script.setdefault(int(idx), {})[int(start)] = (raw, e.translation)
    count = 0
    for idx, changes in by_script.items():
        if idx >= len(scripts):
            continue
        code = rgss_scripts.code_of(scripts[idx])
        if code is None:
            continue
        new_code, n = rgss_scripts.replace(code, changes)
        if n:
            rgss_scripts.set_code(scripts[idx], new_code)
            count += n
    return count


def _events(events) -> List[Tuple[Any, M.RubyObject]]:
    if isinstance(events, dict):
        return [(k, v) for k, v in events.items() if isinstance(v, M.RubyObject)]
    if isinstance(events, list):
        return [(i, v) for i, v in enumerate(events) if isinstance(v, M.RubyObject)]
    return []


def _param(cmd: M.RubyObject, i: int) -> Any:
    params = cmd.get("parameters") or []
    return params[i] if len(params) > i else None


def _commands(cmds: List[Any], prefix: str, version: str, out: List[Entry]) -> None:
    i = 0
    context: List[str] = []
    while i < len(cmds):
        cmd = cmds[i]
        code = cmd.get("code") if isinstance(cmd, M.RubyObject) else None
        if code in (101, 105):
            line_code = 401 if code == 101 else 405
            lines: List[str] = []
            if version == "xp" and code == 101 and M.is_text(_param(cmd, 0)):
                lines.append(str(_param(cmd, 0)))  # в XP первая строка — в самой команде 101
            j = i + 1
            while j < len(cmds) and isinstance(cmds[j], M.RubyObject) and cmds[j].get("code") == line_code:
                val = _param(cmds[j], 0)
                lines.append(str(val) if M.is_text(val) else "")
                j += 1
            text = join_lines(lines)
            if text.strip():
                key = f"{prefix}|c{i}|{'msg' if code == 101 else 'scroll'}"
                face = version != "xp" and code == 101 and bool(M.is_text(_param(cmd, 0)) and str(_param(cmd, 0)))
                out.append(Entry(id=_eid(key), source=text,
                                 kind=TextKind.DIALOGUE if code == 101 else TextKind.NARRATION,
                                 neighbors=context[-2:], target_key=key, notes={"face": face}))
                context.append(text)
            i = j
            continue
        if code == 102 and isinstance(_param(cmd, 0), list):
            for k, choice in enumerate(_param(cmd, 0)):
                if M.is_text(choice) and choice.strip():
                    key = f"{prefix}|c{i}|choice{k}"
                    out.append(Entry(id=_eid(key), source=str(choice), kind=TextKind.CHOICE, target_key=key,
                                     neighbors=context[-2:], max_length=MAX_LENGTHS[TextKind.CHOICE]))
        elif code in (320, 324) and M.is_text(_param(cmd, 1)) and _param(cmd, 1).strip():
            key = f"{prefix}|c{i}|p1"
            out.append(Entry(id=_eid(key), source=str(_param(cmd, 1)), kind=TextKind.CHARACTER_NAME,
                             target_key=key, max_length=MAX_LENGTHS[TextKind.CHARACTER_NAME]))
        i += 1


def _system(data: M.RubyObject, name: str, out: List[Entry]) -> None:
    def add(key: str, val: Any, kind: TextKind) -> None:
        if M.is_text(val) and val.strip():
            out.append(Entry(id=_eid(key), source=str(val), kind=kind, target_key=key,
                             max_length=16 if kind == TextKind.UI else None))

    add(f"{name}|game_title", data.get("game_title"), TextKind.SYSTEM)
    add(f"{name}|currency_unit", data.get("currency_unit"), TextKind.UI)
    for group in ("elements", "skill_types", "weapon_types", "armor_types"):
        for k, v in enumerate(data.get(group) or []):
            add(f"{name}|{group}|{k}", v, TextKind.UI)
    terms = data.get("terms") or data.get("words")
    if isinstance(terms, M.RubyObject):
        for ivar, val in terms.ivars.items():
            field = ivar.lstrip("@")
            if isinstance(val, list):
                for k, v in enumerate(val):
                    add(f"{name}|terms|{field}|{k}", v, TextKind.UI)
            else:
                add(f"{name}|terms|{field}", val, TextKind.UI)


# ---------- внедрение ----------

def inject(game_root: Path, project, version: str) -> Tuple[int, List[str]]:
    game_root = Path(game_root)
    backup = project.backup
    warnings: List[str] = []
    done = {e.target_key: e for e in project.entries
            if e.translation and e.status in (EntryStatus.TRANSLATED, EntryStatus.APPROVED)}
    if not done:
        return 0, ["Нет переведённых строк — нечего внедрять."]
    src = DataSource(game_root, version)
    if src.archive is not None:
        _unpack_archive(game_root, src.archive, backup)
        warnings.append(f"Архив {src.archive.path.name} распакован рядом с игрой (оригинал — в бэкапе).")
    by_file: Dict[str, Dict[str, Entry]] = {}
    for key, e in done.items():
        by_file.setdefault(key.split("|", 1)[0], {})[key] = e
    count = 0
    for name, keys in by_file.items():
        path = game_root / "Data" / name
        if not path.is_file():
            warnings.append(f"Файл Data/{name} не найден — переводы из него не внедрены.")
            continue
        data, utf8 = M.load_with_info(path.read_bytes())
        stem = name.rsplit(".", 1)[0]
        if stem == "Scripts":
            n = _apply_scripts(data if isinstance(data, list) else [], name, keys)
        else:
            n = _apply(data, stem, name, keys, version)
        raw = M.dump(data, utf8_strings=utf8 or version == "ace")
        M.load(raw)  # самопроверка: записанное должно читаться обратно
        backup.write_bytes(path, raw)
        count += n
    return count, warnings


def _unpack_archive(game_root: Path, archive: RgssArchive, backup) -> None:
    created_dirs = set()
    for name in archive.names():
        dest = game_root / name
        if dest.exists():
            continue  # движок взял бы файл из архива — оставляем то, что уже лежит
        top = Path(name).parts[0]
        if not (game_root / top).exists() and top not in created_dirs:
            created_dirs.add(top)
            (game_root / top).mkdir(parents=True)
            backup.track_new(game_root / top)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if top not in created_dirs:
            backup.track_new(dest)
        dest.write_bytes(archive.read(name) or b"")
    backup.save_original(archive.path)
    archive.path.unlink()


def _apply(data: Any, stem: str, name: str, keys: Dict[str, Entry], version: str) -> int:
    count = 0
    if re.fullmatch(r"Map\d+", stem) and isinstance(data, M.RubyObject):
        for ev_id, ev in _events(data.get("events")):
            for p_i, page in enumerate(ev.get("pages") or []):
                if isinstance(page, M.RubyObject):
                    page.ivars["@list"], n = _apply_list(page.get("list") or [], f"{name}|ev{ev_id}|p{p_i}", keys, version)
                    count += n
    elif stem in ("CommonEvents", "Troops"):
        for ev in data or []:
            if not isinstance(ev, M.RubyObject):
                continue
            if stem == "CommonEvents":
                ev.ivars["@list"], n = _apply_list(ev.get("list") or [], f"{name}|ev{ev.get('id')}|p0", keys, version)
                count += n
            else:
                for p_i, page in enumerate(ev.get("pages") or []):
                    if isinstance(page, M.RubyObject):
                        page.ivars["@list"], n = _apply_list(page.get("list") or [], f"{name}|ev{ev.get('id')}|p{p_i}",
                                                             keys, version)
                        count += n
    elif stem == "System" and isinstance(data, M.RubyObject):
        count += _apply_system(data, name, keys)
    elif stem in TABLES:
        for row in data or []:
            if isinstance(row, M.RubyObject):
                for field, _ in TABLES[stem]:
                    e = keys.get(f"{name}|{row.get('id')}|{field}")
                    if e is not None and M.is_text(row.get(field)):
                        text = e.translation
                        if field == "description" and version == "ace":
                            text = fit_description(text)
                        row.ivars["@" + field] = text
                        count += 1
    return count


#: окно справки VX Ace: 544 − 2·12 − 4 (отступ текста), две строки, шрифт 24
_HELP_ACE = layout.Box(516, 0, 24, 26, 516)


def fit_description(text: str) -> str:
    """Описание для окна справки VX Ace (draw_text_ex не сжимает строку — длинная обрезается).

    Переносим по ширине окна в две строки; не помещается и в две — уменьшаем
    шрифт штатным кодом движка ``\\}`` (минус 8) и переносим заново.
    """
    if "\n" in text:
        return text                          # автор разбил сам
    lines = layout.wrap(text, _HELP_ACE)
    if len(lines) <= 2:
        return "\n".join(lines)
    small = layout.Box(516, 0, 16, 18, 516)
    lines = layout.wrap(text, small)
    if len(lines) > 2:                       # строка не ниже 24 px — третья строка не видна
        lines = [lines[0], " ".join(lines[1:])]
    return "\\}" + "\n".join(lines)


def _new_line(template: M.RubyObject, text: str, code: int) -> M.RubyObject:
    return M.RubyObject(template.classname, {"@code": code, "@indent": template.get("indent", 0),
                                             "@parameters": [text]})


def _apply_list(cmds: List[Any], prefix: str, keys: Dict[str, Entry], version: str) -> Tuple[List[Any], int]:
    out: List[Any] = []
    count = 0
    i = 0
    while i < len(cmds):
        cmd = cmds[i]
        code = cmd.get("code") if isinstance(cmd, M.RubyObject) else None
        if code in (101, 105):
            line_code = 401 if code == 101 else 405
            j = i + 1
            while j < len(cmds) and isinstance(cmds[j], M.RubyObject) and cmds[j].get("code") == line_code:
                j += 1
            e = keys.get(f"{prefix}|c{i}|{'msg' if code == 101 else 'scroll'}")
            if e is None:
                out.extend(cmds[i:j])
                i = j
                continue
            box = layout.box_for(version)
            old = [str(_param(c, 0) or "") for c in cmds[i + 1:j]]
            if version == "xp" and code == 101:
                old.insert(0, str(_param(cmd, 0) or ""))
            english = max([0.0] + [layout.text_width(s, box) for s in old])
            lines = layout.wrap(e.translation, box, face=bool((e.notes or {}).get("face")),
                                scroll=code == 105, min_width=english)
            template = cmds[i + 1] if j > i + 1 else cmd
            if version == "xp" and code == 101:
                params = cmd.get("parameters") or []
                params[0] = lines[0]
                out.append(cmd)
                out.extend(_new_line(template, ln, 401) for ln in lines[1:])
            else:
                out.append(cmd)
                out.extend(_new_line(template, ln, line_code) for ln in lines)
            count += 1
            i = j
            continue
        if code == 102 and isinstance(_param(cmd, 0), list):
            choices = _param(cmd, 0)
            for k in range(len(choices)):
                e = keys.get(f"{prefix}|c{i}|choice{k}")
                if e is not None:
                    choices[k] = e.translation
                    count += 1
        elif code in (320, 324):
            e = keys.get(f"{prefix}|c{i}|p1")
            if e is not None:
                cmd.get("parameters")[1] = e.translation
                count += 1
        out.append(cmd)
        i += 1
    return out, count


def _apply_system(data: M.RubyObject, name: str, keys: Dict[str, Entry]) -> int:
    count = 0
    terms = data.get("terms") or data.get("words")
    for key, e in keys.items():
        parts = key.split("|")[1:]
        tr = e.translation
        if len(parts) == 1 and M.is_text(data.get(parts[0])):
            data.ivars["@" + parts[0]] = tr
        elif len(parts) == 2 and isinstance(data.get(parts[0]), list) and parts[1].isdigit():
            data.get(parts[0])[int(parts[1])] = tr
        elif parts[0] == "terms" and isinstance(terms, M.RubyObject):
            val = terms.get(parts[1])
            if len(parts) == 2 and M.is_text(val):
                terms.ivars["@" + parts[1]] = tr
            elif len(parts) == 3 and isinstance(val, list) and parts[2].isdigit():
                val[int(parts[2])] = tr
            else:
                continue
        else:
            continue
        count += 1
    return count
