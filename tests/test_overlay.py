"""Оверлей «Живой перевод»: логика, не зависящая от Windows (текст, устойчивость, отрисовка, выбор игры)."""

from __future__ import annotations

import os

import pytest

from russificator.overlay import detect, text
from russificator.overlay.text import Line, Tracker, group_lines, is_foreign, join_lines


def test_is_foreign():
    assert is_foreign("Press any key to continue")
    assert is_foreign("Welcome back, traveler!")
    assert not is_foreign("Нажмите любую клавишу")        # уже русский (наш перевод или русская игра)
    assert not is_foreign("100/100")
    assert not is_foreign("HP 100/100")
    assert not is_foreign("")
    assert is_foreign("Save")


def test_join_lines_hyphenation():
    assert join_lines(["This is an exam-", "ple of text"]) == "This is an example of text"
    assert join_lines(["Well-", "Known"]) == "Well- Known"


def test_group_lines_into_paragraphs():
    lines = [
        Line("Hello there, traveler! This village", 100, 500, 400, 24),
        Line("has been quiet for a long time.", 100, 528, 360, 24),
        Line("HP 100", 20, 20, 60, 18),                   # далеко — отдельный блок
        Line("Continue", 800, 300, 90, 30),
        Line("Options", 805, 345, 80, 30),                 # меню по центру — один блок
    ]
    blocks = group_lines(lines)
    texts = sorted(b.text for b in blocks)
    assert "Hello there, traveler! This village has been quiet for a long time." in texts
    assert "HP 100" in texts
    assert "Continue Options" in texts
    para = next(b for b in blocks if b.text.startswith("Hello"))
    assert para.rect == (100, 500, 400, 52)


def test_tracker_waits_for_typewriter_and_keeps_translation():
    tr = Tracker(need=2)
    t = 0.0

    def frame(s):
        nonlocal t
        t += 0.5
        return tr.update(group_lines([Line(s, 100, 500, 300, 24)]), now=t)

    frame("Hello the")
    assert not tr.ready()                                   # текст печатается
    frame("Hello there, trav")
    assert not tr.ready()
    frame("Hello there, traveler!")
    assert not tr.ready()
    frame("Hello there, traveler!")
    ready = tr.ready()
    assert [b.text for b in ready] == ["Hello there, traveler!"]
    ready[0].translation = "Привет, путник!"
    frame("Hel1o there, traveler!")                         # шум распознавания — перевод тот же
    assert tr.blocks[0].translation == "Привет, путник!"
    assert not tr.ready()


def test_tracker_forgets_vanished_blocks():
    tr = Tracker(need=1, forget_after=1.0)
    blocks = tr.update(group_lines([Line("Quest updated", 10, 10, 200, 20)]), now=0)
    blocks[0].translation = "Задание обновлено"
    assert len(tr.update([], now=0.5)) == 1                 # мигнул — держим
    assert tr.update([], now=2.0) == []                     # ушёл — убираем


def test_translation_cache_fuzzy():
    c = text.TranslationCache()
    c.put(text.normalize("Welcome back, traveler!"), "С возвращением, путник!")
    assert c.get(text.normalize("Welcome back, traveIer!")) == "С возвращением, путник!"
    assert c.get(text.normalize("Something else entirely")) is None


def test_render_fits_translation_into_box():
    pytest.importorskip("PIL")
    from russificator.overlay import render
    item = render.Item(rect=(100, 500, 300, 24), text="Добро пожаловать обратно, путник! Деревня давно стоит тихой.",
                       line_h=24)
    (bx, by, bw, bh), fnt, lines, size, pad = render.layout(item, (1280, 720), render.Style())
    assert bx <= 100 and bw >= 300 and len(lines) >= 2 and size >= 11
    img = render.render((1280, 720), [item])
    assert img.size == (1280, 720)
    assert img.getpixel((5, 5))[3] == 0                     # вне плашки — прозрачно
    assert img.getpixel((bx + 2, by + bh // 2))[3] > 150    # плашка
    raw = render.to_bgra_premultiplied(img)
    assert len(raw) == 1280 * 720 * 4
    # премультипликация: у прозрачных точек цвет нулевой
    assert raw[:4] == b"\0\0\0\0"


def test_decider_rules(tmp_path):
    games = str(tmp_path / "SteamLibrary" / "common")
    dec = detect.Decider(game_dirs=[games], always=[os.path.join("C:", "Tools", "vn.exe")],
                         never=[os.path.join(games, "Bad", "bad.exe")], gamebar={detect._norm("/x/gb.exe")})
    assert dec.decide(os.path.join(games, "Hollow", "hollow.exe"), False) == (True, "игра из «Моих игр»")
    assert dec.decide(os.path.join(games, "Bad", "bad.exe"), True)[0] is False
    assert dec.decide(os.path.join("C:", "Tools", "vn.exe"), False)[0] is True
    assert dec.decide("/x/gb.exe", False) == (True, "Windows считает это игрой")
    assert dec.decide("/apps/chrome.exe", True)[0] is False          # браузер во весь экран — не игра
    assert dec.decide("/apps/unknown.exe", True) == (True, "полноэкранное окно")
    assert dec.decide("/apps/unknown.exe", False)[0] is False
    assert dec.game_dir(os.path.join(games, "Hollow", "hollow.exe")) == detect._norm(games)


class _FakeGui:
    excluded_from_capture = True

    def __init__(self):
        self.frames, self.hidden, self.keys, self.tips = [], 0, [], []

    def show_frame(self, x, y, w, h, data):
        self.frames.append((x, y, w, h, len(data)))

    def hide(self):
        self.hidden += 1

    def set_hotkeys(self, keys):
        self.keys = [k.name for k in keys]

    def set_tip(self, tip):
        self.tips.append(tip)

    def balloon(self, *a):
        pass


class _FakeOcr:
    name = "fake"
    lang = "en-US"

    def __init__(self, lines):
        self.lines = lines

    def recognize(self, w, h, bgra):
        return list(self.lines)


class _FakeTranslator:
    cache_id = "fake:live"

    def translate(self, entries, glossary):
        return {e.id: "RU " + e.source for e in entries}

    def close(self):
        pass


def test_live_service_pipeline(tmp_path, monkeypatch):
    """Кадр игры → распознанные строки → перевод в фоне → плашки поверх игры (Windows подменён)."""
    pytest.importorskip("PIL")
    monkeypatch.setenv("RUSSIFICATOR_HOME", str(tmp_path / "home"))
    from russificator import paths
    paths.set_home(None)
    from russificator.overlay import service, win32
    game_exe = os.path.join(str(tmp_path), "Games", "Hollow", "hollow.exe")
    fg = win32.WindowInfo(hwnd=100, pid=4242, exe=game_exe, title="Hollow Knight", client=(0, 0, 1280, 720),
                          window=(0, 0, 1280, 720), monitor=(0, 0, 1280, 720), minimized=False)
    frame = [bytes(1280 * 720 * 4)]
    monkeypatch.setattr(win32, "foreground", lambda: fg)
    monkeypatch.setattr(win32, "capture", lambda x, y, w, h: frame[0])
    svc = service.LiveService(quiet=True)
    svc.gui = _FakeGui()
    svc.ocr = _FakeOcr([text.Line("Press any key to continue", 500, 600, 280, 24), text.Line("100/100", 10, 10, 60, 18)])
    svc.translator, svc._reload_translator = _FakeTranslator(), False
    import threading
    threading.Thread(target=svc._translate_loop, daemon=True).start()
    try:
        svc._tick()                                          # кадр 1: блок появился
        assert svc.target is not None and svc.target_reason == "полноэкранное окно"
        assert "toggle" in svc.gui.keys                      # в игре без XUnity Alt+T — наш
        frame[0] = bytes([1]) * (1280 * 720 * 4)             # кадр изменился (анимация), текст тот же
        svc._tick()                                          # кадр 2: текст устойчив — на перевод
        import time
        for _ in range(50):
            if svc.count:
                break
            time.sleep(0.02)
        svc._tick()
        assert svc.count == 1 and svc.recent[0]["tr"] == "RU Press any key to continue"
        assert svc.gui.frames and svc.gui.frames[-1][:4] == (0, 0, 1280, 720)
        st = svc.status()
        assert st["game"]["title"] == "Hollow Knight" and st["ocr"]["ok"] and st["count"] == 1
        # «не игра» (браузер) — оверлей прячется
        fg2 = win32.WindowInfo(hwnd=5, pid=77, exe="C:/Program Files/Google/chrome.exe", title="YouTube",
                               client=(0, 0, 1280, 720), window=(0, 0, 1280, 720), monitor=(0, 0, 1280, 720),
                               minimized=False)
        monkeypatch.setattr(win32, "foreground", lambda: fg2)
        svc._tick()
        assert svc.target is None and svc.gui.hidden >= 1 and svc.last_game.title == "Hollow Knight"
        assert svc.gui.keys == []                           # вне игры Alt+… не перехватываются
        assert svc.candidate is None                         # браузер «Переводить окно» не предлагается
        # игра в окне, которую ничего не выдало — её можно включить из трея или окна программы
        fg3 = win32.WindowInfo(hwnd=9, pid=99, exe="D:/Indie/tiny.exe", title="Tiny Quest",
                               client=(100, 100, 800, 600), window=(90, 70, 910, 710), monitor=(0, 0, 1920, 1080),
                               minimized=False)
        monkeypatch.setattr(win32, "foreground", lambda: fg3)
        svc._tick()
        assert svc.target is None and svc.status()["candidate"]["title"] == "Tiny Quest"
        assert any(cmd == "mark" and "Tiny Quest" in title for cmd, title, _ in svc._menu_items())
        assert svc.mark_current("always", "candidate") == "D:/Indie/tiny.exe"
        svc._tick()
        assert svc.target is not None and svc.target.title == "Tiny Quest" and svc.candidate is None
        # «Не переводить эту игру» из окна программы
        assert svc.mark_current("never") == "D:/Indie/tiny.exe"
        assert svc.target is None and svc.last_game is None
    finally:
        svc.stop_event.set()
        with svc._cond:
            svc._cond.notify_all()


def test_live_region_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("RUSSIFICATOR_HOME", str(tmp_path / "home"))
    from russificator import paths
    paths.set_home(None)
    from russificator.overlay import service, win32
    fg = win32.WindowInfo(hwnd=1, pid=2, exe="/g/game.exe", title="G", client=(100, 50, 1000, 500),
                          window=(0, 0, 1, 1), monitor=(0, 0, 1920, 1080), minimized=False)
    svc = service.LiveService(quiet=True)
    svc.gui = _FakeGui()
    svc._region_target = fg
    svc._on_region((100, 250, 800, 200))                     # обвели окно диалога внизу
    assert svc._region(fg) == (200, 300, 800, 200)
    from russificator import settings
    assert settings.load()["live_profiles"][detect._norm("/g/game.exe")]["region"] == [0.1, 0.5, 0.8, 0.4]
    svc._on_region(None)                                     # Esc — область сброшена
    assert svc._region(fg) == (100, 50, 1000, 500)


def test_live_black_frame_hint(tmp_path, monkeypatch):
    """Эксклюзивный полноэкранный режим: кадр всегда чёрный — подсказка переключить режим экрана."""
    monkeypatch.setenv("RUSSIFICATOR_HOME", str(tmp_path / "home"))
    from russificator import paths
    paths.set_home(None)
    from russificator.overlay import service, win32
    fg = win32.WindowInfo(hwnd=100, pid=4242, exe="C:/Games/X/x.exe", title="X", client=(0, 0, 640, 360),
                          window=(0, 0, 640, 360), monitor=(0, 0, 640, 360), minimized=False)
    frame = [bytes(640 * 360 * 4)]
    monkeypatch.setattr(win32, "foreground", lambda: fg)
    monkeypatch.setattr(win32, "capture", lambda x, y, w, h: frame[0])
    svc = service.LiveService(quiet=True)
    svc.gui = _FakeGui()
    svc.ocr = _FakeOcr([])
    for _ in range(service.BLACK_FRAMES):
        svc._tick()
    assert "Окно без рамки" in svc.status()["hint"]
    pic = bytearray(frame[0])
    pic[640 * 4 * 180 + 64 * 5] = 200                       # появилась картинка
    frame[0] = bytes(pic)
    svc._tick()
    assert svc.status()["hint"] == ""


def test_live_ocr_downscale_for_huge_frames(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    monkeypatch.setenv("RUSSIFICATOR_HOME", str(tmp_path / "home"))
    from russificator import paths
    paths.set_home(None)
    from russificator.overlay import service
    seen = []

    class Small(_FakeOcr):
        max_dim = 1000

        def recognize(self, w, h, bgra):
            seen.append((w, h, len(bgra)))
            return [text.Line("Hello", 100, 50, 200, 20)]
    svc = service.LiveService(quiet=True)
    svc.ocr = Small([])
    lines = svc._recognize(2000, 1000, bytes(2000 * 1000 * 4))
    assert seen == [(1000, 500, 1000 * 500 * 4)]
    assert (lines[0].x, lines[0].y, lines[0].w, lines[0].h) == (200, 100, 400, 40)
