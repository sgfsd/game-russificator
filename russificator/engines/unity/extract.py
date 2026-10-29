"""Статическое извлечение текста Unity — для заранее подготовленного перевода.

Файлы игры здесь только читаются. Перевод потом подставляется в игре на
лету через XUnity.AutoTranslator (см. ``xunity.py``), поэтому ошибка
«приняли служебную строку за текст» стоит только лишних переводов, но
никогда не ломает игру.

Источники:
  1. компоненты текста в сценах и ассетах: UGUI ``Text.m_Text``,
     TextMeshPro ``m_text``, NGUI ``UILabel.mText``, ``TextMesh``; поля
     MonoBehaviour читаются через typetree, сгенерированный из сборок игры
     (релизные сборки хранят данные без описания полей);
  2. прочие строковые поля скриптов (диалоги в ScriptableObject и т.п.) —
     только то, что похоже на фразы;
  3. строки, зашитые в код: куча ``#US`` .NET-сборок (Mono) или строковые
     литералы ``global-metadata.dat`` (IL2CPP);
  4. скрипты, для которых typetree не сошёлся с данными (генератор не понял
     какое-то поле), — строки читаются прямо из сырых байтов объекта
     (строка Unity: int32 длина + UTF-8 + выравнивание на 4);
  5. страницы встроенного браузера ZFBrowser (см. ``zfbrowser.py``).

Для компонентов текста запоминается и путь объекта в сцене с размером
шрифта: если русский текст длиннее, XUnity уменьшит шрифт именно этого
элемента, а не будет переносить слова по буквам (см. ``xunity.write_resizer``).
"""

from __future__ import annotations

import hashlib
import logging
from functools import lru_cache
import re
import struct
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ...core.universal import Entry, TextKind
from ...translation.filters import (halfwidth, looks_like_sentence, looks_spoken, looks_technical,
                                    looks_translatable)
from . import direct, text_assets, unitypy_io
from .detect import UnityGame

log = logging.getLogger("russificator.unity.extract")

#: поле текста -> поле, по которому узнаётся компонент (None — само поле уникально)
TEXT_COMPONENT_FIELDS = {
    "m_Text": "m_FontData",       # UnityEngine.UI.Text
    "m_text": "m_fontAsset",      # TMPro.TextMeshProUGUI / TextMeshPro
    "mText": "mFont",             # NGUI UILabel (bitmap)
}
NGUI_ALT = "mTrueTypeFont"

#: служебные поля, строки из которых не переводим
SKIP_FIELDS = {"m_Name", "m_TagString", "m_ClassName", "m_Namespace", "m_AssemblyName", "name", "id", "ID",
               "key", "Key", "guid", "path", "url", "tag", "m_EditorClassIdentifier", "m_PropertyPath",
               "m_MethodName", "m_TargetAssemblyTypeName", "m_ObjectArgumentAssemblyTypeName",
               "m_StringArgument", "m_HorizontalAxis", "m_VerticalAxis", "m_SubmitButton", "m_CancelButton",
               "m_InputActionReferencePoint", "animationName", "sceneName", "m_SceneName"}

#: сборки фреймворков — их строки не текст игры
FRAMEWORK_PREFIXES = ("UnityEngine", "Unity.", "System", "Mono.", "mscorlib", "netstandard", "Microsoft.",
                      "TextMeshPro", "DOTween", "DemiLib", "Newtonsoft", "Boo.Lang", "UnityScript", "Rewired",
                      "Steamworks", "Facepunch", "Photon", "FMOD", "Cinemachine", "com.unity", "Sirenix",
                      "I18N", "Mirror", "Zenject", "UniRx", "Assembly-CSharp-Editor", "BepInEx", "0Harmony",
                      "MonoMod", "Accessibility", "Purchasing", "Analytics", "PlayFab", "Discord", "Galaxy",
                      "GalaxyCSharp", "EOS", "Epic", "Xbox", "Microsoft", "AstarPathfinding", "Pathfinding",
                      "Febucci", "Lean", "Obi", "NativeGallery", "Kino", "PostProcessing", "Coffee")

ProgressFn = Callable[[str, Optional[float]], None]

#: явно технические строки (имена типов, пути шейдеров, форматы дат, теги логов)
#: клавиша в подсказке «[E] to interact», «[Space] Jump» — это текст, а не метка журнала «[Info]»
_KEY_HINT = (r"(?:[A-Za-z0-9]|Space|Esc|Escape|Tab|Shift|Ctrl|Alt|Enter|Return|Backspace|Del|LMB|RMB|MMB|"
             r"F\d{1,2}|Mouse ?\d|Left Click|Right Click|Up|Down|Left|Right)")
_TECH_RE = re.compile(r"PublicKeyToken|Version=\d|Culture=|\bUnityEngine\.|\bSystem\.[A-Z]|^Hidden/|"
                      r"^[\w\- ]+/[\w\- ]+/|yyyy|HH:mm|^\[(?!" + _KEY_HINT + r"\])[\w. ]+\]|NullReference|"
                      r"Exception\b|\bshaders?\b|\.cs\b|\bdebug\b|\bSteamworks\b", re.I)


def _noop(message: str, fraction: Optional[float] = None) -> None:
    pass


def text_key(text: str) -> str:
    return "xua:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


#: XUnity не переводит строки длиннее (MaxCharactersPerTranslation, предел плагина)
MAX_TEXT = 2500


class _Collector:
    """Уникальные строки с «лучшим» типом (UI важнее прочего)."""

    def __init__(self) -> None:
        self.items: Dict[str, Tuple[TextKind, str]] = {}
        self.font_files: List[Path] = []   # файлы, где есть шрифты (для проверки кириллицы)
        self.ui: List[dict] = []           # компоненты текста: путь в сцене, размер шрифта (для resizer)
        self.prefixes: List[str] = []      # подписи из кода, к которым игра приклеивает текст («Pro Tip: »)
        self.pages: List[Entry] = []       # фразы страниц встроенного браузера
        #: все встреченные строки до отбора -> где встретились (только для проверки полноты перевода)
        self.seen: Optional[Dict[str, str]] = None
        #: строки, найденные уверенно (не только «для словаря»): их можно вписывать и в данные игры
        self.confident: set = set()

    def see(self, text: str, origin: str) -> None:
        if self.seen is not None and isinstance(text, str) and text.strip() and len(text) <= 20000:
            self.seen.setdefault(text, origin)

    def add(self, text: str, kind: TextKind, origin: str, display_only: bool = False) -> None:
        """``display_only`` — строка найдена по смелому признаку (подпись строчными в текстовом поле, английский
        вариант рядом с переводами на другие языки): она идёт только в словарь XUnity, который меняет лишь
        то, что игра показывает, и в данные игры (прямой перевод) не вписывается."""
        # текст не нормализуем: ключ XUnity должен совпасть с тем, что игра выводит на экран
        self.see(text, origin)
        if not text.strip() or len(text) > MAX_TEXT or _TECH_RE.search(text) or looks_technical(text):
            return
        if not display_only:
            self.confident.add(text)
        prev = self.items.get(text)
        if prev is None or (prev[0] == TextKind.OTHER and kind != TextKind.OTHER):
            self.items[text] = (kind, origin)

    def add_prefix(self, p: str) -> None:
        """Подпись, к которой игра приклеивает текст («Pro Tip: », «Narrator:: »): сама переводится как
        строка, а для склейки XUnity получает правило-разделитель (см. ``xunity.prefix_rules``)."""
        if p in self.prefixes or len(self.prefixes) >= 1000:
            return
        self.prefixes.append(p)
        self.add(p, TextKind.UI, "prefix")

    def entries(self) -> List[Entry]:
        out = []
        for text, (kind, origin) in self.items.items():
            out.append(Entry(id=text_key(text), source=text, kind=kind, target_key=text_key(text),
                             scene=origin, max_length=None,
                             notes={} if text in self.confident else {"display_only": True}))
        return out + self.pages


# ---------- ассеты ----------

def serialized_files(game: UnityGame) -> List[Path]:
    data = game.data
    files: List[Path] = []
    for p in sorted(data.iterdir()):
        if not p.is_file():
            continue
        n = p.name.lower()
        if n.endswith((".ress", ".resource", ".json", ".txt", ".bmp", ".info", ".config", ".dll")):
            continue
        if n.startswith("level") or n.endswith(".assets") or n in ("maindata", "data.unity3d") \
                or n.endswith((".unity3d", ".bundle")):
            if n == "globalgamemanagers.assets":
                continue
            files.append(p)
    sa = data / "StreamingAssets"
    if sa.is_dir():
        bundles = [p for p in sa.rglob("*") if p.is_file() and p.stat().st_size > 64 and _is_bundle(p)]
        files += [p for p in bundles if not _foreign_bundle(p, bundles)]
    return files


def _foreign_bundle(path: Path, bundles: List[Path]) -> bool:
    """Бандл таблицы строк другого языка (пакет Unity Localization кладёт каждый язык в свой бандл:
    ``…string-tables-danish(denmark)(da-dk)…``) — если рядом есть такой же английский. Иначе в словарь
    попал бы датский или португальский текст, «переведённый» как английский."""
    toks = _lang_tokens(re.sub(r"[()\[\]]", "_", path.stem))
    hit = [i for i, t in enumerate(toks) if t in _LANGS]
    if not hit or any(t in _ENGLISH for t in toks) or hit[0] < 1:
        return False
    prefix = toks[:hit[0]]
    for other in bundles:
        if other.parent != path.parent or other == path:
            continue
        ot = _lang_tokens(re.sub(r"[()\[\]]", "_", other.stem))
        if ot[:len(prefix)] == prefix and any(t in _ENGLISH for t in ot[len(prefix):]):
            return True
    return False


def _is_bundle(path: Path) -> bool:
    """Бандл Unity — и под простой защитой (ключ XOR, байты впереди), см. ``unitypy_io.layout``."""
    return unitypy_io.layout(path) is not None


def _typetree_generator(game: UnityGame, warnings: List[str]):
    try:
        from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
    except ImportError:
        warnings.append("Генератор typetree недоступен — текст из скриптов сцен будет собран в игре на лету.")
        return None
    try:
        gen = TypeTreeGenerator(game.version or "2019.4.0f1")
        if game.backend == "il2cpp":
            ga = game.root / "GameAssembly.dll"
            meta = game.data / "il2cpp_data" / "Metadata" / "global-metadata.dat"
            gen.load_il2cpp(ga.read_bytes(), meta.read_bytes())
        else:
            gen.load_local_game(str(game.root))
        return gen
    except Exception as exc:  # noqa: BLE001
        log.info("typetree не построен: %s", exc)
        warnings.append("Сборки игры не удалось разобрать (возможно, защищены) — текст из сцен "
                        "будет переведён в игре на лету.")
        return None


def extract_assets(game: UnityGame, col: _Collector, warnings: List[str], progress: ProgressFn = _noop) -> None:
    try:
        import UnityPy
    except ImportError:
        warnings.append("UnityPy не установлен — заранее переведено будет меньше, остальное переведётся на лету.")
        return
    unitypy_io.use_game_version(game.version)
    gen = _typetree_generator(game, warnings)
    files = serialized_files(game)
    donors = None
    for i, f in enumerate(files):
        progress(f"Чтение ассетов: {f.name}", i / max(1, len(files)))
        try:
            env = unitypy_io.open_env(f)
        except Exception as exc:  # noqa: BLE001
            log.debug("не открыт %s: %s", f.name, exc)
            continue
        has_font = False
        try:
            if gen is not None:
                env.typetree_generator = gen
            scene = f.name.lower().startswith("level")
            trees = _TreeCache()
            text_assets_found: List[object] = []
            for obj in env.objects:
                tname = obj.type.name
                try:
                    if tname == "Font":
                        has_font = True
                    elif tname == "MonoBehaviour":
                        try:
                            tree = obj.read_typetree()
                        except Exception:  # noqa: BLE001
                            # генератор ошибся в структуре скрипта — она же есть в бандлах игры (донор);
                            # иначе строки берутся из сырых байтов
                            if donors is None:
                                donors = _donors(files)
                            tree = direct.read_structured(obj, donors) if donors else None
                            if tree is None:
                                _from_raw(obj, col, f.name)
                                continue
                        if "m_glyphInfoList" in tree or "m_GlyphTable" in tree:
                            has_font = True
                            continue
                        comp = _from_monobehaviour(tree, col, f.name)
                        if comp is not None:
                            _remember_ui(obj, tree, comp, col, trees, scene)
                    elif tname == "TextMesh":
                        tree = obj.read_typetree()
                        val = tree.get("m_Text")
                        col.see(val, f.name)
                        if isinstance(val, str) and looks_translatable(val):
                            col.add(val, TextKind.UI, f.name)
                    elif tname == "TextAsset":
                        text_assets_found.append(obj)
                except Exception:  # noqa: BLE001
                    continue
            for obj in _drop_foreign_text_assets(text_assets_found):
                try:
                    _from_text_asset(obj, col, f.name)
                except Exception:  # noqa: BLE001
                    continue
        finally:
            unitypy_io.release(env)   # файлы игры не остаются открытыми
        if has_font:
            col.font_files.append(f)


def _donors(files: List[Path]) -> dict:
    """Структуры скриптов из бандлов игры (бандлы всегда хранят их в себе), см. ``direct.typetree_donors``."""
    try:
        return direct.typetree_donors([p for p in files if "StreamingAssets" in p.parts
                                       or p.suffix.lower() in (".bundle", ".unity3d")])
    except Exception as exc:  # noqa: BLE001
        log.debug("структуры скриптов из бандлов не прочитаны: %s", exc)
        return {}


#: ассеты с именами людей (титры, спонсоры, тестеры) — имена не переводятся
NAMES_ASSET_RE = re.compile(r"credit|patron|tester|supporter|backer|sponsor|thank|translator|contributor|donor", re.I)


def _asset_name(obj) -> str:
    try:
        return str(obj.peek_name() or "")
    except Exception:  # noqa: BLE001
        try:
            return str(obj.read().m_Name or "")
        except Exception:  # noqa: BLE001
            return ""


def _drop_foreign_text_assets(objs: List[object]) -> List[object]:
    """Текстовые ассеты без таблиц других языков: игры кладут локализацию ассетами «English», «French»,
    «Chinese Simplified»… — переводить нужно английскую, остальные в словаре только мешают (французский
    текст «переводился бы» как английский). Чужой язык отбрасывается, только если английский рядом есть."""
    named = [(o, _lang_tokens(re.sub(r"[(),\[\]]", " ", _asset_name(o)))) for o in objs]
    if not any(t and set(t) & _ENGLISH for _, t in named):
        return objs
    out = []
    for o, toks in named:
        if toks and set(toks) <= (_LANGS | {"simplified", "traditional", "brazilian", "portugal", "brazil",
                                            "latin", "american", "spain", "mexico", "chinese"})                 and not set(toks) & _ENGLISH:
            continue
        out.append(o)
    return out


def _from_text_asset(obj, col: _Collector, origin: str) -> None:
    """Диалоги и таблицы в текстовых ассетах (JSON, CSV, XML, Ink, Yarn…), см. ``text_assets.py``."""
    from . import text_assets
    data = obj.read()
    script = getattr(data, "m_Script", None)
    if isinstance(script, str):
        raw = script.encode("utf-8", "surrogateescape")
    elif isinstance(script, (bytes, bytearray, memoryview)):
        raw = bytes(script)
    else:
        return
    text = text_assets.decode(raw)
    name = getattr(data, "m_Name", "") or ""
    where = f"{origin}:{name}"
    if text:
        text_assets.extract(text, lambda s: col.add(s, TextKind.OTHER, where), lambda s: col.see(s, where),
                            tables=not NAMES_ASSET_RE.search(name), prefix=col.add_prefix)
    elif text is None and not NAMES_ASSET_RE.search(name):
        _from_binary_table(raw, col, where)


def _from_binary_table(raw: bytes, col: _Collector, origin: str) -> None:
    """Двоичная таблица строк (Luban, BinaryWriter, protobuf): фразы и строки-форматы — как из любых данных,
    английский вариант многоязычной таблицы (рядом китайский, японский…) — и одним словом («Skip»)."""
    for s, near_foreign in text_assets.binary_texts(raw):
        col.see(s, origin)
        if not looks_translatable(s, strict=True):
            if near_foreign:
                _add_display(s, col, origin)
            continue
        if looks_like_sentence(s) or _format_text(s):
            col.add(s, TextKind.OTHER, origin)
        elif near_foreign and (_NAME_RE.fullmatch(s) or _english_words(s)):
            # английская колонка таблицы: и подпись («Basic Settings»), и имя («Su Lianyan»)
            col.add(s, TextKind.OTHER, origin, display_only=True)


#: имя или название из слов с заглавной: «Su Lianyan», «Jinling City»
_NAME_RE = re.compile(r"[A-Z][a-z']+(?:[ -][A-Z][a-z']+){0,3}")


def _from_monobehaviour(tree: dict, col: _Collector, origin: str) -> Optional[str]:
    """Собрать текст скрипта. Для компонента текста вернуть его поле (m_Text/m_text/mText)."""
    for grids in direct.utage_grid_lists(tree):
        _from_utage_grids(grids, col, origin)
    for field, marker in TEXT_COMPONENT_FIELDS.items():
        val = tree.get(field)
        if isinstance(val, str) and (marker in tree or (field == "mText" and NGUI_ALT in tree)):
            col.see(val, origin)
            if looks_translatable(val):
                col.add(val, TextKind.UI, origin)
            return field
    _walk_strings(tree, col, origin, depth=0)
    return None


def _from_utage_grids(grids: list, col: _Collector, origin: str) -> None:
    """Книга сценария UTAGE: колонка для экрана (English, иначе Text) — реплики и имена персонажей,
    которые игра заведомо показывает: берутся и «Y-You…!», и «female» (имя из таблицы Localize)."""
    from .direct import utage_grids
    for rows, column in utage_grids(grids):
        for r in rows:
            cells = r.get("strings") if isinstance(r, dict) else None
            if isinstance(cells, list) and column < len(cells) and isinstance(cells[column], str):
                v = cells[column]
                col.see(v, origin)
                if looks_spoken(v):
                    col.add(v, TextKind.OTHER, origin)


#: поля скриптов, где короткая строка — скорее всего видимое название («Motion Sensor»)
_LABEL_FIELD_RE = re.compile(r"title|label|caption|header|display|product|item|desc|text|message|hint|tip|"
                             r"name|button|option|objective|quest|note|subject|content|question|answer|localiz|term|"
                             r"translation|english|subtitle|tooltip|prompt|dialog|line", re.I)


def _walk_strings(node, col: _Collector, origin: str, depth: int, field: str = "") -> None:
    if depth > 6:
        return
    if isinstance(node, dict):
        english = _english_of_languages(node)
        if english is not None:
            _add_display(english, col, origin)
        for k, v in node.items():
            if k in SKIP_FIELDS:
                continue
            if isinstance(v, str):
                col.see(v, origin)
                _maybe_add(v, k, col, origin)
            elif isinstance(v, (dict, list)):
                _walk_strings(v, col, origin, depth + 1, k)
    elif isinstance(node, list):
        strings = [v for v in node[:64] if isinstance(v, str)]
        if 3 <= len(strings) <= 40 and len(strings) == len(node) and _multilingual(strings):
            for v in strings:
                if v.isascii():
                    _add_display(v, col, origin)    # английский вариант среди переводов строки
        for v in node[:5000]:
            if isinstance(v, str):
                col.see(v, origin)
                _maybe_add(v, field, col, origin)
            elif isinstance(v, (dict, list)):
                _walk_strings(v, col, origin, depth + 1, field)


#: письменности, по которым видно, что рядом лежат переводы строки на другие языки
_FOREIGN_SCRIPT_RE = re.compile(r"[Ѐ-ӿͰ-Ͽ֐-ۿ฀-๿぀-ヿ"
                                r"㐀-鿿가-힯]")
#: поле с английским текстом: en, english, en_US, textEn, text_en
_EN_FIELD_RE = re.compile(r"^(?:m_)?(?:en|eng|english|en[_-]?(?:us|gb)|text[_-]?en|en[_-]?text|english[_-]?text)$",
                          re.I)
#: поля других языков рядом с английским: ja, zh_CN, korean, french…
_LANG_FIELD_RE = re.compile(r"^(?:m_)?(?:ja|jp|zh|cn|tw|ko|kr|ru|fr|de|es|it|pt|pl|tr|th|ar|uk|japanese|chinese|"
                            r"korean|russian|french|german|spanish|italian|portuguese|polish|turkish|thai|arabic)"
                            r"(?:[_-]?\w{2,4})?$", re.I)
#: поля, в которых строка — текст для экрана по самому названию поля (UGUI Dropdown, свои кнопки и подписи)
_TEXT_FIELDS = {"m_Text", "m_text", "text", "Text", "mText", "label", "Label", "m_Label", "title", "Title", "m_Title",
                "caption", "Caption", "displayName", "DisplayName", "m_DisplayName", "displayText", "DisplayText",
                "description", "Description", "m_Description", "tooltip", "Tooltip", "subtitle", "Subtitle",
                "buttonText", "ButtonText", "header", "Header", "message", "Message"}


def _multilingual(strings: List[str]) -> bool:
    """Список строк — одна фраза на нескольких языках (японский, корейский, тайский… и латиница рядом)."""
    foreign = sum(1 for v in strings if _FOREIGN_SCRIPT_RE.search(v))
    latin = sum(1 for v in strings if v.isascii() and re.search(r"[A-Za-z]{2}", v))
    return foreign >= 2 and latin >= 1


def _english_of_languages(node: dict) -> Optional[str]:
    """Английская строка из записи «язык -> текст» ({"en": …, "ja": …, "ko": …})."""
    en = [v for k, v in node.items() if isinstance(v, str) and _EN_FIELD_RE.match(str(k))]
    if len(en) != 1:
        return None
    others = sum(1 for k, v in node.items() if isinstance(v, str) and _LANG_FIELD_RE.match(str(k)))
    return en[0] if others >= 1 else None


def _add_display(v: str, col: _Collector, origin: str) -> None:
    """Строка, которую место в данных выдаёт за текст для экрана: только в словарь (см. ``_Collector.add``).
    Признак смелый, поэтому строка ещё должна быть английской: слова — из словаря английского (турецкий
    вариант «Kilitli.» рядом с английским, ромадзи «syuugou» отсеиваются), одно слово — без цифр и
    служебных знаков («girl2», «CGSample12», «*Jump»)."""
    if len(v) > 400 or not looks_spoken(v) or _format_text(v) or not _english_words(v):
        return
    col.add(v, TextKind.OTHER, origin, display_only=True)


def _english_words(v: str) -> bool:
    core = re.sub(r"[：，；！？]", lambda m: halfwidth(m.group(0)) + " ", v).strip()   # «WorkBench：Cultivate»
    if re.search(r"[=_{}<>\[\]\|@#$^]", core):
        return False                      # «time=0.5 x=200», «<tag>» — параметры и разметка, не фраза
    if " " not in core and not re.fullmatch(r"[\"'«“(]?[A-Za-z][A-Za-z'-]*[.!?…,:;)\"'»”]*", core):
        return False
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z']+", core)]
    if not words:
        return False
    lex = _lexicon()
    if not lex:
        return True                       # словаря нет — признак места остаётся единственным
    known = sum(1 for w in words if _known_word(w, lex) or _known_word(w.split("'")[0], lex))
    return known >= max(1, 0.6 * len(words)) and any(len(w) >= 3 and _known_word(w, lex) for w in words)


#: в словаре (SCOWL) только основы: нет «is», «recover», «refine» — словоформы проверяются по основе
_FUNCTION_WORDS = frozenset("is are was were be been am has had does did an the of to in on at by for with "
                            "and or not no it its this that".split())
_SUFFIXES = ("ing", "ers", "ed", "es", "er", "ly", "s", "d")
_PREFIXES = ("re", "un", "dis", "pre", "over", "out", "mis")


def _known_word(w: str, lex, depth: int = 0) -> bool:
    if w in lex or w in _FUNCTION_WORDS:
        return True
    for suf in _SUFFIXES:
        stem = w[:-len(suf)]
        if w.endswith(suf) and len(stem) >= 3 and (stem in lex or stem + "e" in lex):
            return True
    if depth == 0:
        return any(w.startswith(p) and len(w) - len(p) >= 3 and _known_word(w[len(p):], lex, 1) for p in _PREFIXES)
    return False


@lru_cache(maxsize=1)
def _lexicon() -> frozenset:
    from ...overlay.words import lexicon
    return frozenset(lexicon())


def _maybe_add(v: str, field: str, col: _Collector, origin: str) -> None:
    if field in _TEXT_FIELDS or _EN_FIELD_RE.match(field or ""):
        # текстовое поле: и подпись строчными («abel face», «sure»), если не идентификатор — в словарь
        if len(v.split()) <= 6 and not looks_translatable(v, strict=True):
            _add_display(v, col, origin)
            return
    if not looks_translatable(v, strict=True):
        return
    if (looks_like_sentence(v) or (field and _LABEL_FIELD_RE.search(field) and (_ui_word(v) or _lower_label(v)))
            or _format_text(v)):
        col.add(v, TextKind.OTHER, origin)


#: вставка .NET String.Format: {0}, {1:N0}
_FORMAT_SLOT_RE = re.compile(r"\{\d+(?:,-?\d+)?(?::[^{}]*)?\}")


def _format_text(v: str) -> bool:
    """Строка-формат для экрана («{0} / {1} completed», «Level {0}»): вставки String.Format и хотя бы
    одно настоящее слово. Короткую фразу без вставок фильтр «предложения» отсеял бы, а это подпись,
    которую игра покажет с числами."""
    if not _FORMAT_SLOT_RE.search(v):
        return False
    rest = _FORMAT_SLOT_RE.sub(" ", v)
    words = re.findall(r"[A-Za-z]{3,}", rest)
    return bool(words) and not re.search(r"[a-z][A-Z]|_|\.\w", rest)


# ---------- скрипты, которые не читаются по typetree ----------

def raw_strings(data: bytes, start: int = 0) -> List[Tuple[int, str]]:
    """Строки сериализации Unity в сырых байтах: int32 длина, UTF-8, выравнивание на 4.

    Поля идут с выравниванием на 4 байта, поэтому кандидаты проверяются только
    на таких смещениях; найденная строка перескакивается целиком.
    """
    out: List[Tuple[int, str]] = []
    i, n = start, len(data)
    while i + 4 <= n:
        ln = struct.unpack_from("<i", data, i)[0]
        if 2 <= ln <= 16384 and i + 4 + ln <= n:
            try:
                s = data[i + 4:i + 4 + ln].decode("utf-8")
            except UnicodeDecodeError:
                s = ""
            if s and all(c.isprintable() or c in "\n\r\t" for c in s) and re.search(r"[A-Za-z]{2}", s):
                out.append((i, s))
                i = (i + 4 + ln + 3) & ~3
                continue
        i += 4
    return out


def _from_raw(obj, col: _Collector, origin: str) -> None:
    try:
        data = obj.get_raw_data()
    except Exception:  # noqa: BLE001
        return
    # заголовок MonoBehaviour: m_GameObject, m_Enabled, m_Script, затем m_Name — имя объекта не переводим
    for off, s in raw_strings(data, 0):
        if off > 32:
            col.see(s, origin)
        if off <= 32 or not looks_translatable(s, strict=True):
            continue
        if looks_like_sentence(s) or _ui_word(s) or _chat_line(s):
            col.add(s, TextKind.OTHER, origin)


def _chat_line(s: str) -> bool:
    """Короткая реплика («lol», «wtf is this?!?», «Hey») — в данных скрипта это текст, а не код."""
    words = re.findall(r"[A-Za-z']+", s)
    return (1 <= len(words) <= 12 and not re.search(r"[_{}<>\\/=@#$|]", s)
            and not re.fullmatch(r"[a-z]+[A-Z]\w*", s))


# ---------- путь компонента в сцене (для уменьшения шрифта длинных переводов) ----------

class _TreeCache:
    """typetree объектов файла по path_id (GameObject/Transform читаются по многу раз)."""

    def __init__(self) -> None:
        self._trees: Dict[Tuple[int, int], Optional[dict]] = {}

    def get(self, sf, path_id: int) -> Optional[dict]:
        key = (id(sf), path_id)
        if key not in self._trees:
            obj = sf.objects.get(path_id)
            try:
                self._trees[key] = obj.read_typetree() if obj is not None else None
            except Exception:  # noqa: BLE001
                self._trees[key] = None
        return self._trees[key]


def game_object_path(sf, go_path_id: int, trees: _TreeCache) -> Optional[str]:
    """Путь «Корень/…/Объект» — так XUnity ищет правила resizer (имена GameObject от корня)."""
    names: List[str] = []
    pid = go_path_id
    for _ in range(64):
        go = trees.get(sf, pid)
        if not go:
            return None
        name = go.get("m_Name", "")
        if not name or "/" in name:
            return None
        names.append(name)
        comps = go.get("m_Component") or []
        if not comps:
            return None
        ptr = comps[0].get("component") or comps[0].get("second") or {}  # первый компонент — Transform
        tr = trees.get(sf, ptr.get("m_PathID", 0)) if not ptr.get("m_FileID") else None
        if not tr:
            return None
        father = tr.get("m_Father") or {}
        if not father.get("m_PathID"):
            return "/".join(reversed(names))
        if father.get("m_FileID"):
            return None
        ftr = trees.get(sf, father["m_PathID"])
        if not ftr:
            return None
        pid = (ftr.get("m_GameObject") or {}).get("m_PathID", 0)
    return None


def _remember_ui(obj, tree: dict, field: str, col: _Collector, trees: _TreeCache, scene: bool) -> None:
    text = tree.get(field)
    if not isinstance(text, str) or not looks_translatable(text):
        return
    if field == "m_Text":          # UGUI Text
        fd = tree.get("m_FontData") or {}
        size = fd.get("m_FontSize")
        # уменьшать шрифт имеет смысл только там, где текст переносится по ширине
        if not size or fd.get("m_BestFit") or fd.get("m_HorizontalOverflow", 0) != 0:
            return
    elif field == "m_text":        # TextMeshPro
        size = tree.get("m_fontSize")
        if not size or tree.get("m_enableAutoSizing"):
            return
    else:
        return
    go = tree.get("m_GameObject") or {}
    if go.get("m_FileID") or not go.get("m_PathID"):
        return
    path = game_object_path(obj.assets_file, go["m_PathID"], trees)
    if path:
        col.ui.append({"text": text, "path": path, "size": float(size), "scene": scene})


# ---------- строки из кода (Mono) ----------

def game_assemblies(game: UnityGame) -> List[Path]:
    managed = game.data / "Managed"
    if not managed.is_dir():
        return []
    return [p for p in sorted(managed.glob("*.dll")) if not p.name.startswith(FRAMEWORK_PREFIXES)]


def dotnet_user_strings(dll: Path) -> List[str]:
    """Строки из кучи #US .NET-сборки (литералы C#-кода) — без сторонних библиотек."""
    data = dll.read_bytes()
    try:
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            return []
        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        opt_size = struct.unpack_from("<H", data, pe + 20)[0]
        opt = pe + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        dd = opt + (96 if magic == 0x10B else 112)
        cli_rva = struct.unpack_from("<I", data, dd + 14 * 8)[0]
        sections = []
        sec = opt + opt_size
        for i in range(nsec):
            vsize, va, rsize, raw = struct.unpack_from("<IIII", data, sec + i * 40 + 8)
            sections.append((va, max(vsize, rsize), raw))

        def off(rva: int) -> int:
            for va, size, raw in sections:
                if va <= rva < va + size:
                    return rva - va + raw
            raise ValueError("rva")

        cli = off(cli_rva)
        md_rva = struct.unpack_from("<I", data, cli + 8)[0]
        md = off(md_rva)
        if data[md:md + 4] != b"BSJB":
            return []
        vlen = struct.unpack_from("<I", data, md + 12)[0]
        p = md + 16 + vlen + 2
        nstreams = struct.unpack_from("<H", data, p)[0]
        p += 2
        us_off = us_size = 0
        for _ in range(nstreams):
            s_off, s_size = struct.unpack_from("<II", data, p)
            p += 8
            end = data.index(b"\0", p)
            name = data[p:end].decode("ascii", "replace")
            p = (end + 4) & ~3
            if name == "#US":
                us_off, us_size = md + s_off, s_size
        if not us_size:
            return []
    except (struct.error, ValueError):
        return []
    out: List[str] = []
    i, end = us_off + 1, us_off + us_size
    while i < end:
        b0 = data[i]
        if b0 & 0x80 == 0:
            n, i = b0, i + 1
        elif b0 & 0xC0 == 0x80:
            n, i = ((b0 & 0x3F) << 8) | data[i + 1], i + 2
        elif b0 & 0xE0 == 0xC0:
            n, i = ((b0 & 0x1F) << 24) | (data[i + 1] << 16) | (data[i + 2] << 8) | data[i + 3], i + 4
        else:
            break
        if n == 0:
            continue
        raw = data[i:i + n - 1]
        i += n
        try:
            out.append(raw.decode("utf-16-le"))
        except UnicodeDecodeError:
            continue
    return out


def extract_code_strings(game: UnityGame, col: _Collector, warnings: List[str]) -> None:
    strings: Iterable[str] = []
    templates: List[str] = []
    if game.backend == "mono":
        from . import il_scan
        collected: List[str] = []
        for dll in game_assemblies(game):
            try:
                collected += dotnet_user_strings(dll)
            except OSError:
                continue
            # склейки «литерал + значение + литерал» из кода: целиком такой фразы нет ни в одном
            # файле — шаблон «Day {0} of {1}» переведётся заранее и станет правилом XUnity
            try:
                templates += il_scan.concat_templates(il_scan._Meta(dll.read_bytes()))
            except (OSError, ValueError, struct.error, IndexError):
                continue
        strings = collected
    else:
        meta = game.data / "il2cpp_data" / "Metadata" / "global-metadata.dat"
        if meta.is_file():
            strings = il2cpp_literals(meta)
            if not strings:
                warnings.append("global-metadata.dat зашифрован или нестандартный — строки из кода "
                                "переведутся в игре на лету.")
    for s in strings:
        col.see(s, "code")
        if looks_translatable(s, strict=True) and (looks_like_sentence(s) or _ui_word(s) or _format_text(s)):
            col.add(s, TextKind.OTHER, "code")
            if _PREFIX_RE.fullmatch(s) and s not in col.prefixes and len(col.prefixes) < 300:
                col.prefixes.append(s)
    for t in templates[:3000]:
        if looks_translatable(t) and not looks_technical(t):
            col.add(t, TextKind.UI, "code-template")


#: «Pro Tip: », «Stock: », «Level - »: подпись, к которой код игры приклеивает текст или число
_PREFIX_RE = re.compile(r"[A-Z][A-Za-z']*(?: [A-Za-z']+){0,4}(?::| -) ")


def _ui_word(s: str) -> bool:
    """Короткая строка интерфейса: «Continue», «Game Over», «Motion Sensor» (но не «MotionSensor»)."""
    words = s.split()
    return (1 <= len(words) <= 4 and s[:1].isupper() and all(w.isalpha() or w[:-1].isalpha() for w in words)
            and not any(re.search(r"[a-z][A-Z]", w) for w in words))


def _lower_label(s: str) -> bool:
    """Подпись строчными из двух-четырёх слов («abel face», «arma outfits» — названия в галерее, игра
    показывает их капсом через стиль шрифта). Одно слово строчными в таком поле — скорее ключ."""
    words = s.split()
    return (2 <= len(words) <= 4 and all(re.fullmatch(r"[A-Za-z][a-z']*", w) for w in words)
            and any(len(w) >= 3 for w in words))


def il2cpp_literals(meta: Path) -> List[str]:
    """Строковые литералы из global-metadata.dat (только чтение).

    Заголовок: sanity 0xFAB11BAF, version, затем пары (offset, size):
    stringLiteral (+8/+12) и stringLiteralData (+16/+20). Запись литерала —
    {uint32 length; int32 dataIndex} (метаданные v24–v31).
    """
    try:
        data = meta.read_bytes()
        sanity, version = struct.unpack_from("<Ii", data, 0)
        if sanity != 0xFAB11BAF or not (16 <= version <= 40):
            return []
        sl_off, sl_size, sld_off, sld_size = struct.unpack_from("<iiii", data, 8)
    except (OSError, struct.error):
        return []
    if min(sl_off, sl_size, sld_off, sld_size) < 0 or sld_off + sld_size > len(data):
        return []
    out = []
    for i in range(sl_size // 8):
        length, index = struct.unpack_from("<Ii", data, sl_off + i * 8)
        if index < 0 or length == 0 or index + length > sld_size:
            continue
        try:
            out.append(data[sld_off + index: sld_off + index + length].decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return out


# ---------- всё вместе ----------

def extract_browser_pages(game: UnityGame, col: _Collector, progress: ProgressFn = _noop) -> None:
    """Фразы HTML-страниц встроенного браузера (ZFBrowser) — переводятся прямо в пакете."""
    from . import zfbrowser
    for pack in zfbrowser.find_packs(game.data):
        progress(f"Страницы встроенного браузера: {pack.name}", None)
        try:
            _, files = zfbrowser.read_pack(pack)
        except Exception as exc:  # noqa: BLE001
            log.warning("пакет %s не прочитан: %s", pack.name, exc)
            continue
        for name, blob in files:
            if not name.lower().endswith((".html", ".htm")):
                continue
            page = zfbrowser.decode_page(blob)
            if page is None:
                continue
            for i, u in enumerate(zfbrowser.units(page)):
                if len(u.source) > 20000 or not looks_translatable(u.source) or looks_technical(u.source):
                    continue
                key = f"zfb:{pack.name}|{name}|{i}"
                col.pages.append(Entry(id=key, source=u.source, kind=TextKind.OTHER, target_key=key,
                                       scene=name))


# ---------- текстовые файлы рядом с ассетами ----------

#: расширения текстовых данных, которые игры кладут файлами в StreamingAssets: описания предметов,
#: таблицы локализации, диалоги (Ink, Yarn), JSON/CSV/XML с текстом
LOOSE_TEXT_EXT = {".json", ".csv", ".tsv", ".txt", ".xml", ".yaml", ".yml", ".ink", ".yarn", ".lang", ".loc",
                  ".strings", ".dialog", ".dlg"}
#: сколько файлов и байт читать (на случай игр с тысячами служебных файлов)
LOOSE_MAX_FILES = 5000
LOOSE_MAX_BYTES = 256 * 1024 * 1024
#: названия языков в путях файлов локализации
_LANGS = {"fr", "fre", "french", "francais", "de", "ger", "german", "deutsch", "es", "spa", "spanish", "espanol",
          "it", "ita", "italian", "pt", "por", "portuguese", "br", "brazilian", "ptbr", "pl", "pol", "polish",
          "tr", "tur", "turkish", "ja", "jp", "jpn", "japanese", "ko", "kr", "kor", "korean", "koreana", "zh",
          "cn", "chs", "cht", "chinese", "schinese", "tchinese", "zhcn", "zhtw", "tw", "ru", "rus", "russian",
          "uk", "ua", "ukrainian", "nl", "dutch", "sv", "swedish", "no", "norwegian", "da", "danish", "fi",
          "finnish", "cs", "czech", "hu", "hungarian", "ro", "romanian", "th", "thai", "vi", "vietnamese", "id",
          "indonesian", "ar", "arabic", "he", "hebrew", "el", "greek", "latam", "mx"}
_ENGLISH = {"en", "eng", "english", "enus", "engb"}


def _lang_tokens(name: str) -> List[str]:
    return [t for t in re.split(r"[\s_.\-]+", name.lower().replace("en-us", "enus").replace("zh-cn", "zhcn")
                                .replace("zh-tw", "zhtw").replace("pt-br", "ptbr").replace("en-gb", "engb")) if t]


def _foreign_language(path: Path) -> bool:
    """Файл другого языка игры (``fr.json``, ``strings_de.csv``, ``Localization/ja/…``) — только если
    рядом есть такой же английский: иначе ``item_id.json`` приняли бы за индонезийский."""
    parent = path.parent
    pname = parent.name.lower()
    if pname in _LANGS and any((parent.parent / e).is_dir() for e in _ENGLISH | {"English", "EN", "En"}):
        return True
    toks = _lang_tokens(path.stem)
    if toks and toks[-1] in _LANGS:
        base = re.sub(r"[\s_.\-]*[A-Za-z]+$", "", path.stem)        # «strings_fr» -> «strings»
        try:
            siblings = [p for p in parent.iterdir() if p.suffix.lower() == path.suffix.lower()]
        except OSError:
            return False
        for p in siblings:
            st = _lang_tokens(p.stem)
            if st and st[-1] in _ENGLISH and p.stem.lower().startswith(base.lower()):
                return True
    return False


def loose_text_files(game: UnityGame) -> List[Path]:
    """Текстовые файлы данных игры в StreamingAssets (без каталога Addressables и бандлов)."""
    sa = game.data / "StreamingAssets"
    if not sa.is_dir():
        return []
    out: List[Path] = []
    total = 0
    for p in sorted(sa.rglob("*")):
        if len(out) >= LOOSE_MAX_FILES or total >= LOOSE_MAX_BYTES:
            break
        try:
            rel = p.relative_to(sa).parts
        except ValueError:
            continue
        if not p.is_file() or p.suffix.lower() not in LOOSE_TEXT_EXT or (rel and rel[0].lower() == "aa"):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size < 8 or size > text_assets.MAX_SIZE or _foreign_language(p):
            continue
        out.append(p)
        total += size
    return out


def extract_loose_files(game: UnityGame, col: _Collector) -> int:
    """Текст из файлов данных в StreamingAssets: описания предметов, таблицы локализации, диалоги.

    Игра читает такой файл сама и выводит строку как есть — XUnity найдёт её перевод в словаре. Файлы
    игры не меняются. Возвращает, сколько файлов дали текст."""
    from . import text_assets
    sa = game.data / "StreamingAssets"
    used = 0
    for p in loose_text_files(game):
        try:
            text = text_assets.decode(p.read_bytes())
        except OSError:
            continue
        if not text:
            continue
        before = len(col.items)
        origin = "StreamingAssets/" + p.relative_to(sa).as_posix()
        text_assets.extract(text, lambda s, o=origin: col.add(s, TextKind.OTHER, o), lambda s, o=origin: col.see(s, o))
        used += len(col.items) > before
    return used


def extract_all(game: UnityGame, progress: ProgressFn = _noop) -> Tuple[List[Entry], List[str], "_Collector"]:
    """Вернуть (строки, предупреждения, сборщик: шрифты, компоненты для resizer, подписи из кода)."""
    warnings: List[str] = []
    col = _Collector()
    extract_assets(game, col, warnings, progress)
    progress("Текстовые файлы игры…", None)
    extract_loose_files(game, col)
    progress("Строки из кода игры…", None)
    extract_code_strings(game, col, warnings)
    extract_browser_pages(game, col, progress)
    return col.entries(), warnings, col
