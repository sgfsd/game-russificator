"""Перевод Unity-игр в рантайме: BepInEx + XUnity.AutoTranslator.

Почему так, а не правкой ассетов: у Unity-игр текст разбросан по сценам,
ассетам, коду (Mono или IL2CPP), бандлам и собирается на лету; статический
патч файлов покрывает малую часть и легко ломает игру. XUnity.AutoTranslator
(MIT) перехватывает вывод любого текста (UGUI, TextMeshPro, NGUI, IMGUI) и
подменяет его переводом из словаря. Файлы игры не меняются — русификатор
только добавляет папку BepInEx и пару файлов загрузчика, откат их удаляет.

Словарь (``BepInEx/Translation/ru/Text/_Russificator.txt``) готовит
русификатор заранее. Строки, которых в нём нет, XUnity отправляет на
локальный сервер перевода русификатора (``live_server.py``), пока программа
открыта, и сам запоминает результаты — второй раз они уже не нужны.
"""

from __future__ import annotations

import logging
import re
import shutil
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ... import net, paths
from .detect import UnityGame

log = logging.getLogger("russificator.unity.xunity")

BEPINEX5 = "5.4.23.5"
BEPINEX6_BUILD, BEPINEX6_HASH = "788", "5b766a3"
XUA = "5.6.2"
TMP_FONTS_URL = ("https://github.com/bbepis/XUnity.AutoTranslator/releases/download/v5.4.4/"
                 "TMP_Font_AssetBundles.zip")

LIVE_PORT = 47631
LIVE_URL = f"http://127.0.0.1:{LIVE_PORT}/translate"

TRANSLATION_FILE = "BepInEx/Translation/ru/Text/_Russificator.txt"
RESIZER_FILE = "BepInEx/Translation/ru/Text/_Russificator.resizer.txt"

#: предел XUnity на длину переводимой строки (Settings.MaxMaxCharactersPerTranslation)
MAX_CHARS = 2500
#: бюджет «частичных» переводов (печатающийся по буквам текст): XUnity создаёт перевод для
#: каждого префикса каждой строки словаря — память растёт как сумма квадратов длин
PARTIAL_BUDGET = 40_000_000

StatusFn = Callable[[str, Optional[float]], None]


def _noop(message: str, fraction: Optional[float] = None) -> None:
    pass


def bepinex_url(game: UnityGame) -> str:
    if game.backend == "il2cpp":
        return (f"https://builds.bepinex.dev/projects/bepinex_be/{BEPINEX6_BUILD}/"
                f"BepInEx-Unity.IL2CPP-win-{game.arch}-6.0.0-be.{BEPINEX6_BUILD}%2B{BEPINEX6_HASH}.zip")
    return (f"https://github.com/BepInEx/BepInEx/releases/download/v{BEPINEX5}/"
            f"BepInEx_win_{game.arch}_{BEPINEX5}.zip")


def xua_url(game: UnityGame) -> str:
    kind = "BepInEx-IL2CPP" if game.backend == "il2cpp" else "BepInEx"
    return (f"https://github.com/bbepis/XUnity.AutoTranslator/releases/download/v{XUA}/"
            f"XUnity.AutoTranslator-{kind}-{XUA}.zip")


def _cached(url: str, status: StatusFn, cancel) -> Path:
    """Архив из кэша программы (скачивается один раз на все игры)."""
    name = url.rsplit("/", 1)[-1].replace("%2B", "+")
    dest = paths.tools_dir() / "unity" / name
    if not dest.is_file():
        def progress(done: int, total: int) -> None:
            status(f"Скачивание {name}: {done >> 20} из {(total >> 20) or '?'} МБ", done / total if total else None)
        net.download(url, dest, progress=progress, cancel=cancel)
    return dest


def is_installed(game_root: Path) -> bool:
    return (Path(game_root) / "BepInEx" / "plugins" / "XUnity.AutoTranslator").is_dir()


def install(game: UnityGame, backup, status: StatusFn = _noop, cancel=None) -> List[str]:
    """Поставить BepInEx (если его нет) и XUnity.AutoTranslator. Возвращает заметки."""
    notes: List[str] = []
    root = game.root
    had_bepinex = (root / "BepInEx" / "core").is_dir()
    if had_bepinex:
        notes.append("В игре уже установлен BepInEx (моды) — добавлен только плагин перевода.")
    else:
        status("Установка BepInEx…", None)
        archive = _cached(bepinex_url(game), status, cancel)
        _unzip(archive, root, backup, track_top_level=True)
    status("Установка XUnity.AutoTranslator…", None)
    archive = _cached(xua_url(game), status, cancel)
    _unzip(archive, root, backup, track_top_level=not had_bepinex)
    install_plugin(game, backup)
    return notes


PLUGIN = "Russificator.Unity.dll"                  # Mono, BepInEx 5
PLUGIN_IL2CPP = "Russificator.Unity.IL2CPP.dll"    # IL2CPP, BepInEx 6
PLUGIN_CONFIG = "BepInEx/config/Russificator.cfg"


def plugin_name(game: UnityGame) -> Optional[str]:
    """Какая сборка плагина подходит игре (по установленному BepInEx) или None."""
    core = game.root / "BepInEx" / "core"
    if game.backend == "mono" and (core / "BepInEx.dll").is_file():
        return PLUGIN
    if game.backend == "il2cpp" and (core / "BepInEx.Unity.IL2CPP.dll").is_file():
        return PLUGIN_IL2CPP
    return None


def install_plugin(game: UnityGame, backup) -> bool:
    """Плагин русификатора в игре (исходник — resources/unity/RussificatorUnity.cs).

    Запускает живой перевод при старте игры, уменьшает шрифт у русского текста,
    который не помещается, направляет TMP ``SetText(string)`` через свойство
    ``text`` и показывает надпись о программе в русификаторах «для друзей».
    Mono — сборка под BepInEx 5, IL2CPP — под BepInEx 6.
    """
    name = plugin_name(game)
    src = Path(__file__).resolve().parents[2] / "resources" / "unity" / (name or PLUGIN)
    if name is None or not src.is_file():
        return False
    dest = game.root / "BepInEx" / "plugins" / name
    if dest.exists():
        backup.save_original(dest)
    else:
        backup.track_new(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return True


def plugin_installed(game: UnityGame) -> bool:
    plugins = game.root / "BepInEx" / "plugins"
    return (plugins / PLUGIN).is_file() or (plugins / PLUGIN_IL2CPP).is_file()


def plugin_config_text(server: Optional[tuple], fit: bool = True, credit: str = "") -> str:
    """Настройки плагина: как запустить сервер живого перевода и надпись о программе."""
    lines = ["# Русификатор игр: настройки плагина Russificator.Unity (пишет программа при русификации).",
             "# server/args — чем запустить живой перевод, когда игра стартует без программы;",
             "# credit — надпись о программе при запуске игры (только в русификаторах «для друзей»).",
             f"port={LIVE_PORT}", f"fit={'true' if fit else 'false'}"]
    if server:
        exe, args, workdir = server
        lines += [f"server={exe}", f"args={args}", f"workdir={workdir}"]
    if credit:
        lines.append("credit=" + credit.replace("\n", " "))
    return "\n".join(lines) + "\n"


def write_plugin_config(game: UnityGame, backup, server: Optional[tuple], fit: bool = True) -> None:
    backup.write_text(game.root / PLUGIN_CONFIG, plugin_config_text(server, fit))


def config_for_friends(ini: str, tmp_font: Optional[str]) -> str:
    """AutoTranslatorConfig.ini для архива: без обращения к серверу программы (у друга её нет)."""
    ini = re.sub(r"(?m)^Endpoint=.*$", "Endpoint=", ini)
    if tmp_font:
        if re.search(r"(?m)^FallbackFontTextMeshPro=", ini):
            ini = re.sub(r"(?m)^FallbackFontTextMeshPro=.*$", f"FallbackFontTextMeshPro={tmp_font}", ini)
        else:
            ini = ini.replace("[Behaviour]\n", f"[Behaviour]\nFallbackFontTextMeshPro={tmp_font}\n", 1)
    return ini


def tmp_font_file(game: UnityGame, status: StatusFn = _noop, cancel=None) -> Optional[Path]:
    """Готовый TMP-шрифт XUnity для версии Unity игры — файлом (для архива), или None."""
    name = tmp_font_bundle(game)
    if name is None:
        return None
    dest = paths.sub("tmp") / "tmp_fonts" / name
    if dest.is_file():
        return dest
    archive = _cached(TMP_FONTS_URL, status, cancel)
    with zipfile.ZipFile(archive) as z:
        member = next((n for n in z.namelist() if Path(n).name == name), None)
        if member is None:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        with z.open(member) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out)
    return dest


def _unzip(archive: Path, root: Path, backup, track_top_level: bool) -> None:
    """Распаковать архив в корень игры, запоминая всё созданное для отката."""
    with zipfile.ZipFile(archive) as z:
        tops = {Path(n).parts[0] for n in z.namelist() if n.strip("/")}
        if track_top_level:
            for top in sorted(tops):
                if not (root / top).exists():
                    backup.track_new(root / top)
        for info in z.infolist():
            if info.is_dir():
                continue
            dest = root / info.filename
            if dest.exists():
                backup.save_original(dest)
            elif not track_top_level:
                backup.track_new(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)


def tmp_font_bundle(game: UnityGame) -> Optional[str]:
    """Какой готовый TMP-шрифт XUnity подходит к версии Unity (или None)."""
    if not game.uses_tmp:
        return None
    if game.major >= 2019:
        return "arialuni_sdf_u2019"
    if game.major == 2018:
        return "arialuni_sdf_u2018"
    return None


def install_tmp_font(game: UnityGame, backup, status: StatusFn = _noop, cancel=None) -> Optional[str]:
    """Положить в игру TMP-шрифт с кириллицей (для FallbackFontTextMeshPro)."""
    name = tmp_font_bundle(game)
    if name is None:
        return None
    dest = game.root / name
    if dest.exists():
        return name
    status("Скачивание шрифта с кириллицей для TextMeshPro…", None)
    archive = _cached(TMP_FONTS_URL, status, cancel)
    with zipfile.ZipFile(archive) as z:
        member = next((n for n in z.namelist() if Path(n).name == name), None)
        if member is None:
            return None
        backup.track_new(dest)
        with z.open(member) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out)
    return name


def partial_translations_fit(sources) -> bool:
    """Можно ли включить GeneratePartialTranslations без риска съесть память игры."""
    return sum(len(s) ** 2 for s in sources) <= PARTIAL_BUDGET


def write_config(game: UnityGame, backup, live: bool, tmp_font: Optional[str], partial: bool = False,
                 getter_compat: bool = False, imgui: bool = False) -> None:
    """Настройки XUnity.AutoTranslator под русский язык и наш сервер перевода.

    ``partial`` — текст, который игра выводит по букве (монологи, «печатная
    машинка»), сразу идёт по-русски: XUnity заранее строит переводы для
    каждого начала строки словаря. Без этого сначала печатается английский
    текст, а перевод появляется, только когда строка допечатана целиком.

    ``getter_compat`` — код игры читает текст обратно и дописывает буквы
    (см. ``il_scan.py``): коду игры возвращается оригинал, иначе он допишет
    английское продолжение к уже переведённому началу.

    ``imgui`` — игра рисует интерфейс через IMGUI (``GUI.Label`` и т.п.).
    """
    endpoint = "CustomTranslate" if live else ""
    ini = f"""[Service]
Endpoint={endpoint}
FallbackEndpoint=

[General]
Language=ru
FromLanguage=en

[Files]
Directory=Translation\\{{Lang}}\\Text
OutputFile=Translation\\{{Lang}}\\Text\\_AutoGeneratedTranslations.txt

[TextFrameworks]
EnableUGUI=True
EnableNGUI=True
EnableTextMeshPro=True
EnableTextMesh=True
EnableIMGUI={imgui}

[Behaviour]
MaxCharactersPerTranslation={MAX_CHARS}
GeneratePartialTranslations={partial}
TextGetterCompatibilityMode={getter_compat}
IgnoreWhitespaceInDialogue=False
IgnoreWhitespaceInNGUI=False
MinDialogueChars=20
EnableUIResizing=True
ForceUIResizing=False
EnableBatching=True
UseStaticTranslations=False
OverrideFont=
OverrideFontTextMeshPro=
FallbackFontTextMeshPro={tmp_font or ""}
HandleRichText=True
PersistRichTextMode=Final
EnableSilentMode=True
HtmlEntityPreprocessing=True
TranslationPostProcessing=ReplaceHtmlEntities

[Custom]
Url={LIVE_URL}
EnableShortDelay=True
DisableSpamChecks=True

[Debug]
EnableConsole=False
EnableLog=False
"""
    backup.write_text(game.root / "BepInEx" / "config" / "AutoTranslatorConfig.ini", ini)
    bep_cfg = game.root / "BepInEx" / "config" / "BepInEx.cfg"
    if not bep_cfg.exists():
        backup.write_text(bep_cfg, "[Logging.Console]\n\nEnabled = false\n")


def encode(text: str) -> str:
    """Экранирование строки для файлов переводов XUnity (обратное TextHelper.ReadTranslationLineAndDecode).

    ``//`` в файле — начало комментария, ``%3D`` — старая запись «=», поэтому
    такие места записываются через ``\\uXXXX``, который XUnity раскрывает.
    """
    out = []
    for i, c in enumerate(text):
        nxt = text[i + 1:i + 2]
        if c == "/" and nxt == "/":
            out.append("\\u002F")
        elif c == "%" and text[i + 1:i + 3] == "3D":
            out.append("\\u0025")
        else:
            out.append({"\\": "\\\\", "=": "\\=", "\n": "\\n", "\r": "\\r"}.get(c, c))
    return "".join(out)


def prefix_rules(prefixes: List[str], pairs: Dict[str, str]) -> List[str]:
    """Правила XUnity для текста «подпись + вставка»: ``Pro Tip: <совет>`` -> ``Совет: <перевод совета>``.

    Игры часто склеивают подпись из кода с текстом из данных, и такой строки
    целиком нет ни в одном файле. Splitter-regex XUnity (``sr:``) отделяет
    подпись и переводит вставку отдельно — по словарю или живым переводом.
    """
    rules = []
    for p in prefixes:
        tr = (pairs.get(p) or "").strip()
        label = p.rstrip()
        if not tr or tr == label or "\n" in tr:
            continue
        tail = p[len(label):]
        if label.endswith(":") and not tr.endswith(":"):
            tr = tr.rstrip(".") + ":"
        value = (tr + tail).replace("$", "$$") + "$1"
        rules.append(f'sr:"^{_net_regex_escape(p)}([\\S\\s]+)$"={encode(value)}')
    return rules


def _net_regex_escape(s: str) -> str:
    return re.sub(r"([\\^$.|?*+()\[\]{}])", r"\\\1", s)


def write_translations(game: UnityGame, backup, pairs: Dict[str, str], rules: Optional[List[str]] = None) -> int:
    lines = ["// Русификация (Game Russificator). Формат: оригинал=перевод.",
             "// Этот файл можно править вручную — изменения подхватятся при запуске игры."]
    if rules:
        lines += ["// Подписи, к которым игра приклеивает текст (переводятся по частям):"] + list(rules)
    n = 0
    for src, dst in pairs.items():
        if not src.strip() or not dst.strip() or src == dst or len(src) > MAX_CHARS:
            continue
        lines.append(f"{encode(src)}={encode(dst)}")
        n += 1
    lang_dir = game.root / "BepInEx" / "Translation" / "ru"
    if not lang_dir.exists():
        lang_dir.mkdir(parents=True)
        backup.track_new(lang_dir)  # туда же XUnity пишет свой кэш — при откате уйдёт всё
    backup.write_text(game.root / TRANSLATION_FILE, "\n".join(lines) + "\n")
    return n


def resize_rules(ui: List[dict], pairs: Dict[str, str]) -> List[str]:
    """Правила уменьшения шрифта для переведённых элементов интерфейса.

    Русский текст обычно шире английского — и словами длиннее, и буквами
    шире: в кнопке под «CONTINUE» слово «ПРОДОЛЖИТЬ» переносится по буквам,
    а строка той же длины обрезается. Считать ширину заранее ненадёжно
    (другой шрифт, дорисованные буквы), поэтому для каждого переведённого
    элемента включается автоподбор размера (TextMeshPro ``enableAutoSizing``,
    UGUI Best Fit) от исходного размера вниз до ~60%: пока текст помещается,
    ничего не меняется. Путь — полный путь объекта от корня сцены; для
    префабов добавляется вариант «(Clone)».
    """
    rules: Dict[str, str] = {}
    for item in ui:
        src, path, size = item.get("text", ""), item.get("path", ""), float(item.get("size") or 0)
        tr = pairs.get(src)
        if not tr or tr == src or not path or size <= 0 or "=" in path:
            continue
        lo = max(min(size, 10.0), round(size * 0.6))
        cmd = f"AutoResize(true,{lo:g},{size:g})"
        rules[path] = cmd
        if not item.get("scene"):
            root, _, rest = path.partition("/")
            rules[f"{root}(Clone)" + (f"/{rest}" if rest else "")] = cmd
    return [f"{encode(p)}={c}" for p, c in rules.items()]


def write_resizer(game: UnityGame, backup, rules: List[str]) -> int:
    if not rules:
        return 0
    head = ["// Русификация (Game Russificator): шрифт уменьшается, только если перевод не помещается.",
            "// Формат XUnity: путь/объекта=AutoResize(включить,мин,макс)."]
    backup.write_text(game.root / RESIZER_FILE, "\n".join(head + rules) + "\n")
    return len(rules)
