"""Проверка обновлений: сравнение версий и кэш результата."""

from __future__ import annotations

import time

from russificator import updates


def test_version_compare():
    assert updates.is_newer("2.1.1", "2.1.0")
    assert updates.is_newer("v3.0", "2.9.9")
    assert updates.is_newer("2.10.0", "2.9.0")
    assert not updates.is_newer("2.1.0", "2.1.0")
    assert not updates.is_newer("2.0", "2.0.0")
    assert not updates.is_newer("", "2.0.0")


def test_check_caches_and_handles_offline(monkeypatch):
    calls = []

    def fake_latest(timeout=8):
        calls.append(1)
        return {"version": "2.2.0", "url": "https://example/releases/v2.2.0"}
    monkeypatch.setattr(updates, "latest_release", fake_latest)
    cfg = {"check_updates": True}
    info = updates.check(cfg, "2.1.0")
    assert info["available"] and info["version"] == "2.2.0" and len(calls) == 1
    assert updates.check(cfg, "2.1.0")["available"] and len(calls) == 1      # 12 часов — из кэша
    assert not updates.check(cfg, "2.2.0")["available"]

    def offline(timeout=8):
        raise OSError("нет сети")
    monkeypatch.setattr(updates, "latest_release", offline)
    info = updates.check(cfg, "2.1.0", force=True)
    assert info["available"] and info.get("error")                         # без сети — прошлый результат
    cfg2 = {"check_updates": False, "update_checked_at": time.time() - 10 ** 6}
    assert not updates.check(cfg2, "2.1.0")["available"]                    # выключено — не спрашиваем
