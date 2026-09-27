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
import re
import struct
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ...core.universal import Entry, TextKind
from ...translation.filters import looks_like_sentence, looks_technical, looks_translatable
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
_TECH_RE = re.compile(r"PublicKeyToken|Version=\d|Culture=|\bUnityEngine\.|\bSystem\.[A-Z]|^Hidden/|"
                      r"^[\w\- ]+/[\w\- ]+/|yyyy|HH:mm|^\[[\w. ]+\]|NullReference|Exception\b|"
                      r"\bshaders?\b|\.cs\b|\bdebug\b|\bSteamworks\b", re.I)


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

    def add(self, text: str, kind: TextKind, origin: str) -> None:
        # текст не нормализуем: ключ XUnity должен совпасть с тем, что игра выводит на экран
        if not text.strip() or len(text) > MAX_TEXT or _TECH_RE.search(text) or looks_technical(text):
            return
        prev = self.items.get(text)
        if prev is None or (prev[0] == TextKind.OTHER and kind != TextKind.OTHER):
            self.items[text] = (kind, origin)

    def entries(self) -> List[Entry]:
        out = []
        for text, (kind, origin) in self.items.items():
            out.append(Entry(id=text_key(text), source=text, kind=kind, target_key=text_key(text),
                             scene=origin, max_length=None))
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
        for p in sa.rglob("*"):
            if p.is_file() and p.stat().st_size > 64 and _is_bundle(p):
                files.append(p)
    return files


def _is_bundle(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(8)
        return head.startswith((b"UnityFS", b"UnityWeb", b"UnityRaw"))
    except OSError:
        return False


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
    gen = _typetree_generator(game, warnings)
    files = serialized_files(game)
    for i, f in enumerate(files):
        progress(f"Чтение ассетов: {f.name}", i / max(1, len(files)))
        try:
            env = UnityPy.load(str(f))
        except Exception as exc:  # noqa: BLE001
            log.debug("не открыт %s: %s", f.name, exc)
            continue
        if gen is not None:
            env.typetree_generator = gen
        has_font = False
        scene = f.name.lower().startswith("level")
        trees = _TreeCache()
        for obj in env.objects:
            tname = obj.type.name
            try:
                if tname == "Font":
                    has_font = True
                elif tname == "MonoBehaviour":
                    try:
                        tree = obj.read_typetree()
                    except Exception:  # noqa: BLE001
                        # typetree не сошёлся с данными — берём строки из сырых байтов
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
                    if isinstance(val, str) and looks_translatable(val):
                        col.add(val, TextKind.UI, f.name)
                elif tname == "TextAsset":
                    _from_text_asset(obj, col, f.name)
            except Exception:  # noqa: BLE001
                continue
        if has_font:
            col.font_files.append(f)


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
    if text:
        name = getattr(data, "m_Name", "") or ""
        text_assets.extract(text, lambda s: col.add(s, TextKind.OTHER, f"{origin}:{name}"))


def _from_monobehaviour(tree: dict, col: _Collector, origin: str) -> Optional[str]:
    """Собрать текст скрипта. Для компонента текста вернуть его поле (m_Text/m_text/mText)."""
    for field, marker in TEXT_COMPONENT_FIELDS.items():
        val = tree.get(field)
        if isinstance(val, str) and (marker in tree or (field == "mText" and NGUI_ALT in tree)):
            if looks_translatable(val):
                col.add(val, TextKind.UI, origin)
            return field
    _walk_strings(tree, col, origin, depth=0)
    return None


#: поля скриптов, где короткая строка — скорее всего видимое название («Motion Sensor»)
_LABEL_FIELD_RE = re.compile(r"title|label|caption|header|display|product|item|desc|text|message|hint|tip|"
                             r"name|button|option|objective|quest|note|subject|content|question|answer|localiz|term|"
                             r"translation|english|subtitle|tooltip|prompt|dialog|line", re.I)


def _walk_strings(node, col: _Collector, origin: str, depth: int, field: str = "") -> None:
    if depth > 6:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k in SKIP_FIELDS:
                continue
            if isinstance(v, str):
                _maybe_add(v, k, col, origin)
            elif isinstance(v, (dict, list)):
                _walk_strings(v, col, origin, depth + 1, k)
    elif isinstance(node, list):
        for v in node[:5000]:
            if isinstance(v, str):
                _maybe_add(v, field, col, origin)
            elif isinstance(v, (dict, list)):
                _walk_strings(v, col, origin, depth + 1, field)


def _maybe_add(v: str, field: str, col: _Collector, origin: str) -> None:
    if not looks_translatable(v, strict=True):
        return
    if looks_like_sentence(v) or (field and _LABEL_FIELD_RE.search(field) and _ui_word(v)):
        col.add(v, TextKind.OTHER, origin)


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
        if looks_translatable(s, strict=True) and (looks_like_sentence(s) or _ui_word(s)):
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


def extract_all(game: UnityGame, progress: ProgressFn = _noop) -> Tuple[List[Entry], List[str], "_Collector"]:
    """Вернуть (строки, предупреждения, сборщик: шрифты, компоненты для resizer, подписи из кода)."""
    warnings: List[str] = []
    col = _Collector()
    extract_assets(game, col, warnings, progress)
    progress("Строки из кода игры…", None)
    extract_code_strings(game, col, warnings)
    extract_browser_pages(game, col, progress)
    return col.entries(), warnings, col
