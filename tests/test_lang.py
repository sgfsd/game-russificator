"""Язык оригинала: игры на японском, китайском, корейском и чужие локализации в английских играх."""

from __future__ import annotations

import json

import pytest

from russificator.core.pipeline import Pipeline, PipelineOptions
from russificator.core.universal import TranslationProject
from russificator.translation import edges, lang, terms
from russificator.translation.base import Translator
from russificator.translation.filters import looks_like_sentence, looks_translatable
from russificator.translation.prompt import SYSTEM_PROMPT, system_prompt

# реплика японской игры на RPG Maker MV из отчёта пользователя (DeepSeek ничего не перевёл)
JA_LINE = "\\c[8]【 トレビー 】\\c[0]<br> 「ボクもそうであることを願ってるよ」"


def test_detect_language_of_game():
    # английские надписи японской игры (меню, параметры плагинов) — меньшинство
    ja = [JA_LINE, "「余計な心配だ」", "あいつは……湊月は俺の、妹だ"] * 20 + ["Yes", "Save game", "HP", "Options"]
    assert lang.detect(ja) == "ja"
    assert lang.detect(["你好，欢迎来到修仙世界。", "开始游戏", "设置"] * 20) == "zh"
    assert lang.detect(["안녕하세요, 모험가님.", "게임 시작"] * 20) == "ko"
    assert lang.detect(["Hello there, are you the new guy?", "Start game"] * 20) == "en"
    assert lang.detect(["¿Qué es esto? No lo sé, pero es muy raro.", "Para ti, una carta del rey."] * 20) == "es"
    # английская игра с китайской и японской локализацией рядом — всё равно английская
    en_with_locales = (["You found a rusty key in the old well."] * 30
                       + ["你在古井里找到了一把生锈的钥匙。", "古い井戸で錆びた鍵を見つけた。"] * 30)
    assert lang.detect(en_with_locales) == "en"
    assert lang.detect([]) == "en"


@pytest.mark.parametrize("text, expected", [
    # английские реплики с каомодзи и японским словом — английские
    ("Good! Sometimes you just feel like a cat! Meow! ㅤ/ᐠ - ˕ -マ", True),
    ("ヽ(ﾟДﾟ)ﾉ┌┛ What's up! I'm Textbox-chan!", True),
    ("Ttyl~! ★⌒ Ｙ⌒Ｙ⌒Ｙ⌒Ｙ⌒ ヾ(oﾟДﾟ)ﾉ", True),
    ('The memorial says "母梅の髪".', True),
    ("Third, Sashimi (刺身, English: sə-SHEE-mee)", True),
    # строки чужой локализации: перевод латинских кусков давал «ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС尺寸»
    ("UI尺寸", False),
    ("グラス（Hot Shot）", False),
    ("瓶子（Old Coast）", False),
    ("按住Alt键可以使用口诀", False),
    ("A.D 选择\nSPACE 确定", False),
    ("이 MRI 기계가 작동되어 있고, 뭔가가 안에 껴있는 것 같아요.", False),
    ("「ボクもそうであることを願ってるよ」", False),
    ("爱吃奶酪的Jerry", False),
    ('"A.S.W.D를 눌러 선택하고, \nSPACE를 눌러 확인하세요."', False),
    ("*Bug反馈及查找攻略加QQ:\n1群: 739644053(已满)", False),
    # английская реплика со вставками «чужой речи» (1001 vies, RPG Maker MZ) — английская
    ("\\c[0]Put it down and\\c[23] 捨てぷ 番号\\c[0].\\c[0]", True),
    ("\\c[0]Don't\\c[23] 好かれて \\c[0]it.\\c[0]", True),
    ("\\c[6]不清楚可 \\c[0]to\\c[6] 走人看\\c[0], lazybones\\c[0]!\\c[0]", True),
    ("its {font=SkyblessingInjeFont.otf}냉면{/font}", True),                     # JOBHUNTING9000
    ("\\c[0]Go\\c[23] ヴェリテ \\c[0]slowly.\\c[0]", True),
    ("\\c[0]Daddy!\\c[23] 以上 腕 染谷 漏れ\\c[0]!\\c[0]", True),
    ("<Task>SUBJECT 01</>\n状态： <Inactive>未知</>\n\n<Error>错误</>：协议無法继续执行。", False),
])
def test_translatable_in_english_game(text, expected):
    assert lang.translatable(text, "en") is expected


def test_translatable_in_japanese_game():
    assert lang.translatable(JA_LINE, "ja")
    assert lang.translatable("Save", "ja")              # английский интерфейс японской игры
    assert lang.translatable("グラス（Hot Shot）", "ja")
    assert not lang.translatable("……！？", "ja")          # одна пунктуация


def test_cjk_text_filters():
    assert looks_translatable(JA_LINE)
    assert looks_translatable("はい/いいえ", strict=True)
    assert looks_translatable("ポーション(小)", strict=True)
    assert looks_translatable("第1章", strict=True)
    assert not looks_translatable("img/pictures/立ち絵")
    assert not looks_translatable("立ち絵01.png")
    assert not looks_translatable("立ち絵_01", strict=True)
    assert looks_like_sentence("ボクもそうだよ") and looks_like_sentence("はい。")
    assert not looks_like_sentence("決定")


def test_untranslated_japanese_goes_to_retry():
    assert edges.check(JA_LINE, JA_LINE)[1]                       # вернулось как есть
    tr = "\\c[8]【 Треви 】\\c[0]<br> «Я тоже на это надеюсь»"
    assert edges.check(JA_LINE, tr) == (tr, None)
    assert edges.check("Hello there", "Привет 世界")[1]             # иероглифы в переводе английской строки
    assert edges.check("Good! Meow! ㅤ/ᐠ - ˕ -マ", "Хорошо! Мяу! ㅤ/ᐠ - ˕ -マ")[1] is None


def test_english_line_with_foreign_inserts_keeps_them():
    """1001 vies: «\\c[0]Put it down and\\c[23] 捨てぷ 番号\\c[0].» — английская реплика, «чужая речь» в ней
    остаётся как есть. Раньше такой перевод отбрасывался как «остались иероглифы» — строка не переводилась;
    лишние иероглифы и строки китайской локализации по-прежнему отбрасываются."""
    src = "\\c[0]Put it down and\\c[23] 捨てぷ 番号\\c[0].\\c[0]"
    good = "\\c[0]Положи это и\\c[23] 捨てぷ 番号\\c[0].\\c[0]"
    assert lang.with_inserts(src, "en") and not lang.with_inserts(src, "ja")
    assert edges.check(src, good)[1] is None and edges.check(src, good, src_lang="en")[1] is None
    assert edges.check(src, good.replace("番号", "番号番号番号"))[1]          # моделью добавлено своё
    assert edges.check(src, good, src_lang="ja")[1]                         # в японской игре это японская строка
    slow = "\\c[0]Go\\c[23] ヴェリテ \\c[0]slowly.\\c[0]"
    assert lang.with_inserts(slow, "en")                                    # английское слово отделено разметкой
    assert edges.check(slow, "\\c[0]Иди\\c[23] ヴェリテ \\c[0]медленно.\\c[0]")[1] is None
    zh = "按住Alt键可以使用口诀，把口诀文字拖动到目标上使用"
    assert not lang.with_inserts(zh, "en")
    assert edges.check(zh, "Удерживайте Alt键可以使用口诀，把口诀文字拖动到目标上使用")[1]
    assert not lang.with_inserts("<b>攻击力</b> Lv.5", "en")                # две буквы — не слово


def test_japanese_punctuation_becomes_russian():
    tr = "\\c[8]【 Треви 】\\c[0]<br> 「Я тоже на это надеюсь」"
    assert edges.fix(JA_LINE, tr) == "\\c[8]【 Треви 】\\c[0]<br> «Я тоже на это надеюсь»"
    assert lang.russian_punctuation("«Я тоже……» ――Нет。Да、конечно！") == "«Я тоже…» — Нет. Да, конечно!"
    assert lang.russian_punctuation("Джон・Смит Ｈｅｌｌｏ") == "Джон Смит Hello"
    # английский оригинал — перевод не трогается
    assert edges.fix("He said 「hi」", "Он сказал «привет»") == "Он сказал «привет»"


def test_japanese_full_stop_keeps_period():
    """«。» — конец фразы: точка перевода остаётся (правило «лишняя точка у кнопки» её срезало)."""
    from russificator.translation.markup import normalize_layout
    assert normalize_layout("古い井戸の底で、錆びた鍵を見つけた。", "На дне колодца я нашёл ключ.") == \
        "На дне колодца я нашёл ключ."
    assert normalize_layout("Go to the forest", "Иди в лес.") == "Иди в лес"


def test_language_names_stay_as_is():
    """Demonic Mahjong узнаёт язык по надписи кнопки: после «English» → «Английский» выбор языка при первом
    запуске не срабатывал. Названия языков на них самих не переводятся (как названия площадок)."""
    from russificator.translation.filters import is_brand
    assert is_brand("English") and is_brand("Deutsch") and is_brand("日本語") and is_brand("Português (Brasil)")
    assert not is_brand("English text") and not is_brand("Language: English")


def test_names_in_brackets_are_game_terms():
    found = terms.collect([JA_LINE, "\\n<トレビー>「余計な心配だ」", "\\nc<Will>Hello there"])
    assert found["トレビー"] == 2
    assert "【 トレビー 】" not in found
    assert "Will" not in found          # латинское имя в окошке не термин: машинный сделал бы из него «Волю»


def test_prompt_names_source_language():
    assert system_prompt("en") == SYSTEM_PROMPT
    ja = system_prompt("ja")
    assert "Translate Japanese game text into Russian." in ja and "Never leave Japanese characters" in ja
    assert "Translate Spanish game text" in system_prompt("es")


# ---------- сквозной прогон: японская игра на RPG Maker MV ----------

class _JapaneseFake(Translator):
    """Переводит японские реплики по словарю (как нейросеть) и запоминает язык оригинала."""
    cache_id = "fake:ja"
    title = "fake"
    context_aware = True
    DICT = {
        JA_LINE: "\\c[8]【 Треви 】\\c[0]<br> 「Я тоже на это надеюсь」",
        "\\c[28]【 梗介 】\\c[0]<br> 「余計な心配だ」": "\\c[28]【 Кёсукэ 】\\c[0]<br> 「Лишние волнения」",
        "トレビー": "Треви", "梗介": "Кёсукэ", "はい": "Да", "いいえ": "Нет",
    }

    def __init__(self):
        self.langs = []

    def translate(self, entries, glossary):
        self.langs.append(self.source_lang)
        return {e.id: self.DICT[e.source] for e in entries if e.source in self.DICT}


class _EnglishOnly(_JapaneseFake):
    title = "Машинный перевод (офлайн)"

    def supports(self, code):
        return code == "en"


@pytest.fixture
def ja_game(tmp_path):
    root = tmp_path / "GameData"
    data = root / "www" / "data"
    data.mkdir(parents=True)
    (root / "www" / "js").mkdir()
    (root / "www" / "js" / "rpg_core.js").write_text("//", encoding="utf-8")
    lines = [JA_LINE, "\\c[28]【 梗介 】\\c[0]<br> 「余計な心配だ」"] * 15
    cmds = []
    for text in lines:
        first, second = text.split("<br> ")
        cmds += [{"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]},
                 {"code": 401, "indent": 0, "parameters": [first + "<br>"]},
                 {"code": 401, "indent": 0, "parameters": [second]}]
    cmds += [{"code": 102, "indent": 0, "parameters": [["はい", "いいえ"], 1, 0, 2, 0]},
             {"code": 0, "indent": 0, "parameters": []}]
    (data / "CommonEvents.json").write_text(json.dumps([None, {"id": 1, "list": cmds}], ensure_ascii=False),
                                            encoding="utf-8")
    return root


def test_japanese_rpgmaker_game_is_translated(ja_game, tmp_path):
    tr = _JapaneseFake()
    res = Pipeline(tr, PipelineOptions(use_memory=False)).run(ja_game, tmp_path / "w")
    assert res.success, res.summary()
    assert res.skipped_lines == 0 and res.translated_lines >= 4
    assert set(tr.langs) == {"ja"}
    assert TranslationProject.load(tmp_path / "w").meta["source_lang"] == "ja"
    text = (ja_game / "www/data/CommonEvents.json").read_text(encoding="utf-8")
    assert "«Я тоже на это надеюсь»" in text and "「" not in text
    assert '["Да","Нет"]' in text


def test_machine_translator_refuses_japanese_game(ja_game, tmp_path):
    res = Pipeline(_EnglishOnly(), PipelineOptions(use_memory=False)).run(ja_game, tmp_path / "w")
    assert not res.success and not res.injected
    assert "на японском" in res.errors[0] and "нейросеть" in res.errors[0]
    assert not (ja_game / "russificator_backup").exists()


def test_english_game_skips_other_locales(tmp_path):
    root = tmp_path / "EnGame"
    data = root / "www" / "data"
    data.mkdir(parents=True)
    (root / "www" / "js").mkdir()
    (root / "www" / "js" / "rpg_core.js").write_text("//", encoding="utf-8")
    texts = [f"Line number {i} of the story." for i in range(20)] + ["グラス（Hot Shot）", "UI尺寸"]
    cmds = []
    for t in texts:
        cmds += [{"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]},
                 {"code": 401, "indent": 0, "parameters": [t]}]
    cmds.append({"code": 0, "indent": 0, "parameters": []})
    (data / "CommonEvents.json").write_text(json.dumps([None, {"id": 1, "list": cmds}], ensure_ascii=False),
                                            encoding="utf-8")

    class Echo(_JapaneseFake):
        def translate(self, entries, glossary):
            return {e.id: "Строка " + e.source.split()[2] for e in entries}

    res = Pipeline(Echo(), PipelineOptions(use_memory=False)).run(root, tmp_path / "w")
    assert res.success, res.summary()
    sources = [e.source for e in TranslationProject.load(tmp_path / "w").entries]
    assert "グラス（Hot Shot）" not in sources and "UI尺寸" not in sources and len(sources) == 20


def test_rpgmaker_namebox_gets_translated_name():
    from russificator.engines.rpgmaker import mv_mz
    names = {"トレビー": "Треви"}
    assert mv_mz.translate_nameboxes("\\n<トレビー>«Привет»", names) == "\\n<Треви>«Привет»"
    assert mv_mz.translate_nameboxes("\\nr<Кто-то>Hi", names) == "\\nr<Кто-то>Hi"


def test_xunity_reads_source_language(tmp_path):
    from russificator.engines.unity import xunity
    assert "source=ja" in xunity.plugin_config_text(None, True, source="ja")
    assert "source=" not in xunity.plugin_config_text(None, True)
