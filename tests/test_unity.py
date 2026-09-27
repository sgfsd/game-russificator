"""Тесты Unity: определение, строки из кода, XUnity, дорисовка кириллицы в шрифты."""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from russificator.engines.unity import detect, extract, tmp_cyrillic, xunity


def _fake_exe(path: Path, machine: int = 0x8664) -> None:
    data = bytearray(512)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, machine)
    path.write_bytes(bytes(data))


def _fake_game(tmp_path: Path, il2cpp: bool = False) -> Path:
    root = tmp_path / "MyGame"
    data = root / "MyGame_Data"
    data.mkdir(parents=True)
    _fake_exe(root / "MyGame.exe")
    (data / "globalgamemanagers").write_bytes(b"\0" * 20 + b"2021.3.16f1\0" + b"\0" * 64)
    if il2cpp:
        (root / "GameAssembly.dll").write_bytes(b"MZ")
        (data / "il2cpp_data" / "Metadata").mkdir(parents=True)
    else:
        (data / "Managed").mkdir()
        (data / "Managed" / "Unity.TextMeshPro.dll").write_bytes(b"MZ")
    return root


# ---------- определение ----------

def test_inspect_mono(tmp_path):
    g = detect.inspect(_fake_game(tmp_path))
    assert g.backend == "mono" and g.arch == "x64" and g.version == "2021.3.16f1" and g.uses_tmp
    assert g.major == 2021


def test_inspect_il2cpp_x86(tmp_path):
    root = _fake_game(tmp_path, il2cpp=True)
    _fake_exe(root / "MyGame.exe", machine=0x14C)
    g = detect.inspect(root)
    assert g.backend == "il2cpp" and g.arch == "x86"


def test_detect_not_unity(tmp_path):
    assert detect.detect(tmp_path)[0] == 0.0


# ---------- строки из кода ----------

def _metadata(strings, version=29) -> bytes:
    data = b"".join(s.encode("utf-8") for s in strings)
    table = b""
    pos = 0
    for s in strings:
        n = len(s.encode("utf-8"))
        table += struct.pack("<Ii", n, pos)
        pos += n
    header_size = 8 + 4 * 4
    sl_off = header_size
    sld_off = sl_off + len(table)
    head = struct.pack("<Ii", 0xFAB11BAF, version) + struct.pack("<iiii", sl_off, len(table), sld_off, len(data))
    return head + table + data


def test_il2cpp_literals_real_layout(tmp_path):
    meta = tmp_path / "global-metadata.dat"
    meta.write_bytes(_metadata(["Hello there!", "Привет", "PlayerPrefsKey"]))
    assert extract.il2cpp_literals(meta) == ["Hello there!", "Привет", "PlayerPrefsKey"]


def test_il2cpp_literals_encrypted_is_empty(tmp_path):
    meta = tmp_path / "global-metadata.dat"
    meta.write_bytes(b"\x13\x37" * 100)
    assert extract.il2cpp_literals(meta) == []


def test_dotnet_user_strings_from_real_assembly():
    """Настоящая .NET-сборка: наш плагин для Unity (или Python.Runtime.dll из pythonnet)."""
    import sys
    plugin = Path(__file__).resolve().parents[1] / "russificator" / "resources" / "unity" / "Russificator.Unity.dll"
    candidates = [plugin] if plugin.is_file() else list(Path(sys.prefix).rglob("Python.Runtime.dll"))
    if not candidates:
        pytest.skip("нет .NET-сборки для проверки")
    strings = extract.dotnet_user_strings(candidates[0])
    assert len(strings) > 10 and all(isinstance(s, str) for s in strings)


def test_tech_strings_filtered():
    col = extract._Collector()
    for s in ["Hidden/Colorful/Vignette", "UnityEngine.UI.Text, UnityEngine.UI, Version=1.0.0.0",
              "yyyy-MM-dd", "You found the key!"]:
        col.add(s, extract.TextKind.OTHER, "code")
    assert list(col.items) == ["You found the key!"]


# ---------- XUnity ----------

def _xua_decode_line(line: str):
    """Порт TextHelper.ReadTranslationLineAndDecode из XUnity 5.6 (как плагин читает файлы)."""
    parts, buf, esc, i = [], [], False, 0
    while i < len(line):
        c = line[i]
        if esc:
            if c in "=\\":
                buf.append(c)
            elif c == "n":
                buf.append("\n")
            elif c == "r":
                buf.append("\r")
            elif c == "u":
                buf.append(chr(int(line[i + 1:i + 5], 16)))
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


@pytest.mark.parametrize("src,dst", [
    ("a=b", "а=б"), ("line1\nline2\r", "строка"), ("C:\\path", "путь"), ("http://x.com/a//b", "ссылка //"),
    ("100%3D", "100%3D"), ("///", "\\u"), ("<color=#f00>Hi</color>", "<color=#f00>Привет</color>"),
])
def test_xunity_encode_roundtrip(src, dst):
    line = f"{xunity.encode(src)}={xunity.encode(dst)}"
    assert _xua_decode_line(line) == [src, dst]


def test_raw_strings_finds_serialized_text():
    def s(t):
        raw = t.encode("utf-8")
        return struct.pack("<i", len(raw)) + raw + b"\0" * ((4 - len(raw) % 4) % 4)
    head = b"\0" * 28 + s("ChatHolder")               # заголовок MonoBehaviour + m_Name
    data = head + struct.pack("<i", 3) + s("Hey") + s("wtf is this?!?") + struct.pack("<f", 1.5) + s("lol")
    found = [t for _, t in extract.raw_strings(data)]
    assert found == ["ChatHolder", "Hey", "wtf is this?!?", "lol"]

    class Obj:
        def get_raw_data(self):
            return data
    col = extract._Collector()
    extract._from_raw(Obj(), col, "level3")
    # имя объекта (m_Name) не берём; одиночное слово строчными («lol») неотличимо от идентификатора
    assert set(col.items) == {"Hey", "wtf is this?!?"}


def test_label_fields_accept_short_names():
    col = extract._Collector()
    extract._from_monobehaviour({"productName": "Motion Sensor", "productID": "MotionSensor",
                                 "internalKey": "Motion Sensor"}, col, "sharedassets3")
    assert list(col.items) == ["Motion Sensor"]
    assert not extract._ui_word("MotionSensor")


def test_resize_rules_for_translated_elements():
    ui = [{"text": "CONTINUE", "path": "MainCanvas/MainMenuHolder/ContinueBTN/TitleText", "size": 60.0, "scene": True},
          {"text": "YES", "path": "MainCanvas/AreYouSure/YesBTN/TitleText", "size": 48.0, "scene": True},
          {"text": "Play", "path": "MenuPrefab/Play/Label", "size": 20.0, "scene": False}]
    rules = xunity.resize_rules(ui, {"CONTINUE": "ПРОДОЛЖИТЬ", "YES": "ДА", "Play": "Play"})
    # ширину русского текста заранее не угадать: правило для каждого переведённого элемента,
    # шрифт не крупнее исходного и уменьшается только когда текст не помещается
    assert rules == ["MainCanvas/MainMenuHolder/ContinueBTN/TitleText=AutoResize(true,36,60)",
                     "MainCanvas/AreYouSure/YesBTN/TitleText=AutoResize(true,29,48)"]
    prefab = xunity.resize_rules(ui[2:], {"Play": "Играть"})
    assert prefab == ["MenuPrefab/Play/Label=AutoResize(true,12,20)",
                      "MenuPrefab(Clone)/Play/Label=AutoResize(true,12,20)"]


def test_partial_translations_budget():
    assert xunity.partial_translations_fit(["Hello there"] * 1000)
    assert not xunity.partial_translations_fit(["x" * 2500] * 10)


def test_config_has_partial_and_limits(tmp_path):
    from russificator.core.backup import GameBackup
    root = _fake_game(tmp_path)
    game = detect.inspect(root)
    xunity.write_config(game, GameBackup(root), live=True, tmp_font=None, partial=True)
    ini = (root / "BepInEx" / "config" / "AutoTranslatorConfig.ini").read_text(encoding="utf-8")
    assert "GeneratePartialTranslations=True" in ini
    assert "MaxCharactersPerTranslation=2500" in ini


def test_xunity_install_and_restore(tmp_path, monkeypatch):
    from russificator.core.backup import GameBackup
    from russificator.core.restore import restore_backups

    root = _fake_game(tmp_path)
    game = detect.inspect(root)
    bep = tmp_path / "bep.zip"
    with zipfile.ZipFile(bep, "w") as z:
        z.writestr("winhttp.dll", b"x")
        z.writestr("doorstop_config.ini", b"x")
        z.writestr("BepInEx/core/BepInEx.dll", b"x")
    xua = tmp_path / "xua.zip"
    with zipfile.ZipFile(xua, "w") as z:
        z.writestr("BepInEx/core/XUnity.Common.dll", b"x")
        z.writestr("BepInEx/plugins/XUnity.AutoTranslator/XUnity.AutoTranslator.Plugin.Core.dll", b"x")
    monkeypatch.setattr(xunity, "_cached", lambda url, status, cancel: bep if "BepInEx_win" in url else xua)

    backup = GameBackup(root)
    xunity.install(game, backup)
    xunity.write_config(game, backup, live=True, tmp_font=None)
    n = xunity.write_translations(game, backup, {"Hello=world": "Привет=мир", "Same": "Same", "x//y": "z"})
    assert n == 2   # «Same» пропущена; «x//y» записана с экранированием «//»
    text = (root / xunity.TRANSLATION_FILE).read_text(encoding="utf-8")
    assert "Hello\\=world=Привет\\=мир" in text
    ini = (root / "BepInEx/config/AutoTranslatorConfig.ini").read_text(encoding="utf-8")
    assert "Endpoint=CustomTranslate" in ini and "Language=ru" in ini and f"Url={xunity.LIVE_URL}" in ini
    assert xunity.is_installed(root)
    assert (root / "BepInEx" / "plugins" / xunity.PLUGIN).is_file()   # Mono: плагин русификатора
    xunity.write_plugin_config(game, backup, ("C:/R/Russificator.exe", '--serve "C:/G" --pid {pid}', "C:/R"))
    cfg = (root / xunity.PLUGIN_CONFIG).read_text(encoding="utf-8")
    assert "server=C:/R/Russificator.exe" in cfg and "{pid}" in cfg and "port=47631" in cfg

    # XUnity во время игры создаёт свои файлы — откат должен убрать и их
    (root / "BepInEx/Translation/ru/Text/_AutoGeneratedTranslations.txt").write_text("x", encoding="utf-8")
    (root / "BepInEx/LogOutput.log").write_text("x", encoding="utf-8")
    restore_backups(root)
    assert sorted(p.name for p in root.iterdir()) == ["MyGame.exe", "MyGame_Data"]


def test_tmp_font_bundle_by_version(tmp_path):
    game = detect.inspect(_fake_game(tmp_path))
    assert xunity.tmp_font_bundle(game) == "arialuni_sdf_u2019"
    game.version = "2018.4.1f1"
    assert xunity.tmp_font_bundle(game) == "arialuni_sdf_u2018"
    game.version = "2017.3.0f3"
    assert xunity.tmp_font_bundle(game) is None


# ---------- шрифты ----------

def test_cmap_reads_embedded_font():
    from russificator.fonts.cmap import codepoints, has_cyrillic
    from russificator.fonts.library import ensure_font
    font = ensure_font()
    assert has_cyrillic(font)
    assert ord("A") in codepoints(font)
    assert codepoints(b"not a font") == set()


def test_sdf_edge_is_half():
    import numpy as np
    from russificator.fonts.library import ensure_font
    g = tmp_cyrillic.GlyphRenderer(ensure_font()).render("Н", 48, 5, 6.0)
    assert g.bitmap.shape == (int(np.ceil(g.height)) + 10, int(np.ceil(g.width)) + 10)
    row = g.bitmap[g.bitmap.shape[0] // 2]
    # снаружи ноль; в середине штриха шириной ~5 px значение 0.5 + 2.5/(2*6) ≈ 0.7
    assert row[0] == 0.0 and 0.6 < row.max() < 0.8
    edge = np.argmax(row > 0.5)
    assert 0.3 < row[edge - 1] <= 0.5 < row[edge] < 0.75  # плавный переход через 0.5 на контуре


class _FakeTexture:
    def __init__(self, w, h):
        from PIL import Image
        self.image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        self.m_TextureFormat = 1

    def set_image(self, img, target_format=None):
        self.image = img


def _tmp2_tree(atlas=256):
    glyphs, chars = [], []
    for i, c in enumerate("HABCDEFGIKLMNOPRSTUXYZabcdeghknopstuxyz"):
        glyphs.append({"m_Index": i + 1, "m_Metrics": {"m_Width": 20.0, "m_Height": 30.0, "m_HorizontalBearingX": 1.0,
                                                       "m_HorizontalBearingY": 30.0, "m_HorizontalAdvance": 22.0},
                       "m_GlyphRect": {"m_X": (i % 8) * 30, "m_Y": atlas - 40 - (i // 8) * 40,
                                       "m_Width": 20, "m_Height": 30},
                       "m_Scale": 1.0, "m_AtlasIndex": 0})
        chars.append({"m_ElementType": 1, "m_Unicode": ord(c), "m_GlyphIndex": i + 1, "m_Scale": 1.0})
    return {"m_Name": "Game SDF", "m_GlyphTable": glyphs, "m_CharacterTable": chars, "m_AtlasWidth": atlas,
            "m_AtlasHeight": atlas, "m_AtlasPadding": 5, "m_AtlasPopulationMode": 0, "m_AtlasRenderMode": 4165,
            "m_UsedGlyphRects": [], "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 7}]}


def test_patch_tmp2_font_adds_cyrillic_and_grows_atlas():
    from russificator.fonts.library import ensure_font
    tree = _tmp2_tree()
    font = tmp_cyrillic.TmpFont(tree)
    tex = _FakeTexture(256, 256)
    mat = {"m_SavedProperties": {"m_Floats": [["_GradientScale", 6.0], ["_TextureHeight", 256.0]]}}
    rend = tmp_cyrillic.GlyphRenderer(ensure_font())
    added = tmp_cyrillic.patch_font(font, tex, [(None, mat)], rend, rend, set(tmp_cyrillic.RUSSIAN))
    assert added == len(tmp_cyrillic.RUSSIAN)
    assert all(ord(c) in font.chars() for c in tmp_cyrillic.RUSSIAN)
    w, h = font.atlas_size()
    assert tex.image.size == (w, h) and h > 256
    # старые глифы не сдвинулись (координаты снизу вверх), новые — внутри атласа
    assert tree["m_GlyphTable"][0]["m_GlyphRect"]["m_Y"] == 256 - 40
    for g in tree["m_GlyphTable"][-66:]:
        r = g["m_GlyphRect"]
        assert 0 <= r["m_X"] and r["m_X"] + r["m_Width"] <= w and 0 <= r["m_Y"] and r["m_Y"] + r["m_Height"] <= h
    assert mat["m_SavedProperties"]["m_Floats"][1] == ["_TextureHeight", float(h)]
    # второй проход ничего не добавляет
    assert tmp_cyrillic.patch_font(font, tex, [(None, mat)], rend, rend, set(tmp_cyrillic.RUSSIAN)) == 0


def test_needed_chars_includes_translation_symbols():
    need = tmp_cyrillic.needed_chars(["«Ёлки» — №5 ✓"])
    assert {"«", "»", "—", "№", "✓", "Ё"} <= need


# ---------- встроенный браузер ZFBrowser ----------

_PAGE = ("<html><head><title>Deep Web News</title><script>var s = 'Hello there';</script></head>"
         "<body><p>Hello <b>world</b> &amp; friends</p><!-- key: abc -->"
         "<div>\n  <a href='next.html'>Next page</a>\n</div></body></html>")


def test_zfbrowser_units_keep_inline_tags_and_skip_code():
    from russificator.engines.unity import zfbrowser
    assert [u.source for u in zfbrowser.units(_PAGE)] == [
        "Deep Web News", "Hello <b>world</b> & friends", "<a href='next.html'>Next page</a>"]


def test_zfbrowser_pages_extract_inject_restore(tmp_path):
    from russificator.core.backup import GameBackup
    from russificator.core.restore import restore_backups
    from russificator.engines.unity import plugin as uplugin
    from russificator.engines.unity import zfbrowser

    root = _fake_game(tmp_path)
    game = detect.inspect(root)
    res = game.data / "Resources"
    res.mkdir()
    pack = res / "browser_assets"
    original = zfbrowser.write_pack("zfbRes_v1", [("site/index.html", _PAGE.encode("utf-8")),
                                                  ("site/logo.png", b"\x89PNG\r\n")])
    pack.write_bytes(original)
    assert zfbrowser.find_packs(game.data) == [pack]

    col = extract._Collector()
    extract.extract_browser_pages(game, col)
    assert [e.source for e in col.pages] == ["Deep Web News", "Hello <b>world</b> & friends",
                                             "<a href='next.html'>Next page</a>"]
    ru = {"Deep Web News": "Новости даркнета", "Hello <b>world</b> & friends": "Привет, <b>мир</b> и друзья",
          "<a href='next.html'>Next page</a>": "<a href='next.html'>Дальше</a>"}
    for e in col.pages:
        e.translation = ru[e.source]

    backup = GameBackup(root)
    assert uplugin.inject_pages(game, backup, col.pages) == 1
    header, files = zfbrowser.read_pack(pack)
    page = files[0][1].decode("utf-8")
    assert files[1] == ("site/logo.png", b"\x89PNG\r\n")
    assert "var s = 'Hello there';" in page and "<!-- key: abc -->" in page and "href='next.html'" in page
    import html as _html
    assert "Привет, <b>мир</b> и друзья" in _html.unescape(page) and "&#1055;" in page
    restore_backups(root)
    assert pack.read_bytes() == original


# ---------- IL: как игра печатает текст ----------

def test_il_scan_finds_append_and_length_reveal():
    from russificator.engines.unity import il_scan
    usage = il_scan.text_usage(Path(__file__).parent / "data" / "il_typer.dll")   # исходник: il_typer.cs
    assert usage.appends == 1 and usage.length_reveals == 1


def test_il_scan_survives_non_dotnet(tmp_path):
    from russificator.engines.unity import il_scan
    junk = tmp_path / "junk.dll"
    junk.write_bytes(b"MZ" + b"\0" * 200)
    assert il_scan.text_usage(junk) == il_scan.TextUsage()


def test_prefix_rules_split_label_and_text():
    rules = xunity.prefix_rules(["Pro Tip: ", "Level (x): "], {"Pro Tip: ": "Совет", "Level (x): ": "Уровень $ (x):"})
    assert rules == [r'sr:"^Pro Tip: ([\S\s]+)$"=Совет: $1',
                     r'sr:"^Level \(x\): ([\S\s]+)$"=Уровень $$ (x): $1']


def test_technical_text_is_not_translated():
    from russificator.translation.filters import looks_technical
    assert looks_technical("systemd[1]: Starting Session 2334 of user root.")
    assert looks_technical("CMD ([ -x /opt/psa/admin/sbin/backupmng ] && run >/dev/null 2>&1)")
    assert not looks_technical("Check the Notes app and/or the chat.")
    col = extract._Collector()
    col.add("CRON[6043]: (root) CMD (backup)", extract.TextKind.OTHER, "level3")
    assert not col.items



# ---------- текстовые ассеты (диалоги в JSON/CSV/XML/Ink/Yarn) ----------

def _asset_texts(text):
    from russificator.engines.unity import text_assets
    out = []
    text_assets.extract(text, out.append)
    return out


def test_text_assets_ink_and_json():
    ink = r'{"inkVersion": 21, "root": [["^Hello there, stranger.", "\n", "^", "ev", "done"]]}'
    assert _asset_texts(ink) == ["Hello there, stranger."]
    data = '[{"id": "a1", "text": "The door is locked."}, {"id": "b2", "title": "Main Menu", "icon": "ico_main"}]'
    assert _asset_texts(data) == ["The door is locked.", "Main Menu"]


def test_text_assets_csv_takes_english_column():
    table = "key,English,French\nq1,Where are you going?,Ou vas-tu ce soir?\nq2,Open,Ouvrir\n"
    assert _asset_texts(table) == ["Where are you going?", "Open"]


def test_text_assets_xml_yarn_and_html():
    xml = """<lines><line speaker="Mae" text="I can't go back now."/><l>Welcome home, dear.</l></lines>"""
    assert sorted(_asset_texts(xml)) == ["I can't go back now.", "Welcome home, dear."]
    yarn = "title: Start\n---\nMae: I missed this town so much.\n<<jump Next>>\n==="
    assert _asset_texts(yarn) == ["I missed this town so much."]
    assert _asset_texts("<!DOCTYPE html><html><body>Hi there, how are you?</body></html>") == []


def test_text_assets_decode_rejects_binary():
    from russificator.engines.unity import text_assets
    assert text_assets.decode(b"\x00\x01\x02" * 100) is None
    assert text_assets.decode("\ufeffHello".encode("utf-8")) == "Hello"
    assert text_assets.decode("Hi there".encode("utf-16")) == "Hi there"


def test_plugin_matches_bepinex(tmp_path):
    """Mono + BepInEx 5 — Russificator.Unity.dll, IL2CPP + BepInEx 6 — Russificator.Unity.IL2CPP.dll."""
    from russificator.core.backup import GameBackup
    from russificator.engines.unity import xunity
    from russificator.engines.unity.detect import UnityGame
    for backend, core_dll, expected in (("mono", "BepInEx.dll", xunity.PLUGIN),
                                        ("il2cpp", "BepInEx.Unity.IL2CPP.dll", xunity.PLUGIN_IL2CPP)):
        root = tmp_path / backend
        (root / "BepInEx" / "core").mkdir(parents=True)
        game = UnityGame(root=root, data=root / "G_Data", exe=None, backend=backend, arch="x64", version="", uses_tmp=True)
        assert xunity.plugin_name(game) is None or (root / "BepInEx" / "core" / core_dll).exists()
        (root / "BepInEx" / "core" / core_dll).write_bytes(b"x")
        assert xunity.plugin_name(game) == expected
        assert xunity.install_plugin(game, GameBackup(root))
        assert (root / "BepInEx" / "plugins" / expected).is_file() and xunity.plugin_installed(game)
    other = UnityGame(root=tmp_path / "none", data=tmp_path, exe=None, backend="il2cpp", arch="x64", version="",
                      uses_tmp=False)
    assert xunity.plugin_name(other) is None


def test_plugin_dlls_reference_expected_runtimes():
    """Собранные плагины: Mono — mscorlib 2.0 + BepInEx 5; IL2CPP — .NET 6 + BepInEx 6 + Il2CppInterop."""
    dnfile = pytest.importorskip("dnfile")
    res = Path(__file__).resolve().parents[1] / "russificator" / "resources" / "unity"

    def refs(name):
        md = dnfile.dnPE(str(res / name)).net.mdtables
        return {str(r.Name): r.MajorVersion for r in md.AssemblyRef}

    mono = refs("Russificator.Unity.dll")
    assert mono["mscorlib"] == 2 and mono["BepInEx"] == 5 and "UnityEngine" in mono
    il2cpp = refs("Russificator.Unity.IL2CPP.dll")
    assert il2cpp["System.Runtime"] == 6 and il2cpp["BepInEx.Core"] == 6 and "Il2CppInterop.Runtime" in il2cpp
    assert "UnityEngine.IMGUIModule" in il2cpp and "mscorlib" not in il2cpp
