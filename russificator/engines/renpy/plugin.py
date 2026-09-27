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
from ...translation.filters import looks_translatable
from . import detect as _detect
from . import rpy_parse, rpyc
from .archive import RpaArchive, open_archives

log = logging.getLogger("russificator.renpy")

KIND = {"dialogue": TextKind.DIALOGUE, "narration": TextKind.NARRATION, "choice": TextKind.CHOICE,
        "name": TextKind.CHARACTER_NAME, "ui": TextKind.UI}
MAX_LENGTHS = {TextKind.CHOICE: 70, TextKind.UI: 40, TextKind.CHARACTER_NAME: 24}

TL_FILE = "russificator_tl.json"
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

        entries = self._entries(texts, characters)
        for e in entries:
            project.add_entry(e)
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
                                     neighbors=neighbors, max_length=MAX_LENGTHS.get(kind),
                                     target_key="rt:" + hashlib.sha1(t.text.encode("utf-8")).hexdigest()[:16]))
        return entries

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
        backup.write_text(game_root / TL_FILE, json.dumps(table, ensure_ascii=False, indent=0, sort_keys=True))

        font_map, font_notes = self._fonts(game_root, project, backup)
        project.warnings.extend(font_notes)
        backup.write_text(game_root / HOOK_FILE, hook_script(font_map))
        project.meta["renpy_translations"] = len(table)

    def _fonts(self, game_root: Path, project: TranslationProject, backup) -> Tuple[Dict, List[str]]:
        """Найти шрифты игры без кириллицы и подменить их встроенным PT Sans."""
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
        if not bad:
            return {}, []
        fonts_dir = game_root / FONT_DIR
        if not fonts_dir.exists():
            fonts_dir.mkdir(parents=True)
            backup.track_new(fonts_dir)
        backup.copy_new(regular, fonts_dir / regular.name)
        if bold:
            backup.copy_new(bold, fonts_dir / bold.name)
        reg_rel = f"{FONT_DIR}/{regular.name}"
        bold_rel = f"{FONT_DIR}/{bold.name}" if bold else reg_rel
        mapping: Dict[Tuple[str, bool, bool], Tuple[str, bool, bool]] = {}
        for name in bad:
            for alias in {name, Path(name).name}:
                for b in (False, True):
                    for i in (False, True):
                        mapping[(alias, b, i)] = (bold_rel if b else reg_rel, False, i)
        note = ("Шрифты игры без русских букв заменены на PT Sans: " + ", ".join(sorted(bad)[:6])
                + (" …" if len(bad) > 6 else ""))
        return mapping, [note]

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
        plan = ExportPlan(method="files")
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
    if hasattr(config, "always_shown_screens"):
        if "russificator_credit" not in config.always_shown_screens:
            config.always_shown_screens.append("russificator_credit")
    elif "russificator_credit" not in config.overlay_screens:
        config.overlay_screens.append("russificator_credit")
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

    def _ru_say(s):
        if _ru_prev_say is not None:
            s = _ru_prev_say(s)
        if not _ru_on[0]:
            return s
        t = _ru_tl.get(s, s)
        if t is not s:
            t = _ru_fit(s, t)
        return t

    config.say_menu_text_filter = _ru_say

    if hasattr(config, "replace_text"):
        _ru_prev_replace = config.replace_text

        def _ru_replace(s):
            if _ru_prev_replace is not None:
                s = _ru_prev_replace(s)
            if not _ru_on[0]:
                return s
            return _ru_tl.get(s, s)

        config.replace_text = _ru_replace

{fonts}'''


from ...core.registry import register  # noqa: E402
RenPyPlugin = register(RenPyPlugin)
