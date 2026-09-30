"""Живой перевод «переводит всё»: русский текст не трогается, надписи на картинках находятся глубоким
проходом, ошибки распознавания исправляются, кадр снимается с окна игры (строки — из настоящих игр)."""

from __future__ import annotations

import pytest

from russificator.overlay import detect, text
from russificator.overlay.text import Line

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
    colorkey = True

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
        return {e.id: "РУ " + e.source for e in entries}

    def close(self):
        pass


def _canvas(w, h, boxes, bg=(90, 90, 90), ink=(240, 240, 240)):
    import numpy as np
    img = np.zeros((h, w, 4), np.uint8)
    img[..., :3] = bg
    img[..., 3] = 255
    for x, y, bw, bh in boxes:
        for cx in range(x + 2, x + bw - 2, 5):
            img[y + 3:y + bh - 3, cx:cx + 2, :3] = ink
    return img


def _game(title="Game", w=640, h=360, exe="C:/G/g.exe"):
    from russificator.overlay import win32
    return win32.WindowInfo(hwnd=11, pid=4242, exe=exe, title=title, client=(0, 0, w, h),
                            window=(0, 0, w, h), monitor=(0, 0, w, h), minimized=False)


def _svc(tmp_path, monkeypatch, frame, ocr_lines, ru_lines=None, title="Game", fg=None):
    monkeypatch.setenv("RUSSIFICATOR_HOME", str(tmp_path / "home"))
    from russificator import paths
    paths.set_home(None)
    from russificator.overlay import pipeline, service, win32
    fg = fg or _game(title)
    monkeypatch.setattr(win32, "foreground", lambda: fg)
    monkeypatch.setattr(win32, "grab_screen", lambda x, y, w, h: frame[0])
    monkeypatch.setattr(win32, "grab_window", lambda hwnd, cw, ch, region: frame[0])
    monkeypatch.setattr(pipeline, "SETTLE", 0.0)
    monkeypatch.setattr(pipeline, "CONFIRM", 0.0)
    monkeypatch.setattr(pipeline, "GONE_HOLD", 0.0)
    svc = service.LiveService(quiet=True)
    svc.gui = _FakeGui()
    svc.ocr = _FakeOcr(ocr_lines)
    svc.ocr_ru = ru_lines if ru_lines is not None else None
    svc.cfg["live_deep"] = False
    svc.cfg["live_always"] = [fg.exe]
    return svc


def _run_jobs(svc):
    """Выполнить задания распознавания сразу (в тестах — без потока)."""
    while not svc._jobs.empty():
        gen, job, fg = svc._jobs.get_nowait()
        svc._results.append((gen, job, svc._read(job, fg)))


def _translate_all(svc):
    for key, src, _ in svc.pipe.needed():
        svc._done(key, src, svc.translator.translate(
            [type("E", (), {"id": "0", "source": src})()], {})["0"])


def test_live_service_pipeline(tmp_path, monkeypatch):
    """Кадр игры → распознанные строки → перевод → картинка поверх игры (Windows подменён)."""
    pytest.importorskip("numpy")
    from russificator.overlay import win32
    frame = [_canvas(1280, 720, [(500, 600, 280, 24), (10, 10, 60, 18)])]
    svc = _svc(tmp_path, monkeypatch, frame, [Line("Press any key to continue", 500, 600, 280, 24),
                                              Line("100/100", 10, 10, 60, 18)],
               fg=_game("Hollow Knight", 1280, 720, exe=str(tmp_path / "Games" / "Hollow" / "hollow.exe")))
    svc.cfg["live_always"] = []
    svc.translator = _FakeTranslator()
    svc._tick()                                          # кадр 1: задание распознавания
    assert svc.target is not None and svc.target_reason == "полноэкранное окно"
    assert "toggle" in svc.gui.keys                      # в игре без XUnity Alt+T — наш
    _run_jobs(svc)
    svc._tick()                                          # результат → надпись; кадр тот же — подтверждена
    assert [b.text for b in svc.pipe.blocks if not b.skip] == ["Press any key to continue"]     # «100/100» не переводим
    assert [src for _, src, _ in svc.pipe.needed()] == ["Press any key to continue"]
    _translate_all(svc)
    svc._tick()
    assert svc.count == 1 and svc.recent[0]["tr"] == "РУ Press any key to continue"
    assert svc.gui.frames and svc.gui.frames[-1][:4] == (0, 0, 1280, 720)
    assert svc._jobs.empty()                             # неподвижный кадр больше не распознаётся
    st = svc.status()
    assert st["game"]["title"] == "Hollow Knight" and st["ocr"]["ok"] and st["count"] == 1
    # «не игра» (браузер) — перевод прячется
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
    assert svc.mark_current("never") == "D:/Indie/tiny.exe"
    assert svc.target is None and svc.last_game is None


def test_live_translation_disappears_with_text(tmp_path, monkeypatch):
    """Реплика кончилась — перевод убирается на следующем кадре (без ожидания распознавания)."""
    pytest.importorskip("numpy")
    frame = [_canvas(640, 360, [(100, 300, 280, 24)])]
    svc = _svc(tmp_path, monkeypatch, frame, [Line("Welcome back, traveler!", 100, 300, 280, 24)])
    svc.translator = _FakeTranslator()
    svc._tick()
    _run_jobs(svc)
    svc._tick()
    _translate_all(svc)
    svc._tick()
    shown = len(svc.gui.frames)
    assert shown >= 1 and svc._on
    frame[0] = _canvas(640, 360, [])
    svc._tick()
    assert not svc._on and svc.gui.hidden >= 1


def test_live_skips_russian_text_with_ru_verifier(tmp_path, monkeypatch):
    """Русский текст в игре английское распознавание читает как «HaqaT», русское — как «Начать»:
    такая надпись не переводится, английская рядом — переводится."""
    pytest.importorskip("numpy")

    class Ru(_FakeOcr):
        def recognize(self, w, h, bgra):
            return [Line("Начать" if w < 120 else "Options menu", 12, 12, w - 24, h - 24)]
    frame = [_canvas(640, 360, [(100, 100, 80, 20), (100, 200, 140, 20)])]
    svc = _svc(tmp_path, monkeypatch, frame, [Line("HaqaT", 100, 100, 80, 20), Line("Options menu", 100, 200, 140, 20)],
               ru_lines=Ru([]))
    blocks = svc._analyze(svc.ocr.lines, frame[0], _game(), 0.0)
    assert [b.text for b in blocks if not b.skip] == ["Options menu"]
    assert [b.text for b in blocks if b.skip] == ["HaqaT"]                # место запомнено, но не переводится


def test_live_skips_caption_and_useless_translations(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    frame = [_canvas(640, 360, [(26, 10, 50, 13), (100, 200, 90, 20)])]
    svc = _svc(tmp_path, monkeypatch, frame, [Line("Lily's well", 26, 10, 50, 13), Line("Zorblax", 100, 200, 90, 20)],
               title="Lily's Well")
    svc._tick()
    _run_jobs(svc)
    svc._tick()
    assert [b.text for b in svc.pipe.blocks if not b.skip] == ["Zorblax"]          # заголовок окна не переводим
    svc._done("zorblax", "Zorblax", "Zorblax")                       # перевод совпал с оригиналом
    svc._tick()
    assert svc.gui.frames == [] and svc.count == 0                  # показывать нечего
    assert svc.pipe.needed() == []                                   # и на перевод больше не уходит


def test_live_same_line_different_color_is_separate(tmp_path, monkeypatch):
    """Имя героя над репликой (другого цвета) — отдельная надпись: не приклеивается к реплике то так,
    то этак (раньше из-за этого один и тот же экран переводился по-разному)."""
    pytest.importorskip("numpy")
    img = _canvas(1280, 720, [(277, 513, 473, 38)])
    pink = _canvas(1280, 720, [(304, 476, 110, 27)], ink=(120, 60, 230))
    img[476:503, 304:414] = pink[476:503, 304:414]
    frame = [img]
    lines = [Line("Mothman", 304, 476, 110, 27), Line("What's with the random questions? Well—v", 277, 513, 473, 34)]
    svc = _svc(tmp_path, monkeypatch, frame, lines)
    blocks = svc._analyze(lines, img, _game(w=1280, h=720), 0.0)
    assert sorted(b.text for b in blocks if not b.skip) == ["Mothman", "What's with the random questions? Well—"]
    reply = next(b for b in blocks if b.text.startswith("What"))
    assert reply.parts == ["What's with the random questions?", "Well—"]


def test_live_low_confidence_word_stays_in_paragraph(tmp_path, monkeypatch):
    """Слово, прочитанное неуверенно, остаётся в абзаце — оно не торчит по-английски посреди перевода."""
    pytest.importorskip("numpy")
    img = _canvas(1280, 720, [(100, 300, 500, 30), (100, 336, 300, 30)])
    frame = [img]
    weak = Line("disintegrate", 100, 336, 300, 30)
    weak.conf = 0.5
    lines = [Line("Those who lack a sense of self tend to", 100, 300, 500, 30), weak]
    svc = _svc(tmp_path, monkeypatch, frame, lines)
    blocks = svc._analyze(lines, img, _game(w=1280, h=720), 0.0)
    assert [b.text for b in blocks if not b.skip] == ["Those who lack a sense of self tend to disintegrate"]
    assert len(blocks[0].lines) == 2


def test_live_capture_falls_back_to_screen_and_masks_plates(tmp_path, monkeypatch):
    """Окно игры не снимается (PrintWindow): кадр берётся с экрана; если окно перевода нельзя исключить
    из захвата (старая Windows) — наши плашки в нём закрашиваются."""
    pytest.importorskip("numpy")
    from russificator.overlay import win32
    frame = [_canvas(640, 360, [], bg=(200, 200, 200))]
    svc = _svc(tmp_path, monkeypatch, frame, [Line("Press any key", 100, 100, 160, 24)])
    monkeypatch.setattr(win32, "grab_window", lambda hwnd, cw, ch, region: None)
    svc.gui.excluded_from_capture = False
    svc._plates = [(90, 90, 200, 50)]
    out = svc._capture(_game(), 0, 0, 640, 360)
    assert svc.captured_by == "screen" and svc._capture_mode == "screen"
    assert out[100, 150, 0] == 0 and out[10, 10, 0] == 200
    assert frame[0][100, 150, 0] == 200                               # сам кадр не испорчен
    svc.gui.excluded_from_capture = True                              # окно перевода исключено — не закрашиваем
    assert svc._capture(_game(), 0, 0, 640, 360)[100, 150, 0] == 200


def test_live_window_capture_preferred(tmp_path, monkeypatch):
    """Кадр с окна игры есть — экран снимается только для редкой сверки (не застыл ли кадр окна)."""
    pytest.importorskip("numpy")
    from russificator.overlay import win32
    frame = [_canvas(640, 360, [(100, 100, 200, 24)], bg=(120, 120, 120))]
    svc = _svc(tmp_path, monkeypatch, frame, [])
    shots = []
    monkeypatch.setattr(win32, "grab_screen", lambda *a: shots.append(a) or frame[0])
    for _ in range(5):
        assert svc._capture(_game(), 0, 0, 640, 360) is frame[0] and svc.captured_by == "window"
    assert len(shots) == 1


def test_live_fullscreen_window_gives_fill_or_stale_frame(tmp_path, monkeypatch):
    """Полноэкранная игра отдаёт с окна заливку цветом фона (или застывший кадр), а на экране — игра:
    кадр берётся с экрана, окно перевода исключается из захвата."""
    pytest.importorskip("numpy")
    from russificator.overlay import service, win32
    game = _canvas(640, 360, [(100, 100, 200, 24)], bg=(20, 30, 40))
    fill = _canvas(640, 360, [], bg=(90, 60, 50))
    svc = _svc(tmp_path, monkeypatch, [game], [])
    svc.gui.can_exclude, svc.gui.excluded_from_capture = True, False
    svc.gui.set_excluded = lambda on: setattr(svc.gui, "excluded_from_capture", on)
    monkeypatch.setattr(win32, "grab_window", lambda *a: fill)
    monkeypatch.setattr(win32, "grab_screen", lambda *a: game)
    for _ in range(3):                                                   # три кадра подряд — не мелькание загрузки
        assert svc._capture(_game(), 0, 0, 640, 360) is game
    assert svc._capture_mode == "screen" and svc.gui.excluded_from_capture
    # застывший кадр окна (не заливка): сверка с экраном замечает расхождение
    svc2 = _svc(tmp_path, monkeypatch, [game], [])
    other = _canvas(640, 360, [(300, 250, 200, 24)], bg=(200, 180, 20))
    monkeypatch.setattr(win32, "grab_window", lambda *a: other)
    monkeypatch.setattr(win32, "grab_screen", lambda *a: game)
    monkeypatch.setattr(service, "CROSSCHECK", 0.0)
    for _ in range(4):
        out = svc2._capture(_game(), 0, 0, 640, 360)
    assert out is game and svc2._capture_mode == "screen"


def test_live_deep_scan_finds_texture_text(tmp_path, monkeypatch):
    """Глубокий проход: надпись, которую видят только варианты кадра (контраст, каналы), добавляется,
    когда экран замер."""
    pytest.importorskip("numpy")
    from russificator.overlay import service
    colored = bytes([10, 120, 240, 255])

    class DeepOcr(_FakeOcr):
        def recognize(self, w, h, bgra):
            if bgra[:4] == colored:                                     # исходный кадр — надписи не видно
                return [Line("Continue", 20, 20, 120, 24)]
            return [Line("Continue", 20, 20, 120, 24), Line("SIGN HERE", 300, 250, 160, 22)]
    img = _canvas(640, 360, [(20, 20, 120, 24), (300, 250, 160, 22)], bg=(10, 120, 240))
    img[:180, 200:, :3] = (200, 30, 60)                                 # две части разного цвета — есть контраст
    img[0, 0, :3] = (10, 120, 240)
    frame = [img]
    svc = _svc(tmp_path, monkeypatch, frame, [])
    svc.ocr = DeepOcr([])
    svc.cfg["live_deep"] = True
    monkeypatch.setattr(service, "DEEP_IDLE", 0.0)
    svc._tick()                                                          # обычное распознавание
    _run_jobs(svc)
    svc._tick()                                                          # экран замер — глубокий проход
    _run_jobs(svc)
    svc._tick()
    assert any(b.text == "SIGN HERE" and b.deep for b in svc.pipe.blocks)
    assert svc.deep_found >= 1 and svc.status()["deep"]["found"] >= 1


def test_live_drops_partial_logos_junk_and_brands(tmp_path, monkeypatch):
    """Обрывок стилизованного логотипа, мусор распознавания и заставка движка не переводятся."""
    pytest.importorskip("numpy")
    img = _canvas(1280, 720, [])
    svc = _svc(tmp_path, monkeypatch, [img], [])
    from russificator.overlay import words
    svc.words = words.Words({w: i for i, w in enumerate(["about", "literally", "doing", "your", "taxes", "sign",
                                                          "here", "exit"])})
    lines = [Line("ABOUT )OOmG", 500, 280, 400, 48), Line("Made with", 560, 400, 160, 30),
             Line("unity", 580, 440, 120, 40), Line("EXIT", 105, 679, 85, 35)]
    kept = svc._analyze(lines, img, _game(w=1280, h=720), 0.0)
    assert [b.text for b in kept if not b.skip] == ["EXIT"]
    assert text.without_junk("Hello )OOmG world") == "Hello world"


def test_live_does_not_translate_game_title_logo(tmp_path, monkeypatch):
    """Крупные слова из названия игры (логотип на титульном экране) не переводятся, кнопки рядом — да."""
    pytest.importorskip("numpy")
    from russificator.overlay import win32
    img = _canvas(1280, 720, [])
    svc = _svc(tmp_path, monkeypatch, [img], [])
    fg = win32.WindowInfo(hwnd=11, pid=1, exe="D:/g/A Game About Literally Doing Your Taxes.exe",
                          title="A Game About Literally Doing Your Taxes", client=(0, 0, 1280, 720),
                          window=(0, 0, 1280, 720), monitor=(0, 0, 1280, 720), minimized=False)
    lines = [Line("ABOUT", 672, 285, 236, 47), Line("EXIT", 105, 679, 85, 35),
             Line("Your taxes are due", 100, 100, 300, 20), Line("About", 100, 400, 80, 22)]
    kept = svc._analyze(lines, img, fg, 0.0)
    assert sorted(b.text for b in kept if not b.skip) == ["About", "EXIT", "Your taxes are due"]
    # логотип с ошибкой распознавания в букве — тоже название
    fg2 = win32.WindowInfo(hwnd=12, pid=1, exe="D:/g/BLOODMONEY!.exe", title="BLOODMONEY!", client=(0, 0, 1280, 720),
                           window=(0, 0, 1280, 720), monitor=(0, 0, 1280, 720), minimized=False)
    kept = svc._analyze([Line("SLOODMONEY!", 250, 150, 700, 90), Line("NEW GAME", 220, 520, 390, 60)], img, fg2, 0.0)
    assert [b.text for b in kept if not b.skip] == ["NEW GAME"]


def test_live_skips_whole_title_and_gibberish(tmp_path, monkeypatch):
    """«RESIDENT LOVER» (логотип обычного размера) — название; «fang:unc bv team avia» — абракадабра."""
    pytest.importorskip("numpy")
    from russificator.overlay import win32, words
    img = _canvas(1920, 1080, [])
    svc = _svc(tmp_path, monkeypatch, [img], [])
    svc.words = words.Words({w: i for i, w in enumerate(["team", "game", "by", "press", "start", "new", "load", "lover"])})
    fg = win32.WindowInfo(hwnd=11, pid=1, exe="D:/g/Resident_Lover.exe", title="Resident Lover",
                          client=(0, 0, 1920, 1080), window=(0, 0, 1920, 1080), monitor=(0, 0, 1920, 1080),
                          minimized=False)
    lines = [Line("RESIDENT LOVER", 700, 300, 520, 40), Line("fang:unc bv team avia", 700, 600, 400, 30),
             Line("Press start", 800, 900, 200, 30), Line("Lover", 100, 100, 80, 20)]
    kept = svc._analyze(lines, img, fg, 0.0)
    assert sorted(b.text for b in kept if not b.skip) == ["Lover", "Press start"]
    assert not text.gibberish("Ethan Grimwald and Lucy Vane", svc.words.ranks)     # имена — не мусор


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
    pytest.importorskip("numpy")
    from russificator.overlay import service
    frame = [_canvas(640, 360, [], bg=(0, 0, 0))]
    svc = _svc(tmp_path, monkeypatch, frame, [])
    for _ in range(service.BLACK_FRAMES):
        svc._tick()
    assert "Окно без рамки" in svc.status()["hint"]
    frame[0] = frame[0].copy()
    frame[0][180, 64, 1] = 200                               # появилась картинка
    svc._tick()
    assert svc.status()["hint"] == ""


def test_live_leaves_alt_t_to_russified_games(tmp_path, monkeypatch):
    """В игре, русифицированной файлами, Alt+T переключает перевод в самой игре — оверлей его не занимает."""
    pytest.importorskip("numpy")
    game = tmp_path / "VN"
    (game / "russificator_backup").mkdir(parents=True)
    (game / "lib" / "py3-windows-x86_64").mkdir(parents=True)
    exe = str(game / "lib" / "py3-windows-x86_64" / "VN.exe")
    frame = [_canvas(640, 360, [])]
    svc = _svc(tmp_path, monkeypatch, frame, [], fg=_game("VN", exe=exe))
    svc._tick()
    assert svc.target is not None and "toggle" not in svc.gui.keys and "now" in svc.gui.keys


def test_live_speaker_name_above_reply_is_kept(tmp_path, monkeypatch):
    """Незнакомое слово с большой буквы табличкой над репликой — имя (не переводится); такое же слово
    пунктом меню — переводится (словарь программы не знает всех слов)."""
    pytest.importorskip("numpy")
    from russificator.overlay import words
    img = _canvas(1280, 720, [(277, 513, 473, 38), (900, 200, 160, 30), (900, 250, 120, 30)])
    pink = _canvas(1280, 720, [(304, 476, 110, 27)], ink=(120, 60, 230))
    img[476:503, 304:414] = pink[476:503, 304:414]
    svc = _svc(tmp_path, monkeypatch, [img], [])
    svc.words = words.Words({w: i for i, w in enumerate(["you", "seem", "rather", "lost", "start", "game"])})
    lines = [Line("Mothman", 304, 476, 110, 27), Line("You seem rather lost.", 277, 513, 473, 34),
             Line("Unlockables", 900, 200, 160, 30), Line("Start Game", 900, 250, 120, 30)]
    blocks = svc._analyze(lines, img, _game(w=1280, h=720), 0.0)
    assert sorted(b.text for b in blocks if not b.skip) == ["Start Game", "Unlockables", "You seem rather lost."]
    assert [b.text for b in blocks if b.skip] == ["Mothman"]
