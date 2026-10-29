"""Плагин движка Ren'Py.

Извлечение — из того, что реально есть у выпущенной игры:
  * скомпилированные скрипты .rpyc (россыпью или внутри архивов .rpa) —
    основной источник, строки берутся ровно в том виде, в каком их видит
    движок;
  * исходники .rpy — только если .rpyc для них нет.

Внедрение — без правки файлов игры. Русификатор добавляет:
  * ``game/russificator_tl.json`` — словарь «оригинал → перевод»;
  * ``game/zz_russificator.rpy`` — подключает словарь через штатные хуки
    движка: ``config.say_menu_text_filter`` (реплики и меню) и
    ``config.replace_text`` (интерфейс, экраны, имена), а шрифты игры без
    кириллицы подменяет встроенным PT Sans через ``config.font_replacement_map``;
  * ``game/russificator/`` — сам шрифт.
Работает для Ren'Py 6.99–8.x, в том числе для игр без исходников. Откат —
удалить эти файлы (кнопка «Откатить»).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ...core.plugin_api import Detection, EnginePlugin, ExportPlan
from ...core.universal import Entry, EntryStatus, ExtractionResult, TextKind, TranslationProject
from ...translation import markup
from ...translation.filters import looks_translatable
from ...translation.length import relative_limit
from . import common as _common
from . import detect as _detect
from . import rpy_parse, rpyc
from .archive import RpaArchive, open_archives

log = logging.getLogger("russificator.renpy")

KIND = {"dialogue": TextKind.DIALOGUE, "narration": TextKind.NARRATION, "choice": TextKind.CHOICE,
        "name": TextKind.CHARACTER_NAME, "ui": TextKind.UI}
#: где ширина места ограничена: перевод сильно длиннее оригинала просим сократить. Лимит — от длины
#: самого оригинала (места под него в игре хватало): (во сколько раз, плюс знаков). Кнопки и подписи
#: не переносятся — им запас меньше; варианты выбора переносятся по строкам. То, что всё равно не
#: влезло, хук ужимает при показе до ширины оригинала.
LIMITS = {TextKind.UI: (1.25, 4), TextKind.CHARACTER_NAME: (1.3, 4), TextKind.CHOICE: (1.4, 8)}

TL_FILE = "russificator_tl.json"
#: строки, которые игра показала, а словаря для них не нашлось (пишет хук во время игры)
MISSED_FILE = "russificator_missed.json"
#: они же, сохранённые в проекте перед откатом (откат удаляет файл из папки игры)
LIVE_FILE = "renpy_missed.json"
#: служебный ключ словаря: переводы, которые длиннее места оригинала (хук ужимает их при показе)
META_KEY = "\u0000russificator"
HOOK_FILE = "zz_russificator.rpy"
FONT_DIR = "russificator"
_TAG_RE = re.compile(r"(?<!\{)\{[^{}]*\}")
_FILE_RE = re.compile(r"^[\w\-./ ]+\.(png|jpe?g|webp|gif|ogg|mp3|wav|opus|webm|mkv|mp4|avi|ttf|otf|ttc|rpy|rpyc|json|txt)$",
                      re.I)

Loader = Callable[[], Optional[bytes]]


def _is_tl(rel: str) -> bool:
    return rel.replace("\\", "/").split("/")[0] == "tl"


class RenPyPlugin(EnginePlugin):
    engine_id = "renpy"
    title = "Ren'Py"

    def detect(self, game_dir: Path) -> Detection:
        conf, details, notes = _detect.detect(Path(game_dir))
        return Detection(confidence=conf, engine_name=self.title, details=details, notes=notes)

    # ---------- извлечение ----------

    def _scripts(self, game_root: Path, archives: List[RpaArchive]) -> Dict[str, Tuple[str, str, Loader]]:
        """База имени скрипта -> (тип rpyc/rpy, откуда, загрузчик). Приоритет: .rpyc, затем .rpy."""
        found: Dict[str, Tuple[str, str, Loader]] = {}

        def offer(rel: str, loader: Loader, where: str) -> None:
            rel = rel.replace("\\", "/")
            if _is_tl(rel) or Path(rel).name.startswith("zz_russificator"):
                return
            base, ext = rel.rsplit(".", 1)
            kind = ext.lower()
            cur = found.get(base)
            rank = {"rpyc": 0, "rpy": 1}[kind]
            if cur is None or rank < {"rpyc": 0, "rpy": 1}[cur[0]]:
                found[base] = (kind, where, loader)

        for p in sorted(game_root.rglob("*.rpy*")):
            if p.suffix in (".rpy", ".rpyc") and p.is_file():
                offer(p.relative_to(game_root).as_posix(), p.read_bytes, "файл")
        for arc in archives:
            for name in arc.names():
                if name.endswith((".rpy", ".rpyc")):
                    offer(name, (lambda a=arc, n=name: a.read(n)), arc.path.name)
        return found

    def extract(self, game_dir: Path, project: TranslationProject) -> ExtractionResult:
        game_root = _detect.find_game_dir(Path(game_dir))
        if game_root is None:
            return ExtractionResult([], warnings=["Папка game/ не найдена."])
        warnings: List[str] = []
        archives = open_archives(game_root)
        for a in archives:
            if a.error:
                warnings.append(f"Архив {a.path.name} не прочитан: {a.error}")
        scripts = self._scripts(game_root, archives)
        if not scripts:
            warnings.append("В игре не найдено скриптов (.rpy/.rpyc) — возможно, архивы защищены.")

        texts: List[rpyc.FoundText] = []
        characters: Dict[str, str] = {}
        fonts: set = set()
        broken = 0
        for base, (kind, where, loader) in sorted(scripts.items()):
            try:
                data = loader()
                if data is None:
                    continue
                if kind == "rpyc":
                    stmts = rpyc.load_rpyc(data)
                    found, chars = rpyc.extract_texts(stmts, base + ".rpy")
                    fonts.update(rpyc.font_references(stmts))
                else:
                    found, chars = rpy_parse.extract_texts(data.decode("utf-8", "replace"), base + ".rpy")
                    fonts.update(re.findall(r"""['"]([^'"\n]+\.(?:ttf|otf|ttc))['"]""", data.decode("utf-8", "replace")))
                texts += found
                characters.update(chars)
            except Exception as exc:  # noqa: BLE001
                broken += 1
                log.warning("Скрипт %s не разобран: %s", base, exc)
        if broken:
            warnings.append(f"Не удалось разобрать скриптов: {broken} (текст из них останется на английском).")

        engine = _common.engine_texts(game_root)       # «Are you sure you want to quit?» и пр. — из движка
        texts += engine
        entries = self._entries(texts, characters)
        known = {e.source for e in entries}
        live = self._live_entries(project, game_root, known)
        entries += live
        for e in entries:
            project.add_entry(e)
        project.meta["renpy_engine_strings"] = len({t.text for t in engine})
        project.meta["renpy_live_found"] = len(live)
        project.meta["renpy_fonts"] = sorted(fonts)
        project.meta["renpy_characters"] = characters
        return ExtractionResult(entries, warnings=warnings)

    def _entries(self, texts: List[rpyc.FoundText], characters: Dict[str, str]) -> List[Entry]:
        entries: List[Entry] = []
        by_file: Dict[str, List[rpyc.FoundText]] = {}
        for t in texts:
            by_file.setdefault(t.file, []).append(t)
        for file, items in by_file.items():
            dialog = [t for t in items if t.kind in ("dialogue", "narration", "choice")]
            pos = {id(t): i for i, t in enumerate(dialog)}
            for n, t in enumerate(items):
                if t.kind == "ui" and (_FILE_RE.match(t.text.strip()) or not looks_translatable(t.text)):
                    continue
                if t.kind in ("dialogue", "narration") and _FILE_RE.match(t.text.strip()):
                    continue
                kind = KIND[t.kind]
                neighbors: List[str] = []
                if id(t) in pos:
                    i = pos[id(t)]
                    neighbors = [x.text for x in dialog[max(0, i - 2):i] + dialog[i + 1:i + 2]]
                speaker = characters.get(t.who or "", t.who) if t.who else None
                key = hashlib.sha1(f"{file}:{t.line}:{n}:{t.text}".encode("utf-8")).hexdigest()[:14]
                entries.append(Entry(id=f"rp{key}", source=t.text, kind=kind, speaker=speaker, scene=file,
                                     neighbors=neighbors,
                                     max_length=relative_limit(t.text, *LIMITS[kind]) if kind in LIMITS else None,
                                     target_key="rt:" + hashlib.sha1(t.text.encode("utf-8")).hexdigest()[:16],
                                     notes={"fits_at_runtime": True}))
        return entries

    # ---------- найденное во время игры ----------

    @staticmethod
    def _missed_files(game_root: Path) -> List[Path]:
        """Журналы хука: в папке игры, а если она была только для чтения — в папке сохранений Ren'Py
        (там журнал знает, какой игры он: поле gamedir)."""
        out = [game_root / MISSED_FILE]
        import os
        appdata = os.environ.get("APPDATA")
        if appdata and (Path(appdata) / "RenPy").is_dir():
            for p in (Path(appdata) / "RenPy").glob(f"*/{MISSED_FILE}"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if Path(str(data.get("gamedir", ""))).resolve() == game_root.resolve():
                        out.append(p)
                except Exception:  # noqa: BLE001
                    continue
        return [p for p in out if p.is_file()]

    @staticmethod
    def _read_missed(path: Path) -> List[str]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
        items = data.get("strings", []) if isinstance(data, dict) else []
        return [s for s in items if isinstance(s, str)]

    def harvest_live(self, game_dir: Path, work_dir: Path) -> int:
        """Перед откатом: строки из журнала хука — в проект (откат удалит журнал вместе с русификацией)."""
        game_root = _detect.find_game_dir(Path(game_dir))
        if game_root is None:
            return 0
        store = Path(work_dir) / LIVE_FILE
        try:
            have = json.loads(store.read_text(encoding="utf-8")) if store.is_file() else []
        except Exception:  # noqa: BLE001
            have = []
        seen, before = set(have), len(have)
        for p in self._missed_files(game_root):
            for s in self._read_missed(p):
                if s not in seen:
                    seen.add(s)
                    have.append(s)
        added = len(have) - before
        if added:
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(json.dumps(have, ensure_ascii=False, indent=0), encoding="utf-8")
        return added

    def _live_entries(self, project: TranslationProject, game_root: Path, known: set) -> List[Entry]:
        """Строки, которые игра показала, а поиск не нашёл (собраны хуком во время игры): они входят
        в следующий словарь — так любой пропуск извлечения закрывается после первой же игры."""
        texts: List[str] = []
        store = Path(project.work_dir) / LIVE_FILE
        if store.is_file():
            try:
                texts += [s for s in json.loads(store.read_text(encoding="utf-8")) if isinstance(s, str)]
            except Exception:  # noqa: BLE001
                pass
        for p in self._missed_files(game_root):
            texts += self._read_missed(p)
        out: List[Entry] = []
        for text in dict.fromkeys(texts):
            if text in known or _FILE_RE.match(text.strip()) or not looks_translatable(text):
                continue
            kind = TextKind.NARRATION if len(text) > 60 and " " in text else TextKind.UI
            key = hashlib.sha1(text.encode("utf-8")).hexdigest()
            out.append(Entry(id=f"rl{key[:14]}", source=text, kind=kind, scene="live",
                             max_length=relative_limit(text, *LIMITS[kind]) if kind in LIMITS else None,
                             target_key="rt:" + key[:16], notes={"fits_at_runtime": True}))
        return out

    # ---------- внедрение ----------

    def inject(self, game_dir: Path, project: TranslationProject) -> None:
        game_root = _detect.find_game_dir(Path(game_dir))
        if game_root is None:
            raise RuntimeError("Папка game/ не найдена — внедрение невозможно.")
        backup = project.backup
        table = build_table(project.entries)
        if not table:
            project.warnings.append("Нет переведённых строк — внедрение пропущено.")
            return
        translations = list(table.values())
        fit = overflowing(project.entries)
        if fit:
            table[META_KEY] = {"fit": fit}
        backup.write_text(game_root / TL_FILE, json.dumps(table, ensure_ascii=False, indent=0, sort_keys=True))
        backup.write_text(game_root / MISSED_FILE, json.dumps({"version": 1, "strings": []}))

        font_map, font_notes = self._fonts(game_root, project, backup, translations)
        project.warnings.extend(font_notes)
        backup.write_text(game_root / HOOK_FILE, hook_script(font_map))
        project.meta["renpy_translations"] = len(table)

    def _fonts(self, game_root: Path, project: TranslationProject, backup,
               translations: List[str] = ()) -> Tuple[Dict, List[str]]:
        """Найти шрифты игры без кириллицы и подменить их копиями с русскими буквами.

        Русские буквы дописываются в сам шрифт игры по его меркам (``fonts/cyrillic.py``) — тот же
        рост, толщина и вид, что у латиницы; копия ложится в ``game/russificator/``, а Ren'Py
        подменяет шрифт через ``config.font_replacement_map``. Если шрифт дополнить нельзя
        (повреждён, это не TrueType/OpenType), его место занимает встроенный PT Sans."""
        from ...fonts import cyrillic
        from ...fonts.cmap import has_cyrillic
        from ...fonts.library import ensure_font

        regular, bold = ensure_font(), ensure_font(bold=True)
        if regular is None:
            return {}, ["Встроенный шрифт не найден — кириллица может отображаться неверно."]
        archives = open_archives(game_root)
        candidates: Dict[str, Optional[bytes]] = {}
        for ref in project.meta.get("renpy_fonts", []):
            candidates[ref.replace("\\", "/")] = None
        for p in game_root.rglob("*"):
            if p.suffix.lower() in (".ttf", ".otf", ".ttc") and p.is_file() and FONT_DIR not in p.parts:
                candidates.setdefault(p.relative_to(game_root).as_posix(), None)
        for a in archives:
            for n in a.names():
                if n.lower().endswith((".ttf", ".otf", ".ttc")):
                    candidates.setdefault(n, None)

        bad: List[str] = []
        datas: Dict[str, bytes] = {}
        for name in candidates:
            data = None
            loose = game_root / name
            if loose.is_file():
                data = loose.read_bytes()
            else:
                for a in archives:
                    data = a.read(name) or (a.read(Path(name).name) if "/" in name else None)
                    if data:
                        break
            if data and not has_cyrillic(data):
                bad.append(name)
                datas[name] = data
        if not bad:
            return {}, []
        fonts_dir = game_root / FONT_DIR
        if not fonts_dir.exists():
            fonts_dir.mkdir(parents=True)
            backup.track_new(fonts_dir)
        need = cyrillic.needed_chars(translations)
        mapping: Dict[Tuple[str, bool, bool], Tuple[str, bool, bool]] = {}
        own: List[str] = []
        fallback: List[str] = []
        used_names: set = set()
        for name in bad:
            patched = cyrillic.add_cyrillic(datas[name], need)
            if patched is not None:
                stem = Path(name).stem
                fname = f"{stem}-ru.ttf"
                k = 2
                while fname.lower() in used_names:
                    fname = f"{stem}-ru{k}.ttf"
                    k += 1
                used_names.add(fname.lower())
                backup.write_bytes(fonts_dir / fname, patched)
                rel = f"{FONT_DIR}/{fname}"
                for alias in {name, Path(name).name}:
                    for b in (False, True):
                        for i in (False, True):
                            mapping[(alias, b, i)] = (rel, b, i)
                own.append(name)
                continue
            fallback.append(name)
        notes: List[str] = []
        if own:
            notes.append("Шрифтам игры без кириллицы дорисованы русские буквы по их меркам: "
                         + ", ".join(sorted(own)[:6]) + (" …" if len(own) > 6 else ""))
        if fallback:
            backup.copy_new(regular, fonts_dir / regular.name)
            if bold:
                backup.copy_new(bold, fonts_dir / bold.name)
            reg_rel = f"{FONT_DIR}/{regular.name}"
            bold_rel = f"{FONT_DIR}/{bold.name}" if bold else reg_rel
            for name in fallback:
                for alias in {name, Path(name).name}:
                    for b in (False, True):
                        for i in (False, True):
                            mapping[(alias, b, i)] = (bold_rel if b else reg_rel, False, i)
            notes.append("Шрифты игры без русских букв заменены на PT Sans: " + ", ".join(sorted(fallback)[:6])
                         + (" …" if len(fallback) > 6 else ""))
        return mapping, notes

    def install_font(self, game_dir: Path, project: TranslationProject, font_path) -> bool:
        return True  # шрифты подключаются в inject (нужен список шрифтов игры)

    def post_inject_instructions(self) -> List[str]:
        return [
            "Запустите игру — текст будет на русском. Если игра уже была открыта, перезапустите её.",
            "Alt+T в игре переключает перевод и оригинал.",
            "Перевод можно поправить вручную в файле game/russificator_tl.json (оригинал → перевод).",
        ]

    def export_plan(self, game_dir: Path, project: TranslationProject, credit: Optional[str]) -> ExportPlan:
        """Всё, что русификатор добавил в игру, — новые файлы: их и копируем (словарь по тексту
        строк подходит и к другой версии игры). Надпись о программе — отдельным экраном."""
        plan = ExportPlan(method="files", exclude=[MISSED_FILE])   # журнал пропусков — только свой
        game_root = _detect.find_game_dir(Path(game_dir))
        if game_root is None or not credit:
            return plan
        rel_root = game_root.relative_to(Path(game_dir).resolve()).as_posix() if game_root.resolve() != \
            Path(game_dir).resolve() else ""
        hook = game_root / HOOK_FILE
        try:
            text = hook.read_text(encoding="utf-8")
        except OSError:
            return plan
        from ...fonts.library import ensure_font
        font = ensure_font()
        prefix = f"{rel_root}/" if rel_root else ""
        if font is not None:
            plan.extra[f"{prefix}{FONT_DIR}/{font.name}"] = font
            plan.replace[f"{prefix}{HOOK_FILE}"] = (text.rstrip("\n") + "\n" + credit_block(credit, font.name)).encode("utf-8")
        return plan


def build_table(entries: List[Entry]) -> Dict[str, str]:
    """Словарь для игры: оригинал -> перевод, плюс варианты без тегов для config.replace_text."""
    table: Dict[str, str] = {}
    for e in entries:
        if not e.translation or e.status not in (EntryStatus.TRANSLATED, EntryStatus.APPROVED):
            continue
        if e.translation == e.source:
            continue
        table.setdefault(e.source, e.translation)
    # config.replace_text получает текст уже без тегов {b}…{/b} — добавим и такие ключи
    for src, dst in list(table.items()):
        if "{" in src:
            s, d = _TAG_RE.sub("", src).strip(), _TAG_RE.sub("", dst).strip()
            if s and d and s != src:
                table.setdefault(s, d)
    return table


def overflowing(entries: List[Entry]) -> List[str]:
    """Подписи и кнопки, чей перевод заметно длиннее оригинала. Сколько у них места на самом деле,
    знает только игра — хук ужимает их при показе, если они шире английского оригинала."""
    out = set()
    for e in entries:
        if e.kind not in (TextKind.UI, TextKind.CHARACTER_NAME) or not e.translation \
                or e.status not in (EntryStatus.TRANSLATED, EntryStatus.APPROVED):
            continue
        src, tr = markup.plain_text(e.source).strip(), markup.plain_text(e.translation).strip()
        if src and len(tr) > len(src) * 1.1:
            out.add(e.source)
    return sorted(out)


def credit_block(credit: str, font_name: str) -> str:
    """Экран с надписью о программе — только в главном меню (для русификаторов «для друзей»)."""
    text = credit.replace("\\", "\\\\").replace('"', '\\"').replace("[", "[[").replace("{", "{{")
    return f'''
## Надпись о программе в главном меню (русификатор создан для раздачи друзьям).
screen russificator_credit():
    zorder 1000
    if main_menu:
        text "{text}":
            font "{FONT_DIR}/{font_name}"
            size max(12, int(config.screen_height / 54))
            color "#ffffffc8"
            outlines [(1, "#000000a0", 0, 0)]
            xalign 0.99
            yalign 0.995

init 999 python:
    def _ru_credit_show():
        try:
            if main_menu and renpy.get_screen("russificator_credit") is None:
                renpy.show_screen("russificator_credit")
        except Exception:
            pass

    if hasattr(config, "always_shown_screens"):
        if "russificator_credit" not in config.always_shown_screens:
            config.always_shown_screens.append("russificator_credit")
    else:
        # Ren'Py до 8.1: экраны overlay в главном меню скрыты — показываем экран сами
        config.start_interact_callbacks.append(_ru_credit_show)
'''


def hook_script(font_map: Dict[Tuple[str, bool, bool], Tuple[str, bool, bool]]) -> str:
    """Скрипт, подключающий перевод штатными хуками Ren'Py (совместим с Python 2 и 3)."""
    fonts = "".join(f"    config.font_replacement_map[{k!r}] = {v!r}\n" for k, v in sorted(font_map.items()))
    return f'''## Русификация (Game Russificator).
## Чтобы вернуть оригинал — «Откатить» в программе или удалите этот файл,
## {TL_FILE} и папку {FONT_DIR}.

init 999 python:
    import json as _ru_json

    def _ru_load():
        try:
            opener = getattr(renpy, "open_file", None) or renpy.file
            f = opener("{TL_FILE}")
            try:
                data = f.read()
            finally:
                f.close()
            if not isinstance(data, str):
                data = data.decode("utf-8")
            return _ru_json.loads(data)
        except Exception:
            return {{}}

    _ru_tl = _ru_load()
    _ru_meta = _ru_tl.pop(u"\\u0000russificator", None) or {{}}
    _ru_prev_say = config.say_menu_text_filter

    # Русская реплика длиннее английской и может не влезть в окно диалога
    # (оно фиксированной высоты). Если английская помещалась, а русская нет —
    # шрифт реплики уменьшается тегом {{size=-N}} ровно настолько, чтобы влезла.
    def _ru_box():
        try:
            w = gui.dialogue_width
            h = gui.textbox_height - gui.dialogue_ypos
            if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                return w, h
        except Exception:
            pass
        return None

    def _ru_adv_say():
        try:
            node = renpy.game.script.lookup(renpy.game.context().current)
            if type(node).__name__ != "Say":
                return False
            who = getattr(node, "who", None)
            ch = getattr(store, who, None) if who else None
            return not isinstance(ch, NVLCharacter)
        except Exception:
            return False

    def _ru_height(s, w):
        return Text(s, style="say_dialogue").size(width=w)[1]

    def _ru_fit(orig, s):
        box = _ru_box()
        if box is None or not _ru_adv_say():
            return s
        w, h = box
        try:
            if _ru_height(s, w) <= h or _ru_height(orig, w) > h:
                return s
            base = style.say_dialogue.size or gui.text_size
            for k in range(1, 9):
                t = "{{size=-%d}}%s{{/size}}" % (int(round(base * 0.04 * k)), s)
                if _ru_height(t, w) <= h:
                    return t
            return t
        except Exception:
            return s

    # Alt+T — перевод / оригинал (интерфейс сразу, реплика — со следующей)
    _ru_on = [True]
    _ru_measure = [False]        # замер ширины английского оригинала — без перевода

    def _ru_toggle():
        _ru_on[0] = not _ru_on[0]
        try:
            renpy.notify(u"Перевод: вкл" if _ru_on[0] else u"Перевод: выкл (оригинал)")
        except Exception:
            pass
        renpy.restart_interaction()

    try:
        config.underlay.append(renpy.Keymap(alt_K_t=_ru_toggle))
    except Exception:
        pass

    # Журнал: строки, которые игра показала, а словаря для них нет (поиск текста их пропустил —
    # собраны кодом игры, лежат в необычном месте). Следующая русификация берёт их в словарь,
    # так любой пропуск закрывается после первой же игры. Пишется в папку игры, а если она
    # только для чтения — в папку сохранений.
    import collections as _ru_coll, io as _ru_io, os as _ru_os, re as _ru_re, time as _ru_time
    _ru_missed = _ru_coll.OrderedDict()
    _ru_jstate = _ru_coll.OrderedDict([("dirty", False), ("at", 0.0)])
    _ru_latin = _ru_re.compile(r"[A-Za-z]{{2}}")
    _ru_cyr = _ru_re.compile(u"[\\u0400-\\u04ff]")

    def _ru_journals():
        out = []
        try:
            out.append(_ru_os.path.join(config.gamedir, "{MISSED_FILE}"))
            if config.savedir:
                out.append(_ru_os.path.join(config.savedir, "{MISSED_FILE}"))
        except Exception:
            pass
        return out

    for _ru_p in _ru_journals():
        try:
            with _ru_io.open(_ru_p, encoding="utf-8") as _ru_f:
                for _ru_s in _ru_json.loads(_ru_f.read()).get("strings", []):
                    _ru_missed[_ru_s] = True
        except Exception:
            pass

    def _ru_miss(s):
        try:
            # название игры (заголовок окна) — имя продукта, его не переводим
            if s in (getattr(config, "name", None), getattr(config, "window_title", None)):
                return
            if (s not in _ru_missed and len(_ru_missed) < 20000 and len(s) < 4000
                    and _ru_latin.search(s) and not _ru_cyr.search(s)):
                _ru_missed[s] = True
                _ru_jstate["dirty"] = True
        except Exception:
            pass

    def _ru_flush(*args, **kwargs):
        try:
            now = _ru_time.time()
            if not _ru_jstate["dirty"] or now - _ru_jstate["at"] < 5:
                return
            _ru_jstate["dirty"], _ru_jstate["at"] = False, now
            data = _ru_json.dumps({{"version": 1, "gamedir": config.gamedir, "strings": list(_ru_missed)}})
            if not isinstance(data, type(u"")):
                data = data.decode("ascii")
            for p in _ru_journals():
                try:
                    with _ru_io.open(p, "w", encoding="utf-8") as f:
                        f.write(data)
                    return
                except Exception:
                    continue
        except Exception:
            pass

    try:
        config.interact_callbacks.append(_ru_flush)
    except Exception:
        pass

    def _ru_say(s):
        if _ru_prev_say is not None:
            s = _ru_prev_say(s)
        if not _ru_on[0]:
            return s
        t = _ru_tl.get(s, s)
        if t is not s:
            t = _ru_fit(s, t)
        else:
            _ru_miss(s)
        return t

    config.say_menu_text_filter = _ru_say

    # Текст экранов и _("…") переводит штатный переводчик строк Ren'Py: он видит строку
    # такой, как она записана в скрипте, — до подстановки [переменных], вместе с тегами
    # ({{size=24}}Name: [player_name]{{/size}}, {{#quick_page}}Quick) и до .format() у
    # шаблонов вроде _("Page {{}}"). config.replace_text ниже получает уже готовый текст
    # («Name: sdf», «Page 1»), разрезанный по тегам, — он ловит только остатки.
    try:
        _ru_stl = renpy.translation.StringTranslator
        _ru_prev_translate = _ru_stl.translate

        def _ru_translate(self, old):
            new = _ru_prev_translate(self, old)
            if new != old or not _ru_on[0] or _ru_measure[0]:
                return new
            t = _ru_tl.get(old)
            if t is None:
                _ru_miss(old)
                return new
            return t

        _ru_stl.translate = _ru_translate
    except Exception:
        pass

    # Кнопка или подпись, чей перевод длиннее оригинала (список — в словаре): если Ren'Py рисует
    # её шире, чем рисовал английский текст тем же стилем, надпись ужимается до ширины оригинала —
    # под неё в игре было место, а за ним может быть край экрана или соседняя кнопка.
    _ru_fitset = set(_ru_meta.get("fit", ()))
    _ru_strs = (type(""), type(u""))
    _ru_enw = _ru_coll.OrderedDict()

    def _ru_en_width(text, src, width, height):
        key = (src, id(text.style), int(width))
        w = _ru_enw.get(key)
        if w is None:
            _ru_measure[0] = True
            try:
                # Text(style=готовый стиль) даёт несобранный стиль — берём то, от чего зависит ширина
                st = text.style
                en = Text(src, substitute=False, font=st.font, size=st.size, bold=st.bold,
                          italic=st.italic, kerning=getattr(st, "kerning", 0) or 0).size(width, height)[0]
                # место под надпись обычно шире её самой: запас — четыре буквы этого шрифта или
                # четверть ширины; ужимается только то, что вылезает и за него
                w = max(en * 1.25, en + 4 * float(st.size or 0))
            finally:
                _ru_measure[0] = False
            _ru_enw[key] = w
            if len(_ru_enw) > 3000:
                _ru_enw.popitem(last=False)
        return w

    try:
        _ru_Text = renpy.text.text.Text
        _ru_prev_render = _ru_Text.render

        def _ru_render(self, width, height, st, at):
            rv = _ru_prev_render(self, width, height, st, at)
            try:
                if not _ru_fitset or not _ru_on[0] or _ru_measure[0]:
                    return rv
                # не isinstance(src, list): в скриптах Ren'Py list — это RevertableList
                src = getattr(self, "text_parameter", None)
                if src is None or isinstance(src, _ru_strs) or len(src) != 1:
                    return rv
                if not isinstance(src[0], _ru_strs) or src[0] not in _ru_fitset:
                    return rv
                room = _ru_en_width(self, src[0], width, height)
                if rv.width <= room:
                    return rv
                k = max(0.6, room / float(rv.width))
                inner = renpy.Render(rv.width, rv.height)
                inner.blit(rv, (0, 0))
                inner.zoom(k, k)
                out = renpy.Render(rv.width * k, rv.height * k)
                out.blit(inner, (0, 0))
                return out
            except Exception:
                return rv

        _ru_Text.render = _ru_render
    except Exception:
        pass

    if hasattr(config, "replace_text"):
        _ru_prev_replace = config.replace_text

        def _ru_replace(s):
            if _ru_prev_replace is not None:
                s = _ru_prev_replace(s)
            if not _ru_on[0] or _ru_measure[0]:
                return s
            return _ru_tl.get(s, s)

        config.replace_text = _ru_replace

{fonts}'''


from ...core.registry import register  # noqa: E402
RenPyPlugin = register(RenPyPlugin)
