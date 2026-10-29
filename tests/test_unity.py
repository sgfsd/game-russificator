"""Тесты Unity: определение, строки из кода, XUnity, дорисовка кириллицы в шрифты."""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from russificator.engines.unity import detect, extract, il_scan, tmp_cyrillic, xunity


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
    """Как плагин читает строку файла (порт TextHelper.ReadTranslationLineAndDecode из XUnity 5.6)."""
    return xunity.decode_line(line)


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


def test_bepinex_entrypoint_is_monobehaviour(tmp_path):
    """Загрузчик BepInEx стартует из MonoBehaviour..cctor: из Application..cctor XUnity создаёт свой
    компонент раньше, чем движок готов, — старые Unity (Crawl, 5.4) падали прямо при запуске."""
    from russificator.core.backup import GameBackup
    root = _fake_game(tmp_path)
    game = detect.inspect(root)
    cfg = root / "BepInEx" / "config" / "BepInEx.cfg"
    xunity.write_config(game, GameBackup(root), live=True, tmp_font=None)
    text = cfg.read_text(encoding="utf-8")
    assert "Enabled = false" in text
    assert xunity.ini_value(text, "Preloader.Entrypoint", "Type") == "MonoBehaviour"
    assert xunity.ini_value(text, "Preloader.Entrypoint", "Assembly") == "UnityEngine.dll"     # Unity 5 и раньше
    assert xunity.ini_value(text, "Preloader.Entrypoint", "Method") == ".cctor"
    # BepInEx уже запускался и записал своё умолчание — оно заменяется, остальное остаётся как было
    cfg.write_text("[Caching]\n\nEnableAssemblyCache = true\n\n[Preloader.Entrypoint]\n\n"
                   "## Сборка\nAssembly = UnityEngine.dll\n\nType = Application\n\nMethod = .cctor\n\n"
                   "[Logging]\n\nUnityLogListening = true\n", encoding="utf-8")
    (game.data / "Managed").mkdir(exist_ok=True)
    (game.data / "Managed" / "UnityEngine.CoreModule.dll").write_bytes(b"x")    # Unity 2017+
    xunity.write_config(game, GameBackup(root), live=True, tmp_font=None)
    text = cfg.read_text(encoding="utf-8")
    assert xunity.ini_value(text, "Preloader.Entrypoint", "Type") == "MonoBehaviour"
    assert xunity.ini_value(text, "Preloader.Entrypoint", "Assembly") == "UnityEngine.CoreModule.dll"
    assert "## Сборка" in text and "EnableAssemblyCache = true" in text and "UnityLogListening = true" in text
    # точку входа, выбранную владельцем для модов, не трогаем
    cfg.write_text("[Preloader.Entrypoint]\nAssembly = UnityEngine.dll\nType = Camera\nMethod = .cctor\n",
                   encoding="utf-8")
    xunity.write_config(game, GameBackup(root), live=True, tmp_font=None)
    assert xunity.ini_value(cfg.read_text(encoding="utf-8"), "Preloader.Entrypoint", "Type") == "Camera"


def test_unitypy_files_are_closed(tmp_path):
    """После чтения ассетов файлы игры закрыты: Steam может их проверить и перезаписать, а их
    можно удалить, не закрывая программу."""
    from russificator.engines.unity import unitypy_io

    class Reader:
        def __init__(self, path):
            self.stream = open(path, "rb")

    class Bundle:
        def __init__(self, path):
            self.files = {"inner": type("F", (), {"reader": Reader(path)})()}

    a, b = tmp_path / "level0", tmp_path / "data.unity3d"
    a.write_bytes(b"x" * 10)
    b.write_bytes(b"y" * 10)
    env = type("Env", (), {})()
    env.files = {"level0": type("F", (), {"reader": Reader(a)})(), "bundle": Bundle(b)}
    env.cabs = {"cab": env.files["level0"]}
    streams = [env.files["level0"].reader.stream, env.files["bundle"].files["inner"].reader.stream]
    unitypy_io.release(env)
    assert all(s.closed for s in streams) and env.files == {} and env.cabs == {}
    a.unlink()
    b.unlink()


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
    game.version = "2019.4.40f1"
    assert xunity.tmp_font_bundle(game) == "arialuni_sdf_u2019"
    game.version = "2020.3.25f1"          # бандл под 2019 роняет игру 2020.3 при запуске
    assert xunity.tmp_font_bundle(game) is None
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
    tree = _tmp2_tree()
    tree["m_FaceInfo"] = {"m_PointSize": 42.0}
    font = tmp_cyrillic.TmpFont(tree)
    glyphs_before = len(tree["m_GlyphTable"])
    tex = _FakeTexture(256, 256)
    mat = {"m_SavedProperties": {"m_Floats": [["_GradientScale", 6.0], ["_TextureHeight", 256.0]]}}
    added = tmp_cyrillic.patch_font(font, tex, [(None, mat)], set(tmp_cyrillic.RUSSIAN))
    assert added == len(tmp_cyrillic.RUSSIAN)
    assert all(ord(c) in font.chars() for c in tmp_cyrillic.RUSSIAN)
    # русская «А» — тот же глиф, что латинская «A» (атлас не растёт ради двойников)
    by_code = {c["m_Unicode"]: c["m_GlyphIndex"] for c in tree["m_CharacterTable"]}
    assert by_code[ord("А")] == by_code[ord("A")] and by_code[ord("О")] == by_code[ord("O")]
    assert by_code[ord("Ж")] != by_code[ord("A")]
    new_glyphs = tree["m_GlyphTable"][glyphs_before:]
    assert 30 < len(new_glyphs) < 66
    w, h = font.atlas_size()
    assert tex.image.size == (w, h) and h > 256
    # старые глифы не сдвинулись (координаты снизу вверх), новые — внутри атласа
    assert tree["m_GlyphTable"][0]["m_GlyphRect"]["m_Y"] == 256 - 40
    for g in new_glyphs:
        r = g["m_GlyphRect"]
        assert 0 <= r["m_X"] and r["m_X"] + r["m_Width"] <= w and 0 <= r["m_Y"] and r["m_Y"] + r["m_Height"] <= h
    # рост букв — как у букв атласа (H — 30 px)
    zh = next(g for g in new_glyphs if g["m_Index"] == by_code[ord("Ж")])
    assert abs(zh["m_Metrics"]["m_Height"] - 30) <= 1.5
    assert mat["m_SavedProperties"]["m_Floats"][1] == ["_TextureHeight", float(h)]
    # второй проход ничего не добавляет
    assert tmp_cyrillic.patch_font(font, tex, [(None, mat)], set(tmp_cyrillic.RUSSIAN)) == 0


def test_tmp_font_upgraded_from_v1_uses_new_tables():
    tree = _tmp2_tree()
    tree["m_glyphInfoList"] = []          # TMP 1.4+: старый список остаётся пустым
    tree["m_GlyphTable"] = []             # …и у динамического шрифта новая таблица тоже может быть пуста
    assert not tmp_cyrillic.TmpFont(tree).v1
    tree["m_glyphInfoList"] = [{"id": 65}]
    assert tmp_cyrillic.TmpFont(tree).v1


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
    # так правила увидит XUnity после чтения файла (обратные слэши в файле удвоены)
    assert [_xua_decode_line(r) for r in rules] == [[r'sr:"^Pro\s+Tip:\s+([\S\s]+)$"', "Совет: $1"],
                                                    [r'sr:"^Level\s+\(x\):\s+([\S\s]+)$"', "Уровень $$ (x): $1"]]


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


def test_ink_lines_tags_templates_and_lowercase_speakers():
    """Ink Date Everything: ник со строчной («tinfoilhat:: …»), описания предметов в тегах
    («#null_hate_phrase Just a box.»), имя игрока внутри реплики (шаблон с вставкой «как есть»),
    короткие реплики чата («sure», «dang») — всё это игра показывает, всё должно быть в словаре."""
    import json
    story = {"inkVersion": 21, "root": [[
        "^tinfoilhat:: Okay. Here goes nothing...  ", "#", "^audio_name thiscord_phone_tinfoilhat_2", "/#", "\n",
        "^tinfoilhat:: Hello, ", "ev", {"VAR?": "player_name"}, "out", "/ev", "^. ", "\n",
        "#", "^null_hate_phrase Just a box.", "/#", "#", "^character penelope_stationery.sorter", "/#",
        "#", "^Name: tinfoilhat", "/#",
        "^Sam:: sure", "\n", "^dang", "\n", "^Player:: S.P.E.C.S.", "\n", "done"]]}
    got = _asset_texts(json.dumps(story))
    assert "Okay. Here goes nothing..." in got
    assert "Hello, {0:v}." in got and "Just a box." in got
    assert "sure" in got and "dang" in got and "Sam" in got
    assert not any("audio_name" in s or "penelope" in s or "tinfoilhat::" in s for s in got)


def test_xunity_case_variants_label_and_verbatim_rules():
    from russificator.engines.unity import xunity
    pairs = {"Tom": "Том", "abel face": "лицо Абеля", "<b>Hi</b>": "<b>Привет</b>", "TOM": "ТОМ!",
             "Hello, {0:v}.": "Привет, {0:v}.", "You have {0} coins": "У вас {0} монет"}
    var = xunity.case_variants(pairs)
    assert var == {"ABEL FACE": "ЛИЦО АБЕЛЯ", "TOM": "ТОМ", "tom": "том"}   # разметка не трогается
    # капс, переведённый сам по себе хуже, — по переводу той же строки обычным регистром
    assert xunity.case_variants({"David Most": "Дэвид Мост", "DAVID MOST": "ДЭВИДА БОЛЬШИНСТВО"}) == \
        {"DAVID MOST": "ДЭВИД МОСТ", "david most": "дэвид мост"}
    assert xunity.case_variants({"Ok": "Ок", "OK": "ОК"}) == {"ok": "ок"}
    rules = xunity.template_rules(pairs)
    assert any(r.startswith("r:") and "Hello" in r and r.endswith("Привет, $1.") for r in rules)
    assert any(r.startswith("sr:") and "coins" in r for r in rules)       # текстовая вставка — по словарю
    assert len(xunity.label_rules()) == 4 and len(xunity.number_rules()) == 3
    assert not xunity.template_rules({'Failed: "{0}" - Unknown': 'Ошибка: "{0}"'})   # кавычки ломают XUnity


def test_utage_book_display_column_is_trusted():
    """Таблица UTAGE: колонка English — текст для экрана, даже «female» (имя) и «Y-You…!» (заикание)."""
    tree = {"importGridList": [
        {"headerRow": 0, "rows": [{"strings": ["Key", "Korea", "English", "Turkish"]},
                                  {"strings": ["女性", "여성", "female", "dişi"]}]},
        {"headerRow": 0, "rows": [{"strings": ["Command", "Arg1", "Text", "Japanese", "English"]},
                                  {"strings": ["", "girlB", "你…！", "あっ…", "Y-You…!"]},
                                  {"strings": ["Wait", "0.5", "", "", ""]}]}]}
    col = extract._Collector()
    extract._from_monobehaviour(tree, col, "book")
    assert {"female", "Y-You…!"} <= set(col.items)
    assert not {"girlB", "dişi", "Key"} & set(col.items)


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


def _apply_xua_rule(rule: str, text: str):
    """Правило из файла XUnity -> (вид, результат для text) — как его прочитает плагин (регулярки .NET
    в наших правилах совместимы с Python re)."""
    import re as _re
    key, value = _xua_decode_line(rule)
    m = _re.fullmatch(r'(s?r):"(.*)"', key, _re.S)
    kind, rx = m.group(1), m.group(2).replace("\\u00a0", "\u00a0")
    hit = _re.match(rx, text)
    if not hit:
        return kind, None
    out = _re.sub(r"\$(\d)", lambda g: hit.group(int(g.group(1))), value).replace("$$", "$")
    return kind, out


def test_template_rules_translate_formatted_text():
    pairs = {"You have {0} coins": "У вас {0} монет", "Day {0} of {1}": "День {0} из {1}",
             "Score: {0:N0}": "Счёт: {0:N0}", "{0}/{1}": "{0}/{1}", "Lost {0}{1}": "Потеряно {0}{1}",
             "Hello {1}, meet {0}!": "{1}, познакомься с {0}!", "Price = {0} (x2)": "Цена = {0} (x2)",
             "Broken {0}": "Сломано"}                          # перевод потерял вставку — правила нет
    rules = xunity.template_rules(pairs)
    got = {}
    for r in rules:
        for text in ("You have 5 coins", "Day 3 of 10", "Score: 12,500", "Hello Bob, meet Ann!", "Price = 9 (x2)"):
            kind, out = _apply_xua_rule(r, text)
            if out is not None:
                got[text] = (kind, out)
    assert got["You have 5 coins"] == ("sr", "У вас 5 монет")
    assert got["Day 3 of 10"] == ("sr", "День 3 из 10")
    assert got["Score: 12,500"] == ("r", "Счёт: 12,500")               # числа — без перевода
    assert got["Hello Bob, meet Ann!"] == ("sr", "Bob, познакомься с Ann!")   # порядок вставок другой
    assert got["Price = 9 (x2)"] == ("sr", "Цена = 9 (x2)")           # «=» и скобки экранированы
    assert len(rules) == 5


def test_prefix_rules_escape_regex_for_xunity_file():
    rules = xunity.prefix_rules(["Path C:\\Games: "], {"Path C:\\Games: ": "Путь C:\\Games:"})
    kind, out = _apply_xua_rule(rules[0], "Path C:\\Games: Save1")
    assert kind == "sr" and out == "Путь C:\\Games: Save1"


def test_concat_templates_from_compiled_code():
    """Склейки строк в коде (исходник фикстуры: tests/data/il_concat.cs, собрано mcs)."""
    meta = il_scan._Meta((Path(__file__).parent / "data" / "il_concat.dll").read_bytes())
    assert il_scan.concat_templates(meta) == [
        "Day {0} of {1}", "Gold: {0}", "Welcome back, {0}! Ready?", "Level {0} — {1} has {2} coins"]


def test_addressables_crc_zeroed_without_changing_lengths(tmp_path):
    import base64
    import json
    from russificator.engines.unity import addressables
    rec = '{"m_Hash":"abc","m_Crc":3735928559,"m_Timeout":0}'.encode("utf-16-le")
    blob = b"\x07\x10\x00" + rec + b"\x00\x01"
    cat_dir = tmp_path / "Game_Data" / "StreamingAssets" / "aa"
    (cat_dir / "StandaloneWindows64").mkdir(parents=True)
    bundle = cat_dir / "StandaloneWindows64" / "fonts.bundle"
    bundle.write_bytes(b"UnityFS")
    cat = cat_dir / "catalog.json"
    cat.write_text(json.dumps({"m_LocatorId": "x", "m_ExtraDataString": base64.b64encode(blob).decode()}),
                   encoding="utf-8")
    assert addressables.catalog_for(bundle) == cat

    class _Backup:
        def write_bytes(self, path, data):
            path.write_bytes(data)
    assert addressables.disable_crc([bundle], _Backup()) == 1
    new = base64.b64decode(json.loads(cat.read_text(encoding="utf-8"))["m_ExtraDataString"])
    assert len(new) == len(blob)
    text = new[3:-2].decode("utf-16-le")
    assert json.loads(text)["m_Crc"] == 0 and json.loads(text)["m_Hash"] == "abc"


def test_paragraph_rule_splits_glued_text():
    rule = xunity.paragraph_rules()[0]
    kind, out = _apply_xua_rule(rule, "For those who favour speed.\n\nTrade Rusted Blade +VALUE")
    assert kind == "sr" and out == "For those who favour speed.\n\nTrade Rusted Blade +VALUE"
    assert _apply_xua_rule(rule, "One paragraph only\nsecond line")[1] is None


def test_loose_text_files_in_streaming_assets(tmp_path):
    """Описания и таблицы в StreamingAssets (json/csv/xml) попадают в словарь; чужие языки и каталог
    Addressables — нет."""
    import json as _json
    root = _fake_game(tmp_path)
    sa = root / "MyGame_Data" / "StreamingAssets"
    (sa / "Data").mkdir(parents=True)
    (sa / "Data" / "items.json").write_text(_json.dumps({"items": [
        {"id": "dagger_01", "name": "Dagger", "desc": "For those who favour speed and accuracy over range and power."}]}),
        encoding="utf-8")
    (sa / "Loc").mkdir()
    (sa / "Loc" / "strings_en.csv").write_text("key,english\nmenu_start,Start the journey now\n", encoding="utf-8")
    (sa / "Loc" / "strings_fr.csv").write_text("key,english\nmenu_start,Commencer le voyage maintenant\n",
                                               encoding="utf-8")
    (sa / "aa").mkdir()
    (sa / "aa" / "settings.json").write_text(_json.dumps({"m_buildTarget": "Windows build for the player"}),
                                             encoding="utf-8")
    (sa / "item_id.json").write_text(_json.dumps({"text": "Press any key to continue."}), encoding="utf-8")
    game = detect.inspect(root)
    col = extract._Collector()
    assert extract.extract_loose_files(game, col) == 3
    found = set(col.items)
    assert "For those who favour speed and accuracy over range and power." in found
    assert "Dagger" in found and "Start the journey now" in found and "Press any key to continue." in found
    assert "Commencer le voyage maintenant" not in found and "Windows build for the player" not in found
    assert "dagger_01" not in found


def test_coverage_harvest_keeps_live_lines_in_project(tmp_path):
    from russificator.engines.unity import coverage
    root = _fake_game(tmp_path)
    live = root / coverage.LIVE_FILE
    live.parent.mkdir(parents=True)
    live.write_text(xunity.encode("For those who favour speed.") + "=" + xunity.encode("Для любителей скорости.") +
                    "\nPapaNovember=PapaNovember\n", encoding="utf-8")
    work = tmp_path / "proj"
    assert coverage.harvest(root, work) == 2
    live.unlink()                                   # откат удалил папку BepInEx — в проекте всё осталось
    found = coverage.load_found(work)
    assert found["For those who favour speed."]["translation"] == "Для любителей скорости."
    assert coverage.missed(root, work)["PapaNovember"] == "PapaNovember"
    assert coverage.worth_translating("For those who favour speed.", "Для любителей скорости.")
    assert not coverage.worth_translating("PapaNovember", "PapaNovember")   # живой перевод вернул как есть
    assert not coverage.worth_translating("Discord", "Разногласия")


def test_coverage_classify_explains_each_miss():
    from russificator.engines.unity import coverage
    seen = {"For those who favour speed and accuracy over range and power": "resources.assets",
            "Trade": "code", "Rusted Blade": "level1", "VALUE": "code", "DAMAGE": "code",
            "You have {0} coins": "code", "ok": "level0"}
    texts = {"For those who favour speed and accuracy over range and power": "",
             "Trade Rusted Blade <color=#9f3>+VALUE</color>\n<color=#9f3>+DAMAGE</color>": "",
             "You have 15 coins": "", "Trade Golden Spoon +VALUE": "", "Completely generated words here": ""}
    got = {m.text: m for m in coverage.classify(texts, seen)}
    first = got["For those who favour speed and accuracy over range and power"]
    assert first.kind == "filtered" and first.where == "resources.assets" and first.note
    assert got["Trade Rusted Blade <color=#9f3>+VALUE</color>\n<color=#9f3>+DAMAGE</color>"].kind == "composed"
    assert got["You have 15 coins"].kind == "template"
    assert got["Trade Golden Spoon +VALUE"].kind == "partly"
    assert got["Completely generated words here"].kind == "absent"
    text = coverage.format_report({"game": "G", "date": "d", "total": 5, "items": [m.__dict__ for m in got.values()]})
    assert "поиск её отбросил" in text and "resources.assets" in text
    # уже находится нынешним поиском; имена из титров — отдельно
    got = {m.text: m.kind for m in coverage.classify({"Alex0080": "", "Difficulty": "", "Trade Golden Spoon +VALUE": ""},
                                                       {"Difficulty": "English", "Alex0080": "resources.assets:Credits_Names"},
                                                       found={"Difficulty"})}
    assert got == {"Difficulty": "found", "Alex0080": "names", "Trade Golden Spoon +VALUE": "absent"}


def test_live_found_strings_join_dictionary(tmp_path):
    """Найденное во время прошлых сеансов входит в словарь новой русификации."""
    from russificator.core.universal import TranslationProject
    from russificator.engines.unity import coverage
    from russificator.engines.unity.plugin import UnityPlugin
    root = _fake_game(tmp_path)
    live = root / coverage.LIVE_FILE
    live.parent.mkdir(parents=True)
    live.write_text("For those who favour speed.=Для любителей скорости.\nPapaNovember=PapaNovember\n", encoding="utf-8")
    proj = TranslationProject(root, "unity", tmp_path / "proj")
    plugin = UnityPlugin()
    assert plugin.harvest_live(root, proj.work_dir) == 2
    entries = plugin._live_entries(proj, known=set())
    assert [e.source for e in entries] == ["For those who favour speed."]
    assert entries[0].target_key.startswith("xua:") and proj.meta["unity_live_found"] == 1


def test_localization_table_keeps_short_labels():
    """Таблица «идентификатор -> текст»: короткие подписи берутся при любом имени ключа."""
    import json as _json
    from russificator.engines.unity import text_assets
    table = {"LBL_CRYO_POD": "Cryo pod", "SET_DIFFICULTY": "Difficulty", "SET_POST": "Post processing",
             "CMD_COMPLETED": "Task completed.", "CMD_SCAN_FAIL": "Couldn't find any items to scan.",
             "TXT_CONTINUE": "Press <Key><KeyBind></> to continue...", "LBL_DISPENSER": "Task dispenser",
             "SET_SFX": "Sound effects", "ID_X": "player_spawn_point"}
    got = []
    text_assets.extract(_json.dumps(table), got.append)
    assert {"Cryo pod", "Difficulty", "Post processing", "Sound effects", "Task completed."} <= set(got)
    assert "player_spawn_point" not in got
    # обычный JSON с данными (не таблица) — короткие значения по-прежнему только под «текстовыми» ключами
    got = []
    text_assets.extract(_json.dumps({"speed": "Fast", "mode": "Hard", "title": "Main Menu"}), got.append)
    assert got == ["Main Menu"]


def test_foreign_language_text_assets_are_skipped():
    class _Obj:
        def __init__(self, name):
            self.name = name

        def peek_name(self):
            return self.name
    objs = [_Obj(n) for n in ("English", "French", "Chinese Simplified", "Portuguese, Brazilian", "Credits_Names",
                              "Pirate English", "Dialogue")]
    kept = [o.name for o in extract._drop_foreign_text_assets(objs)]
    assert kept == ["English", "Credits_Names", "Pirate English", "Dialogue"]
    # английского нет — ничего не выбрасывается (таблица может быть единственной)
    assert len(extract._drop_foreign_text_assets([_Obj("French"), _Obj("Dialogue")])) == 2


def test_credits_names_are_not_a_localization_table():
    import json as _json
    from russificator.engines.unity import text_assets
    names = {f"N{i}": n for i, n in enumerate(["Alexander Kairento", "Capitan Noob", "Gabriel Steel", "Brackhar",
                                               "Josh Warren", "Sophia Katsaros", "Nate Laurence", "Corey K"])}
    got = []
    text_assets.extract(_json.dumps(names), got.append)
    assert got == []
    assert extract.NAMES_ASSET_RE.search("Credits_Names") and extract.NAMES_ASSET_RE.search("FormattedPatrons")


def test_key_hints_are_text_not_log_tags():
    col = extract._Collector()
    for s in ("[C] to Crouch", "[Space] Jump over the gap", "[E] Interact", "[Info] Loading scene", "[Debug] x=1"):
        col.add(s, extract.TextKind.UI, "level1")
    assert {"[C] to Crouch", "[Space] Jump over the gap", "[E] Interact"} <= set(col.items)
    assert "[Info] Loading scene" not in col.items and "[Debug] x=1" not in col.items


def test_object_table_guard_catches_script_index_change():
    """Сохранение не должно менять тип и номер скрипта объектов (UnityPy путал их в файлах Unity 5)."""
    from russificator.engines.unity import unitypy_io

    class _St:
        script_type_index = 7

    class _Obj:
        def __init__(self, cid):
            self.class_id, self.type_id, self.serialized_type = cid, 3, _St()

    class _Sf:
        def __init__(self, own):
            self.objects = {1: _Obj(114), 2: _Obj(28)}
            self._russificator_script_index = own

    class _Env:
        def __init__(self, own):
            self.file = _Sf(own)
            # соседний файл игры, подгруженный при чтении скриптов: те же path_id — не в счёт
            self.files = {"f": self.file, "other": _Sf({1: 1, 2: 2})}
    before = unitypy_io.object_table(_Env({1: 9}))
    assert before == {(0, 1): (114, 3, 9), (0, 2): (28, 3, 7)}
    assert unitypy_io.same_object_table(before, unitypy_io.object_table(_Env({1: 9}))) == ""
    assert "номер скрипта" in unitypy_io.same_object_table(before, unitypy_io.object_table(_Env({1: 188})))
    unitypy_io.fix_script_type_index()          # обёртка ставится один раз и не ломает чтение
    unitypy_io.fix_script_type_index()


def test_short_texts_are_guarded_from_partial_translations():
    """XUnity с частичными переводами подставлял в одиночные буквы начало чужого перевода (сетка
    ввода имени Crawl: «B» → «Б», «I» → «Вс»). Буквы, цифры и первые 1–2 знака строк словаря —
    записи «как есть»; строки, у которых есть свой перевод, не трогаются."""
    from russificator.engines.unity import xunity
    pairs = {"Bringing your own hero": "Вернуть своего героя", "On": "Вкл", "Slay the boss": "Убить босса"}
    guards = xunity.short_guards(pairs)
    keys = {g.split("=", 1)[0] for g in guards}
    assert {"B", "Br", "S", "Sl", "A", "z", "7"} <= keys
    assert "On" not in keys and "O" in keys
    assert all(g.split("=", 1)[0] == g.split("=", 1)[1] for g in guards)


def test_template_rules_match_text_the_game_wrapped_itself():
    """Игра сама переносит строки (вставляет перевод строки по ширине) — правило-шаблон всё равно
    срабатывает: пробелы и переводы строк в нём — любые пробельные."""
    import re
    from russificator.engines.unity import xunity
    rule = xunity.template_rules({"{0} / {1} entries found\r\n\r\nDiscover more entries by buying items in-game":
                                  "Найдено {0} / {1}\r\n\r\nОткройте больше записей, покупая предметы"})[0]
    key, _ = xunity.decode_line(rule)
    rx = re.compile(key[len('sr:"'):-1])
    assert rx.match("1 / 61 entries found\r\n\r\nDiscover more entries by buying items\nin-game")
    assert rx.match("1 / 61 entries found\n\nDiscover more entries by buying items in-game")
    assert not rx.match("1 / 61 entries lost\n\nDiscover more entries by buying items in-game")


def test_format_strings_from_script_fields_are_collected():
    """Короткая строка-формат в поле скрипта («{0} / {1} completed», «Level {0}») — подпись для экрана:
    собирается, хотя «предложением» не выглядит."""
    from russificator.engines.unity.extract import _format_text, _maybe_add
    col = extract._Collector()
    for v in ("{0} / {1} completed", "Level {0}", "{0}_{1}.png", "GetComponent<{0}>()"):
        _maybe_add(v, "m_stringAchievementsCategorySummary", col, "level2")
    assert set(col.items) == {"{0} / {1} completed", "Level {0}"}
    assert _format_text("{0} / {1} completed") and _format_text("Level {0}")
    assert not _format_text("{0}_{1}.png") and not _format_text("x{0}")


def _unity_string(s: str) -> bytes:
    b = s.encode("utf-8")
    pad = (-(4 + len(b))) % 4
    return len(b).to_bytes(4, "little") + b + b"\0" * pad


def test_direct_patch_raw_keeps_layout():
    """Строка внутри объекта без типового дерева: длина + байты + выравнивание. После замены
    всё, что дальше, читается так же; ключ рядом и фраза-дубль (может быть ключом) не трогаются."""
    from russificator.engines.unity.direct import patch_raw
    phrase = "We got lucky. Now we can find things here."
    tr = "Нам повезло. Теперь мы найдём здесь вещи."
    data = (_unity_string("E_story_131") + (7).to_bytes(4, "little") + _unity_string(phrase)
            + (42).to_bytes(4, "little") + _unity_string("tail"))
    new, n, amb = patch_raw(data, {phrase.encode(): tr.encode()})
    assert (n, amb) == (1, 0)
    assert new == (_unity_string("E_story_131") + (7).to_bytes(4, "little") + _unity_string(tr)
                   + (42).to_bytes(4, "little") + _unity_string("tail"))
    twice = _unity_string(phrase) + _unity_string(phrase)
    same, n, amb = patch_raw(twice, {phrase.encode(): tr.encode()})
    assert same == twice and (n, amb) == (0, 2)


def test_direct_patch_csv_and_json():
    import json
    from russificator.engines.unity.direct import is_phrase, patch_document
    m = {"Use the battery to turn on the flashlight.": "Включи фонарик батарейкой.",
         "It's locked, try again.": "Заперто, попробуй ещё раз."}
    csv_text = ("Key,English,Japanese\n"
                "a_001,Use the battery to turn on the flashlight.,x\n"
                "a_002,\"It's locked, try again.\",y\n")
    new, n = patch_document(csv_text, m)
    assert n == 2
    assert "a_001,Включи фонарик батарейкой.,x" in new
    assert 'a_002,"Заперто, попробуй ещё раз.",y' in new
    unquoted = {"Use the battery to turn on the flashlight.": "Батарейка, фонарик, свет."}
    same, n = patch_document(csv_text, unquoted)         # без кавычек запятая сломала бы колонки
    assert n == 0 and same == csv_text
    js, n = patch_document('{"hint": "Use the battery to turn on the flashlight.", "id": "a_001"}', m)
    assert n == 1 and json.loads(js) == {"hint": "Включи фонарик батарейкой.", "id": "a_001"}
    assert is_phrase("We got lucky. Now we can find things.") and not is_phrase("Yunxin")
    assert not is_phrase("E_story_131") and not is_phrase("Main Menu")


def test_direct_utage_book_writes_only_the_shown_column():
    """Сценарий UTAGE: перевод — в колонку English (её игра показывает), любой длины и с повторами;
    команды, аргументы-ключи персонажей, голос и японская колонка — как были."""
    from russificator.engines.unity.direct import patch_tree
    rows = [{"strings": ["Command", "Arg1", "Text", "Voice", "Japanese", "English"]},
            {"strings": ["", "雲欣", "怎麼了？", "v_01", "どうした？", "What's wrong?"]},
            {"strings": ["", "雲欣", "怎麼了？", "v_02", "どうした？", "What's wrong?"]},
            {"strings": ["Jump", "*Patient", "", "", "", ""]}]
    tree = {"importGridList": [{"rows": rows, "headerRow": 0, "name": "story"}]}
    full = {"What's wrong?": "Что случилось?", "Patient": "Пациент"}
    assert patch_tree(tree, full, {}) == 2
    assert rows[1]["strings"] == ["", "雲欣", "怎麼了？", "v_01", "どうした？", "Что случилось?"]
    assert rows[3]["strings"][1] == "*Patient"                # метка перехода — ключ


def test_direct_i2_source_keeps_term_keys():
    from russificator.engines.unity.direct import patch_tree
    tree = {"mSource": {"mLanguages": [{"Name": "Chinese"}, {"Name": "English"}],
                        "mTerms": [{"Term": "Use the battery", "Languages": ["用電池", "Use the battery"]}]}}
    assert patch_tree(tree, {"Use the battery": "Включи батарейку"}, {}) == 1
    term = tree["mSource"]["mTerms"][0]
    assert term["Term"] == "Use the battery" and term["Languages"] == ["用電池", "Включи батарейку"]


def test_direct_generic_fields_skip_keys():
    from russificator.engines.unity.direct import patch_tree
    phrase = "The door is locked from the inside."
    tree = {"m_Name": phrase, "description": phrase, "questId": phrase, "lines": [phrase]}
    assert patch_tree(tree, {}, {phrase: "Дверь заперта изнутри."}) == 2
    assert tree["m_Name"] == phrase and tree["questId"] == phrase
    assert tree["description"] == tree["lines"][0] == "Дверь заперта изнутри."


def test_tmp_font_upgraded_asset_with_leftover_legacy_list_is_v2():
    """Шрифт TMP, обновлённый со старой версии, хранит и остатки m_glyphInfoList, и новые таблицы:
    TMP читает новые — русские буквы дописываются туда (Date Everything: дописывались в старый список,
    и в игре вместо букв было «ↄↄↄ»)."""
    both = {"m_glyphInfoList": [{"id": 65}], "m_GlyphTable": [{"m_Index": 1}],
            "m_CharacterTable": [{"m_Unicode": 65, "m_GlyphIndex": 1}]}
    assert not tmp_cyrillic.TmpFont(both).v1
    assert tmp_cyrillic.TmpFont({"m_glyphInfoList": [{"id": 65}], "m_GlyphTable": [], "m_CharacterTable": []}).v1
    assert tmp_cyrillic.TmpFont({"m_glyphInfoList": [{"id": 65}]}).v1


def test_broken_synthesized_letters_are_redrawn():
    """Буква, которую построенный шрифт нарисовал пустой («к» в Game of Life 2) или вдвое шире
    соседних, рисуется запасным шрифтом — в слове нет дырки."""
    import numpy as np
    ok = [tmp_cyrillic.RenderedGlyph(c, 10, 10, 0, 10, 12.0, np.ones((14, 14), np.float32)) for c in "абвгде"]
    empty = tmp_cyrillic.RenderedGlyph("к", 0, 0, 0, 0, 12.0, np.zeros((0, 0), np.float32))
    wide = tmp_cyrillic.RenderedGlyph("т", 10, 10, 0, 10, 40.0, np.ones((14, 14), np.float32))
    out = tmp_cyrillic._sane_glyphs(ok + [empty, wide], 40.0, 4, 5.0, True, "Font")
    by = {g.char: g for g in out}
    assert by["к"].bitmap.size and (by["к"].bitmap > 0.5).any() and by["т"].advance < 30
    assert by["а"] is ok[0]


def test_blank_letters_in_game_font_are_redrawn():
    """Шрифт знает «к», но рисует её пустой (Dimbo, The Game of Life 2): буква считается отсутствующей."""
    chars = [{"m_Unicode": 0x43A, "m_GlyphIndex": 5}, {"m_Unicode": 0x41A, "m_GlyphIndex": 6},
             {"m_Unicode": 0x20, "m_GlyphIndex": 7}]
    glyphs = [{"m_Index": 5, "m_GlyphRect": {"m_Width": 0, "m_Height": 0}},
              {"m_Index": 6, "m_GlyphRect": {"m_Width": 20, "m_Height": 30}},
              {"m_Index": 7, "m_GlyphRect": {"m_Width": 0, "m_Height": 0}}]
    font = tmp_cyrillic.TmpFont({"m_CharacterTable": chars, "m_GlyphTable": glyphs})
    assert font.drop_blank_letters("кК ") == 1
    assert font.chars() == {0x41A, 0x20}                     # пробел пустой по праву — остаётся


def test_display_only_text_goes_to_dictionary_not_into_game_data():
    """Смелые источники текста (английский вариант среди переводов, подпись строчными в текстовом поле)
    дают строки только для словаря XUnity: он меняет лишь показанный текст, в данные игры они не пишутся."""
    col = extract._Collector()
    tree = {"rows": [{"strings": ["女性", "여성", "หญิง", "female", "dişi", "女性"]}],
            "opt": {"en": "Classic world", "ja": "クラシック"}, "text": "sure", "key": "ui_start",
            "title": "Main Menu", "note": "The door is locked."}
    extract._walk_strings(tree, col, "x", 0)
    items = {e.source: e.notes.get("display_only", False) for e in col.entries()}
    assert items["female"] and items["Classic world"] and items["sure"]
    assert items["Main Menu"] is False and items["The door is locked."] is False
    assert "dişi" not in items and "ui_start" not in items


def test_full_height_atlas_grows_in_width(monkeypatch):
    """Шрифт, сгенерированный огромным кеглем, занял атлас 1024×8192 целиком: русские буквы встают
    справа, атлас растёт вширь, старые глифы остаются на своих координатах (Handshakes: 4 шрифта)."""
    import numpy as np
    from PIL import Image

    class Tex:
        def __init__(self, img):
            self.image, self.m_TextureFormat = img, 1

        def set_image(self, img, target_format=None):
            self.image = img
    W, H = 64, 8192
    glyphs = [{"m_Index": i + 1, "m_GlyphRect": {"m_X": 0, "m_Y": i * 1024, "m_Width": 60, "m_Height": 1000},
               "m_Metrics": {"m_Width": 60.0, "m_Height": 1000.0, "m_HorizontalBearingX": 0.0,
                             "m_HorizontalBearingY": 900.0, "m_HorizontalAdvance": 64.0},
               "m_Scale": 1.0, "m_AtlasIndex": 0} for i in range(8)]
    chars = [{"m_ElementType": 1, "m_Unicode": ord(c), "m_GlyphIndex": i + 1, "m_Scale": 1.0}
             for i, c in enumerate("HIEFTLNx")]
    tree = {"m_Name": "Pixel", "m_GlyphTable": glyphs, "m_CharacterTable": chars, "m_AtlasWidth": W,
            "m_AtlasHeight": H, "m_AtlasPadding": 2, "m_AtlasRenderMode": 4165, "m_AtlasPopulationMode": 0,
            "m_FaceInfo": {"m_PointSize": 40.0, "m_CapLine": 30.0, "m_MeanLine": 20.0, "m_Scale": 1.0},
            "m_UsedGlyphRects": [], "m_FreeGlyphRects": []}
    alpha = np.zeros((H, W), np.uint8)
    alpha[:, :60] = 255
    tex = Tex(Image.merge("RGBA", [Image.fromarray(alpha, "L")] * 4))
    from russificator.fonts import cyrillic
    donor = tmp_cyrillic.GlyphRenderer(cyrillic.donor_bytes(False))
    monkeypatch.setattr(tmp_cyrillic, "plan_atlas", lambda *a, **k: ({}, ["Ж"], donor, 40.0))
    font = tmp_cyrillic.TmpFont(tree)
    added = tmp_cyrillic.patch_font(font, tex, [], "Ж")
    assert added == 1
    assert tree["m_AtlasHeight"] == H and tree["m_AtlasWidth"] == 2 * W and tex.image.size == (2 * W, H)
    new = [g for g in tree["m_GlyphTable"] if g["m_Index"] > 8][0]
    assert new["m_GlyphRect"]["m_X"] >= W                       # справа от старых букв
    assert tree["m_GlyphTable"][0]["m_GlyphRect"] == {"m_X": 0, "m_Y": 0, "m_Width": 60, "m_Height": 1000}


def test_fullwidth_cyrillic_in_tmp_atlas_is_dropped_for_redraw():
    """В атласе TMP китайского шрифта кириллица полноширинная — её буквы убираются из таблицы и потом
    дорисовываются, как отсутствующие, по меркам латиницы атласа."""
    chars, glyphs = [], []
    for i, ch in enumerate("ABCDEFGHKLMNOPRSTUXYZabcdeghknopsuxyz" + tmp_cyrillic.RUSSIAN):
        wide = ch in tmp_cyrillic.RUSSIAN
        chars.append({"m_Unicode": ord(ch), "m_GlyphIndex": i})
        glyphs.append({"m_Index": i, "m_GlyphRect": {"m_Width": 20, "m_Height": 30},
                       "m_Metrics": {"m_HorizontalAdvance": 40.0 if wide else 18.0 + i % 7}})
    font = tmp_cyrillic.TmpFont({"m_CharacterTable": chars, "m_GlyphTable": glyphs,
                                 "m_FaceInfo": {"m_PointSize": 42}})
    font.drop_blank_letters("Привет")
    left = {chr(c) for c in font.chars()}
    assert not set("Привет") & left and "ж" in left and "A" in left


def test_xor_protected_bundles_are_read_and_written_back(tmp_path):
    """Бандл, закрытый XOR коротким ключом (CardCultivation: StreamingAssets/*.dat), читается как обычный,
    а изменённый пишется обратно тем же ключом; склейку бандлов и чужие файлы защищённым бандлом не считаем."""
    from russificator.engines.unity import unitypy_io
    body = (b"UnityFS\0" + struct.pack(">I", 8) + b"5.x.x\0" + b"2021.3.45f1\0")
    plain = body + struct.pack(">Q", len(body) + 8 + 100) + bytes(100)
    key = b"\x4d\x7e\xa2\xb2"
    enc = bytes(c ^ key[i % 4] for i, c in enumerate(plain))
    p = tmp_path / "English.dat"
    p.write_bytes(enc)
    lay = unitypy_io.layout(p)
    assert lay is not None and lay.key == key and not lay.plain
    assert unitypy_io.decode(enc, lay) == plain
    assert unitypy_io.as_stored(p, plain + b"x") == bytes(c ^ key[i % 4] for i, c in enumerate(plain + b"x"))
    # байты мусора впереди
    q = tmp_path / "pad.bin"
    q.write_bytes(b"JUNK" * 8 + plain)
    assert unitypy_io.layout(q).offset == 32 and unitypy_io.as_stored(q, b"NEW")[:32] == b"JUNK" * 8
    # два бандла в одном файле (размер в заголовке не сходится) — не трогаем; обычный бандл — как был
    (tmp_path / "pack.bin").write_bytes(b"JUNK" + plain + plain)
    assert unitypy_io.layout(tmp_path / "pack.bin") is None
    (tmp_path / "plain.bundle").write_bytes(plain)
    assert unitypy_io.layout(tmp_path / "plain.bundle").plain
    (tmp_path / "video.dat").write_bytes(bytes(range(256)) * 4)
    assert unitypy_io.layout(tmp_path / "video.dat") is None


def _luban_str(s: str) -> bytes:
    b = s.encode("utf-8")
    n = len(b)
    head = bytes([n]) if n < 0x80 else bytes([0x80 | (n >> 8), n & 0xFF])
    return head + b


def test_binary_table_strings_and_english_column():
    """Двоичная таблица Luban «ключ, китайский, тайваньский, английский»: строки с длиной впереди читаются
    все и точно (первая буква ключа «n» = 110 байт не захватывает соседние записи), английская колонка
    берётся и одним словом, ключи и китайский — нет."""
    from russificator.engines.unity import text_assets
    rows = [("narration1_5", "当前工作台的动作是<color=#f3b123>修炼</color>", "當前工作臺的動作是",
             "The current action of this workbench is <color=#f3b123>Cultivate</color>."),
            ("tiaoguo_CNS", "跳过", "跳過", "Skip"),
            ("xiuliangongzuotai_CNS", "修炼工作台：修炼", "修煉工作臺：修煉", "WorkBench：Cultivate"),
            ("shouyuan_CNS", "寿元", "壽元", "Lifespan +20 years after cultivation;"),
            ("Story_20221_6", "：王婶.....", "：王嬸.....", ": Aunt Wang..... (I also blacked out)")]
    raw = bytes([len(rows)]) + b"".join(_luban_str(s) for r in rows for s in r) + b"\x05\x00"
    assert text_assets.decode(raw) is None
    found = text_assets.binary_strings(raw)
    assert found == [s for r in rows for s in r]
    col = extract._Collector()
    extract._from_binary_table(raw, col, "LubanTables.dat:lang")
    got = {e.source for e in col.entries()}
    assert {"Skip", "WorkBench：Cultivate", "Lifespan +20 years after cultivation;", rows[0][3]} <= got
    assert not got & {"narration1_5", "tiaoguo_CNS", "跳过"}


def test_template_tail_goes_to_dictionary():
    """«370 points!» общее правило «число + слово» делит на число и «points!» — хвост шаблона
    «{0} points!» → «{0} очков!» лежит в словаре, слово не уходит в живой перевод («370 точки!»)."""
    got = xunity.template_fragments({"{0} points!": "{0} очков!", "Added {0} points!": "Добавлено {0} баллов!",
                                     "{0} coins left": "осталось {0} монет", "{0}/{1}": "{0}/{1}"})
    assert got == {"points!": "очков!"}
