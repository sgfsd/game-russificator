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


def test_bug_report_has_facts_and_no_secrets(api, tmp_path, monkeypatch):
    """«Скопировать отчёт об ошибке»: в тексте версия, способ перевода, игра с движком и хвост журнала;
    ключей API (и запомненных, и просто похожих на ключ в журнале) и домашней папки в нём нет."""
    import russificator
    home = tmp_path / "Users" / "Вася Пупкин"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP-VASYA42")
    monkeypatch.setattr(type(api), "_live_call", lambda self, path, timeout=0.6: None)
    vn = _renpy(home / "Games" / "My VN")
    assert api.detect(str(vn))["ok"]
    remembered, session = "sk-remembered0123456789abcdef", "my-own-proxy-key-XYZ"
    api.save_settings({"mode": "cloud", "cloud_provider": "deepseek", "cloud_model": "deepseek-chat"})
    api.set_api_key("deepseek", remembered, remember=True)
    api.set_api_key("custom", session, remember=False)
    log = paths.logs_dir() / "russificator.log"
    lines = [f"2026-10-17 12:00:{i % 60:02d} INFO russificator: строка {i}" for i in range(200)]
    lines += [f"ERROR cloud: 401 for key {remembered} and {session}",
              "ERROR cloud: headers {'Authorization': 'Bearer sk-or-v1-0123456789abcdef0123'}",
              "WARNING google: ?key=AIzaSyA0123456789abcdefghijklmnopqrstu failed",
              f"INFO game: {vn}\\game\\script.rpy (DESKTOP-VASYA42, vasya@mail.ru)",
              "INFO tail: ``` в журнале"]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (paths.logs_dir() / "russificator.prev.log").write_text("прошлый запуск\n" * 200, encoding="utf-8")

    text = api.bug_report()["text"]

    assert f"**Версия программы:** {russificator.__version__}" in text
    assert "**Windows:**" in text and "**ОЗУ:**" in text and "**Видеокарта:**" in text
    assert "DeepSeek" in text and "deepseek-chat" in text and "**Модель установлена:** да" in text
    assert "**Название:** My VN" in text and "**Движок:** Ren'Py" in text
    assert "строка 199" in text and "строка 119" not in text           # последние 80 строк, не весь файл
    assert "russificator.prev.log): есть" in text
    for secret in (remembered, session, "sk-or-v1-0123456789abcdef0123", "AIzaSyA0123456789",
                   "Вася Пупкин", str(home), "DESKTOP-VASYA42", "vasya@mail.ru"):
        assert secret not in text, secret
    assert "%USERPROFILE%" in text and "<ключ удалён>" in text and "%COMPUTERNAME%" in text
    assert "Bearer <ключ удалён>" in text                                # видно, что ключ был, но не какой
    assert "\n````text\n" in text                                        # ``` в журнале не ломает блок


def _unity(root: Path) -> Path:
    (root / "Game_Data" / "Managed").mkdir(parents=True)
    (root / "Game_Data" / "globalgamemanagers").write_bytes(b"x")
    (root / "Game.exe").write_bytes(b"MZ")
    (root / "UnityPlayer.dll").write_bytes(b"MZ")
    return root


def test_library_status_is_read_from_disk_not_cache(api, tmp_path):
    """«На русском» из кэша не держится: игру переустановили/откатили в обход программы — список
    показывает правду; папки, которой больше нет, в списке нет."""
    import shutil
    from russificator.core.backup import GameBackup
    game = _unity(tmp_path / "Steam" / "Crawl")
    gone = _unity(tmp_path / "Steam" / "Gone")
    api.library_add(str(game))
    api.library_add(str(gone))
    b = GameBackup(game)
    b.write_text(game / "Game_Data" / "ru.txt", "русский")
    b.snapshot({"translator": "machine"})
    api._library.refresh_status(str(game))
    assert next(g for g in api.library()["games"] if g["title"] == "Crawl")["russified"] is True
    shutil.rmtree(game)                                   # «переустановили» игру — бэкапа больше нет
    _unity(game)
    shutil.rmtree(gone)
    games = api.library()["games"]
    assert [g["title"] for g in games] == ["Crawl"] and games[0]["russified"] is False
    assert api._library.games[0]["russified"] is False      # и в кэше тоже


def test_library_hidden_games_can_be_returned(api, tmp_path):
    """Скрытая игра не пропадает: она во вкладке «Скрытые» и возвращается одной кнопкой — даже
    добавленная вручную (раньше её папку приходилось искать заново)."""
    game = _unity(tmp_path / "Games" / "Manual")
    api.library_add(str(game))
    api.library_hide(str(game).upper())                   # путь в другом регистре — та же папка
    g = api.library()["games"][0]
    assert g["hidden"] is True and api._cfg["library_manual"]
    api.library_unhide(str(game))
    assert api.library()["games"][0]["hidden"] is False and not api._cfg["library_hidden"]
    api.library_hide(str(game))
    api.library_forget(str(game))
    assert api.library()["games"] == [] and not api._cfg["library_manual"]


def test_library_custom_cover(api, tmp_path, monkeypatch):
    """Своя обложка: картинка кадрируется под карточку, хранится копией и приходит data-URL."""
    import base64
    import io
    import sys
    import types
    from PIL import Image
    game = _unity(tmp_path / "Games" / "G")
    api.library_add(str(game))
    pic = tmp_path / "cover.png"
    Image.new("RGB", (1000, 1000), (200, 30, 30)).save(pic)
    fake = types.SimpleNamespace(FileDialog=types.SimpleNamespace(OPEN=10))
    monkeypatch.setitem(sys.modules, "webview", fake)
    api._window = types.SimpleNamespace(create_file_dialog=lambda *a, **k: [str(pic)])
    r = api.library_cover(str(game))
    assert r["ok"] and r["cover"].startswith("data:image/jpeg;base64,")
    im = Image.open(io.BytesIO(base64.b64decode(r["cover"].split(",", 1)[1])))
    assert abs(im.width / im.height - 460 / 215) < 0.01 and im.width <= 920
    g = api.library()["games"][0]
    assert g["cover_custom"] and g["cover"] == r["cover"]
    assert api.library_cover(str(game), reset=True)["ok"]
    g = api.library()["games"][0]
    assert not g.get("cover_custom") and not g["cover"]
    (tmp_path / "bad.png").write_bytes(b"not an image")
    api._window = types.SimpleNamespace(create_file_dialog=lambda *a, **k: [str(tmp_path / "bad.png")])
    r = api.library_cover(str(game))
    assert not r["ok"] and "картинку" in r["error"]
