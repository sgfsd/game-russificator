"""Настройки: два процесса (окно программы и трей живого перевода) не затирают изменения друг друга."""

from __future__ import annotations

import json

from russificator import paths, settings


def test_save_keeps_changes_of_other_process(tmp_path, monkeypatch):
    monkeypatch.setenv("RUSSIFICATOR_HOME", str(tmp_path))
    paths.set_home(None)
    window = settings.load()                      # окно программы
    tray = settings.load()                        # «другой процесс»
    tray["live_never"] = ["C:/Games/bad.exe"]
    settings.save(tray)
    window["mode"] = "cloud"
    settings.save(window)                         # окно не знает о live_never — но и не затирает
    disk = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert disk["mode"] == "cloud" and disk["live_never"] == ["C:/Games/bad.exe"]
    assert window["live_never"] == ["C:/Games/bad.exe"]          # словарь окна обновился
    window["live_never"] = []                     # своё изменение важнее
    settings.save(window)
    assert settings.read()["live_never"] == []
    assert not list(tmp_path.glob("*.tmp"))
