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


def test_webview_cache_reset_after_update(tmp_path, monkeypatch):
    """Интерфейс открывается по одному адресу — после обновления программы кэш WebView2 сбрасывается,
    иначе показывалась бы прошлая версия страниц."""
    from russificator.ui import app
    storage = tmp_path / "webview"
    cache = storage / "EBWebView" / "Default" / "Cache"
    cache.mkdir(parents=True)
    (cache / "data_0").write_bytes(b"old index.html")
    app._fresh_webview_cache(storage)
    assert not cache.exists() and (storage / "ui-version.txt").is_file()
    cache.mkdir(parents=True)
    (cache / "data_0").write_bytes(b"same version")
    app._fresh_webview_cache(storage)                    # версия та же — кэш не трогаем
    assert (cache / "data_0").is_file()
