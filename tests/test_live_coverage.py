"""Живой перевод «переводит всё»: русский текст не трогается, надписи на картинках находятся глубоким
проходом, ошибки распознавания исправляются, кадр снимается с окна игры (строки — из настоящих игр)."""

from __future__ import annotations

import pytest

from russificator.overlay import detect, text
from russificator.overlay.text import Line, Tracker, group_lines

# Как английское распознавание Windows прочитало русский текст (Lily's Well, A Game About Literally Doing
# Your Taxes после русификации и собственные плашки перевода).
MISREAD = ["HaqaTb", "K0H$nrypau", "110KPOBhTen", "3T0 paK0BhHa", "fi BblKVlHyn g P", "Hanor0Bble Z -..«napa",
           "EP>KVITE HAnor", "HCChTb HAYUJHHKM", "3HAK", "3K3AXE6XO"]
# Настоящий английский текст с ошибками распознавания пиксельного шрифта — должен переводиться.
ENGLISH = ["This same contains", "violent and sraphic", "content not sui table", "for chi Idren.",
           "Player discretion", "is hiShlg advised.", "Lily's Well", "DAILY", "SIGN", "EXIT", "TAXES", "New Game",
           "Press any key to continue", "A Game About Literally Doing Your Taxes", "Wear headphones", "HP 100",
           "Level 2", "x2 Speed", "MP3 Player"]


def test_misread_cyrillic_heuristics():
    for s in MISREAD:
        assert text.misread_cyrillic(s), s
    for s in ENGLISH:
        assert not text.misread_cyrillic(s), s
    words = {"this", "same", "contains", "violent", "and", "content", "not", "table", "player", "discretion",
             "advised", "for", "press", "any", "key", "to", "continue", "new", "game", "daily", "sign", "exit"}
    assert text.misread_cyrillic("Hanor0Bble napa caenan", words)      # приметы и ни одного английского слова
    for s in ENGLISH:
        assert not text.misread_cyrillic(s, words), s


def test_looks_russian_ignores_latin_lookalikes():
    assert text.looks_russian("Это раковина")
    assert text.looks_russian("Я сделал 10 Налогов")
    assert text.looks_russian("Да")
    assert not text.looks_russian("ЕХ'Т")                              # так русское распознавание читает «EXIT»
    assert not text.looks_russian("This эате contains")                 # английский с одной ошибкой
    assert not text.looks_russian("DAILY")
    assert text.cyrillic_share("abc где") == 0.5


def test_words_fix_ocr_errors():
    from russificator.overlay.words import Words
    w = Words({k: i for i, k in enumerate(["the", "game", "contains", "violent", "and", "graphic", "content", "not",
                                           "suitable", "for", "children", "player", "discretion", "is", "highly",
                                           "advised", "version", "play"])})
    assert w.fix("violent and sraphic") == "violent and graphic"
    assert w.fix("content not sui table") == "content not suitable"
    assert w.fix("for chi Idren.") == "for children."
    assert w.fix("is hiShlg advised.") == "is highly advised."
    assert w.fix("PIAY") == "PLAY"
    assert w.fix("Player discretion") == "Player discretion"          # верное — не трогаем
    assert w.fix("Zorblax") == "Zorblax"                              # незнакомое и без пары — тоже


def test_merge_deep_needs_two_votes():
    base = [Line("DAILY", 417, 93, 149, 31)]
    variants = [
        base,
        [Line("DAILY", 417, 93, 149, 31), Line("TAXES", 430, 150, 280, 60), Line("wra", 3, 85, 76, 30)],
        [Line("TAXES", 431, 151, 279, 59), Line("SIGN HERE", 535, 652, 180, 22)],
        [Line("slGN HERE", 536, 652, 178, 22), Line("zoom", 700, 400, 60, 20)],
    ]
    found = text.merge_deep(base, variants)
    assert sorted(ln.text for ln in found) == ["SIGN HERE", "TAXES"]    # шум из одного варианта отброшен
    # быстрый проход видел только часть надписи — берём полное прочтение
    found = text.merge_deep([Line("DOING", 600, 348, 120, 48)],
                            [[Line("LITERALLY DOING", 330, 348, 620, 48)],
                             [Line("LITERALLY DOING", 331, 349, 619, 47)]])
    assert [ln.text for ln in found] == ["LITERALLY DOING"]


def test_tracker_holds_deep_blocks_and_covered_areas():
    tr = Tracker(need=2, forget_after=1.0)
    tr.update(group_lines([Line("Continue", 100, 100, 120, 24)]), now=0)
    added = tr.add(group_lines([Line("PLAY", 300, 500, 200, 60)]), now=0.1, hold=4.0)
    assert [b.text for b in added] == ["PLAY"] and added[0].deep
    assert [b.text for b in tr.ready()] == ["PLAY"]                     # статичный кадр — сразу на перевод
    added[0].translation = "ИГРАТЬ"
    tr.update(group_lines([Line("Continue", 100, 100, 120, 24)]), now=2.0)
    assert any(b.text == "PLAY" for b in tr.blocks)                     # быстрый проход не видит — держим
    tr.update(group_lines([Line("Continue", 100, 100, 120, 24)]), now=5.0)
    assert not any(b.text == "PLAY" for b in tr.blocks)                 # срок вышел
    # под нашей плашкой (снимок экрана) текст не виден, но блок остаётся
    tr2 = Tracker(need=1, forget_after=1.0)
    b = tr2.update(group_lines([Line("Quest updated", 10, 10, 200, 20)]), now=0)[0]
    b.translation = "Задание обновлено"
    assert len(tr2.update([], now=5.0, covered=[(0, 0, 300, 60)])) == 1
    # смена сцены — пропавшее убирается сразу
    assert tr2.update([], now=5.1, scene_cut=True) == []


def test_decider_finds_windowed_games_by_engine(tmp_path):
    unity = tmp_path / "Indie" / "Tiny"
    (unity / "Tiny_Data").mkdir(parents=True)
    (unity / "UnityPlayer.dll").write_bytes(b"")
    rpg = tmp_path / "Indie" / "Rpg"
    (rpg / "www" / "data").mkdir(parents=True)
    (rpg / "www" / "js").mkdir(parents=True)
    (rpg / "www" / "js" / "rpg_core.js").write_text("", encoding="utf-8")
    russ = tmp_path / "Indie" / "Done"
    (russ / "russificator_backup").mkdir(parents=True)
    tool = tmp_path / "Tools" / "App"
    tool.mkdir(parents=True)
    dec = detect.Decider()
    assert dec.decide(str(unity / "Tiny.exe"), False) == (True, "игра на Unity")
    assert dec.decide(str(rpg / "Game.exe"), False) == (True, "игра на RPG Maker")
    assert dec.decide(str(russ / "bin" / "Game.exe"), False) == (True, "игра, русифицированная программой")
    assert dec.decide(str(tool / "app.exe"), False) == (False, detect.REASON_WINDOW)
    assert detect.Decider(by_engine=False).decide(str(unity / "Tiny.exe"), False)[0] is False


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


def _game(title="Game", w=640, h=360):
    from russificator.overlay import win32
    return win32.WindowInfo(hwnd=11, pid=4242, exe="C:/G/g.exe", title=title, client=(0, 0, w, h),
                            window=(0, 0, w, h), monitor=(0, 0, w, h), minimized=False)


def _svc(tmp_path, monkeypatch, frame, ocr_lines, ru_lines=None, title="Game"):
    monkeypatch.setenv("RUSSIFICATOR_HOME", str(tmp_path / "home"))
    from russificator import paths
    paths.set_home(None)
    from russificator.overlay import service, win32
    fg = _game(title)
    monkeypatch.setattr(win32, "foreground", lambda: fg)
    monkeypatch.setattr(win32, "capture", lambda x, y, w, h: frame[0])
    monkeypatch.setattr(win32, "capture_window", lambda hwnd, cw, ch, region: frame[0])
    svc = service.LiveService(quiet=True)
    svc.gui = _FakeGui()
    svc.ocr = _FakeOcr(ocr_lines)
    svc.ocr_ru = _FakeOcr(ru_lines) if ru_lines is not None else None
    svc.cfg["live_deep"] = False
    svc.cfg["live_always"] = [fg.exe]
    return svc


def test_live_skips_russian_text_with_ru_verifier(tmp_path, monkeypatch):
    """Русский текст в игре английское распознавание читает как «HaqaT», русское — как «Начать»:
    такая строка не переводится, английская рядом — переводится."""
    frame = [bytes([90]) * (640 * 360 * 4)]
    # русское распознавание получает область вокруг строк-кандидатов (с полем 12 пикселей):
    # её начало — (88, 88), координаты его строк — от этого угла
    svc = _svc(tmp_path, monkeypatch, frame,
               [Line("HaqaT", 100, 100, 80, 20), Line("Options menu", 100, 200, 140, 20)],
               [Line("Начать", 12, 12, 80, 20), Line("Options menu", 12, 112, 140, 20)])
    svc._tick()
    svc._tick()
    assert [b.text for b in svc.tracker.blocks] == ["Options menu"]


def test_live_skips_caption_and_useless_translations(tmp_path, monkeypatch):
    frame = [bytes([90]) * (640 * 360 * 4)]
    svc = _svc(tmp_path, monkeypatch, frame, [Line("Lily's well", 26, 10, 50, 13), Line("Zorblax", 100, 200, 90, 20)],
               title="Lily's Well")
    svc._tick()
    svc._tick()
    assert [b.text for b in svc.tracker.blocks] == ["Zorblax"]          # заголовок окна не переводим
    b = svc.tracker.blocks[0]
    svc._done(b, "Zorblax")                                              # перевод совпал с оригиналом
    assert b.skip and svc.count == 0
    svc._show(0, 0, 640, 360)
    assert svc.gui.frames == []                                          # плашки нет
    svc._queue.clear()
    svc._pending.clear()
    svc._tick()
    assert not svc.tracker.ready() and not svc._queue                    # и на перевод больше не уходит


def test_live_capture_falls_back_to_screen_and_masks_plates(tmp_path, monkeypatch):
    """Окно игры не снимается (PrintWindow): кадр берётся с экрана, а наши плашки в нём закрашиваются."""
    from russificator.overlay import win32
    frame = [bytes([200]) * (640 * 360 * 4)]
    svc = _svc(tmp_path, monkeypatch, frame, [Line("Press any key", 100, 100, 160, 24)])
    monkeypatch.setattr(win32, "capture_window", lambda hwnd, cw, ch, region: None)
    svc.gui.excluded_from_capture = False
    svc._plates = [(90, 90, 200, 50)]
    out = svc._capture(_game(), 0, 0, 640, 360)
    assert svc.captured_by == "screen" and svc._capture_mode == "screen"
    stride = 640 * 4
    assert out[100 * stride + 150 * 4] == 0 and out[10 * stride + 10 * 4] == 200
    assert svc._covered() == [(90, 90, 200, 50)]


def test_live_window_capture_preferred(tmp_path, monkeypatch):
    """Кадр с окна игры есть — экран не снимается вовсе (в нём могли бы быть наши плашки)."""
    from russificator.overlay import win32
    frame = [bytes([120]) * (640 * 360 * 4)]
    svc = _svc(tmp_path, monkeypatch, frame, [])

    def no_screen(*a):
        raise AssertionError("снимок экрана не нужен")
    monkeypatch.setattr(win32, "capture", no_screen)
    assert svc._capture(_game(), 0, 0, 640, 360) == frame[0] and svc.captured_by == "window"


def test_live_deep_scan_finds_texture_text(tmp_path, monkeypatch):
    """Глубокий проход: надпись, которую видят только варианты кадра (контраст, каналы), добавляется."""
    pytest.importorskip("numpy")
    colored = bytes([10, 120, 240, 255])

    class DeepOcr(_FakeOcr):
        def recognize(self, w, h, bgra):
            if bgra[:4] == colored:                                     # исходный кадр — надписи не видно
                return [Line("Continue", 20, 20, 120, 24)]
            return [Line("Continue", 20, 20, 120, 24), Line("SIGN HERE", 300, 250, 160, 22)]
    px = bytearray(colored * (640 * 360))
    for i in range(4, len(px) // 2, 4):                                 # две половины разного цвета — есть контраст
        px[i:i + 4] = bytes([200, 30, 60, 255])
    frame = [bytes(px)]
    svc = _svc(tmp_path, monkeypatch, frame, [])
    svc.ocr = DeepOcr([])
    svc.cfg["live_deep"] = True
    svc._tick()                                                          # быстрый проход + глубокий (по времени)
    assert any(b.text == "SIGN HERE" and b.deep for b in svc.tracker.blocks)
    assert svc.deep_found >= 1 and svc.status()["deep"]["found"] >= 1


def test_live_drops_partial_logos_junk_and_brands(tmp_path, monkeypatch):
    """Обрывок стилизованного логотипа, мусор распознавания и заставка движка не переводятся."""
    frame = [bytes([90]) * (1280 * 720 * 4)]
    svc = _svc(tmp_path, monkeypatch, frame, [])
    from russificator.overlay import words
    svc.words = words.Words({w: i for i, w in enumerate(["about", "literally", "doing", "your", "taxes", "sign",
                                                          "here", "exit"])})
    lines = [Line("ABOUT )OOmG", 500, 280, 400, 48), Line("Made with", 560, 300, 160, 30), Line("unity", 580, 340, 120, 40),
             Line("EXIT", 105, 679, 85, 35)]
    kept = svc._foreign(lines, _game(w=1280, h=720), frame[0], 0, 0, 1280, 720)
    assert [ln.text for ln in kept] == ["EXIT"]
    assert text.without_junk("Hello )OOmG world") == "Hello world"


def test_live_does_not_translate_game_title_logo(tmp_path, monkeypatch):
    """Крупные слова из названия игры (логотип на титульном экране) не переводятся, кнопки рядом — да."""
    from russificator.overlay import win32
    frame = [bytes([90]) * (1280 * 720 * 4)]
    svc = _svc(tmp_path, monkeypatch, frame, [])
    fg = win32.WindowInfo(hwnd=11, pid=1, exe="D:/g/A Game About Literally Doing Your Taxes.exe",
                          title="A Game About Literally Doing Your Taxes", client=(0, 0, 1280, 720),
                          window=(0, 0, 1280, 720), monitor=(0, 0, 1280, 720), minimized=False)
    lines = [Line("ABOUT", 672, 285, 236, 47), Line("EXIT", 105, 679, 85, 35),
             Line("Your taxes are due", 100, 100, 300, 20), Line("About", 100, 400, 80, 22)]
    kept = svc._foreign(lines, fg, frame[0], 0, 0, 1280, 720)
    assert [ln.text for ln in kept] == ["EXIT", "Your taxes are due", "About"]
    # логотип с ошибкой распознавания в букве — тоже название
    fg2 = win32.WindowInfo(hwnd=12, pid=1, exe="D:/g/BLOODMONEY!.exe", title="BLOODMONEY!", client=(0, 0, 1280, 751),
                           window=(0, 0, 1280, 751), monitor=(0, 0, 1280, 751), minimized=False)
    kept = svc._foreign([Line("SLOODMONEY!", 250, 150, 700, 90), Line("NEW GAME", 220, 520, 390, 60)], fg2, frame[0],
                        0, 0, 1280, 720)
    assert [ln.text for ln in kept] == ["NEW GAME"]


def test_live_skips_whole_title_and_gibberish(tmp_path, monkeypatch):
    """«RESIDENT LOVER» (логотип обычного размера) — название; «fang:unc bv team avia» — абракадабра."""
    from russificator.overlay import win32, words
    frame = [bytes([90]) * (1920 * 1080 * 4)]
    svc = _svc(tmp_path, monkeypatch, frame, [])
    svc.words = words.Words({w: i for i, w in enumerate(["team", "game", "by", "press", "start", "new", "load"])})
    fg = win32.WindowInfo(hwnd=11, pid=1, exe="D:/g/Resident_Lover.exe", title="Resident Lover",
                          client=(0, 0, 1920, 1080), window=(0, 0, 1920, 1080), monitor=(0, 0, 1920, 1080),
                          minimized=False)
    lines = [Line("RESIDENT LOVER", 700, 300, 520, 40), Line("fang:unc bv team avia", 700, 600, 400, 30),
             Line("Press start", 800, 900, 200, 30), Line("Lover", 100, 100, 80, 20)]
    kept = svc._foreign(lines, fg, frame[0], 0, 0, 1920, 1080)
    assert [ln.text for ln in kept] == ["Press start", "Lover"]
    assert not text.gibberish("Ethan Grimwald and Lucy Vane", svc.words.ranks)     # имена — не мусор
