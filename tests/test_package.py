"""Архив-русификатор «для друзей»: создание, установка на чистую копию игры, удаление."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from russificator import branding
from russificator.core import delta
from russificator.core.backup import BACKUP_DIR, GameBackup
from russificator.core.package import (DATA_DIR, PackageError, _excluded, apply_translations, can_export,
                                       check_game, export_package, install_package, open_package,
                                       translator_title, uninstall)
from russificator.core.pipeline import Pipeline, PipelineOptions
from russificator.core.restore import restore_backups

from .test_engines import ace_game  # noqa: F401  (фикстура: игра VX Ace в архиве .rgss3a)
from .test_pipeline import FakeTranslator

DATA = Path(__file__).parent / "data"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """Данные программы (проекты, временные файлы) — во временной папке теста."""
    monkeypatch.setenv("RUSSIFICATOR_HOME", str(tmp_path / "home"))
    from russificator import paths
    paths.set_home(None)
    yield


def _tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _russify(game: Path, fn=None) -> None:
    tr = FakeTranslator()
    if fn is not None:
        tr.translate = lambda entries, glossary: {e.id: fn(e.source) for e in entries}
    res = Pipeline(tr, PipelineOptions(use_memory=False)).run(game)
    assert res.success, res.summary()


def _export(game: Path, out: Path, credit: bool = True) -> Path:
    res = export_package(game, out, credit=credit)
    path = Path(res["path"])
    assert path.is_file() and path.suffix == ".zip"
    return path


# ------------------------------------------------------------------ RPG Maker MV (повторное внедрение)

@pytest.fixture
def mv_game(tmp_path):
    root = tmp_path / "MVGame"
    data = root / "www" / "data"
    data.mkdir(parents=True)
    js = root / "www" / "js"
    (js / "plugins").mkdir(parents=True)
    (js / "rpg_core.js").write_text("//", encoding="utf-8")
    (js / "plugins.js").write_text("var $plugins =\n[\n];\n", encoding="utf-8")
    (root / "www" / "fonts").mkdir()
    (root / "www" / "fonts" / "gamefont.css").write_text(
        '@font-face { font-family: GameFont; src: url("mplus-1m-regular.ttf"); }', encoding="utf-8")
    (root / "www" / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    (root / "Game.exe").write_bytes(b"MZ")
    page = {"list": [
        {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]},
        {"code": 401, "indent": 0, "parameters": ["Hello there, traveler! Welcome to the quiet village."]},
        {"code": 102, "indent": 0, "parameters": [["Yes", "No"], 1, 0, 2, 0]},
        {"code": 0, "indent": 0, "parameters": []},
    ]}
    (data / "Map001.json").write_text(json.dumps({"events": [None, {"id": 1, "name": "Door", "pages": [page]}]}),
                                      encoding="utf-8")
    (data / "Items.json").write_text(json.dumps([None, {"id": 1, "name": "Potion", "description": "Heals."}]),
                                     encoding="utf-8")
    (data / "System.json").write_text(json.dumps({"gameTitle": "My Game", "terms": {
        "basic": ["Level"], "commands": ["Fight"], "params": ["Max HP"], "messages": {}}}), encoding="utf-8")
    return root


def test_mv_package_roundtrip_with_credit(mv_game, tmp_path):
    friend = tmp_path / "friend" / "MVGame"
    shutil.copytree(mv_game, friend)
    original = _tree(friend)

    _russify(mv_game)
    assert can_export(mv_game)[0]
    z = _export(mv_game, tmp_path / "out")
    assert z.name == "MVGame — русификатор.zip"
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
    top = "MVGame — русификатор/"
    assert top + "Прочти.txt" in names and top + DATA_DIR + "/package.json" in names
    assert not any("www/data" in n for n in names)            # файлов игры в архиве нет
    readme = zipfile.ZipFile(z).read(top + "Прочти.txt").decode("utf-8-sig")
    assert "@TRAPYCHINO" in readme and "Переведено строк" in readme

    pkg = open_package(z)
    try:
        assert pkg.info["method"] == "reinject" and pkg.engine == "rpgmaker"
        chk = check_game(pkg, friend)
        assert chk.ok and chk.exact
        res = install_package(pkg, friend)
    finally:
        pkg.close()
    assert res["ok"] and res["translated"] >= 4
    text = (friend / "www/data/Map001.json").read_text(encoding="utf-8")
    assert "RU Hello there" in text and '"RU Yes"' in text
    plugins = (friend / "www/js/plugins.js").read_text(encoding="utf-8")
    assert branding.CREDIT in plugins                           # надпись о программе — параметром плагина
    info = GameBackup(friend).info
    assert info["package"]["title"] == "MVGame"
    assert GameBackup(friend).check()[0]

    uninstall(friend)
    assert _tree(friend) == original                            # удаление возвращает игру байт в байт


def test_mv_package_without_credit_and_other_version(mv_game, tmp_path):
    friend = tmp_path / "friend" / "MVGame"
    shutil.copytree(mv_game, friend)
    # у друга версия новее: одна реплика добавлена
    mp = json.loads((friend / "www/data/Map001.json").read_text(encoding="utf-8"))
    mp["events"][1]["pages"][0]["list"].insert(-1, {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]})
    mp["events"][1]["pages"][0]["list"].insert(-1, {"code": 401, "indent": 0,
                                                    "parameters": ["A brand new line from the update."]})
    (friend / "www/data/Map001.json").write_text(json.dumps(mp), encoding="utf-8")

    _russify(mv_game)
    pkg = open_package(_export(mv_game, tmp_path / "out", credit=False))
    try:
        res = install_package(pkg, friend)
    finally:
        pkg.close()
    text = (friend / "www/data/Map001.json").read_text(encoding="utf-8")
    assert "RU Hello there" in text and "A brand new line from the update." in text
    assert any("остальные строки" in n for n in res["notes"])
    assert branding.CREDIT not in (friend / "www/js/plugins.js").read_text(encoding="utf-8")


def test_install_rejects_other_engine(mv_game, tmp_path):
    _russify(mv_game)
    pkg = open_package(_export(mv_game, tmp_path / "out"))
    other = tmp_path / "Other"
    (other / "game").mkdir(parents=True)
    (other / "game" / "script.rpy").write_text('label start:\n    "Hi"\n', encoding="utf-8")
    (other / "renpy").mkdir()
    try:
        chk = check_game(pkg, other)
        assert not chk.ok and "другом движке" in chk.message
        assert not check_game(pkg, tmp_path / "missing").ok
    finally:
        pkg.close()


def test_export_requires_russified_game(mv_game, tmp_path):
    ok, why = can_export(mv_game)
    assert not ok and "русифицируйте" in why
    with pytest.raises(PackageError):
        export_package(mv_game, tmp_path / "out")


def test_vxace_package_from_archive(ace_game, tmp_path):
    from russificator.engines.rpgmaker import ruby_marshal as M
    game = ace_game
    friend = tmp_path / "friend" / "AceGame"
    shutil.copytree(game, friend)
    original = _tree(friend)
    # у игры должны быть скрипты, иначе надпись некуда вставить
    _russify(game)
    pkg = open_package(_export(game, tmp_path / "out"))
    try:
        install_package(pkg, friend)
    finally:
        pkg.close()
    mp = M.load((friend / "Data/Map001.rvdata2").read_bytes())
    cmds = mp.ivars["@events"][1].ivars["@pages"][0].ivars["@list"]
    assert any(str(c.ivars["@parameters"][0]).startswith("RU Welcome") for c in cmds if c.ivars["@code"] == 401)
    uninstall(friend)
    assert _tree(friend) == original


def test_rgss_credit_script_inserted_before_main(tmp_path):
    import zlib
    from russificator.engines.rpgmaker import ruby_marshal as M
    from russificator.engines.rpgmaker.plugin import RPGMakerPlugin
    root = tmp_path / "Ace"
    (root / "Data").mkdir(parents=True)
    scripts = [[1, M.RubyString("Vocab"), M.RubyString(zlib.compress(b"module Vocab; end").decode("latin-1"))],
               [2, M.RubyString("Main"), M.RubyString(zlib.compress(b"rgss_main { }").decode("latin-1"))]]
    (root / "Data" / "Scripts.rvdata2").write_bytes(M.dump(scripts, utf8_strings=True))
    plugin = RPGMakerPlugin()
    plugin.version = "ace"
    assert plugin._credit_rgss(root, branding.CREDIT, GameBackup(root))
    data = M.load((root / "Data" / "Scripts.rvdata2").read_bytes())
    names = [str(s[1]) for s in data]
    assert names == ["Vocab", "Russificator Credit", "Main"]
    code = zlib.decompress(str(data[1][2]).encode("utf-8", "surrogateescape")).decode("utf-8")
    assert "class Scene_Title" in code and branding.CREDIT in code
    assert not plugin._credit_rgss(root, branding.CREDIT, GameBackup(root))   # повторно не вставляется


# ------------------------------------------------------------------ Ren'Py (копирование файлов)

@pytest.fixture
def renpy_game(tmp_path):
    from .test_engines import write_rpa
    root = tmp_path / "MyVN"
    (root / "game").mkdir(parents=True)
    (root / "renpy").mkdir()
    (root / "MyVN.exe").write_bytes(b"MZ")
    write_rpa(root / "game" / "archive.rpa", {
        "script.rpyc": (DATA / "renpy813_script.rpyc").read_bytes(),
        "fonts/LatinOnly.ttf": b"not a font with cyrillic",
    })
    return root


def test_renpy_package_copies_files_and_adds_credit(renpy_game, tmp_path):
    friend = tmp_path / "friend" / "MyVN"
    shutil.copytree(renpy_game, friend)
    original = _tree(friend)
    _russify(renpy_game)
    z = _export(renpy_game, tmp_path / "out")
    pkg = open_package(z)
    try:
        assert pkg.info["method"] == "files" and not pkg.info["patches"]
        paths = {f["path"] for f in pkg.info["files"]}
        assert {"game/russificator_tl.json", "game/zz_russificator.rpy",
                "game/russificator/PT_Sans-Web-Regular.ttf"} <= paths
        install_package(pkg, friend)
    finally:
        pkg.close()
    hook = (friend / "game" / "zz_russificator.rpy").read_text(encoding="utf-8")
    assert "screen russificator_credit" in hook and branding.CREDIT in hook
    assert "alt_K_t" in hook                                    # Alt+T — перевод/оригинал
    # у автора надписи нет — только в архиве
    assert "russificator_credit" not in (renpy_game / "game" / "zz_russificator.rpy").read_text(encoding="utf-8")
    table = json.loads((friend / "game" / "russificator_tl.json").read_text(encoding="utf-8"))
    assert table["Go to the forest"] == "RU Go to the forest"
    uninstall(friend)
    assert _tree(friend) == original


# ------------------------------------------------------------------ Unity (файлы + бинарные патчи)

def _fake_unity(root: Path) -> None:
    data = root / "Fake_Data"
    (data / "Managed").mkdir(parents=True)
    (data / "globalgamemanagers").write_bytes(b"\0" * 64 + b"2021.3.5f1" + b"\0" * 64)
    (root / "Fake.exe").write_bytes(b"MZ" + b"\0" * 200)
    (root / "UnityPlayer.dll").write_bytes(b"MZ")
    (data / "sharedassets0.assets").write_bytes(os.urandom(300_000) + b"FONT-ATLAS-EN" + os.urandom(300_000))


def _fake_unity_russification(root: Path, work: Path) -> None:
    """То, что делает UnityPlugin.inject, без UnityPy: BepInEx + словарь + изменённый ассет шрифта."""
    from russificator.core.pipeline import project_dir
    from russificator.core.universal import Entry, EntryStatus, TranslationProject
    from russificator.engines.unity import xunity
    b = GameBackup(root)
    (root / "BepInEx" / "core").mkdir(parents=True)
    b.track_new(root / "BepInEx")
    b.write_bytes(root / "winhttp.dll", b"doorstop")
    (root / "BepInEx" / "core" / "BepInEx.dll").write_bytes(b"core")
    (root / "BepInEx" / "plugins").mkdir()
    (root / "BepInEx" / "plugins" / xunity.PLUGIN).write_bytes(b"plugin")
    (root / "BepInEx" / "cache").mkdir()
    (root / "BepInEx" / "cache" / "junk.dat").write_bytes(b"x" * 100)
    (root / "BepInEx" / "LogOutput.log").write_text("log", encoding="utf-8")
    cfg = root / "BepInEx" / "config"
    cfg.mkdir()
    (cfg / "AutoTranslatorConfig.ini").write_text(
        "[Service]\nEndpoint=CustomTranslate\nFallbackEndpoint=\n\n[Behaviour]\nFallbackFontTextMeshPro=\n",
        encoding="utf-8")
    (cfg / "Russificator.cfg").write_text(xunity.plugin_config_text(("C:/Prog/Russificator.exe", "--serve", "C:/Prog")),
                                          encoding="utf-8")
    text = root / "BepInEx" / "Translation" / "ru" / "Text"
    text.mkdir(parents=True)
    (text / "_Russificator.txt").write_text("Start=Начать\n", encoding="utf-8")
    (text / "_AutoGeneratedTranslations.txt").write_text("Runtime line=Строка на лету\n", encoding="utf-8")
    b.write_bytes(root / "Играть на русском.lnk", b"lnk")
    asset = root / "Fake_Data" / "sharedassets0.assets"
    raw = asset.read_bytes()
    b.write_bytes(asset, raw.replace(b"FONT-ATLAS-EN", b"FONT-ATLAS-EN+CYRILLIC-GLYPHS" + os.urandom(5000)))
    proj = TranslationProject(root, "unity", project_dir(root))
    proj.entries = [Entry(id="1", source="Start", translation="Начать", status=EntryStatus.TRANSLATED)]
    proj.meta = {"translator": "CloudTranslator:llm:deepseek-chat", "unity_fonts": [{"added": 0}],
                 "unity": {"exe": str(root / "Fake.exe")}, "game_title": "Fake"}
    proj.save()
    b.snapshot({})


def test_unity_package_files_patches_and_friend_config(tmp_path):
    game = tmp_path / "Fake"
    _fake_unity(game)
    friend = tmp_path / "friend" / "Fake"
    shutil.copytree(game, friend)
    original = _tree(friend)
    _fake_unity_russification(game, tmp_path)

    z = _export(game, tmp_path / "out")
    pkg = open_package(z)
    try:
        paths = {f["path"] for f in pkg.info["files"]}
        assert "BepInEx/core/BepInEx.dll" in paths and "winhttp.dll" in paths
        assert "BepInEx/Translation/ru/Text/_AutoGeneratedTranslations.txt" in paths   # перевод «на лету» — в архиве
        assert not any("cache" in p or "LogOutput" in p or p.endswith(".lnk") for p in paths)
        assert [p["path"] for p in pkg.info["patches"]] == ["Fake_Data/sharedassets0.assets"]
        assert os.path.getsize(pkg.data / pkg.info["patches"][0]["patch"]) < 20_000     # патч, а не файл
        assert pkg.info["translator"] == "облачная нейросеть (deepseek-chat)"
        install_package(pkg, friend)
    finally:
        pkg.close()
    assert (friend / "Fake_Data" / "sharedassets0.assets").read_bytes() == \
        (game / "Fake_Data" / "sharedassets0.assets").read_bytes()
    ini = (friend / "BepInEx/config/AutoTranslatorConfig.ini").read_text(encoding="utf-8")
    assert "Endpoint=\n" in ini and "CustomTranslate" not in ini   # у друга нет программы — сервер не нужен
    cfg = (friend / "BepInEx/config/Russificator.cfg").read_text(encoding="utf-8")
    assert "server=" not in cfg and f"credit={branding.CREDIT}" in cfg
    assert not (friend / "Играть на русском.lnk").exists()

    # игра записала свои файлы в BepInEx — удаление всё равно возвращает оригинал
    (friend / "BepInEx" / "LogOutput.log").write_text("new log", encoding="utf-8")
    uninstall(friend)
    assert _tree(friend) == original


def test_unity_package_other_version_skips_patch(tmp_path):
    game = tmp_path / "Fake"
    _fake_unity(game)
    friend = tmp_path / "friend" / "Fake"
    shutil.copytree(game, friend)
    (friend / "Fake_Data" / "sharedassets0.assets").write_bytes(b"other version")
    _fake_unity_russification(game, tmp_path)
    pkg = open_package(_export(game, tmp_path / "out"))
    try:
        res = install_package(pkg, friend)
    finally:
        pkg.close()
    assert (friend / "Fake_Data" / "sharedassets0.assets").read_bytes() == b"other version"
    assert any("версия игры отличается" in n for n in res["notes"])
    assert (friend / "BepInEx/Translation/ru/Text/_Russificator.txt").is_file()


# ------------------------------------------------------------------ мелочи

def test_integrity_check_detects_game_update(mv_game):
    _russify(mv_game)
    ok, broken = GameBackup(mv_game).check()
    assert ok and not broken
    f = mv_game / "www/data/Map001.json"
    shutil.copyfile(mv_game / BACKUP_DIR / "www/data/Map001.json", f)   # «проверка целостности» вернула оригинал
    os.utime(f, ns=(1, 1))
    ok, broken = GameBackup(mv_game).check()
    assert not ok and "www/data/Map001.json" in broken


def test_exclusion_patterns():
    pats = ["Играть на русском.lnk", "BepInEx/cache/*", "BepInEx/LogOutput.log*", "*.tmp"]
    assert _excluded("BepInEx/cache", pats) and _excluded("BepInEx/cache/a/b", pats)
    assert _excluded("Играть на русском.lnk", pats) and _excluded("x/y.tmp", pats)
    assert not _excluded("BepInEx/core/BepInEx.dll", pats) and not _excluded("BepInEx", pats)


def test_translator_titles():
    assert translator_title("MachineTranslator:mt:argos-en-ru-1.9") == "машинный перевод"
    assert translator_title("LocalTranslator:llm:Qwen2.5-7B.gguf") == "нейросеть на ПК (Qwen2.5-7B)"
    assert translator_title("CloudTranslator:llm:gpt-5") == "облачная нейросеть (gpt-5)"


def test_apply_translations_by_key_then_text():
    from russificator.core.universal import Entry, TranslationProject
    proj = TranslationProject(Path("."), "x", Path(os.environ["RUSSIFICATOR_HOME"]) / "p")
    proj.entries = [Entry(id="a", source="Hi", target_key="k1"), Entry(id="b", source="Hi", target_key="k2"),
                    Entry(id="c", source="New", target_key="k3")]
    n = apply_translations(proj, [{"k": "k1", "s": "Hi", "t": "Привет", "st": "translated"}])
    assert n == 2 and proj.entries[1].translation == "Привет" and proj.entries[2].translation is None


def test_delta_roundtrip(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    base = os.urandom(200_000)
    a.write_bytes(base)
    b.write_bytes(base[:50_000] + os.urandom(3000) + base[60_000:] + b"tail")
    info = delta.make_patch(a, b, tmp_path / "p")
    assert info and info["patch_size"] < 10_000
    delta.apply_patch(a, tmp_path / "p", tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b.read_bytes()
    a.write_bytes(b"changed")
    with pytest.raises(delta.PatchError):
        delta.apply_patch(a, tmp_path / "p", tmp_path / "out")
    big = os.urandom(3_000_000)
    a.write_bytes(big)
    b.write_bytes(os.urandom(3_000_000))                         # совсем другой файл — патч не нужен
    assert delta.make_patch(a, b, tmp_path / "p2") is None
