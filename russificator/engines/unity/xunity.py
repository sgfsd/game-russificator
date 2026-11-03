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
import string
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


#: SHA-256 и размер архивов, которые ставятся в игру: BepInEx и XUnity — исполняемый код, их подмена по дороге
#: (или на зеркале) не должна дойти до игры. Хеши сверены с опубликованными на GitHub (digest
#: релиза); у сборок BepInEx 6 с builds.bepinex.dev и у архива шрифтов XUnity 5.4.4 опубликованных
#: хешей нет — записаны хеши официальных файлов, скачанных дважды в разное время.
PINNED = {
    "BepInEx_win_x64_5.4.23.5.zip":
        ("82f9878551030f54657792c0740d9d51a09500eeae1fba21106b0c441e6732c4", 639118),
    "BepInEx_win_x86_5.4.23.5.zip":
        ("37651c79e40d6f909572a4f461ac25350bb3ef8fe7fbd29f1aa8791a33b84c82", 638544),
    "BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.788+5b766a3.zip":
        ("f4cc496bd098a0df4164b81e3737297707f13a47c2478dba2f60eefab784817a", 34336405),
    "BepInEx-Unity.IL2CPP-win-x86-6.0.0-be.788+5b766a3.zip":
        ("d5954a5993ec39cd1133603d85bff93875d30b6411b712cc13dcf03c8e08a4d3", 31440336),
    "XUnity.AutoTranslator-BepInEx-5.6.2.zip":
        ("836a4066b9369b0d23dc1b0ef6336ad7fe7ae16880931259ec4d157aff7a81c0", 758678),
    "XUnity.AutoTranslator-BepInEx-IL2CPP-5.6.2.zip":
        ("639392d3ee3c7542cca98c6c04b384deee4961fe965f3d80a4d9c8413884a0f3", 761721),
    "TMP_Font_AssetBundles.zip":
        ("cd18628ea3bd1128fa172b910ae15685f89ff689b20c4836d42d42491c26dd39", 60160941),
}


def _cached(url: str, status: StatusFn, cancel) -> Path:
    """Архив из кэша программы (скачивается один раз на все игры, хеш и размер — строго по :data:`PINNED`;
    архив в кэше, который хешу не соответствует, скачивается заново)."""
    name = url.rsplit("/", 1)[-1].replace("%2B", "+")
    sha, size = PINNED[name]
    dest = paths.tools_dir() / "unity" / name
    if dest.is_file() and net.file_sha256(dest) != sha:
        log.warning("архив %s в кэше не совпал с известным хешем — скачивается заново", name)
        dest.unlink()
    if not dest.is_file():
        def progress(done: int, total: int) -> None:
            status(f"Скачивание {name}: {done >> 20} из {(total >> 20) or '?'} МБ", done / total if total else None)
        net.download(net.with_github_mirrors([url]), dest, progress=progress, cancel=cancel, sha256=sha,
                     expected_size=size, strict=True)
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


def plugin_config_text(server: Optional[tuple], fit: bool = True, credit: str = "", source: str = "en") -> str:
    """Настройки плагина: как запустить сервер живого перевода, надпись о программе, язык оригинала."""
    lines = ["# Русификатор игр: настройки плагина Russificator.Unity (пишет программа при русификации).",
             "# server/args — чем запустить живой перевод, когда игра стартует без программы;",
             "# credit — надпись о программе при запуске игры (только в русификаторах «для друзей»);",
             "# source — язык оригинала (ja, zh, ko: иероглифы и кана — тоже текст).",
             f"port={LIVE_PORT}", f"fit={'true' if fit else 'false'}"]
    if source != "en":
        lines.append(f"source={source}")
    if server:
        exe, args, workdir = server
        lines += [f"server={exe}", f"args={args}", f"workdir={workdir}"]
    if credit:
        lines.append("credit=" + credit.replace("\n", " "))
    return "\n".join(lines) + "\n"


def write_plugin_config(game: UnityGame, backup, server: Optional[tuple], fit: bool = True,
                        source: str = "en") -> None:
    backup.write_text(game.root / PLUGIN_CONFIG, plugin_config_text(server, fit, source=source))


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
    """Распаковать архив в корень игры, запоминая всё созданное для отката.

    Имя внутри архива, которое ведёт за пределы папки игры («..», абсолютный путь, диск), — отказ
    до записи первого файла: такой архив не ставится вовсе (откат не узнал бы о записанном снаружи)."""
    with zipfile.ZipFile(archive) as z:
        members = []
        for info in z.infolist():
            dest = paths.inside(root, info.filename)
            if dest is None:
                raise RuntimeError(f"В архиве {archive.name} недопустимый путь: {info.filename!r}")
            members.append((info, dest))
        tops = {Path(info.filename.replace("\\", "/")).parts[0] for info, _ in members if info.filename.strip("/")}
        if track_top_level:
            for top in sorted(tops):
                if not (root / top).exists():
                    backup.track_new(root / top)
        for info, dest in members:
            if info.is_dir():
                continue
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
    # только своя версия Unity: бандл, собранный под 2019, в Unity 2020.3 роняет игру при запуске
    # (Handshakes / Lost Relic Jam: вылет в AssetBundle.LoadAllAssets) — игра с квадратиками вместо
    # части букв лучше игры, которая не открывается
    if game.major == 2019:
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


#: язык оригинала (``translation.lang``) → код XUnity: по нему XUnity решает, какой текст переводить
#: (с «en» японская строка для него — не текст, и словарь к ней не применяется)
XUNITY_LANGS = {"zh": "zh-CN"}


def write_config(game: UnityGame, backup, live: bool, tmp_font: Optional[str], partial: bool = False,
                 getter_compat: bool = False, imgui: bool = False, source: str = "en") -> None:
    """Настройки XUnity.AutoTranslator под русский язык и наш сервер перевода.

    ``partial`` — текст, который игра выводит по букве (монологи, «печатная
    машинка»), сразу идёт по-русски: XUnity заранее строит переводы для
    каждого начала строки словаря. Без этого сначала печатается английский
    текст, а перевод появляется, только когда строка допечатана целиком.

    ``getter_compat`` — код игры читает текст обратно и дописывает буквы
    (см. ``il_scan.py``): коду игры возвращается оригинал, иначе он допишет
    английское продолжение к уже переведённому началу.

    ``imgui`` — игра рисует интерфейс через IMGUI (``GUI.Label`` и т.п.).

    ``IgnoreWhitespace…`` — переносы строк, которые игра вставляет сама (NGUI, длинные реплики:
    «The Sacred↵Sword» у Celestian Tales), не мешают найти фразу в словаре: для английского XUnity
    заменяет перенос пробелом (слова не склеиваются), а ключи словаря хранит и в таком виде.
    """
    endpoint = "CustomTranslate" if live else ""
    ini = f"""[Service]
Endpoint={endpoint}
FallbackEndpoint=

[General]
Language=ru
FromLanguage={XUNITY_LANGS.get(source, source)}

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
IgnoreWhitespaceInDialogue=True
IgnoreWhitespaceInNGUI=True
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
    write_bepinex_config(game, backup)


#: точка входа BepInEx 5 по умолчанию: статический конструктор UnityEngine.Application
DEFAULT_ENTRY_TYPE = "Application"


def entrypoint_assembly(game: UnityGame) -> str:
    """Сборка с MonoBehaviour: в Unity 2017+ — UnityEngine.CoreModule.dll, раньше — UnityEngine.dll."""
    return "UnityEngine.CoreModule.dll" if (game.data / "Managed" / "UnityEngine.CoreModule.dll").is_file() \
        else "UnityEngine.dll"


#: последняя версия метаданных IL2CPP, с которой работает BepInEx 6 (Il2CppInterop 1.5.3). Unity 6000.3
#: (метаданные v38/v39) поменял устройство классов в памяти: перехват Class::Init промахивается, и игра
#: падает при запуске (Il2CppInterop #283; Demonic Mahjong, Unity 6000.3.21f1: «при первом запуске
#: качает что должен и закрывается»). Новый BepInEx, который это чинит, — поднять вместе с ним
IL2CPP_BEPINEX_MAX_METADATA = 31

_FILES_AND_LIVE = (" Перевод вписан прямо в файлы игры, а текст, который собирает код игры, переводит "
                   "«Живой перевод» поверх экрана.")


def bepinex_blocker(game: UnityGame) -> str:
    """Почему BepInEx не может работать в этой игре; "" — может. Такие игры переводятся только
    правкой их файлов и живым переводом поверх экрана.

    IL2CPP: BepInEx 6 читает global-metadata.dat (Cpp2IL) и перехватывает функции движка
    (Il2CppInterop). Зашифрованные метаданные он не прочтёт, а на Unity 6000.3+ роняет игру
    (см. :data:`IL2CPP_BEPINEX_MAX_METADATA`).

    Mono: Unity при сборке с высоким уровнем «урезания» кода (Managed Stripping Level) выбрасывает из
    библиотек всё, чем не пользуется сама игра. Загрузчику BepInEx нужно событие
    ``AppDomain.AssemblyResolve`` (без него Doorstop молча падает на старте — missed messages., Unity
    2019.1), а его журналу — событие логов Unity (без него падает статический конструктор
    MonoBehaviour, и с ним вся игра). Подложить полные библиотеки (dll_search_path_override) нельзя:
    код самой игры урезан под урезанные библиотеки, и с полными у его корутин не собираются таблицы
    методов (TypeLoadException: VTable setup failed) — игра зависает."""
    if game.backend == "il2cpp":
        from .extract import metadata_version
        meta = game.data / "il2cpp_data" / "Metadata" / "global-metadata.dat"
        if not meta.is_file():
            return ""
        version = metadata_version(meta)
        if version is None:
            return ("Код игры зашифрован (global-metadata.dat): модуль перевода (BepInEx) в ней не работает "
                    "и не ставится." + _FILES_AND_LIVE)
        if version > IL2CPP_BEPINEX_MAX_METADATA:
            return (f"Игра на новой Unity ({game.version or f'метаданные v{version}'}): модуль перевода "
                    "(BepInEx) пока роняет такие игры при запуске, поэтому не ставится." + _FILES_AND_LIVE)
        return ""
    if game.backend != "mono":
        return ""
    managed = game.data / "Managed"

    def has(name: str, *needles: bytes) -> Optional[bool]:
        try:
            data = (managed / name).read_bytes()      # библиотеки — единицы МБ: целиком, без стыков блоков
        except OSError:
            return None                               # файла нет — судить не по чему
        return any(n in data for n in needles)

    corlib = has("mscorlib.dll", b"add_AssemblyResolve")
    engine = has(entrypoint_assembly(game), b"logMessageReceived", b"RegisterLogCallback")
    if corlib is False or engine is False:
        return ("Игра собрана с урезанными библиотеками .NET: модуль перевода (BepInEx) в ней не "
                "запускается." + _FILES_AND_LIVE)
    return ""


def write_bepinex_config(game: UnityGame, backup) -> None:
    """BepInEx.cfg: консоль выключена, загрузчик плагинов стартует из ``MonoBehaviour..cctor``.

    Точка входа BepInEx 5 по умолчанию — ``Application..cctor`` — в части игр вызывается раньше,
    чем движок готов создавать компоненты: XUnity добавляет свой компонент прямо при загрузке,
    и игра падает (Crawl, Unity 5.4: «Crash!!!» в AddComponent сразу после «Loading [XUnity
    Auto Translator]»), а в других играх у компонентов не вызывается Update. MonoBehaviour..cctor
    срабатывает позже, когда движок уже готов, — так советует документация BepInEx для таких
    случаев, и это точка по умолчанию в BepInEx 6. Другую точку входа, выбранную владельцем
    (для модов), не трогаем — меняется только умолчание.

    BepInEx, который стоял в игре до русификатора (моды игрока), не трогаем совсем: его моды
    работают с его настройкой, а с другой точкой входа стартуют в другой момент — Mad Island:
    «Mad Island Uncensor Tool» после переноса на MonoBehaviour..cctor ронял игру («Graphics device is
    null», «Crash!!!»)."""
    if game.backend != "mono":
        return                                     # BepInEx 6 (IL2CPP): своя настройка, и так поздняя
    core = game.root / "BepInEx" / "core"
    if core.is_dir() and not backup.is_ours(core / "BepInEx.dll"):
        return
    path = game.root / "BepInEx" / "config" / "BepInEx.cfg"
    try:
        text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    except OSError:
        text = ""
    if not text:
        text = "[Logging.Console]\n\nEnabled = false\n"
    current = ini_value(text, "Preloader.Entrypoint", "Type")
    if current not in (None, "", DEFAULT_ENTRY_TYPE):
        return
    new = set_ini_values(text, "Preloader.Entrypoint", {"Assembly": entrypoint_assembly(game),
                                                         "Type": "MonoBehaviour", "Method": ".cctor"})
    if new != text or not path.is_file():
        backup.write_text(path, new)


def ini_value(text: str, section: str, key: str) -> Optional[str]:
    """Значение ключа из секции ini/cfg (None — ключа нет)."""
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
        elif cur == section and "=" in s and not s.startswith(("#", ";")):
            k, v = s.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return None


def set_ini_values(text: str, section: str, values: Dict[str, str]) -> str:
    """Проставить ключи в секции ini/cfg (остальное — как было; секции нет — добавляется в конец)."""
    lines = text.splitlines()
    out: List[str] = []
    todo = dict(values)
    cur = None
    found = False

    def flush() -> None:
        while out and not out[-1].strip():
            out.pop()
        out.extend(f"{k} = {v}" for k, v in todo.items())
        todo.clear()
        out.append("")

    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if cur == section and todo:
                flush()
            cur = s[1:-1].strip()
            found = found or cur == section
        elif cur == section and "=" in s and not s.startswith(("#", ";")):
            k = s.split("=", 1)[0].strip()
            if k in values:
                out.append(f"{k} = {values[k]}")
                todo.pop(k, None)
                continue
        out.append(line)
    if cur == section and todo:
        flush()
    if not found:
        while out and not out[-1].strip():
            out.pop()
        out += ["", f"[{section}]", ""] + [f"{k} = {v}" for k, v in values.items()]
    return "\n".join(out).rstrip("\n") + "\n"


def decode_line(line: str) -> Optional[List[str]]:
    """Строка файла переводов XUnity -> [оригинал, перевод] (порт TextHelper.ReadTranslationLineAndDecode
    из XUnity 5.6) или None — комментарий, пустая или битая строка."""
    parts: List[str] = []
    buf: List[str] = []
    esc = False
    i = 0
    while i < len(line):
        c = line[i]
        if esc:
            if c in "=\\":
                buf.append(c)
            elif c == "n":
                buf.append("\n")
            elif c == "r":
                buf.append("\r")
            elif c == "u" and len(line) >= i + 5:
                try:
                    buf.append(chr(int(line[i + 1:i + 5], 16)))
                except ValueError:
                    buf.append("\\u")
                    i -= 4
                i += 4
            else:
                buf.append("\\" + c)
            esc = False
        elif c == "\\":
            esc = True
        elif c == "=":
            if len(parts) > 1:
                return None
            parts.append("".join(buf))
            buf = []
        elif c == "%" and line[i + 1:i + 3] == "3D":
            buf.append("=")
            i += 2
        elif c == "/" and line[i + 1:i + 2] == "/":   # комментарий до конца строки
            parts.append("".join(buf))
            return parts if len(parts) == 2 else None
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts if len(parts) == 2 else None


def read_translations(path: Path) -> Dict[str, str]:
    """Файл переводов XUnity (``оригинал=перевод``) -> словарь; правила-регулярки пропускаются."""
    out: Dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        kv = decode_line(line)
        if kv and kv[0] and not re.match(r'^s?r:"', kv[0]):
            out.setdefault(kv[0], kv[1])
    return out


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
        rules.append(_rule("sr", f"^{_flex_ws(_net_regex_escape(p))}([\\S\\s]+)$", value))
    return rules


def _rule(kind: str, regex: str, value: str) -> str:
    """Строка правила XUnity: ключ и значение экранируются целиком (``\\`` -> ``\\\\``, ``=`` -> ``\\=``,
    ``//`` -> ``\\u002F``), XUnity раскрывает это обратно в точное регулярное выражение."""
    return f'{encode(f"{kind}:" + chr(34) + regex + chr(34))}={encode(value)}'


#: плейсхолдер .NET String.Format: {0}, {1,-8}, {2:N0}
_FMT_RE = re.compile(r"\{(\d+)(,-?\d+)?(:[^{}]*)?\}")
_NUMERIC_FMT = re.compile(r":[NnDdFfCcPpEeGgXxRr0#.,%]")
#: вставка, которая переносится в перевод как есть (имя игрока в реплике Ink: «Hello, {0:v}.»)
VERBATIM_SLOT = ":v"
#: сколько правил-шаблонов писать: XUnity проверяет их только для текста, которого нет в словаре, а
#: регулярка, начинающаяся со слова, отсекается на первых буквах — тысячи правил не тормозят
MAX_TEMPLATE_RULES = 3000


def template_rules(pairs: Dict[str, str]) -> List[str]:
    """Правила XUnity для строк-шаблонов: ``You have {0} coins`` -> ``У вас {0} монет``.

    Игра подставляет в шаблон число или имя (String.Format, склейка строк в коде), и
    целиком такой строки нет ни в одном файле. Правило-регулярка ловит готовый текст
    («You have 5 coins») и подставляет вставки в русский шаблон. Числа переносятся как
    есть (``r:``), текстовые вставки XUnity переводит отдельно (``sr:`` — по словарю
    или живым переводом).
    """
    scored = []
    for src, tr in pairs.items():
        if "{" not in src or not tr or len(src) > 300 or '"' in src or '"' in tr:
            continue                      # кавычки в правиле XUnity читает как границы регулярки
        items = list(_FMT_RE.finditer(src))
        if not items or len(items) > 6:
            continue
        literal = _FMT_RE.sub("", src).replace("{{", "{").replace("}}", "}")
        letters = sum(1 for c in literal if c.isalpha())
        if letters < 4 or not re.search(r"[A-Za-z]{3}", literal):
            continue                      # «{0}/{1}», «x{0}» — слишком общее правило
        idx = [m.group(1) for m in items]
        if len(set(idx)) != len(idx):
            continue                      # одна вставка дважды — регулярка неоднозначна
        if sorted(m.group(0) for m in _FMT_RE.finditer(tr)) != sorted(m.group(0) for m in items):
            continue                      # перевод потерял или добавил вставку
        if any(items[k].end() == items[k + 1].start() for k in range(len(items) - 1)):
            continue                      # «{0}{1}» — не разделить
        parts, group_of, numeric, pos = [], {}, True, 0
        for n, m in enumerate(items, 1):
            parts.append(_flex_ws(_net_regex_escape(src[pos:m.start()].replace("{{", "{").replace("}}", "}"))))
            if m.group(3) and _NUMERIC_FMT.match(m.group(3)):
                parts.append(r"([-+]?[\d\s.,\u00a0]*\d[%]?)")
            elif m.group(3) == VERBATIM_SLOT:
                parts.append(r"([\S\s]+?)")     # \u0438\u043c\u044f \u0438\u0433\u0440\u043e\u043a\u0430 \u0438 \u043f\u0440\u043e\u0447\u0438\u0435 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u044f \u2014 \u043a\u0430\u043a \u0435\u0441\u0442\u044c
            else:
                parts.append(r"([\S\s]+?)")
                numeric = False
            group_of[m.group(1)] = n
            pos = m.end()
        parts.append(_flex_ws(_net_regex_escape(src[pos:].replace("{{", "{").replace("}}", "}"))))
        value = tr.replace("$", "$$").replace("{{", "{").replace("}}", "}")
        ok = True

        def sub(m: "re.Match") -> str:
            nonlocal ok
            after = value[m.end():m.end() + 1]
            if after.isdigit():
                ok = False                # «$1» перед цифрой прочитается как «$10»
            return "$" + str(group_of.get(m.group(1), 0))
        value = _FMT_RE.sub(sub, value)
        if not ok or "$0" in value:
            continue
        scored.append((letters, _rule("r" if numeric else "sr", "^" + "".join(parts) + "$", value)))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:MAX_TEMPLATE_RULES]]


def template_fragments(pairs: Dict[str, str]) -> Dict[str, str]:
    """Хвосты шаблонов с одной вставкой с краю: ``{0} points!`` -> ``{0} очков!`` даёт ``points!`` ->
    ``очков!``. Общее правило «число + слово» (:func:`number_rules`) делит «370 points!» на число и слово
    и переводит слово отдельно — без этой строки в словаре слово уходило в живой перевод и выходило
    «370 точки!». Хвост из перевода всего шаблона согласован с числом и важнее перевода слова в отрыве."""
    out: Dict[str, str] = {}
    for src, tr in pairs.items():
        items = list(_FMT_RE.finditer(src))
        if len(items) != 1 or not tr or "\n" in src:
            continue
        m = items[0]
        slot = m.group(0)
        if m.start() == 0 and src[m.end():m.end() + 1] == " " and tr.startswith(slot + " "):
            frag, frag_tr = src[m.end():].strip(), tr[len(slot):].strip()
        elif m.end() == len(src) and src[m.start() - 1:m.start()] == " " and tr.endswith(" " + slot):
            frag, frag_tr = src[:m.start()].strip(), tr[:-len(slot)].strip()
        else:
            continue
        if re.search(r"[A-Za-z]{3}", frag) and frag_tr and "{" not in frag and "{" not in frag_tr:
            out[frag] = frag_tr
    return out


def number_rules() -> List[str]:
    """Правила XUnity «число + слово»: игра склеивает число со словом из словаря («+2 Vitality», «-1
    Agility», «Level 7», «Gold: 50») — целиком такой строки нет нигде. Число остаётся как есть, слово
    переводится по словарю (или живым переводом). Ставятся после точных строк и шаблонов."""
    num = r"[-+\u2212]?\d+(?:[.,]\d+)?%?"
    return [_rule("sr", rf"^({num}\s+)(\p{{L}}[\S\s]*)$", "$1$2"),
            # \u00ab1 - Title\u00bb, \u00ab2. Check This Box\u00bb, \u00ab3) Options\u00bb \u2014 \u043d\u043e\u043c\u0435\u0440 \u043f\u0443\u043d\u043a\u0442\u0430 \u0441\u043f\u0438\u0441\u043a\u0430 \u043f\u0435\u0440\u0435\u0434 \u043f\u043e\u0434\u043f\u0438\u0441\u044c\u044e
            _rule("sr", rf"^({num}\s*[-\u2013\u2014.)]\s+)(\p{{L}}[\S\s]*)$", "$1$2"),
            _rule("sr", rf"^(\p{{L}}[\S\s]*?)(:?\s+{num})$", "$1$2")]


def label_rules() -> List[str]:
    """\u041f\u0440\u0430\u0432\u0438\u043b\u043e XUnity \u00ab\u043f\u043e\u0434\u043f\u0438\u0441\u044c + \u0434\u0432\u043e\u0435\u0442\u043e\u0447\u0438\u0435\u00bb: \u0438\u0433\u0440\u0430 \u043f\u0440\u0438\u043a\u043b\u0435\u0438\u0432\u0430\u0435\u0442 \u00ab:\u00bb \u043a \u043f\u043e\u0434\u043f\u0438\u0441\u0438 \u0438\u0437 \u0434\u0430\u043d\u043d\u044b\u0445 (\u00abPause\u00bb \u2192 \u00abPause:\u00bb \u0432
    \u0441\u043f\u0438\u0441\u043a\u0435 \u043a\u043b\u0430\u0432\u0438\u0448) \u2014 \u0442\u0430\u043a\u043e\u0439 \u0441\u0442\u0440\u043e\u043a\u0438 \u043d\u0435\u0442 \u043d\u0438\u0433\u0434\u0435. \u041f\u043e\u0434\u043f\u0438\u0441\u044c \u043f\u0435\u0440\u0435\u0432\u043e\u0434\u0438\u0442\u0441\u044f \u043f\u043e \u0441\u043b\u043e\u0432\u0430\u0440\u044e, \u0434\u0432\u043e\u0435\u0442\u043e\u0447\u0438\u0435 \u2014 \u043a\u0430\u043a \u0432 \u0438\u0433\u0440\u0435."""
    return [_rule("sr", r"^(\p{L}[^\n:]{0,40}?):$", "$1:"),      # без пустых групп-вставок
            # «Loading...», «Quit?», «Continue!» — знак, приклеенный кодом к подписи из словаря
            _rule("sr", r"^(\p{L}[^\n.…!?]{0,40}?)(\s*(?:\.{1,3}|…|[!?]{1,3}))$", "$1$2"),
            # «> Start», «• Item», «- Option» — маркер пункта меню перед подписью
            _rule("sr", r"^([-–—•·>*~]{1,3}\s*)(\p{L}[^\n]{0,60})$", "$1$2"),
            # «< Easy >», «‹ Normal ›» — переключатель значения стрелками
            _rule("sr", r"^([<‹«]\s*)(\p{L}[^\n<>‹›«»]{0,40}?)(\s*[>›»])$", "$1$2$3")]


def case_variants(pairs: Dict[str, str]) -> Dict[str, str]:
    """\u041a\u0410\u041f\u0421-\u0432\u0430\u0440\u0438\u0430\u043d\u0442\u044b \u043a\u043e\u0440\u043e\u0442\u043a\u0438\u0445 \u0441\u0442\u0440\u043e\u043a \u0441\u043b\u043e\u0432\u0430\u0440\u044f: \u0438\u0433\u0440\u044b \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u044e\u0442 \u0438\u043c\u044f \u0438\u043b\u0438 \u043f\u043e\u0434\u043f\u0438\u0441\u044c \u0438 \u043a\u0430\u043a \u0435\u0441\u0442\u044c, \u0438 \u0447\u0435\u0440\u0435\u0437
    ToUpper() (\u00abTom\u00bb \u0432 \u0441\u043f\u0438\u0441\u043a\u0435, \u00abTOM\u00bb \u0432 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0435) \u2014 \u0441\u0442\u0440\u043e\u043a\u0438 \u043a\u0430\u043f\u0441\u043e\u043c \u043d\u0435\u0442 \u0432 \u0434\u0430\u043d\u043d\u044b\u0445, \u0430 \u043f\u0435\u0440\u0435\u0432\u043e\u0434 \u0438\u0437\u0432\u0435\u0441\u0442\u0435\u043d:
    \u00ab\u0422\u041e\u041c\u00bb. \u0421\u0442\u0440\u043e\u043a\u0438 \u0441 \u0440\u0430\u0437\u043c\u0435\u0442\u043a\u043e\u0439 \u0438 \u0448\u0430\u0431\u043b\u043e\u043d\u044b \u043d\u0435 \u0442\u0440\u043e\u0433\u0430\u044e\u0442\u0441\u044f (\u0442\u0435\u0433\u0438 \u0438 \u0432\u0441\u0442\u0430\u0432\u043a\u0438 \u043a\u0430\u043f\u0441\u043e\u043c \u043d\u0435 \u0442\u0435 \u0436\u0435 \u0441\u0430\u043c\u044b\u0435)."""
    out: Dict[str, str] = {}
    for src, tr in pairs.items():
        if not tr or tr == src or len(src) > 40 or len(src.split()) > 6 or not re.search(r"[a-z]", src):
            continue
        if any(c in src for c in "<>{}[]\\\n=") or any(c in tr for c in "<>{}[]\\\n"):
            continue
        up = src.upper()
        if up in out:
            continue
        # строка капсом уже переведена сама по себе — переводчик капс понимает хуже («DAVID MOST» →
        # «ДЭВИДА БОЛЬШИНСТВО»), а та же строка обычным регистром переведена правильно: берётся она
        if up not in pairs or (pairs[up] and pairs[up] != tr.upper() and re.search(r"[a-z]", src[1:])):
            out[up] = tr.upper()
        low = src.lower()                 # ToLower(): «options», «tom» — строчными, если такой строки нет
        if low != src and low not in pairs and low not in out and re.search(r"[A-Z]", src):
            out[low] = tr.lower()
    return out


def paragraph_rules() -> List[str]:
    """Правило XUnity «по абзацам»: текст, которого целиком нет в словаре, но который игра склеила из
    абзацев (описание предмета + пустая строка + строка обмена/характеристик), переводится по частям —
    каждый абзац по словарю, правилам-шаблонам или живым переводом. Так склейки переводятся и в
    русификаторе «для друзей», где живого перевода нет. Правило — последним: словарь и более точные
    правила проверяются раньше."""
    return [_rule("sr", r"^([\S\s]+?)\n\n([\S\s]+)$", "$1\n\n$2")]


def _net_regex_escape(s: str) -> str:
    return re.sub(r"([\\^$.|?*+()\[\]{}])", r"\\\1", s)


def _flex_ws(pattern: str) -> str:
    """Пробелы и переводы строк в тексте правила — любые пробельные: игра переносит строки сама
    («…by buying items↵in-game») и вставляет «\r\n» вместо «\n» — правило должно сработать."""
    return re.sub(r"[ \t\r\n]+", lambda m: r"\s+", pattern)


def short_guards(pairs: Dict[str, str]) -> List[str]:
    """Записи «как есть» для одиночных букв и цифр и для 1–2 первых знаков каждой строки словаря.

    С GeneratePartialTranslations XUnity строит перевод для каждого начала строки словаря (чтобы
    текст, который игра печатает по букве, сразу шёл по-русски) и применяет его к любому тексту:
    буква «B» в сетке ввода имени или подпись кнопки «A» получают начало чужого перевода («Б»,
    «Сл»). Точная запись важнее частичной — короткие строки остаются как в игре; печатающийся
    текст теряет перевод только на первых двух буквах."""
    keep = set(string.ascii_letters + string.digits)
    for src in pairs:
        for k in (1, 2):
            if len(src) > k:
                keep.add(src[:k])
    out = []
    for p in sorted(keep):
        if p in pairs or not p.strip() or p != p.strip() or p.startswith(("r:", "//")) or any(c in p for c in "\r\n="):
            continue
        out.append(f"{encode(p)}={encode(p)}")
    return out


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
    lines += ["// Короткие строки — как есть: иначе XUnity подставит в них начало чужого перевода",
              "// (частичные переводы для печатающегося текста: «B» → «Б», «I» → «Вс»)."] + short_guards(pairs)
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
