"""Тесты плагинов Ren'Py и RPG Maker (все версии) и диспетчера движков."""

from __future__ import annotations

import io
import json
import pickle
import re
import shutil
import struct
import zlib
from pathlib import Path

import pytest

from russificator.core.backup import GameBackup
from russificator.core.plugin_api import EngineNotDetectedError
from russificator.core.registry import detect_engine
from russificator.core.restore import restore_backups
from russificator.core.universal import EntryStatus, TextKind, TranslationProject

DATA = Path(__file__).parent / "data"


def _project(game: Path, engine: str) -> TranslationProject:
    proj = TranslationProject(game, engine, game.parent / "work")
    proj.backup = GameBackup(game)
    return proj


def _translate_all(proj: TranslationProject, fn=lambda s: "RU " + s) -> None:
    for e in proj.entries:
        e.translation = fn(e.source)
        e.status = EntryStatus.TRANSLATED


# ============================= Ren'Py =============================

def write_rpa(path: Path, files: dict, key: int = 0x42424242) -> None:
    """Архив RPA-3.0 в том же виде, в каком его пишет Ren'Py."""
    with path.open("wb") as fh:
        fh.write(b"RPA-3.0 %016x %08x\n" % (0, 0))
        index = {}
        for name, data in files.items():
            off = fh.tell()
            fh.write(data)
            index[name] = [(off ^ key, len(data) ^ key, b"")]
        idx_off = fh.tell()
        fh.write(zlib.compress(pickle.dumps(index, protocol=2)))
        fh.seek(0)
        fh.write(b"RPA-3.0 %016x %08x\n" % (idx_off, key))


def test_renpy_string_rules_match_lexer():
    from russificator.engines.renpy.rpy_parse import renpy_string
    assert renpy_string('"Hello   world"') == "Hello world"            # пробелы схлопываются
    assert renpy_string(r'"Line\nnext"') == "Line\nnext"
    assert renpy_string(r'"Use \{b\} and \[x\] and 50\%"') == "Use {{b} and [[x] and 50%%"
    assert renpy_string(r'"Quote \" here"') == 'Quote " here'
    assert renpy_string(r'"Ж"') == "Ж"


def test_rpyc_from_real_renpy_813():
    """Файл скомпилирован настоящим Ren'Py 8.1.3 (исходник — tests/data/renpy813_script.rpy)."""
    from russificator.engines.renpy import rpyc
    stmts = rpyc.load_rpyc((DATA / "renpy813_script.rpyc").read_bytes())
    found, chars = rpyc.extract_texts(stmts, "script.rpy")
    texts = {(f.kind, f.text) for f in found}
    assert ("narration", "It was a dark and stormy night.") in texts
    assert ("dialogue", "Hello, [player]! How are you today?") in texts
    assert ("dialogue", "I'm {b}really{/b} happy to see you.{w} Let's go!") in texts
    assert ("dialogue", "Wait for me!\nI'm coming.") in texts
    assert ("choice", "Go to the forest") in texts
    assert ("ui", "Start the adventure") in texts           # textbutton _("…") на экране
    assert ("ui", "Welcome to the test screen") in texts    # text "…" на экране
    assert ("ui", "Game saved") in texts                    # _("…") в Python
    assert chars == {"e": "Eileen", "m": "Mary"}
    assert rpyc.font_references(stmts) == ["fonts/LatinOnly.ttf"]


def _sl_node(cls: str, **attrs):
    """Узел дерева .rpyc в том виде, в каком его распаковывает rpyc (заглушка класса Ren'Py)."""
    from russificator.engines.renpy import rpyc
    module, name = cls.rsplit(".", 1)
    node = rpyc._SafeUnpickler.find_class(rpyc._SafeUnpickler(io.BytesIO(b"")), module, name)()
    node.__dict__.update(attrs)
    return node


def test_rpyc_screen_expressions_give_their_strings():
    """_("…") внутри выражений экрана — тоже текст для игрока: «empty slot» у FileTime,
    шаблон «Page {}» у FilePageNameInputValue; литерал-аргумент текста берётся целиком."""
    from russificator.engines.renpy import rpyc
    tree = [
        _sl_node("renpy.sl2.slast.SLDisplayable", positional=[
            'FileTime(slot, format=_("{#file_time}%A, %B %d %Y, %H:%M"), empty=_("empty slot"))'], keyword=[]),
        _sl_node("renpy.sl2.slast.SLDisplayable", positional=['_("{#quick_page}Quick")'],
                 keyword=[("action", 'FilePage("quick")'), ("hovered", 'Notify("Quick saves here")')]),
        _sl_node("renpy.sl2.slast.SLDefault", variable="page_name_value", expression=(
            'FilePageNameInputValue(pattern=_("Page {}"), auto=_("Automatic saves"), quick=_("Quick saves"))')),
        _sl_node("renpy.sl2.slast.SLIf", entries=[('persistent.flashing', None)]),
        _sl_node("renpy.ast.ArgumentInfo", arguments=[(None, '_("Pick a card")'), ("style", '"card"')]),
    ]
    found, _ = rpyc.extract_texts(tree, "screens.rpy")
    texts = [f.text for f in found]
    for s in ("empty slot", "{#quick_page}Quick", "Quick saves here", "Page {}", "Automatic saves",
              "Quick saves", "Pick a card"):
        assert texts.count(s) == 1, s
    assert "quick" not in texts and "card" not in texts and "persistent.flashing" not in texts


def test_renpy_show_text_splash_is_extracted():
    """Заставки «show text …» (предупреждение, вступление) — текст, а не картинка: в .rpyc строка
    лежит частями имени картинки text, в исходнике — после «show text»."""
    from russificator.engines.renpy import rpy_parse, rpyc
    warning = "{size=72}WARNING{/size}\n\nThis is a horror game."
    tree = [
        _sl_node("renpy.ast.Show", linenumber=1,
                 imspec=(("text", "_", '("{size=72}WARNING{/size}\\n\\nThis is a horror game.")'), None, None, [],
                         None, None, None)),
        _sl_node("renpy.ast.Show", linenumber=2, imspec=(("text", '"Welcome home."'), None, None, [], None, None, None)),
        _sl_node("renpy.ast.Show", linenumber=3, imspec=(("bg", "forest"), None, None, [], None, None, None)),
        _sl_node("renpy.ast.Show", linenumber=4, imspec=(None, 'Text(_("Chapter one"))', None, [], None, None, None)),
    ]
    found, _ = rpyc.extract_texts(tree, "script.rpy")
    assert [(f.kind, f.text) for f in found] == [
        ("narration", warning), ("narration", "Welcome home."), ("narration", "Chapter one")]

    src = 'label start:\n    show text _("{size=72}WARNING{/size}\\n\\nThis is a horror game.") at truecenter\n' \
          '    show text "Welcome home."\n    show bg forest\n'
    found, _ = rpy_parse.extract_texts(src, "script.rpy")
    assert [(f.kind, f.text) for f in found] == [("narration", warning), ("narration", "Welcome home.")]


def _run_hook(tmp_path, table, translations=None):
    """Выполнить хук Ren'Py на подставных объектах движка. Возвращает (пространство имён, Cfg, класс
    переводчика строк, класс Text)."""
    from russificator.engines.renpy.plugin import hook_script
    code = hook_script({}).split("init 999 python:", 1)[1].replace("\n    ", "\n")

    class StringTranslator:
        def translate(self, old):
            return (translations or {}).get(old, old)

    class Cfg:
        say_menu_text_filter = None
        replace_text = None
        underlay: list = []
        interact_callbacks: list = []
        font_replacement_map: dict = {}
        gamedir = str(tmp_path)
        savedir = None

    class Render:
        def __init__(self, w, h):
            self.width, self.height, self.zoomed = w, h, None

        def blit(self, r, pos):
            self.child = r

        def zoom(self, x, y):
            self.zoomed = (x, y)

    class Text:
        """Ширина текста — 10 px на знак видимого текста (теги не в счёт)."""
        def __init__(self, text, style=None, substitute=None, **props):
            self.text_parameter = [text]
            self.style = style or type("Style", (), {"font": "f.ttf", "size": 20, "bold": False,
                                                     "italic": False, "kerning": 0})()

        def size(self, width=4096, height=4096, st=0, at=0):
            visible = re.sub(r"\{[^{}]*\}", "", ns["Translator"]().translate(self.text_parameter[0])
                             if not ns["_ru_measure"][0] else self.text_parameter[0])
            return float(len(visible) * 10), 20.0

        def render(self, width, height, st, at):
            w, h = self.size(width, height)
            return Render(w, h)

    class Translation:
        pass
    Translation.StringTranslator = StringTranslator

    class RenPy:
        translation = Translation
        text = type("m", (), {"text": type("m", (), {"Text": Text})})

        @staticmethod
        def open_file(name):
            return io.BytesIO(json.dumps(table).encode("utf-8"))

        @staticmethod
        def Keymap(**kw):
            return kw

    class RevertableList(list):
        pass

    class RevertableDict(dict):
        pass

    class RevertableSet(set):
        pass

    # в скриптах Ren'Py list/dict/set — «откатываемые» подклассы: isinstance(обычный список, list) ложно
    ns = {"config": Cfg, "renpy": RenPy, "Text": Text, "Translator": StringTranslator,
          "list": RevertableList, "dict": RevertableDict, "set": RevertableSet}
    RenPy.Render = Render
    exec(code, ns)
    return ns, Cfg, StringTranslator, Text


def test_renpy_hook_translates_screen_text_before_substitution(tmp_path):
    """Строки экранов переводит штатный переводчик строк Ren'Py — до подстановки [переменных]
    и .format(): config.replace_text видит уже «Name: sdf» и «Page 1», которых нет в словаре."""
    table = {"{size=24}Name: [player_name]{/size}": "{size=24}Имя: [player_name]{/size}",
             "Page {}": "Страница {}", "{#quick_page}Quick": "{#quick_page}Быстрые"}
    ns, cfg, stl_cls, _ = _run_hook(tmp_path, table, {"Start": "Начать (перевод игры)"})
    stl = stl_cls()
    assert stl.translate("{size=24}Name: [player_name]{/size}") == "{size=24}Имя: [player_name]{/size}"
    assert stl.translate("Page {}").format(1) == "Страница 1"
    assert stl.translate("{#quick_page}Quick") == "{#quick_page}Быстрые"
    assert stl.translate("Start") == "Начать (перевод игры)"   # свой перевод игры важнее
    assert stl.translate("Unknown") == "Unknown"
    cfg.underlay[0]["alt_K_t"].__globals__["_ru_on"][0] = False  # Alt+T — оригинал
    assert stl.translate("Page {}") == "Page {}"


def test_renpy_hook_journals_untranslated_strings(tmp_path):
    """Строка, которую игра показала, а словаря для неё нет, попадает в журнал (папка игры, иначе
    папка сохранений) — следующая русификация возьмёт её в словарь. Числа, русский текст и
    строки без слов в журнал не идут; журнал копится между запусками игры."""
    from russificator.engines.renpy.plugin import MISSED_FILE, RenPyPlugin
    ns, cfg, stl_cls, _ = _run_hook(tmp_path, {"Known": "Известно"})
    stl = stl_cls()
    cfg.name = "My Game Demo"                                    # название игры — не в журнал
    for s in ("Known", "Built by code: [n] coins", "42", "Уже по-русски", "{w}", "My Game Demo"):
        stl.translate(s)
    ns["config"].say_menu_text_filter("A line the extractor missed.")
    ns["_ru_jstate"]["at"] = 0.0
    for cb in cfg.interact_callbacks:
        cb()
    data = json.loads((tmp_path / MISSED_FILE).read_text(encoding="utf-8"))
    assert data["strings"] == ["Built by code: [n] coins", "A line the extractor missed."]

    ns2, cfg2, stl_cls2, _ = _run_hook(tmp_path, {})      # второй запуск игры — журнал дополняется
    stl_cls2().translate("Another one")
    ns2["_ru_jstate"]["at"] = 0.0
    cfg2.interact_callbacks[0]()
    data = json.loads((tmp_path / MISSED_FILE).read_text(encoding="utf-8"))
    assert data["strings"][-1] == "Another one" and len(data["strings"]) == 3

    work = tmp_path / "work"
    game = tmp_path / "Game"
    (game / "game").mkdir(parents=True)
    (game / "game" / MISSED_FILE).write_text(json.dumps(data), encoding="utf-8")
    assert RenPyPlugin().harvest_live(game, work) == 3          # перед откатом — в проект
    assert RenPyPlugin().harvest_live(game, work) == 0


def test_renpy_hook_shrinks_label_wider_than_original(tmp_path):
    """Пункт меню, чей перевод шире английского оригинала, ужимается до его ширины; влезающий —
    не трогается; без списка «fit» в словаре хук ничего не ужимает."""
    from russificator.engines.renpy.plugin import META_KEY
    table = {"Join our Discord": "Присоединяйтесь к нашему Discord", "Start": "Начать",
             "Load": "Загрузить", META_KEY: {"fit": ["Join our Discord", "Load"]}}
    ns, cfg, stl_cls, text_cls = _run_hook(tmp_path, table)
    long = text_cls("Join our Discord").render(1920, 1080, 0, 0)    # 320 px при оригинале 160
    assert long.child.zoomed and abs(long.width - 240) < 1           # запас: 4 буквы шрифта (20 px)
    load = text_cls("Load").render(1920, 1080, 0, 0)                # 90 px при оригинале 40: в запасе
    assert not hasattr(load, "child") and load.width == 90
    short = text_cls("Start").render(1920, 1080, 0, 0)
    assert not hasattr(short, "child") and short.width == 60
    assert "Join our Discord" not in ns["_ru_missed"]                 # замер оригинала — не пропуск


def test_rpa_archive_roundtrip(tmp_path):
    from russificator.engines.renpy.archive import RpaArchive
    arc = tmp_path / "archive.rpa"
    write_rpa(arc, {"script.rpyc": b"abc", "images/x.png": b"\x89PNG"})
    a = RpaArchive(arc)
    assert not a.error and a.names() == ["images/x.png", "script.rpyc"]
    assert a.read("script.rpyc") == b"abc"


def test_rpa_rejects_code_in_index(tmp_path):
    from russificator.engines.renpy.archive import RpaArchive

    class Evil:
        def __reduce__(self):
            return (print, ("pwned",))

    arc = tmp_path / "evil.rpa"
    with arc.open("wb") as fh:
        body = zlib.compress(pickle.dumps({"x": Evil()}))
        fh.write(b"RPA-3.0 %016x %08x\n" % (34, 0) + body)
    assert RpaArchive(arc).error


@pytest.fixture
def renpy_release(tmp_path):
    """Игра как её выпускают: только .rpyc внутри .rpa, шрифт без кириллицы."""
    root = tmp_path / "MyGame"
    (root / "game").mkdir(parents=True)
    (root / "renpy").mkdir()
    write_rpa(root / "game" / "archive.rpa", {
        "script.rpyc": (DATA / "renpy813_script.rpyc").read_bytes(),
        "fonts/LatinOnly.ttf": b"not a font with cyrillic",
    })
    return root


def test_renpy_release_game_end_to_end(renpy_release):
    from russificator.engines.renpy.plugin import RenPyPlugin, TL_FILE, HOOK_FILE
    plugin = RenPyPlugin()
    assert plugin.detect(renpy_release).confidence >= 0.6
    proj = _project(renpy_release, "renpy")
    res = plugin.extract(renpy_release, proj)
    sources = {e.source for e in res.entries}
    assert "Hello, [player]! How are you today?" in sources
    assert "gui/bg.png" not in sources                       # пути к картинкам не текст
    eileen = next(e for e in res.entries if e.source.startswith("Hello, [player]"))
    assert eileen.speaker == "Eileen" and eileen.neighbors   # имя персонажа и контекст для нейросети

    _translate_all(proj)
    plugin.inject(renpy_release, proj)
    game = renpy_release / "game"
    table = json.loads((game / TL_FILE).read_text(encoding="utf-8"))
    assert table["Go to the forest"] == "RU Go to the forest"
    assert table["I'm really happy to see you. Let's go!"].startswith("RU I'm really")  # ключ без тегов
    hook = (game / HOOK_FILE).read_text(encoding="utf-8")
    assert "config.say_menu_text_filter" in hook and "config.replace_text" in hook
    assert "('fonts/LatinOnly.ttf', False, False)" in hook       # шрифт без кириллицы подменён
    assert (game / "russificator" / "PT_Sans-Web-Regular.ttf").is_file()
    compile(hook.split("init 999 python:", 1)[1].replace("\n    ", "\n"), "hook", "exec")  # валидный Python

    restore_backups(renpy_release)
    assert sorted(p.name for p in game.iterdir()) == ["archive.rpa"]


@pytest.fixture
def renpy_source_game(tmp_path):
    root = tmp_path / "Src"
    game = root / "game"
    game.mkdir(parents=True)
    (game / "script.rpy").write_text(
        'define e = Character("Eileen")\n'
        'label start:\n'
        '    scene bg room\n'
        '    "It\'s a  sunny day."\n'
        '    e happy "Nice weather, right?"\n'
        '    menu:\n'
        '        "Go outside":\n'
        '            jump outside\n'
        '    $ score = _("Best score")\n'
        '    play music "audio/theme.ogg"\n', encoding="utf-8")
    return root


def test_renpy_source_only_game(renpy_source_game):
    from russificator.engines.renpy.plugin import RenPyPlugin
    proj = _project(renpy_source_game, "renpy")
    res = RenPyPlugin().extract(renpy_source_game, proj)
    by_src = {e.source: e for e in res.entries}
    assert "It's a sunny day." in by_src                 # как в движке: двойной пробел схлопнут
    assert by_src["Nice weather, right?"].speaker == "Eileen"
    assert by_src["Go outside"].kind == TextKind.CHOICE
    assert "Best score" in by_src and "Eileen" in by_src
    assert not any("theme.ogg" in s or "bg room" in s for s in by_src)


# ============================= RPG Maker: Marshal и архивы =============================

RUBY_VECTORS = {  # байты, которые выдаёт Marshal.dump в Ruby 1.9+
    b"\x04\x08I\"\x08abc\x06:\x06ET": "abc",
    b"\x04\x08[\x07i\x06i\x07": [1, 2],
    b"\x04\x08i\x02,\x01": 300,
    b"\x04\x08i\xfa": -1,
    b"\x04\x08i\x7f": 122,
    b"\x04\x08i\x01{": 123,
    b"\x04\x08i\x80": -123,
    b"\x04\x08i\xff\x84": -124,
    b"\x04\x08i\xff\x7f": -129,
    b"\x04\x08f\x081.5": 1.5,
    b"\x04\x08:\x08sym": "sym",
    b"\x04\x08[\x07I\"\x06x\x06:\x06ET@\x06": ["x", "x"],
    b"\x04\x08[\x07:\x06a;\x00": ["a", "a"],
    b"\x04\x08{\x06i\x06I\"\x06a\x06:\x06ET": {1: "a"},
}


@pytest.mark.parametrize("raw,expected", list(RUBY_VECTORS.items()))
def test_marshal_matches_ruby(raw, expected):
    from russificator.engines.rpgmaker import ruby_marshal as M
    value = M.load(raw)
    assert value == expected
    assert M.dump(value, utf8_strings=True) == raw


def test_marshal_links_and_types_survive_roundtrip():
    from russificator.engines.rpgmaker import ruby_marshal as M
    table = M.RubyUserDef("Table", b"\x01\x02\x03")
    shared = M.RubyString("shared", [("E", True)])
    obj = M.RubyObject("RPG::Event", {"@id": 1, "@name": shared, "@x": 1.25, "@t": table, "@sym": M.RubySymbol("a")})
    data = [obj, obj, table, shared, 2 ** 40]
    back = M.load(M.dump(data, utf8_strings=True))
    assert back[0] is back[1] and back[2] is back[0].ivars["@t"] and back[3] is back[0].ivars["@name"]
    assert isinstance(back[0].ivars["@sym"], M.RubySymbol) and back[4] == 2 ** 40
    assert back[0].ivars["@x"] == 1.25 and back[2].payload == b"\x01\x02\x03"


def _build_v1(files: dict) -> bytes:
    """Архив RGSSAD v1 (XP/VX) — шифрование симметрично чтению."""
    from russificator.engines.rpgmaker.rgssad import MASK, _decrypt_data
    key = 0xDEADCAFE
    out = bytearray(b"RGSSAD\0\x01")
    for name, data in files.items():
        nb = name.replace("/", "\\").encode()
        out += struct.pack("<I", len(nb) ^ key)
        key = (key * 7 + 3) & MASK
        for b in nb:
            out.append(b ^ (key & 0xFF))
            key = (key * 7 + 3) & MASK
        out += struct.pack("<I", len(data) ^ key)
        key = (key * 7 + 3) & MASK
        out += _decrypt_data(data, key)
    return bytes(out)


@pytest.mark.parametrize("version", [1, 3])
def test_rgssad_archives(tmp_path, version):
    from russificator.engines.rpgmaker.rgssad import RgssArchive, build_v3
    files = {"Data/Map001.rvdata2": b"map-bytes" * 7, "Graphics/Pictures/a.png": bytes(range(250))}
    path = tmp_path / ("Game.rgss3a" if version == 3 else "Game.rgssad")
    path.write_bytes(build_v3(files) if version == 3 else _build_v1(files))
    arc = RgssArchive(path)
    assert arc.version == version and arc.names() == sorted(files)
    for name, data in files.items():
        assert arc.read(name) == data


# ============================= RPG Maker VX Ace (данные в архиве) =============================

def _cmd(code, params, indent=0):
    from russificator.engines.rpgmaker import ruby_marshal as M
    return M.RubyObject("RPG::EventCommand", {"@code": code, "@indent": indent, "@parameters": params})


@pytest.fixture
def ace_game(tmp_path):
    from russificator.engines.rpgmaker import ruby_marshal as M
    from russificator.engines.rpgmaker.rgssad import build_v3
    s = lambda t: M.RubyString(t, [("E", True)])  # noqa: E731
    page = M.RubyObject("RPG::Event::Page", {"@list": [
        _cmd(101, [s("Actor1"), 0, 0, 2]),
        _cmd(401, [s("Welcome to our village,")]),
        _cmd(401, [s("traveler! \\C[2]Rest\\C[0] here.")]),
        _cmd(102, [[s("Yes"), s("No")], 2]),
        _cmd(0, []),
    ]})
    events = M.RubyHash({1: M.RubyObject("RPG::Event", {"@id": 1, "@name": s("EV001"), "@pages": [page]}),
                         2: M.RubyObject("RPG::Event", {"@id": 2, "@name": s("EV001"), "@pages": [page]})})
    mp = M.RubyObject("RPG::Map", {"@events": events, "@data": M.RubyUserDef("Table", b"\0" * 20)})
    items = [None, M.RubyObject("RPG::Item", {"@id": 1, "@name": s("Potion"), "@description": s("Heals 50 HP.")})]
    root = tmp_path / "AceGame"
    root.mkdir()
    (root / "Game.exe").write_bytes(b"MZ")
    (root / "Game.rgss3a").write_bytes(build_v3({
        "Data/Map001.rvdata2": M.dump(mp, utf8_strings=True),
        "Data/Items.rvdata2": M.dump(items, utf8_strings=True),
        "Graphics/System/Window.png": b"png",
    }))
    return root


def test_vxace_from_archive_end_to_end(ace_game):
    from russificator.engines.rpgmaker import ruby_marshal as M
    from russificator.engines.rpgmaker.plugin import RPGMakerPlugin
    plugin = RPGMakerPlugin()
    det = plugin.detect(ace_game)
    assert det.confidence >= 0.9 and plugin.version == "ace"
    proj = _project(ace_game, "rpgmaker")
    res = plugin.extract(ace_game, proj)
    assert not (ace_game / "Data").exists()                   # до внедрения игра не тронута
    msgs = [e for e in res.entries if e.kind == TextKind.DIALOGUE]
    assert len(msgs) == 2 and len({e.target_key for e in msgs}) == 2    # одноимённые события не слиплись
    assert msgs[0].source == "Welcome to our village, traveler! \\C[2]Rest\\C[0] here."
    assert {e.source for e in res.entries} >= {"Yes", "No", "Potion", "Heals 50 HP."}

    _translate_all(proj, lambda s: "Добро пожаловать в нашу деревню, путник! \\C[2]Отдохни\\C[0] здесь."
                   if s.startswith("Welcome") else "RU " + s)
    plugin.inject(ace_game, proj)
    assert not (ace_game / "Game.rgss3a").exists()             # движок иначе прочитает архив
    assert (ace_game / "Graphics/System/Window.png").read_bytes() == b"png"
    mp = M.load((ace_game / "Data/Map001.rvdata2").read_bytes())
    cmds = mp.ivars["@events"][1].ivars["@pages"][0].ivars["@list"]
    lines = [c.ivars["@parameters"][0] for c in cmds if c.ivars["@code"] == 401]
    assert " ".join(lines) == "Добро пожаловать в нашу деревню, путник! \\C[2]Отдохни\\C[0] здесь."
    assert all(len(ln) <= 42 for ln in lines) and len(lines) >= 2  # перенос по ширине окна (с лицом)
    assert cmds[-2].ivars["@code"] == 102 and cmds[-2].ivars["@parameters"][0] == ["RU Yes", "RU No"]
    raw = (ace_game / "Data/Map001.rvdata2").read_bytes()
    assert "Добро".encode() in raw and b":\x06ET" in raw          # строки записаны как UTF-8 (E: true)

    restore_backups(ace_game)
    assert sorted(p.name for p in ace_game.iterdir()) == ["Game.exe", "Game.rgss3a"]


# ============================= RPG Maker MV/MZ =============================

@pytest.fixture
def mv_game(tmp_path):
    root = tmp_path / "MVGame"
    data = root / "www" / "data"
    data.mkdir(parents=True)
    (root / "www" / "js").mkdir()
    (root / "www" / "js" / "rpg_core.js").write_text("//", encoding="utf-8")
    (root / "www" / "fonts").mkdir()
    (root / "www" / "fonts" / "gamefont.css").write_text(
        '@font-face { font-family: GameFont; src: url("mplus-1m-regular.ttf"); }', encoding="utf-8")
    (root / "www" / "index.html").write_text("<html><body><script></script></body></html>", encoding="utf-8")
    page = {"list": [
        {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]},
        {"code": 401, "indent": 0, "parameters": ["Hello there, traveler! This village has been"]},
        {"code": 401, "indent": 0, "parameters": ["quiet for a very long time."]},
        {"code": 102, "indent": 0, "parameters": [["Yes", "No"], 1, 0, 2, 0]},
        {"code": 402, "indent": 0, "parameters": [0, "Yes"]},
        {"code": 0, "indent": 1, "parameters": []},
        {"code": 0, "indent": 0, "parameters": []},
    ]}
    same_name = [{"id": i, "name": "Door", "pages": [json.loads(json.dumps(page))]} for i in (1, 2)]
    (data / "Map001.json").write_text(json.dumps({"events": [None] + same_name}), encoding="utf-8")
    (data / "Items.json").write_text(json.dumps([None, {"id": 1, "name": "Potion", "description": "Heals."}]),
                                     encoding="utf-8")
    (data / "System.json").write_text(json.dumps({"gameTitle": "My Game", "terms": {
        "basic": ["Level"], "commands": ["Fight"], "params": ["Max HP"], "messages": {"actionFailure": "Failed!"}}}),
        encoding="utf-8")
    return root


def test_mv_extract_inject_wrap_and_font(mv_game):
    from russificator.engines.rpgmaker.plugin import RPGMakerPlugin
    plugin = RPGMakerPlugin()
    assert plugin.detect(mv_game).confidence >= 0.9 and plugin.version == "mv"
    proj = _project(mv_game, "rpgmaker")
    res = plugin.extract(mv_game, proj)
    msgs = [e for e in res.entries if e.kind == TextKind.DIALOGUE]
    assert len(msgs) == 2 and len({e.id for e in msgs}) == 2
    assert msgs[0].source == "Hello there, traveler! This village has been quiet for a very long time."

    long_ru = ("Привет, путник! В этой деревне уже очень давно было тихо, и никто не приходил сюда "
               "с тех самых пор, как закрылась старая мельница.")
    _translate_all(proj, lambda s: long_ru if s.startswith("Hello") else "RU " + s)
    plugin.inject(mv_game, proj)
    plugin.install_font(mv_game, proj, None)

    data = json.loads((mv_game / "www/data/Map001.json").read_text(encoding="utf-8"))
    lst = data["events"][1]["pages"][0]["list"]
    lines = [c["parameters"][0] for c in lst if c["code"] == 401]
    from russificator.engines.rpgmaker import layout
    box = layout.DEFAULTS["mv"]
    assert " ".join(lines) == long_ru and len(lines) >= 2
    assert all(layout.text_width(ln, box) <= box.limit() for ln in lines)   # по пикселям, с запасом
    assert [c["code"] for c in lst][-4:] == [102, 402, 0, 0]          # ветвления на месте
    assert lst[len(lines) + 1]["parameters"][0] == ["RU Yes", "RU No"]
    sysj = json.loads((mv_game / "www/data/System.json").read_text(encoding="utf-8"))
    assert sysj["terms"]["commands"] == ["RU Fight"] and sysj["gameTitle"] == "My Game"
    css = (mv_game / "www/fonts/gamefont.css").read_text(encoding="utf-8")
    assert "unicode-range" in css and "PT_Sans" in css                  # кириллица из PT Sans
    assert "russificator-preload" in (mv_game / "www/index.html").read_text(encoding="utf-8")

    restore_backups(mv_game)
    assert "unicode-range" not in (mv_game / "www/fonts/gamefont.css").read_text(encoding="utf-8")
    assert not (mv_game / "www/fonts/PT_Sans-Web-Regular.ttf").exists()


def test_mv_wrap_by_pixels_and_codes():
    from russificator.engines.rpgmaker import layout
    box = layout.DEFAULTS["mv"]
    # коды цвета ширины не занимают, иконка — клетку, имя героя — как слово
    assert layout.text_width(r"\C[2]Слово\C[0]", box) == layout.text_width("Слово", box)
    assert layout.text_width(r"\I[64]", box) == box.icon
    wide = "Широкий шум жужжащих щёток, " * 8
    for face in (False, True):
        lines = layout.wrap(wide.strip(), box, face=face)
        assert all(layout.text_width(ln, box) <= box.limit(face) for ln in lines)
        assert " ".join(lines) == wide.strip()
    narrow = "ill " * 60
    assert len(layout.wrap(narrow.strip(), box)) < len(layout.wrap(wide.strip(), box))


# ============================= диспетчер =============================

def test_dispatcher(renpy_release, mv_game, ace_game, tmp_path):
    assert detect_engine(renpy_release)[0].engine_id == "renpy"
    assert detect_engine(mv_game)[0].engine_id == "rpgmaker"
    assert detect_engine(ace_game)[0].engine_id == "rpgmaker"
    empty = tmp_path / "Empty"
    empty.mkdir()
    with pytest.raises(EngineNotDetectedError):
        detect_engine(empty)


def test_rpgmaker_runtime_plugin_js():
    """Плагин переноса слов на моделях окон MV и MZ (node)."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node не установлен")
    script = Path(__file__).parent / "js" / "rpgmaker_plugin_test.js"
    res = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert res.returncode == 0 and "OK" in res.stdout, res.stderr


def test_mv_runtime_plugin_registered_last(mv_game):
    from russificator.core.backup import GameBackup
    from russificator.core.restore import restore_backups
    from russificator.engines.rpgmaker import layout, mv_mz
    plugins_js = mv_game / "www/js/plugins.js"
    plugins_js.write_text('// Generated by RPG Maker.\nvar $plugins =\n[\n'
                          '{"name":"YEP_MessageCore","status":true,"description":"","parameters":{"Default Rows":"4"}}'
                          '\n];\n', encoding="utf-8")
    original = plugins_js.read_text(encoding="utf-8")
    backup = GameBackup(mv_game)
    assert mv_mz.install_runtime(mv_game, backup)
    plugins = layout.plugin_list(plugins_js.read_text(encoding="utf-8"))
    assert [p["name"] for p in plugins] == ["YEP_MessageCore", "Russificator"]
    assert (mv_game / "www/js/plugins/Russificator.js").is_file()
    assert mv_mz.install_runtime(mv_game, backup)          # повторно — без дубликатов
    assert len(layout.plugin_list(plugins_js.read_text(encoding="utf-8"))) == 2
    restore_backups(mv_game)
    assert plugins_js.read_text(encoding="utf-8") == original
    assert not (mv_game / "www/js/plugins/Russificator.js").exists()



def test_mv_plugin_params_scripts_args_and_map_names(mv_game):
    from russificator.engines.rpgmaker import layout, mv_mz
    from russificator.engines.rpgmaker.plugin import RPGMakerPlugin
    base = mv_game / "www"
    struct = json.dumps({"Name": "Main Quest", "Help Text": "Find the lost sword.", "Icon": "87", "Switch": "12"})
    plugins = [
        {"name": "YEP_EquipCore", "status": True, "description": "",
         "parameters": {"Optimize Command": "Optimize", "Command Symbol": "optimize", "Help Eval": "this.refresh();",
                        "Text Color": "#ffffff", "Image File": "Window2", "Remove Text": "Remove all"}},
        {"name": "QuestLog", "status": True, "description": "",
         "parameters": {"Quests": json.dumps([struct])}},
        {"name": "Disabled", "status": False, "description": "", "parameters": {"Title Text": "Old title"}},
    ]
    (base / "js" / "plugins.js").write_text(
        "// Generated by RPG Maker.\nvar $plugins =\n[\n" + ",\n".join(json.dumps(x) for x in plugins) + "\n];\n",
        encoding="utf-8")
    mapf = base / "data" / "Map001.json"
    data = json.loads(mapf.read_text(encoding="utf-8"))
    data["displayName"] = "Old Mill"
    data["events"][1]["pages"][0]["list"][0:0] = [
        {"code": 355, "indent": 0, "parameters": ['$gameMessage.add("The gate is sealed.");']},
        {"code": 357, "indent": 0, "parameters": ["Popup", "show", "Show", {"text": "Door unlocked!", "se": "Open1"}]},
    ]
    mapf.write_text(json.dumps(data), encoding="utf-8")

    plugin = RPGMakerPlugin()
    plugin.detect(mv_game)
    proj = _project(mv_game, "rpgmaker")
    res = plugin.extract(mv_game, proj)
    sources = {e.source for e in res.entries}
    assert {"Optimize", "Remove all", "Main Quest", "Find the lost sword.", "The gate is sealed.",
            "Door unlocked!", "Old Mill"} <= sources
    # служебные значения не трогаем: символы, код, цвета, файлы, выключенные плагины, номера и переключатели
    assert not sources & {"optimize", "this.refresh();", "#ffffff", "Window2", "Old title", "87", "12", "Open1"}

    _translate_all(proj, lambda src: "RU " + src)
    plugin.inject(mv_game, proj)
    new = layout.plugin_list((base / "js" / "plugins.js").read_text(encoding="utf-8"))
    assert new[0]["parameters"]["Optimize Command"] == "RU Optimize"
    assert new[0]["parameters"]["Command Symbol"] == "optimize"
    quest = json.loads(json.loads(new[1]["parameters"]["Quests"])[0])
    assert quest == {"Name": "RU Main Quest", "Help Text": "RU Find the lost sword.", "Icon": "87", "Switch": "12"}
    assert new[-1]["name"] == mv_mz.RUNTIME_PLUGIN                     # наш плагин — последним
    data = json.loads(mapf.read_text(encoding="utf-8"))
    assert data["displayName"] == "RU Old Mill"
    lst = data["events"][1]["pages"][0]["list"]
    assert lst[0]["parameters"][0] == '$gameMessage.add("RU The gate is sealed.");'
    assert lst[1]["parameters"][3] == {"text": "RU Door unlocked!", "se": "Open1"}


def test_js_string_escape_roundtrip():
    from russificator.engines.rpgmaker.mv_mz import _GM_ADD_RE, _js_escape, _js_unescape
    for q in ("'", '"'):
        text = 'Он сказал: "Стой!"\nИ ушёл \\ прочь.'
        code = f"$gameMessage.add({q}{_js_escape(text, q)}{q});"
        m = _GM_ADD_RE.search(code)
        assert m and _js_unescape(m.group(2)) == text



def test_vxace_scripts_vocab_and_commands(tmp_path):
    """Надписи из Ruby-скриптов (Vocab, add_command, draw_text) — перевод с сохранением синтаксиса."""
    import zlib
    from russificator.engines.rpgmaker import rgss_scripts
    from russificator.engines.rpgmaker import ruby_marshal as M
    from russificator.engines.rpgmaker.plugin import RPGMakerPlugin

    def script(sid, name, code):
        packed = zlib.compress(code if isinstance(code, bytes) else code.encode("utf-8"))
        return [sid, M.RubyString(name, [("E", True)]), M.RubyString(packed.decode("utf-8", "surrogateescape"))]

    vocab = (b'module Vocab\n  Emerge = "%s emerges!"\n  ShopBuy = "Buy"\n  Fmt = "#{x} wins"\nend\n'
             b'# \x82\xb1\x82\xea\n')              # байты Shift-JIS в комментарии японской игры
    quest = ('class Window_Quest < Window_Command\n  WINDOWSKIN = "Window"\n'
             '  def make_command_list\n    add_command("Quests", :quest)\n  end\n'
             '  def refresh\n    draw_text(4, 0, 120, 32, "Gold")\n    Audio.se_play("Audio/SE/Cursor")\n  end\nend\n')
    root = tmp_path / "AceScripts"
    (root / "Data").mkdir(parents=True)
    (root / "Game.exe").write_bytes(b"MZ")
    (root / "Game.ini").write_text("[Game]\nLibrary=System\\RGSS301.dll\n", encoding="utf-8")
    scripts = [script(1, "Vocab", vocab), script(2, "Window_Quest", quest), script(3, "Main", "rgss_main { }\n")]
    (root / "Data" / "Scripts.rvdata2").write_bytes(M.dump(scripts, utf8_strings=True))
    (root / "Data" / "System.rvdata2").write_bytes(M.dump(M.RubyObject("RPG::System", {}), utf8_strings=True))

    plugin = RPGMakerPlugin()
    plugin.detect(root)
    assert plugin.version == "ace"
    proj = _project(root, "rpgmaker")
    res = plugin.extract(root, proj)
    found = {e.source for e in res.entries}
    assert {"%s emerges!", "Buy", "Quests", "Gold"} <= found
    assert not found & {"Window", "Audio/SE/Cursor", "#{x} wins"}

    _translate_all(proj, lambda src: 'RU "' + src + '" #1')
    plugin.inject(root, proj)
    new, _ = rgss_scripts.load((root / "Data" / "Scripts.rvdata2").read_bytes())
    code = rgss_scripts.code_of(new[0])
    assert 'Emerge = "RU \\"%s emerges!\\" \\#1"' in code and 'Fmt = "#{x} wins"' in code
    assert code.encode("utf-8", "surrogateescape").endswith(b"# \x82\xb1\x82\xea\n")   # чужие байты целы
    code2 = rgss_scripts.code_of(new[1])
    assert 'add_command("RU \\"Quests\\" \\#1", :quest)' in code2 and 'WINDOWSKIN = "Window"' in code2
    assert 'draw_text(4, 0, 120, 32, "RU \\"Gold\\" \\#1")' in code2
    assert [str(x[1]) for x in new] == ["Vocab", "Window_Quest", "Main"]
    # значения литералов после перевода читаются обратно ровно в перевод
    lit = next(x for x in rgss_scripts.literals(code, "Vocab") if x.value.startswith("RU \"%s"))
    assert lit.value == 'RU "%s emerges!" #1'


def test_ace_description_fits_help_window():
    from russificator.engines.rpgmaker import layout
    from russificator.engines.rpgmaker.old_marshal import fit_description, _HELP_ACE
    assert fit_description("Лечит 50 HP.") == "Лечит 50 HP."
    two = fit_description("Восстанавливает здоровье одного союзника на пятьдесят единиц и снимает отравление.")
    assert two.count("\n") == 1 and all(layout.text_width(x, _HELP_ACE) <= _HELP_ACE.limit() for x in two.split("\n"))
    small = fit_description("Древний клинок, выкованный мастерами исчезнувшего королевства; наносит двойной урон "
                            "нежити и светится в темноте, освещая путь.")
    assert small.startswith("\\}") and small.count("\n") == 1


def test_rgssad_xor_without_numpy_matches_reference():
    """Установщик собирается без numpy: быстрый XOR на длинной арифметике = эталонный побайтовый."""
    import os
    from russificator.engines.rpgmaker.rgssad import _decrypt_data, _xor_bigint
    for n in (0, 1, 5, 4096, (1 << 18) + 3, 700_001):
        data = os.urandom(n)
        assert _xor_bigint(data, 0xDEADCAFE ^ n) == _decrypt_data(data, 0xDEADCAFE ^ n)


def test_rgss_credit_script_never_breaks_game(tmp_path):
    """Надпись на титульном экране XP/VX/Ace: свой титульный экран в игре — надписи нет, но и ошибки нет."""
    import shutil
    import subprocess
    if shutil.which("ruby") is None:
        pytest.skip("нет ruby")
    from russificator import branding
    from russificator.engines.rpgmaker.plugin import credit_script
    stubs = """
class Color; def initialize(*a); end; end
class Font; attr_accessor :size, :color; end
class Bitmap; attr_reader :font; def initialize(w, h); @font = Font.new; end
  def draw_text(*a); $drawn = a[4]; end; def dispose; $disposed = true; end; end
class Sprite; attr_accessor :bitmap, :y, :z; def dispose; end; end
module Graphics; def self.width; 544; end; def self.height; 416; end; end
"""
    cases = [
        ("ace", "class Scene_Base; def start; end; def terminate; end; end\n"
                "class Scene_Title < Scene_Base; def start; end; end\n", "t.start; t.terminate", True),
        ("xp", "class Scene_Title; def main; end; end\n", "t.main", True),
        ("ace", "", "", False),                       # Scene_Title нет вовсе
        ("xp", "", "", False),
        ("ace", "class Scene_Title; end\n", "", False),  # свой титульный экран без start/terminate
    ]
    for version, pre, run, drawn in cases:
        code = (stubs + pre + credit_script(version, branding.CREDIT)
                + (f"\nt = Scene_Title.new\n{run}\n" if run else "\n")
                + "puts($drawn ? 'drawn' : 'none')\n")
        script = tmp_path / "credit.rb"                   # файлом, а не -e: кириллица цела и в Windows
        script.write_text(code, encoding="utf-8")
        r = subprocess.run(["ruby", str(script)], capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ("drawn" if drawn else "none")


def test_renpy_credit_block_is_valid_and_works_on_old_renpy():
    """Надпись о программе: экран с экранированным текстом и код init, который компилируется;
    в Ren'Py без always_shown_screens экран показывается через start_interact_callbacks."""
    from russificator.engines.renpy.plugin import credit_block
    block = credit_block('Текст "в кавычках" [var] {tag}', "PT.ttf")
    assert 'text "Текст \\"в кавычках\\" [[var] {{tag}"' in block
    code = block.split("init 999 python:", 1)[1].replace("\n    ", "\n")
    compile(code, "credit", "exec")
    calls = []

    class Cfg:
        start_interact_callbacks: list = []

    class RenPy:
        @staticmethod
        def get_screen(name):
            return None

        @staticmethod
        def show_screen(name):
            calls.append(name)
    ns = {"config": Cfg, "renpy": RenPy, "main_menu": True}
    exec(code, ns)
    assert Cfg.start_interact_callbacks and not calls
    Cfg.start_interact_callbacks[0]()
    assert calls == ["russificator_credit"]


def test_mv_font_gets_own_cyrillic(mv_game):
    """В шрифт игры дописываются русские буквы по его меркам — копия подключается для кириллицы."""
    from russificator.engines.rpgmaker.plugin import RPGMakerPlugin
    from russificator.fonts import sfnt
    from tests.test_fonts import _transformed
    (mv_game / "www/fonts/mplus-1m-regular.ttf").write_bytes(_transformed(0.85, 0.9))
    plugin = RPGMakerPlugin()
    plugin.detect(mv_game)
    proj = _project(mv_game, "rpgmaker")
    plugin.extract(mv_game, proj)
    _translate_all(proj)
    plugin.inject(mv_game, proj)
    plugin.install_font(mv_game, proj, None)
    css = (mv_game / "www/fonts/gamefont.css").read_text(encoding="utf-8")
    assert "mplus-1m-regular-ru.ttf" in css and "PT_Sans" not in css
    font = sfnt.Font((mv_game / "www/fonts/mplus-1m-regular-ru.ttf").read_bytes())
    assert font.has("Ж") and font.has("ё")
    restore_backups(mv_game)
    assert not (mv_game / "www/fonts/mplus-1m-regular-ru.ttf").exists()


def test_renpy_font_gets_own_cyrillic(tmp_path):
    from russificator.engines.renpy.plugin import HOOK_FILE, RenPyPlugin
    from tests.test_fonts import _transformed
    root = tmp_path / "MyGame"
    (root / "game").mkdir(parents=True)
    (root / "renpy").mkdir()
    write_rpa(root / "game" / "archive.rpa", {
        "script.rpyc": (DATA / "renpy813_script.rpyc").read_bytes(),
        "fonts/LatinOnly.ttf": _transformed(0.9, 1.0),
    })
    plugin = RenPyPlugin()
    plugin.detect(root)
    proj = _project(root, "renpy")
    plugin.extract(root, proj)
    _translate_all(proj)
    plugin.inject(root, proj)
    hook = (root / "game" / HOOK_FILE).read_text(encoding="utf-8")
    assert "'russificator/LatinOnly-ru.ttf'" in hook
    assert (root / "game" / "russificator" / "LatinOnly-ru.ttf").is_file()
    assert not (root / "game" / "russificator" / "PT_Sans-Web-Regular.ttf").exists()
    restore_backups(root)


def test_mv_scroll_text_keeps_its_layout():
    """Бегущая строка (титры, вступление): абзацы переводятся по отдельности, строки титров не
    склеиваются в кашу, пустые строки на месте, непереведённый абзац (имена) — как был."""
    from russificator.engines.rpgmaker import layout, mv_mz
    lines = ["Five Nights", "( Hold To Speed Up )", "", "", "Programs used :", "",
             "Blender --- Render", "Photoshop --- Edit", "", "Yukari Moscow"]
    cmds = [{"code": 105, "indent": 0, "parameters": [2, False]}] + \
        [{"code": 405, "indent": 0, "parameters": [ln]} for ln in lines] + [{"code": 0, "parameters": []}]
    found: list = []
    mv_mz._event_list(cmds, "Map001.json|ev1|p0", found)
    texts = [e.source for e in found]
    assert texts == ["Five Nights\n( Hold To Speed Up )", "Programs used :",
                     "Blender --- Render\nPhotoshop --- Edit", "Yukari Moscow"]
    tr = {"Five Nights\n( Hold To Speed Up )": "Пять ночей\n( Зажмите, чтобы ускорить )",
          "Programs used :": "Программы:", "Blender --- Render\nPhotoshop --- Edit": "Blender — рендер\nPhotoshop — правка"}
    keys = {}
    for e in found:
        if e.source in tr:
            e.translation = tr[e.source]
            keys[e.target_key] = e
    out, n = mv_mz._apply_list(cmds, "Map001.json|ev1|p0", keys, layout.DEFAULTS["mv"])
    got = [c["parameters"][0] for c in out if c.get("code") == 405]
    assert n == 3
    assert got == ["Пять ночей", "( Зажмите, чтобы ускорить )", "", "", "Программы:", "",
                   "Blender — рендер", "Photoshop — правка", "", "Yukari Moscow"]


def test_mv_message_keeps_list_breaks():
    from russificator.engines.rpgmaker.mv_mz import join_lines
    assert join_lines(["Hello there, how are", "you today?"]) == "Hello there, how are you today?"
    assert join_lines(["Pick one:", "- Attack", "- Run"]) == "Pick one:\n- Attack\n- Run"


@pytest.mark.parametrize("key,value,text", [
    ("Menu Order", "Quit: toTitle, Save: save", False),     # порядок меню: плагин делает split(": ")
    ("Show Gold Window", "no", False),                      # флаг: плагин сравнивает == "yes"
    ("Text Alignment", "left", False),
    ("Island Name", "Coral Bay", True),                     # «is» в начале слова — не флаг «Is …»
    ("Name", "Quit", True),
    ("Yes Text", "Yes", True),                              # явная надпись кнопки
    ("Help Text", "Press Z to talk", True),
])
def test_plugin_params_settings_are_not_text(key, value, text):
    from russificator.engines.rpgmaker.plugin_text import is_text
    assert is_text(key, value) is text
