"""Оверлей «Живой перевод»: логика, не зависящая от Windows (текст, кадры и надписи, отрисовка, выбор игры)."""

from __future__ import annotations

import os

import pytest

from russificator.overlay import detect, text
from russificator.overlay.text import Line, group_lines, is_foreign, join_lines


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
        Line("Options", 805, 345, 80, 30),                 # пункты меню — отдельные надписи
        Line("Press any key", 400, 650, 150, 24),
        Line("to continue", 410, 678, 130, 24),            # короткий перенос фразы — одна надпись
    ]
    blocks = group_lines(lines)
    texts = sorted(b.text for b in blocks)
    assert "Hello there, traveler! This village has been quiet for a long time." in texts
    assert "HP 100" in texts
    assert "Continue" in texts and "Options" in texts
    assert "Press any key to continue" in texts
    para = next(b for b in blocks if b.text.startswith("Hello"))
    assert para.rect == (100, 500, 400, 52)


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




def test_sentences_and_continue_marker():
    assert text.sentences("What's with the random questions? Well-") == ["What's with the random questions?", "Well-"]
    assert text.sentences("Mr. Smith is here. He waits.") == ["Mr. Smith is here.", "He waits."]
    assert text.sentences("I have a pretty good idea of who I am. (choose a preset)") == \
        ["I have a pretty good idea of who I am. (choose a preset)"]
    assert text.strip_marker("Well—v") == "Well—"                  # мигающий ▼ распознан как «v»
    assert text.strip_marker("Hello there. ▼") == "Hello there."
    assert text.strip_marker("Plan A") == "Plan A"
    assert text.complete("Done.") and text.complete("Really?!") and not text.complete("Well-")


def _canvas(w, h, boxes, bg=(40, 40, 40), ink=(240, 240, 240)):
    """Кадр BGRA: ровный фон и «буквы» (вертикальные штрихи) в рамках строк."""
    import numpy as np
    img = np.zeros((h, w, 4), np.uint8)
    img[..., :3] = bg
    img[..., 3] = 255
    for x, y, bw, bh in boxes:
        for cx in range(x + 2, x + bw - 2, 5):
            img[y + 3:y + bh - 3, cx:cx + 2, :3] = ink
    return img


def _read(p, job, lines, now):
    from russificator.overlay.pipeline import make_block
    p.ocr_done(job, [b for b in (make_block([ln], job.img, job.at) for ln in lines) if b], now=now)


def test_pipeline_static_screen_is_read_and_translated_once():
    """Неподвижный экран распознаётся один раз: перевод появляется и больше не меняется и не мигает."""
    pytest.importorskip("numpy")
    from russificator.overlay.pipeline import Pipeline
    cache = {}
    p = Pipeline(cache.get)
    img = _canvas(640, 360, [(100, 300, 280, 24)])
    job = p.frame(img, now=0.0)
    assert job is not None and job.full
    assert p.frame(img, now=0.05) is None                          # распознавание идёт — второе не нужно
    _read(p, job, [Line("Press any key to continue", 100, 300, 280, 24)], now=0.1)
    assert p.needed() == []                                        # не закончено точкой — ждём подтверждения
    for t in (0.15, 0.2, 0.25):
        assert p.frame(img, now=t) is None                         # тот же кадр — не распознаётся заново
    assert p.needed() == [("press any key to continue", "Press any key to continue", True)]
    assert p.visible() == []
    cache["press any key to continue"] = "Нажмите любую клавишу"
    shown = p.visible()
    assert [(b.text, tr) for b, tr in shown] == [("Press any key to continue", "Нажмите любую клавишу")]
    for t in (0.3, 1.0, 5.0):
        assert p.frame(img, now=t) is None
    assert p.visible()[0][0] is shown[0][0]


def test_pipeline_typewriter_translates_sentences_and_shows_final_text():
    """Реплика печатается по буквам: недопечатанное не показывается, законченные предложения
    переводятся сразу и потом не меняются."""
    pytest.importorskip("numpy")
    from russificator.overlay.pipeline import Pipeline
    cache = {}
    p = Pipeline(cache.get)

    def state(s):
        return _canvas(640, 360, [(40, 300, 10 * len(s), 24)]), Line(s, 40, 300, 10 * len(s), 24)
    a, la = state("What's with the ran")
    job = p.frame(a, now=0.0)
    _read(p, job, [la], now=0.02)
    assert p.needed() == []
    b, lb = state("What's with the random questions? W")
    assert p.frame(b, now=0.05) is None                            # только что сменилось — ждём, пока успокоится
    job = p.frame(b, now=0.13)
    assert job is not None and not job.full                        # только полоса со строкой
    _read(p, job, [lb], now=0.14)
    assert [k for k, _, _ in p.needed()] == ["whats with the random questions"]
    cache["whats with the random questions"] = "Что за случайные вопросы?"
    c, lc = state("What's with the random questions? Well, never mind.")
    p.frame(c, now=0.2)
    job = p.frame(c, now=0.28)
    _read(p, job, [lc], now=0.3)
    assert p.visible() == []                                       # ещё не подтверждено
    p.frame(c, now=0.45)
    assert [k for k, _, _ in p.needed()] == ["well never mind"]
    cache["well never mind"] = "Ну, неважно."
    assert [tr for _, tr in p.visible()] == ["Что за случайные вопросы? Ну, неважно."]


def test_pipeline_keeps_old_translation_until_new_one_is_ready():
    pytest.importorskip("numpy")
    from russificator.overlay.pipeline import Pipeline
    cache = {"hello there": "Привет."}
    p = Pipeline(cache.get)
    a = _canvas(640, 360, [(40, 300, 120, 24)])
    job = p.frame(a, now=0.0)
    _read(p, job, [Line("Hello there.", 40, 300, 120, 24)], now=0.01)
    p.frame(a, now=0.3)
    assert [tr for _, tr in p.visible()] == ["Привет."]
    b = _canvas(640, 360, [(40, 300, 250, 24)])                    # допечаталось второе предложение
    p.frame(b, now=0.4)
    job = p.frame(b, now=0.5)
    _read(p, job, [Line("Hello there. How are you?", 40, 300, 250, 24)], now=0.55)
    p.frame(b, now=0.8)
    assert [tr for _, tr in p.visible()] == ["Привет."]             # новое ещё не переведено — прежний перевод
    cache["how are you"] = "Как дела?"
    assert [tr for _, tr in p.visible()] == ["Привет. Как дела?"]


def test_pipeline_hides_translation_as_soon_as_text_is_gone():
    """Реплика пропала — перевод убирается через 0,15 с (пара кадров), без распознавания."""
    pytest.importorskip("numpy")
    from russificator.overlay.pipeline import Pipeline
    cache = {"quest updated": "Задание обновлено."}
    p = Pipeline(cache.get)
    img = _canvas(640, 360, [(100, 100, 200, 24)])
    job = p.frame(img, now=0.0)
    _read(p, job, [Line("Quest updated.", 100, 100, 200, 24)], now=0.01)
    p.frame(img, now=0.3)
    assert len(p.visible()) == 1
    gone = _canvas(640, 360, [])
    p.frame(gone, now=0.35)
    assert len(p.visible()) == 1                                    # мелькнуло на кадр (глитч) — не убираем
    p.frame(gone, now=0.5)
    assert p.visible() == [] and p.blocks == []                     # пропала — убрана через 0,15 с


def test_pipeline_ocr_miss_and_animation_keep_text():
    """Рядом с надписью мигает значок: распознавание полосы не нашло надпись (промах), но её
    отпечаток на месте — перевод остаётся; смена всей картинки (новая сцена) убирает всё."""
    pytest.importorskip("numpy")
    from russificator.overlay.pipeline import Pipeline
    cache = {"quest updated": "Задание обновлено."}
    p = Pipeline(cache.get)
    img = _canvas(640, 360, [(100, 100, 200, 24)])
    job = p.frame(img, now=0.0)
    _read(p, job, [Line("Quest updated.", 100, 100, 200, 24)], now=0.01)
    p.frame(img, now=0.3)
    blink = img.copy()
    blink[104:118, 320:334, :3] = 250                              # значок «дальше» справа от строки
    p.frame(blink, now=0.4)
    job = p.frame(blink, now=0.5)
    assert job is not None and job.rect[1] <= 100 <= job.rect[1] + job.rect[3]
    p.ocr_done(job, [], now=0.55)
    assert [tr for _, tr in p.visible()] == ["Задание обновлено."]
    other = _canvas(640, 360, [], bg=(200, 180, 20))
    p.frame(other, now=0.6)
    assert p.blocks == [] and p.scene_cuts == 1


def test_pipeline_deep_blocks_only_fill_empty_places():
    pytest.importorskip("numpy")
    from russificator.overlay.pipeline import Pipeline, make_block
    p = Pipeline({}.get)
    img = _canvas(640, 360, [(100, 100, 120, 24), (300, 250, 160, 22)])
    job = p.frame(img, now=0.0)
    _read(p, job, [Line("Continue", 100, 100, 120, 24)], now=0.01)
    added = p.add_deep([make_block([Line("SIGN HERE", 300, 250, 160, 22)], img, 0.5),
                        make_block([Line("Contlnue", 100, 100, 120, 24)], img, 0.5)])
    assert [b.text for b in added] == ["SIGN HERE"] and added[0].confirmed and added[0].deep


def test_colorkey_image_blends_translucent_pixels():
    pytest.importorskip("numpy")
    from PIL import Image
    from russificator.overlay import render
    img = Image.new("RGBA", (4, 1), (0, 0, 0, 0))
    img.putpixel((1, 0), (200, 100, 50, 255))
    img.putpixel((2, 0), (200, 0, 0, 128))
    img.putpixel((3, 0), (254, 0, 255, 255))                        # случайно совпал с ключевым цветом
    under = _canvas(4, 1, [], bg=(0, 0, 100))                       # под окном — красноватый кадр (BGR)
    out = render.to_colorkey(img, under)
    px = [tuple(out[i * 4:i * 4 + 3]) for i in range(4)]
    assert px[0] == render.KEY_BGR                                  # прозрачное — ключ
    assert px[1] == (50, 100, 200)                                  # непрозрачное — как есть (BGR)
    assert px[2][2] in (150, 151) and px[2][0] in (0, 1)            # полупрозрачное смешано с кадром под ним
    assert px[3] != render.KEY_BGR                                  # настоящий пиксель не стал дырой


def test_inline_style_book_keeps_sizes_stable():
    from russificator.overlay import inline
    book = inline.StyleBook()
    assert book.snap(20, ("regular",)) == 20
    assert book.snap(21, ("regular",)) == 20 and book.snap(19, ("regular",)) == 20    # дрожание рамки
    assert book.snap(30, ("regular",)) == 30                                          # другой вид текста
    assert book.snap(21, ("bold",)) == 21


def test_merge_rows_joins_pieces_of_one_line():
    """Строку, разрезанную распознаванием (слово с эффектом прочитано как кириллица), собираем целиком."""
    a = Line("Those who lack a sense of self tend to", 285, 520, 395, 30)
    glitch = Line("дисинтеграте", 686, 521, 120, 29)
    glitch.ru, glitch.alt, glitch.conf = True, "disintegrate", 0.7
    b = Line("upon entry into this", 812, 520, 190, 30)
    far = Line("Menu", 1150, 520, 60, 30)                         # далеко — своя надпись
    rows = text.merge_rows([a, glitch, b, far])
    assert [r.text for r in rows] == ["Those who lack a sense of self tend to disintegrate upon entry into this", "Menu"]
    assert not rows[0].ru
    # черта рамки между кусками (соседние кнопки) — не склеиваем
    assert len(text.merge_rows([a, b], apart=lambda x, y: True)) == 2


def test_caps_junk_and_unknown_words():
    words = {"exit", "play", "did", "new", "game", "about"}
    assert text.caps_junk("DMSL", words) and text.caps_junk("DYDD CDMSL", words)
    assert not text.caps_junk("EXIT", words) and not text.caps_junk("NEW GAME", words)
    assert not text.caps_junk("Zorblax", words)                      # имя — не заглавными целиком
    assert text.unknown_words("06 urpe", words) == ["urpe"]


def _shown(cache, text_, box, now=0.3):
    """Конвейер с одной переведённой и показанной надписью."""
    from russificator.overlay.pipeline import Pipeline
    p = Pipeline(cache.get)
    img = _canvas(640, 360, [box])
    job = p.frame(img, now=0.0)
    _read(p, job, [Line(text_, *box)], now=0.01)
    p.frame(img, now=now)
    assert len(p.visible()) == 1
    return p, img


def test_pipeline_animated_text_stays_and_needs_two_readings():
    """Надпись с глитчем (буквы сдвигаются полосами): перевод не пропадает, а искажённое прочтение
    не подменяет его, пока не подтвердится вторым таким же."""
    pytest.importorskip("numpy")
    p, img = _shown({"quest updated": "Задание обновлено."}, "Quest updated.", (100, 100, 200, 24))
    glitch = img.copy()
    glitch[103:112, 100:300] = img[103:112, 97:297]                  # полосы букв сдвинуты
    glitch[112:121, 100:300] = img[112:121, 104:304]
    p.frame(glitch, now=0.4)
    assert len(p.visible()) == 1 and p.blocks[0].restless           # буквы на месте — перевод остаётся
    job = p.frame(glitch, now=0.5)
    _read(p, job, [Line("Qucst updatcd.", 100, 100, 200, 24)], now=0.55)
    assert [b.text for b in p.blocks] == ["Quest updated."]         # одного искажённого прочтения мало
    p.frame(img, now=0.6)
    assert p.frame(img, now=0.7) is None                             # «ничего нового» — место проверяется реже
    job = p.frame(glitch, now=1.1)                                   # пауза прошла — прочитать снова
    _read(p, job, [Line("Qucst updatcd.", 100, 100, 200, 24)], now=1.15)
    assert [b.text for b in p.blocks] == ["Qucst updatcd."]         # подтвердилось — принято


def test_pipeline_backs_off_where_nothing_new_appears():
    """Мигающий значок рядом с репликой: после прочтения «ничего нового» это место проверяется всё
    реже, а новый текст в другом месте распознаётся сразу."""
    pytest.importorskip("numpy")
    p, img = _shown({"quest updated": "Задание обновлено."}, "Quest updated.", (100, 100, 200, 24))
    on = img.copy()
    on[104:118, 320:334, :3] = 250
    frames = [on, img]
    job = p.frame(on, now=0.4) or p.frame(on, now=0.5)
    _read(p, job, [Line("Quest updated.", 100, 100, 200, 24)], now=0.55)
    jobs = 0
    t = 0.6
    while t < 1.0:                                                   # значок мигает — пауза 0,5 с
        jobs += p.frame(frames[int(t * 10) % 2], now=t) is not None
        t += 0.07
    assert jobs == 0
    other = img.copy()
    other[250:274, 100:300:5, :3] = 240                             # новый текст в другом месте
    p.frame(other, now=1.0)
    assert p.frame(other, now=1.1) is not None


def test_pipeline_smooth_background_animation_does_not_trigger_ocr():
    """Плавная анимация фона (переливы цвета) не запускает распознавание на каждом кадре."""
    pytest.importorskip("numpy")
    from russificator.overlay.pipeline import Pipeline
    p = Pipeline({}.get)
    job = p.frame(_canvas(640, 360, [], bg=(40, 40, 40)), now=0.0)
    p.ocr_done(job, [], now=0.05)
    jobs = 0
    for i in range(1, 12):                                           # фон светлеет на 3 за кадр
        jobs += p.frame(_canvas(640, 360, [], bg=(40 + 3 * i,) * 3), now=0.1 * i) is not None
    assert jobs == 0
    assert p.frame(_canvas(640, 360, [], bg=(80, 80, 80)), now=1.6) is not None    # накопилось — проверка


def test_shape_cache_ignores_background_but_not_digits():
    """Строка над анимированным фоном узнаётся без повторного чтения, а сменившаяся цифра счётчика — нет."""
    pytest.importorskip("numpy")
    import numpy as np
    from PIL import Image, ImageDraw
    from russificator.overlay import paddle, render

    def crop(text_, bg):
        img = Image.new("RGB", (220, 40), bg)
        ImageDraw.Draw(img).text((6, 4), text_, font=render.font(26), fill=(250, 250, 250))
        return np.asarray(img)
    cache = paddle._ShapeCache()
    hit, sh = cache.get(crop("HP: 45 / 100", (60, 40, 90)))
    assert hit is None and sh is not None
    cache.put(sh, ("HP: 45 / 100", 0.99, False, ""))
    assert cache.get(crop("HP: 45 / 100", (80, 55, 120)))[0][0] == "HP: 45 / 100"   # фон под строкой другой
    assert cache.get(crop("HP: 46 / 100", (60, 40, 90)))[0] is None                  # другая цифра
