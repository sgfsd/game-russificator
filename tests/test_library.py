"""«Мои игры»: поиск установленных игр (Steam, папки), быстрое определение движка."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from russificator import library


def _unity(root: Path, name: str = "Game") -> Path:
    (root / f"{name}_Data" / "Managed").mkdir(parents=True)
    (root / f"{name}_Data" / "globalgamemanagers").write_bytes(b"x")
    (root / f"{name}.exe").write_bytes(b"MZ")
    (root / "UnityPlayer.dll").write_bytes(b"MZ")
    (root / "UnityCrashHandler64.exe").write_bytes(b"MZ")
    return root


def _renpy(root: Path) -> Path:
    (root / "game").mkdir(parents=True)
    (root / "game" / "script.rpyc").write_bytes(b"x")
    (root / "renpy").mkdir()
    (root / "MyVN.exe").write_bytes(b"MZ")
    return root


def _mv(root: Path) -> Path:
    (root / "www" / "js").mkdir(parents=True)
    (root / "www" / "data").mkdir()
    (root / "www" / "js" / "rpg_core.js").write_text("//")
    (root / "Game.exe").write_bytes(b"MZ")
    return root


def test_quick_engine(tmp_path):
    assert library.quick_engine(_unity(tmp_path / "u")) == "unity"
    assert library.quick_engine(_renpy(tmp_path / "r")) == "renpy"
    assert library.quick_engine(_mv(tmp_path / "m")) == "rpgmaker"
    ace = tmp_path / "ace"
    ace.mkdir()
    (ace / "Game.rgss3a").write_bytes(b"RGSSAD")
    assert library.quick_engine(ace) == "rpgmaker"
    ue = tmp_path / "ue"
    (ue / "Engine" / "Binaries").mkdir(parents=True)
    assert library.quick_engine(ue) == "unreal"
    godot = tmp_path / "gd"
    godot.mkdir()
    (godot / "Brotato.exe").write_bytes(b"MZ")
    (godot / "Brotato.pck").write_bytes(b"GDPC")
    assert library.quick_engine(godot) == "godot"
    gm = tmp_path / "gm"
    gm.mkdir()
    (gm / "data.win").write_bytes(b"FORM")
    assert library.quick_engine(gm) == "gamemaker"
    assert library.quick_engine(tmp_path / "missing") == ""


def test_main_exe_prefers_unity_player_and_skips_helpers(tmp_path):
    root = _unity(tmp_path / "Hollow", "hollow_knight")
    (root / "unins000.exe").write_bytes(b"MZ")
    assert library.main_exe(root).name == "hollow_knight.exe"


VDF = '''"libraryfolders"
{
	"0"
	{
		"path"		"%s"
		"apps" { "367520" "123" }
	}
	"1"
	{
		"path"		"%s"
	}
}
'''


def _acf(lib: Path, appid: str, name: str, folder: str) -> None:
    (lib / "steamapps").mkdir(parents=True, exist_ok=True)
    (lib / "steamapps" / f"appmanifest_{appid}.acf").write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n\t"installdir"\t\t"{folder}"\n}}\n',
        encoding="utf-8")


@pytest.fixture
def steam(tmp_path, monkeypatch):
    root = tmp_path / "Steam"
    lib2 = tmp_path / "D" / "SteamLibrary"
    (root / "steamapps").mkdir(parents=True)
    (lib2 / "steamapps").mkdir(parents=True)
    esc = lambda p: str(p).replace("\\", "\\\\")  # noqa: E731
    (root / "steamapps" / "libraryfolders.vdf").write_text(VDF % (esc(root), esc(lib2)), encoding="utf-8")
    _acf(root, "367520", "Hollow Knight™", "Hollow Knight")
    _unity(root / "steamapps" / "common" / "Hollow Knight", "hollow_knight")
    _acf(lib2, "1150690", "OMORI", "OMORI")
    _mv(lib2 / "steamapps" / "common" / "OMORI")
    _acf(lib2, "228980", "Steamworks Common Redistributables", "Steamworks Shared")
    (lib2 / "steamapps" / "common" / "Steamworks Shared").mkdir(parents=True)
    _acf(lib2, "999", "Some Game Soundtrack", "Some Game OST")
    (lib2 / "steamapps" / "common" / "Some Game OST").mkdir(parents=True)
    monkeypatch.setattr(library, "steam_root", lambda: root)
    return root


def test_steam_libraries_and_games(steam):
    libs = library.steam_libraries()
    assert len(libs) == 2
    games = {g.title: g for g in library.steam_games()}
    assert set(games) == {"Hollow Knight™", "OMORI"}        # инструменты и саундтреки пропущены
    assert games["OMORI"].appid == "1150690"


def test_discover_merges_sources_and_cleans_titles(steam, tmp_path):
    extra = tmp_path / "Games"
    _renpy(extra / "MyVN")
    (extra / "Docs").mkdir(parents=True)                    # папка без exe — не игра
    hk = steam / "steamapps" / "common" / "Hollow Knight"
    games = library.discover(extra_folders=[str(extra)], manual=[str(hk)])
    by_title = {g.title: g for g in games}
    assert set(by_title) == {"Hollow Knight", "OMORI", "MyVN"}
    hkg = by_title["Hollow Knight"]
    assert hkg.source == "steam" and set(hkg.sources) == {"steam", "manual"} and hkg.appid == "367520"
    assert hkg.engine == "unity" and hkg.exe.endswith("hollow_knight.exe")
    d = hkg.to_dict()
    assert d["supported"] and d["cover"].endswith("/367520/header.jpg") and d["engine_title"] == "Unity"
    assert by_title["MyVN"].engine == "renpy" and by_title["MyVN"].source == "folder"


def test_find_game_for_installer(steam):
    games = library.discover()
    found = library.find_game(folder_name="OMORI", exe_name="Game.exe", title="OMORI", games=games)
    assert found and found[0].name == "OMORI"
    assert library.find_game(folder_name="Nope", exe_name="nope.exe", games=games) == []


def test_xbox_games_are_overlay_only():
    g = library.GameEntry(path="C:/XboxGames/Starfield/Content", title="Starfield", source="xbox", engine="unity",
                          sources=["xbox"])
    assert not g.supported


def test_vdf_parser_handles_escapes():
    d = library._vdf('"a" { "path" "C:\\\\Games\\\\Steam" "q" "say \\"hi\\"" }')
    assert d["a"]["path"] == "C:\\Games\\Steam" and d["a"]["q"] == 'say "hi"'


def test_games_status_and_live_count(tmp_path, monkeypatch):
    monkeypatch.setenv("RUSSIFICATOR_HOME", str(tmp_path / "home"))
    from russificator import paths
    paths.set_home(None)
    from russificator.core.backup import GameBackup
    from russificator.ui import games
    root = _unity(tmp_path / "G")
    assert games.status(root) == {"russified": False}
    b = GameBackup(root)
    f = root / "BepInEx" / "Translation" / "ru" / "Text" / "_AutoGeneratedTranslations.txt"
    f.parent.mkdir(parents=True)
    b.track_new(root / "BepInEx")
    f.write_text("// comment\nHello=Привет\nBye=Пока\n", encoding="utf-8")
    b.snapshot({"translator": "CloudTranslator:llm:gpt-5", "date": "2026-09-27 18:00", "translated": 10, "total": 12})
    st = games.status(root)
    assert st["russified"] and st["intact"] and st["live"] == 2
    assert st["translator"] == "облачная нейросеть (gpt-5)" and st["total"] == 12
    # проекты программы — список уже русифицированных игр
    proj = paths.sub("projects") / "G-1"
    proj.mkdir(parents=True)
    (proj / "project.json").write_text(json.dumps({"version": 1, "game_dir": str(root), "entries": []}),
                                       encoding="utf-8")
    assert games.russified_dirs() == [str(root)]


def test_folder_scan_finds_games_with_exe(tmp_path):
    """Своя папка с играми: игра без узнаваемого движка, но с exe — тоже игра; служебные папки — нет."""
    from russificator import library
    (tmp_path / "GodotThing").mkdir()
    (tmp_path / "GodotThing" / "thing.exe").write_bytes(b"MZ")
    (tmp_path / "_CommonRedist").mkdir()
    (tmp_path / "_CommonRedist" / "run.exe").write_bytes(b"MZ")
    (tmp_path / "Collection" / "Part1").mkdir(parents=True)
    (tmp_path / "Collection" / "Part1" / "p1.exe").write_bytes(b"MZ")
    (tmp_path / "Empty").mkdir()
    (tmp_path / "Game").mkdir()
    _unity(tmp_path / "Game")
    got = sorted(Path(e.path).relative_to(tmp_path).as_posix() for e in library.folder_games([str(tmp_path)]))
    assert got == ["Collection/Part1", "Game", "GodotThing"]
    # добавили саму папку игры
    assert [Path(e.path).name for e in library.folder_games([str(tmp_path / "GodotThing")])] == ["GodotThing"]


def test_find_root_from_inner_or_outer_folder(tmp_path):
    """Вручную выбрали не корень игры: папку внутри (Game_Data, www, game) или папку над игрой."""
    from russificator import library
    u = tmp_path / "Unity Game"
    u.mkdir()
    _unity(u)
    assert library.find_root(u / "Game_Data" / "Managed") == u
    r = tmp_path / "VN"
    r.mkdir()
    _renpy(r)
    assert library.find_root(r / "game") == r
    m = tmp_path / "MV"
    m.mkdir()
    _mv(m)
    assert library.find_root(m / "www" / "data") == m
    lone = tmp_path / "Downloads"
    (lone / "Some Game").mkdir(parents=True)
    _unity(lone / "Some Game")
    assert library.find_root(lone) == lone / "Some Game"      # одна игра внутри — берём её
    assert library.find_root(tmp_path) is None                 # игр несколько — пусть выберет сам
