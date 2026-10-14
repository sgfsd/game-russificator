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
