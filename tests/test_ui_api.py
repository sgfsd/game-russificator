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


def test_closing_window_never_waits_for_the_window(api):
    """Обработчик закрытия окна выполняется в потоке окна: если он (или что-то, что он вызывает) ждёт
    ответа окна (evaluate_js), окно не отвечает никогда — программа «зависала» при закрытии. Теперь
    события уходят в окно отдельным потоком, а закрытие только подаёт сигналы."""
    import threading
    import time
    calls = []
    release = threading.Event()

    class FrozenWindow:                     # как окно, чей поток занят обработчиком закрытия
        def evaluate_js(self, script):
            calls.append(script)
            release.wait(10)

    api._bind(FrozenWindow())
    started = time.monotonic()
    api._emit({"type": "status", "message": "раз", "final": True})
    api._emit({"type": "status", "message": "два", "final": True})
    assert time.monotonic() - started < 0.5                  # отправка события окна не ждёт
    api.begin_shutdown()                                     # stop_live внутри тоже шлёт событие
    assert time.monotonic() - started < 0.5
    api._emit({"type": "status", "message": "после закрытия", "final": True})
    release.set()
    api.finish_shutdown(timeout=5)
    time.sleep(0.2)
    assert not any("после закрытия" in c or "Живой перевод выключен" in c for c in calls)
