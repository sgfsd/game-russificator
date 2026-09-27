"""Окно программы (Api без окна): выбор папки игры вручную прощает неточный выбор."""

from __future__ import annotations

from pathlib import Path

import pytest

from russificator import paths


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("RUSSIFICATOR_HOME", str(tmp_path / "home"))
    paths.set_home(None)
    from russificator.ui.app import Api
    return Api()


def _renpy(root: Path) -> Path:
    (root / "game").mkdir(parents=True)
    (root / "renpy").mkdir()
    (root / "game" / "script.rpy").write_text('label start:\n    "Hello there."\n', encoding="utf-8")
    (root / "VN.exe").write_bytes(b"MZ")
    return root


def test_detect_takes_game_root(api, tmp_path):
    vn = _renpy(tmp_path / "Library" / "My VN")
    r = api.detect(str(vn / "game"))                         # выбрали папку game внутри
    assert r["ok"] and Path(r["path"]) == vn and r["moved_from"]
    r = api.detect(str(tmp_path / "Library"))                # выбрали папку над игрой
    assert r["ok"] and Path(r["path"]) == vn
    r = api.detect(str(vn))                                  # корень — как есть
    assert r["ok"] and Path(r["path"]) == vn and not r["moved_from"]
    (tmp_path / "Nothing").mkdir()
    r = api.detect(str(tmp_path / "Nothing"))
    assert not r["ok"] and "корень игры" in r["error"]


def test_library_add_takes_game_root(api, tmp_path):
    vn = _renpy(tmp_path / "Games" / "VN")
    r = api.library_add(str(vn / "game"))
    assert r["ok"] and Path(r["game"]["path"]) == vn
