"""RPG Maker MV/MZ: текст в JSON-файлах ``data/`` (MV — ``www/data/``).

Извлекается:
  * сообщения — блок «Show Text» (101 + строки 401) целиком одной строкой:
    так переводчик видит фразу целиком, а не обрывки по 40 символов;
  * «Scrolling Text» (105 + 405), варианты выбора (102), смена имени/
    ника/профиля героя (320/324/325), имя говорящего в заголовке MZ (101);
  * таблицы базы данных (герои, предметы, навыки, враги, состояния...) и
    System.json (название игры, термины, сообщения боя, валюта);
  * названия карт (``displayName``), строки ``$gameMessage.add("…")`` в
    скриптах событий (355/655), текстовые аргументы команд плагинов MZ (357)
    и надписи в параметрах плагинов ``js/plugins.js`` (см. ``plugin_text.py``).

Внедрение переписывает JSON (оригиналы — в бэкап). Переведённый блок
сообщения заново разбивается на строки по ширине окна в пикселях (с лицом
персонажа окно уже, см. ``layout.py``), лишние строки движок сам покажет на
следующей странице — русский текст длиннее английского и без этого вылезал
бы за рамку.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...core.universal import Entry, EntryStatus, TextKind
from . import layout, plugin_text

TABLE_FILES = {
    "Actors": [("name", TextKind.CHARACTER_NAME), ("nickname", TextKind.CHARACTER_NAME),
               ("profile", TextKind.ITEM_DESCRIPTION)],
    "Classes": [("name", TextKind.ITEM_NAME)],
    "Skills": [("name", TextKind.ITEM_NAME), ("description", TextKind.ITEM_DESCRIPTION),
               ("message1", TextKind.SYSTEM), ("message2", TextKind.SYSTEM)],
    "Items": [("name", TextKind.ITEM_NAME), ("description", TextKind.ITEM_DESCRIPTION)],
    "Weapons": [("name", TextKind.ITEM_NAME), ("description", TextKind.ITEM_DESCRIPTION)],
    "Armors": [("name", TextKind.ITEM_NAME), ("description", TextKind.ITEM_DESCRIPTION)],
    "Enemies": [("name", TextKind.CHARACTER_NAME)],
    "States": [("name", TextKind.ITEM_NAME), ("message1", TextKind.SYSTEM), ("message2", TextKind.SYSTEM),
               ("message3", TextKind.SYSTEM), ("message4", TextKind.SYSTEM)],
    "MapInfos": [],
}
MAX_LENGTHS = {TextKind.CHOICE: 30, TextKind.ITEM_NAME: 24, TextKind.CHARACTER_NAME: 16}


def data_dir(game_root: Path) -> Path:
    for base in (game_root / "www", game_root):
        if (base / "data").is_dir():
            return base / "data"
    return game_root / "data"


def _eid(key: str) -> str:
    return "rm" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:14]


# ---------- извлечение ----------

def extract(game_root: Path) -> Tuple[List[Entry], List[str], Dict[str, Any]]:
    ddir = data_dir(game_root)
    entries: List[Entry] = []
    warnings: List[str] = []
    if not ddir.exists():
        return entries, ["Папка data/ не найдена."], {}

    for f in sorted(ddir.glob("Map*.json")):
        if not re.fullmatch(r"Map\d+\.json", f.name):
            continue
        data = _load(f, warnings)
        if isinstance(data, dict):
            name = data.get("displayName")
            if isinstance(name, str) and name.strip():
                key = f"{f.name}|displayName"
                entries.append(Entry(id=_eid(key), source=name, kind=TextKind.UI, target_key=key))
            for ev in data.get("events") or []:
                if isinstance(ev, dict):
                    for p_i, page in enumerate(ev.get("pages") or []):
                        _event_list(page.get("list") or [], f"{f.name}|ev{ev.get('id')}|p{p_i}", entries)
    ce = _load(ddir / "CommonEvents.json", warnings)
    for ev in ce or []:
        if isinstance(ev, dict):
            _event_list(ev.get("list") or [], f"CommonEvents.json|ev{ev.get('id')}|p0", entries)
    troops = _load(ddir / "Troops.json", warnings)
    for tr in troops or []:
        if isinstance(tr, dict):
            for p_i, page in enumerate(tr.get("pages") or []):
                _event_list(page.get("list") or [], f"Troops.json|ev{tr.get('id')}|p{p_i}", entries)

    for name, fields in TABLE_FILES.items():
        rows = _load(ddir / f"{name}.json", warnings)
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            for field, kind in fields:
                val = row.get(field)
                if isinstance(val, str) and val.strip():
                    key = f"{name}.json|{row.get('id')}|{field}"
                    entries.append(Entry(id=_eid(key), source=val, kind=kind, target_key=key,
                                         max_length=MAX_LENGTHS.get(kind)))
    sysdata = _load(ddir / "System.json", warnings)
    if isinstance(sysdata, dict):
        _system(sysdata, entries)
    _plugins_extract(game_root, entries, warnings)
    return entries, warnings, {"data_dir": str(ddir)}


def _plugins_js(game_root: Path) -> Path:
    base = game_root / "www" if (game_root / "www").is_dir() else game_root
    return base / "js" / "plugins.js"


def _plugins_extract(game_root: Path, out: List[Entry], warnings: List[str]) -> None:
    path = _plugins_js(game_root)
    if not path.is_file():
        return
    try:
        plugins = layout.plugin_list(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        warnings.append(f"js/plugins.js не прочитан: {exc}")
        return
    for idx, plug in enumerate(plugins):
        if not isinstance(plug, dict) or not plug.get("status") or plug.get("name") == RUNTIME_PLUGIN:
            continue
        for p, leaf in plugin_text.texts(plug.get("parameters") or {}, []):
            key = f"plugins.js|{idx}|{plug.get('name')}|{plugin_text.path_key(p)}"
            out.append(Entry(id=_eid(key), source=leaf, kind=TextKind.UI, target_key=key))


_GM_ADD_RE = re.compile(r"""\$gameMessage\.add\(\s*(['"])((?:\\.|(?!\1).)*)\1\s*\)""")


def _js_unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def _js_escape(s: str, quote: str) -> str:
    return s.replace("\\", "\\\\").replace(quote, "\\" + quote).replace("\n", "\\n")


def _load(f: Path, warnings: List[str]):
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        warnings.append(f"Не удалось прочитать {f.name}: {exc}")
        return None


def _event_list(cmds: List[Any], prefix: str, out: List[Entry]) -> None:
    """Собрать тексты одного списка команд события (ключ — индекс команды-заголовка)."""
    i = 0
    context: List[str] = []
    speaker: Optional[str] = None
    while i < len(cmds):
        cmd = cmds[i] if isinstance(cmds[i], dict) else {}
        code, params = cmd.get("code", 0), cmd.get("parameters") or []
        if code in (101, 105):
            line_code = 401 if code == 101 else 405
            j, lines = i + 1, []
            while j < len(cmds) and isinstance(cmds[j], dict) and cmds[j].get("code") == line_code:
                p = cmds[j].get("parameters") or [""]
                lines.append(str(p[0]) if p else "")
                j += 1
            if code == 101:
                name = params[4] if len(params) > 4 and isinstance(params[4], str) else ""
                if name.strip():
                    speaker = name
                    key = f"{prefix}|c{i}|name"
                    out.append(Entry(id=_eid(key), source=name, kind=TextKind.CHARACTER_NAME, target_key=key,
                                     max_length=MAX_LENGTHS[TextKind.CHARACTER_NAME]))
            text = join_lines(lines)
            if text.strip():
                key = f"{prefix}|c{i}|{'msg' if code == 101 else 'scroll'}"
                face = code == 101 and bool(params and isinstance(params[0], str) and params[0])
                out.append(Entry(id=_eid(key), source=text,
                                 kind=TextKind.DIALOGUE if code == 101 else TextKind.NARRATION,
                                 speaker=speaker, neighbors=context[-2:], target_key=key,
                                 notes={"lines": len(lines), "face": face}))
                context.append(text)
            i = j
            continue
        if code == 102 and params and isinstance(params[0], list):
            for k, choice in enumerate(params[0]):
                if isinstance(choice, str) and choice.strip():
                    key = f"{prefix}|c{i}|choice{k}"
                    out.append(Entry(id=_eid(key), source=choice, kind=TextKind.CHOICE, target_key=key,
                                     neighbors=context[-2:], max_length=MAX_LENGTHS[TextKind.CHOICE]))
        elif code in (320, 324, 325) and len(params) > 1 and isinstance(params[1], str) and params[1].strip():
            kind = TextKind.ITEM_DESCRIPTION if code == 325 else TextKind.CHARACTER_NAME
            key = f"{prefix}|c{i}|p1"
            out.append(Entry(id=_eid(key), source=params[1], kind=kind, target_key=key,
                             max_length=MAX_LENGTHS.get(kind)))
        elif code in (355, 655) and params and isinstance(params[0], str):
            for k, m in enumerate(_GM_ADD_RE.finditer(params[0])):
                text = _js_unescape(m.group(2))
                if text.strip():
                    key = f"{prefix}|c{i}|script{k}"
                    out.append(Entry(id=_eid(key), source=text, kind=TextKind.DIALOGUE, target_key=key,
                                     neighbors=context[-2:]))
        elif code == 357 and len(params) > 3 and isinstance(params[3], dict):
            for p, leaf in plugin_text.texts(params[3], []):
                key = f"{prefix}|c{i}|arg|{plugin_text.path_key(p)}"
                out.append(Entry(id=_eid(key), source=leaf, kind=TextKind.UI, target_key=key))
        i += 1


def join_lines(lines: List[str]) -> str:
    """Строки окна сообщения -> одна фраза (переносы внутри предложения — просто пробелы)."""
    parts = [ln.strip() for ln in lines if ln.strip()]
    return " ".join(parts)


def _system(sysdata: Dict[str, Any], out: List[Entry]) -> None:
    def add(key: str, val: Any, kind: TextKind) -> None:
        if isinstance(val, str) and val.strip() and not re.fullmatch(r"[%\d\s\w]{0,3}%\d+[%\d\s]*", val):
            out.append(Entry(id=_eid(key), source=val, kind=kind, target_key=key,
                             max_length=16 if kind == TextKind.UI else None))

    add("System.json|gameTitle", sysdata.get("gameTitle"), TextKind.SYSTEM)
    add("System.json|currencyUnit", sysdata.get("currencyUnit"), TextKind.UI)
    terms = sysdata.get("terms") or {}
    for group in ("basic", "commands", "params"):
        for k, v in enumerate(terms.get(group) or []):
            add(f"System.json|terms|{group}|{k}", v, TextKind.UI)
    for k, v in (terms.get("messages") or {}).items():
        add(f"System.json|terms|messages|{k}", v, TextKind.SYSTEM)
    for group in ("elements", "skillTypes", "weaponTypes", "armorTypes", "equipTypes"):
        for k, v in enumerate(sysdata.get(group) or []):
            add(f"System.json|{group}|{k}", v, TextKind.UI)


# ---------- внедрение ----------

def version_of(game_root: Path) -> str:
    base = game_root / "www" if (game_root / "www").is_dir() else game_root
    return "mz" if (base / "js" / "rmmz_core.js").is_file() else "mv"


def inject(game_root: Path, project, version: Optional[str] = None) -> Tuple[int, List[str]]:
    ddir = data_dir(game_root)
    box = layout.box_for(version or version_of(game_root), game_root)
    warnings: List[str] = []
    done = {e.target_key: e for e in project.entries
            if e.translation and e.status in (EntryStatus.TRANSLATED, EntryStatus.APPROVED)}
    if not done:
        return 0, ["Нет переведённых строк — нечего внедрять."]
    by_file: Dict[str, Dict[str, Entry]] = {}
    for key, e in done.items():
        by_file.setdefault(key.split("|", 1)[0], {})[key] = e
    count = _plugins_inject(game_root, by_file.pop("plugins.js", {}), project.backup, warnings)

    for fname, keys in by_file.items():
        f = ddir / fname
        data = _load(f, warnings)
        if data is None:
            warnings.append(f"Файл {fname} отсутствует — переводы из него не внедрены.")
            continue
        if fname == "System.json":
            count += _apply_system(data, keys)
        elif fname.startswith("Map"):
            e = keys.get(f"{fname}|displayName")
            if e is not None and isinstance(data, dict):
                data["displayName"] = e.translation
                count += 1
            for ev in data.get("events") or []:
                if isinstance(ev, dict):
                    for p_i, page in enumerate(ev.get("pages") or []):
                        page["list"], n = _apply_list(page.get("list") or [], f"{fname}|ev{ev.get('id')}|p{p_i}",
                                                      keys, box)
                        count += n
        elif fname in ("CommonEvents.json", "Troops.json"):
            for ev in data or []:
                if not isinstance(ev, dict):
                    continue
                if fname == "CommonEvents.json":
                    ev["list"], n = _apply_list(ev.get("list") or [], f"{fname}|ev{ev.get('id')}|p0", keys, box)
                    count += n
                else:
                    for p_i, page in enumerate(ev.get("pages") or []):
                        page["list"], n = _apply_list(page.get("list") or [], f"{fname}|ev{ev.get('id')}|p{p_i}",
                                                      keys, box)
                        count += n
        else:
            for row in data or []:
                if isinstance(row, dict):
                    for field in [k for k in row if isinstance(row[k], str)]:
                        e = keys.get(f"{fname}|{row.get('id')}|{field}")
                        if e is not None:
                            row[field] = e.translation
                            count += 1
        project.backup.write_text(f, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return count, warnings


def _apply_list(cmds: List[Any], prefix: str, keys: Dict[str, Entry],
                box: Optional[layout.Box] = None) -> Tuple[List[Any], int]:
    """Новый список команд с переводом (строки сообщений могут добавиться — ветвления это не ломает)."""
    out: List[Any] = []
    count = 0
    i = 0
    while i < len(cmds):
        cmd = cmds[i]
        code = cmd.get("code", 0) if isinstance(cmd, dict) else 0
        params = (cmd.get("parameters") or []) if isinstance(cmd, dict) else []
        if code in (101, 105):
            line_code = 401 if code == 101 else 405
            j = i + 1
            while j < len(cmds) and isinstance(cmds[j], dict) and cmds[j].get("code") == line_code:
                j += 1
            block = cmds[i + 1:j]
            name_e = keys.get(f"{prefix}|c{i}|name")
            if name_e is not None and len(params) > 4:
                params[4] = name_e.translation
                count += 1
            e = keys.get(f"{prefix}|c{i}|{'msg' if code == 101 else 'scroll'}")
            out.append(cmd)
            if e is not None and block:
                box = box or layout.DEFAULTS["mv"]
                # окно не уже, чем английские строки оригинала (игра могла расширить окно)
                english = max(layout.text_width(str((b.get("parameters") or [""])[0]), box) for b in block)
                lines = layout.wrap(e.translation, box, face=code == 101 and bool((e.notes or {}).get("face")),
                                    scroll=code == 105, min_width=english)
                for line in lines:
                    new = dict(block[0])
                    new["parameters"] = [line]
                    out.append(new)
                count += 1
            else:
                out.extend(block)
            i = j
            continue
        if code == 102 and params and isinstance(params[0], list):
            for k in range(len(params[0])):
                e = keys.get(f"{prefix}|c{i}|choice{k}")
                if e is not None:
                    params[0][k] = e.translation
                    count += 1
        elif code in (320, 324, 325):
            e = keys.get(f"{prefix}|c{i}|p1")
            if e is not None and len(params) > 1:
                params[1] = e.translation
                count += 1
        elif code in (355, 655) and params and isinstance(params[0], str):
            n = [0]

            def repl(m, _i=i):
                e = keys.get(f"{prefix}|c{_i}|script{n[0]}")
                n[0] += 1
                if e is None:
                    return m.group(0)
                q = m.group(1)
                return f"$gameMessage.add({q}{_js_escape(e.translation, q)}{q})"
            new_code = _GM_ADD_RE.sub(repl, params[0])
            if new_code != params[0]:
                params[0] = new_code
                count += 1
        elif code == 357 and len(params) > 3 and isinstance(params[3], dict):
            for p, _leaf in list(plugin_text.walk(params[3], [])):
                e = keys.get(f"{prefix}|c{i}|arg|{plugin_text.path_key(p)}")
                if e is not None:
                    params[3] = plugin_text.set_at(params[3], p, e.translation)
                    count += 1
        out.append(cmd)
        i += 1
    return out, count


def _plugins_inject(game_root: Path, keys: Dict[str, Entry], backup, warnings: List[str]) -> int:
    """Подставить переводы в параметры плагинов js/plugins.js."""
    if not keys:
        return 0
    path = _plugins_js(game_root)
    try:
        text = path.read_text(encoding="utf-8-sig")
        plugins = layout.plugin_list(text)
    except (OSError, ValueError) as exc:
        warnings.append(f"js/plugins.js не изменён: {exc}")
        return 0
    count = 0
    for idx, plug in enumerate(plugins):
        if not isinstance(plug, dict):
            continue
        params = plug.get("parameters") or {}
        for p, _leaf in list(plugin_text.walk(params, [])):
            e = keys.get(f"plugins.js|{idx}|{plug.get('name')}|{plugin_text.path_key(p)}")
            if e is not None:
                params = plugin_text.set_at(params, p, e.translation)
                count += 1
        plug["parameters"] = params
    if count:
        head = text[:text.find("[")]
        body = ",\n".join(json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in plugins)
        backup.write_text(path, f"{head}[\n{body}\n];\n")
    return count


def _apply_system(data: Dict[str, Any], keys: Dict[str, Entry]) -> int:
    count = 0
    terms = data.get("terms") or {}
    for key, e in keys.items():
        parts = key.split("|")[1:]
        tr = e.translation
        if parts == ["gameTitle"] or parts == ["currencyUnit"]:
            data[parts[0]] = tr
        elif parts[0] == "terms" and len(parts) == 3:
            bucket = terms.get(parts[1])
            if isinstance(bucket, dict) and parts[2] in bucket:
                bucket[parts[2]] = tr
            elif isinstance(bucket, list) and parts[2].isdigit() and int(parts[2]) < len(bucket):
                bucket[int(parts[2])] = tr
            else:
                continue
        elif len(parts) == 2 and isinstance(data.get(parts[0]), list) and parts[1].isdigit():
            data[parts[0]][int(parts[1])] = tr
        else:
            continue
        count += 1
    return count


# ---------- плагин переноса слов в игре ----------

RUNTIME_PLUGIN = "Russificator"


def install_runtime(game_root: Path, backup, credit: str = "") -> bool:
    """Положить js/plugins/Russificator.js и включить его последним в js/plugins.js.

    Плагин (resources/rpgmaker/Russificator.js) переносит слова, которые не
    помещаются в окно, и уменьшает шрифт описаний и строк боя. Последним — чтобы
    работать поверх плагинов сообщений игры.
    """
    base = game_root / "www" if (game_root / "www").is_dir() else game_root
    plugins_js = base / "js" / "plugins.js"
    src = Path(__file__).resolve().parents[2] / "resources" / "rpgmaker" / f"{RUNTIME_PLUGIN}.js"
    if not plugins_js.is_file() or not src.is_file():
        return False
    text = plugins_js.read_text(encoding="utf-8-sig")
    try:
        plugins = layout.plugin_list(text)
    except ValueError:
        return False
    plugins = [p for p in plugins if not (isinstance(p, dict) and p.get("name") == RUNTIME_PLUGIN)]
    plugins.append({"name": RUNTIME_PLUGIN, "status": True,
                    "description": "Русификатор: перенос слов и подгонка шрифта под русский текст.",
                    "parameters": {"credit": credit or ""}})
    dest = base / "js" / "plugins" / f"{RUNTIME_PLUGIN}.js"
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(dest, src.read_bytes())
    head = text[:text.find("[")]
    body = ",\n".join(json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in plugins)
    backup.write_text(plugins_js, f"{head}[\n{body}\n];\n")
    return True
