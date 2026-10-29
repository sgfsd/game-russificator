"""Тесты слоя перевода: разметка, фильтр, глоссарий, сервис, LLM-клиент."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from russificator.core.universal import Entry, EntryStatus, TextKind, TranslationProject
from russificator.translation import markup
from russificator.translation.base import Translator, TranslatorError
from russificator.translation.filters import looks_translatable
from russificator.translation.glossary import match_case, ui_phrase
from russificator.translation.memory import TranslationMemory
from russificator.translation.service import TranslationCancelled, TranslationService


# ============================= разметка =============================

@pytest.mark.parametrize("text, expected", [
    ("Hello, [player]!", ["[player]"]),
    ("{i}Where am I?{/i}{w}", ["{i}", "{/i}", "{w}"]),
    (r"\C[2]Harold\C[0] joined!", [r"\C[2]", r"\C[0]"]),
    (r"Got \I[64]\N[1]\. Wait\|", [r"\I[64]", r"\N[1]", r"\.", r"\|"]),
    ("<color=#ff0000>Warning:</color> <b>x</b>", ["<color=#ff0000>", "</color>", "<b>", "</b>"]),
    ("%s has %d HP, %(name)s", ["%s", "%d", "%(name)s"]),
    ("Found {0} coins, %1 exp", ["{0}", "%1"]),
    ("Take 100% damage and 50 % more", []),
    ("[Press E] to talk", []),  # скобки с пробелами — это текст
])
def test_markup_tokens(text, expected):
    assert markup.tokens(text) == expected


def test_mask_unmask_roundtrip_and_spacing():
    src = "{i}Where am I?{/i} It's dark"
    masked, originals, sign = markup.mask(src)
    assert masked == "@0Where am I?@1 It's dark"
    # модель вставила пробелы вокруг «приклеенных» маркеров — их нужно убрать
    restored, ok = markup.unmask("@0 Где я? @1 Темно", originals, sign, masked_source=masked)
    assert ok and restored == "{i}Где я?{/i} Темно"


def test_mask_uses_other_sign_if_text_has_at_digit():
    masked, originals, sign = markup.mask("Email me@2x {0}")
    assert sign == "#" and "#0" in masked


def test_unmask_detects_lost_marker():
    _, originals, sign = markup.mask("Hi {0} and {1}")
    _, ok = markup.unmask("Привет @0", originals, sign)
    assert not ok


def test_same_markup_ignores_order():
    assert markup.same_markup("{0} of {1}", "{1} из {0}")
    assert not markup.same_markup("{0} of {1}", "{0} из")


def test_normalize_layout_removes_invented_newlines_and_glues_tags():
    src = "{i}Where am I?{/i}{w} It's so dark"
    assert markup.normalize_layout(src, "{i}Где я?{/i}\n{w} Темно") == "{i}Где я?{/i}{w} Темно"
    assert markup.normalize_layout("Line1\nLine2", "Строка1\nСтрока2") == "Строка1\nСтрока2"


def test_cyrillic_detection():
    assert markup.is_cyrillic("Привет, мир")
    assert not markup.is_cyrillic("Hello мир world")


# ============================= фильтр =============================

@pytest.mark.parametrize("text", [
    "Hello there!", "Save", "Load Game", "HP", "Are you sure you want to quit?", "Press <b>E</b> to open",
])
def test_translatable_texts(text):
    assert looks_translatable(text, strict=True)


@pytest.mark.parametrize("text", [
    "", "   ", "12345", "player_name", "isGrounded", "Assets/Prefabs/Enemy.prefab", "enemy.png",
    "https://example.com", "a1b2c3d4e5f6", "Item01x", "{0}", "<b></b>", "Привет", "idle", "jump_start",
    "wait-for-native-debugger=0", "UnityEngine.CoreModule", "foo();",
])
def test_not_translatable_texts(text):
    assert not looks_translatable(text, strict=True)


def test_soft_mode_accepts_lowercase_words():
    assert looks_translatable("run", strict=False)
    assert not looks_translatable("run", strict=True)


def test_soft_mode_judges_visible_text_not_tags():
    """Теги Ren'Py и перевод строки не делают надпись идентификатором: «_» в {#quick_page}
    и цифры в {size=32} — служебные, игрок видит «Quick» и «Flashing Images»."""
    assert looks_translatable("{#quick_page}Quick")
    assert looks_translatable("{size=32}Flashing\nImages")
    assert looks_translatable("{b}Save{/b}")
    for code in ("player_name", "{#x}item_name", "anim{0}_idle", "{size=32}Item01x", "{#file_time}%H:%M"):
        assert not looks_translatable(code), code
    assert not looks_translatable("{size=32}Flashing\nImages", strict=True)  # строгий режим — как был


# ============================= глоссарий =============================

def test_ui_phrases_keep_case():
    assert ui_phrase("Load Game") == "Загрузить игру"
    assert ui_phrase("QUIT") == "ВЫХОД"
    assert ui_phrase("  Options ") == "  Настройки "
    assert ui_phrase("hp") == "HP"
    assert ui_phrase("Something unusual here") is None


def test_match_case():
    assert match_case("привет", "Hello") == "Привет"
    assert match_case("Привет", "hello") == "привет"
    assert match_case("привет", "HELLO") == "ПРИВЕТ"


# ============================= сервис перевода =============================

class FakeTranslator(Translator):
    cache_id = "fake:1"
    title = "fake"
    context_aware = True
    max_batch_items = 3
    max_batch_chars = 1000

    def __init__(self, fn=None, fatal_on=None):
        self.calls = []
        self.fn = fn or (lambda e: "RU:" + e.source)
        self.fatal_on = fatal_on

    def translate(self, entries, glossary):
        self.calls.append([e.source for e in entries])
        if self.fatal_on and any(self.fatal_on in e.source for e in entries):
            raise TranslatorError("ключ не подходит")
        return {e.id: self.fn(e) for e in entries}

    def shorten(self, text, limit, source):
        return text[:limit]


def _project(tmp_path, *sources, **kw):
    proj = TranslationProject(tmp_path / "g", "test", tmp_path / "w")
    for i, s in enumerate(sources):
        proj.add_entry(Entry(id=f"e{i}", source=s, **kw))
    return proj


def test_service_dedups_and_skips(tmp_path):
    proj = _project(tmp_path, "Hello", "Hello", "World", "{0}", "Привет", "Bye")
    tr = FakeTranslator()
    stats = TranslationService(tr).run(proj)
    flat = [s for call in tr.calls for s in call]
    assert sorted(flat) == ["Bye", "Hello", "World"]  # дубликат и мусор не ушли в перевод
    assert proj.entries[1].translation == "RU:Hello"
    assert proj.entries[3].status == EntryStatus.SKIPPED
    assert proj.entries[4].status == EntryStatus.SKIPPED
    assert stats.translated == 3 and stats.skipped == 2


def test_service_uses_memory_second_time(tmp_path):
    mem = TranslationMemory(tmp_path / "tm.db")
    TranslationService(FakeTranslator(), mem).run(_project(tmp_path, "One", "Two"))
    tr2 = FakeTranslator()
    proj = _project(tmp_path, "One", "Two", "Three")
    stats = TranslationService(tr2, mem).run(proj)
    assert tr2.calls == [["Three"]]
    assert stats.from_memory == 2
    assert proj.entries[0].translation == "RU:One"
    mem.close()


def test_service_rejects_broken_markup(tmp_path):
    proj = _project(tmp_path, "Hello {0}", "Bye [player]")
    tr = FakeTranslator(fn=lambda e: "Привет" if "{0}" in e.source else "Пока [player]")
    stats = TranslationService(tr).run(proj)
    assert proj.entries[0].status == EntryStatus.FAILED
    assert proj.entries[1].translation == "Пока [player]"
    assert stats.failed == 1


def test_service_fatal_error_stops_everything(tmp_path):
    proj = _project(tmp_path, *[f"Line number {i}" for i in range(10)])
    tr = FakeTranslator(fatal_on="Line number 0")
    with pytest.raises(TranslatorError):
        TranslationService(tr).run(proj)
    assert len(tr.calls) == 1  # после фатальной ошибки новые батчи не отправляются


def test_service_cancel(tmp_path):
    proj = _project(tmp_path, *[f"Sentence {i}" for i in range(9)])
    cancel = threading.Event()
    tr = FakeTranslator(fn=lambda e: (cancel.set(), "RU")[1])
    with pytest.raises(TranslationCancelled):
        TranslationService(tr, cancel=cancel).run(proj)
    assert len(tr.calls) == 1


def test_service_length_control(tmp_path):
    """Перевод сильно длиннее места обрезается, но лимит никогда не меньше места самого оригинала:
    абсолютный лимит движка не режет перевод длинного оригинала («описание черты» → «Страстная…»)."""
    proj = _project(tmp_path, "Save", max_length=6, kind=TextKind.UI)
    TranslationService(FakeTranslator(fn=lambda e: "Сохранить игру")).run(proj)
    assert proj.entries[0].translation == "Сохран"                      # переводчик сократил
    proj = _project(tmp_path / "b", "Save the game", max_length=6, kind=TextKind.UI)
    TranslationService(FakeTranslator(fn=lambda e: "Сохранить игру")).run(proj)
    assert proj.entries[0].translation == "Сохранить игру"
    proj = _project(tmp_path / "c", "Save", max_length=6, kind=TextKind.UI)
    proj.entries[0].notes["fits_at_runtime"] = True                 # движок сам ужмёт при показе
    tr = FakeTranslator(fn=lambda e: "Сохранить игру")
    tr.shorten = lambda *a: None                                     # сократить не смог — не режем
    TranslationService(tr).run(proj)
    assert proj.entries[0].translation == "Сохранить игру"


def test_trimmed_translations_are_redone():
    from russificator.translation.length import was_trimmed
    assert was_trimmed("Hot. Extremely good looking. A natural flirt.", "Страстная. Очень красивая…")
    assert not was_trimmed("Wait...", "Подожди…")
    assert not was_trimmed("Save", "Сохранить")
    assert was_trimmed("About", "О пр.") and was_trimmed("Save game", "Сохр. игру")   # обрубки
    assert not was_trimmed("Something is wrong.", "Что-то не так.")


# ============================= промпт и LLM-клиент =============================

def test_parse_reply_variants():
    from russificator.translation.prompt import parse_reply
    assert parse_reply('```json\n{"1": "а"}\n```') == {"1": "а"}
    assert parse_reply('<think>hmm</think>{"1": "а", "2": "б"}') == {"1": "а", "2": "б"}
    assert parse_reply('[{"id": 1, "translation": "а"}]') == {"1": "а"}
    assert parse_reply('{"items": [{"id": "1", "text": "а"}]}') == {"1": "а"}
    with pytest.raises(ValueError):
        parse_reply("не JSON")


def test_build_user_message_short_ids_and_context():
    from russificator.translation.prompt import build_user_message
    entries = [Entry(id="long-id-1", source="Hi, Mary!", speaker="Bob", kind=TextKind.DIALOGUE,
                     neighbors=["Previous line"]),
               Entry(id="long-id-2", source="Quit", kind=TextKind.UI, max_length=10)]
    text, ids = build_user_message(entries, {"Mary": "Мэри", "Unused": ""})
    data = json.loads(text)
    assert ids == {"1": "long-id-1", "2": "long-id-2"}
    assert data["items"][0]["context"] == ["Previous line"]
    assert data["items"][1]["max_chars"] == 10
    assert data["glossary"] == {"Mary": "Мэри"}


class _FakeOpenAI(BaseHTTPRequestHandler):
    """Мини-сервер OpenAI-совместимого API для тестов клиента."""
    script: list = []
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeOpenAI.seen.append(body)
        status, payload = _FakeOpenAI.script.pop(0) if _FakeOpenAI.script else (200, None)
        if payload is None:
            items = json.loads(body["messages"][1]["content"])["items"]
            content = json.dumps({it["id"]: "RU " + it["text"] for it in items}, ensure_ascii=False)
            payload = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        raw = json.dumps({"data": [{"id": "whisper-1"}, {"id": "model-pro-thinking"}, {"id": "model-flash"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_api():
    _FakeOpenAI.script, _FakeOpenAI.seen = [], []
    srv = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_llm_translator_roundtrip(fake_api):
    from russificator.translation.cloud import CloudTranslator
    tr = CloudTranslator("custom", "k", base_url=fake_api, model="m")
    tr.client.local = False
    out = tr.translate([Entry(id="a", source="Hello"), Entry(id="b", source="Bye")], {})
    assert out == {"a": "RU Hello", "b": "RU Bye"}
    assert _FakeOpenAI.seen[0]["response_format"] == {"type": "json_object"}


def test_llm_disables_json_mode_when_unsupported(fake_api):
    from russificator.translation.cloud import CloudTranslator
    _FakeOpenAI.script = [(400, {"error": {"message": "response_format is not supported"}})]
    tr = CloudTranslator("custom", "k", base_url=fake_api, model="m")
    assert tr.translate([Entry(id="a", source="Hi")], {}) == {"a": "RU Hi"}
    assert "response_format" not in _FakeOpenAI.seen[-1]


def test_llm_bad_key_is_fatal(fake_api):
    from russificator.translation.cloud import CloudTranslator
    _FakeOpenAI.script = [(401, {"error": {"message": "invalid api key"}})]
    tr = CloudTranslator("custom", "k", base_url=fake_api, model="m")
    with pytest.raises(TranslatorError) as err:
        tr.translate([Entry(id="a", source="Hi")], {})
    assert err.value.fatal and "Ключ" in str(err.value)


def test_llm_splits_batch_on_garbage(fake_api):
    from russificator.translation.cloud import CloudTranslator
    garbage = {"choices": [{"message": {"content": "sorry, I can't"}, "finish_reason": "stop"}]}
    _FakeOpenAI.script = [(200, garbage)]
    tr = CloudTranslator("custom", "k", base_url=fake_api, model="m")
    out = tr.translate([Entry(id="a", source="One"), Entry(id="b", source="Two")], {})
    assert out == {"a": "RU One", "b": "RU Two"}  # батч поделён пополам и переведён


def test_cloud_list_models_recommends(fake_api):
    from russificator.translation.cloud import list_models
    models = list_models("custom", "k", fake_api)
    assert "whisper-1" not in models
    assert models[0] == "model-flash"  # «думающие» модели — в конце


# ============================= живой перевод =============================

def _get(port, text):
    import urllib.error
    import urllib.parse
    import urllib.request
    url = f"http://127.0.0.1:{port}/translate?from=en&to=ru&text=" + urllib.parse.quote(text)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_live_server_waits_for_translator_and_never_errors_on_bad_markup():
    import threading
    import time
    from russificator.translation.live_server import LiveServer

    ready = threading.Event()
    tr = FakeTranslator(fn=lambda e: "Привет" if e.source == "<b>Hi</b>" else "RU:" + e.source)
    srv = LiveServer(tr)
    port = _free_port()
    # сервер слушает сразу; перевод ждёт, пока «модель загрузится»
    srv.start(port, prepare=lambda: (time.sleep(0.3), ready.set()))
    try:
        t0 = time.monotonic()
        assert _get(port, "Hello there") == (200, "RU:Hello there")
        assert ready.is_set() and time.monotonic() - t0 >= 0.25
        # перевод потерял тег — строка переводится по кускам между тегами (и никогда не 503)
        assert _get(port, "<b>Hi</b>") == (200, "<b>RU:Hi</b>")
        assert _get(port, "N0D3H3X3R") == (200, "N0D3H3X3R")
    finally:
        srv.stop()


def test_live_server_reports_failed_preparation():
    from russificator.translation.live_server import LiveServer

    def boom():
        raise TranslatorError("модель не скачана")
    srv = LiveServer(FakeTranslator())
    port = _free_port()
    srv.start(port, prepare=boom)
    try:
        assert _get(port, "Hello there")[0] == 503
        assert srv.prepare_error == "модель не скачана"
    finally:
        srv.stop()


# ============================= спасение непереведённого =============================

def test_service_rescues_failed_batch_one_by_one(tmp_path):
    """Сбой всей пачки (сеть, мусор в ответе) не оставляет строки английскими: повтор по одной."""
    class Flaky(FakeTranslator):
        def translate(self, entries, glossary):
            self.calls.append([e.source for e in entries])
            if len(entries) > 1:
                raise TranslatorError("временный сбой", fatal=False)
            return {e.id: "RU:" + e.source for e in entries}

    proj = _project(tmp_path, "Open the door.", "Close the window.", "Wait here.")
    stats = TranslationService(Flaky()).run(proj)
    assert stats.failed == 0 and stats.translated == 3
    assert all(e.translation.startswith("RU:") for e in proj.entries)


def test_service_rescues_markup_by_segments(tmp_path):
    """Переводчик теряет теги в целой строке — строка переводится по кускам между тегами."""
    def fn(e):
        return "Привет мир" if "{b}" in e.source else "RU:" + e.source
    proj = _project(tmp_path, "Hello {b}world{/b}!")
    stats = TranslationService(FakeTranslator(fn=fn)).run(proj)
    assert stats.failed == 0
    assert proj.entries[0].translation == "RU:Hello {b}RU:world{/b}!"


def test_service_uses_fallback_translator(tmp_path):
    class Broken(FakeTranslator):
        def translate(self, entries, glossary):
            return {}
    proj = _project(tmp_path, "Save the game?", "Quit to title.")
    stats = TranslationService(Broken(), fallback=FakeTranslator(fn=lambda e: "ЗАПАС:" + e.source)).run(proj)
    assert stats.failed == 0
    assert [e.translation for e in proj.entries] == ["ЗАПАС:Save the game?", "ЗАПАС:Quit to title."]


def test_service_reports_failed_only_after_all_attempts(tmp_path):
    class Broken(FakeTranslator):
        def translate(self, entries, glossary):
            return {}
    proj = _project(tmp_path, "Nothing works.")
    stats = TranslationService(Broken()).run(proj)
    assert stats.failed == 1 and proj.entries[0].status == EntryStatus.FAILED


def test_machine_splits_sentences_for_big_model():
    """Большая модель теряет часть реплики из нескольких предложений — ей строка даётся по предложениям."""
    from russificator.translation.machine import MachineTranslator
    line = "You seem rather lost. Is there anything I can help you with?"
    assert MachineTranslator._sentences(line) == [line]                     # базовой — целиком (как раньше)
    parts = MachineTranslator._sentences(line, always=True)
    assert [p.strip() for p in parts] == ["You seem rather lost.", "Is there anything I can help you with?"]
    assert "".join(parts) == line                                            # склейка без потерь


def test_mask_takes_first_sign_the_model_knows():
    """Знак маркера выбирает переводчик (из тех, что есть в словаре его модели)."""
    masked, originals, sign = markup.mask("Hi {0}", signs="&*")
    assert (masked, sign) == ("Hi &0", "&")
    masked, _, sign = markup.mask("Press &1 then {0}", signs="&*")
    assert (masked, sign) == ("Press &1 then *0", "*")


def _tiny_spm(tmp_path):
    """Крошечная модель sentencepiece: в словаре только буквы, цифры, «&», «*» и «,.!?:()» — «=», «+»,
    «@», «<», «>», «{», «}» вне словаря (как у настоящей модели tc-big)."""
    spm = pytest.importorskip("sentencepiece")
    corpus = tmp_path / "corpus.txt"
    words = "damage speed range warning the door is locked hello where gold of level complete new"
    line = f"{words} {words.upper()} & * 0 1 2 3 4 5 6 7 8 9 , . ! ? : ( )"
    corpus.write_text("\n".join([line] * 200), encoding="utf-8")
    spm.SentencePieceTrainer.train(input=str(corpus), model_prefix=str(tmp_path / "tiny"), vocab_size=200,
                                   hard_vocab_limit=False, character_coverage=1.0, model_type="char",
                                   minloglevel=2)
    # папка пользователя с русскими буквами: sentencepiece сам такой путь на Windows не открывает
    home = tmp_path / "Игорь"
    home.mkdir()
    (home / "source.spm").write_bytes((tmp_path / "tiny.model").read_bytes())
    return home / "source.spm"


def test_machine_keeps_symbols_markers_and_drops_model_additions(tmp_path):
    """Символы вне словаря модели не превращаются в «⁇», маркеры разметки не теряются (знак — из
    словаря модели), маркер, приклеенный к слову, не мешает перевести слово, приписки модели в
    скобках («(англ.)») не попадают в перевод. Модель грузится и из папки с русскими буквами."""
    from russificator.translation import machine
    sp = machine.load_spm(_tiny_spm(tmp_path))
    words = {"damage": "урон", "speed": "скорость", "range": "дальность", "warning": "внимание",
             "the door is locked": "дверь заперта", "hello": "привет"}

    def model(s: str) -> str:
        """Как ведёт себя настоящая модель: символ вне словаря — «⁇», слово, слитое с маркером, не
        переводится, непереводимое — с припиской «(англ.)»."""
        s = out = "".join(" ⁇ " if machine._unknown(sp, ch) else ch for ch in s)
        for en, ru in words.items():
            out = re.sub(rf"\b{en}\b", lambda m, ru=ru: ru.capitalize() if m.group(0)[0].isupper() else ru, out,
                         flags=re.I)
        return out if out != s else s + " (англ.)"

    t = machine.MachineTranslator()
    t._big = type("Big", (), {"src": sp})()
    t._run = lambda sentences: [model(s) for s in sentences]
    assert t._signs() == "&*"
    got = t.translate_texts(["+DAMAGE +SPEED =RANGE", "<b>Warning:</b> the door is locked.", "62CO", "Hello, {0}!"])
    assert got[0] == "+УРОН +СКОРОСТЬ =ДАЛЬНОСТЬ"
    assert got[1] == "<b>Внимание:</b> дверь заперта."
    assert got[2] == "62CO"
    assert got[3] == "Привет, {0}!"
    assert all("⁇" not in g for g in got)


def test_brand_names_are_not_translated():
    from russificator.translation.filters import is_brand
    assert is_brand("Discord") and is_brand("Made with Unity") and is_brand("Patreon")
    assert not is_brand("Discord server rules") and not is_brand("Continue")


def test_phrase_split_across_lines_is_translated_whole():
    """«insert↵coin»: игра разбила фразу на строки ради вёрстки — переводится вся фраза («вставить
    монету», а не «вставить» + «монета»), перевод раскладывается на столько же строк."""
    from russificator.translation import machine
    seen = []

    def run(sentences):
        seen.extend(sentences)
        table = {"Insert coin": "Вставить монету", "Insert": "Вставить", "Coin": "Монета",
                 "Press start to join": "Нажмите старт, чтобы присоединиться", "Options": "Настройки",
                 "Quit": "Выход", "You found a key.": "Вы нашли ключ.", "It is rusty": "Он ржавый"}
        return [table.get(s[:1].upper() + s[1:], s) for s in sentences]
    t = machine.MachineTranslator()
    t._big = type("Big", (), {"src": None})()
    t._run = run
    got = t.translate_texts(["insert\r\ncoin", "Press start\nto join", "Options\nQuit",
                             "You found a key.\nIt is rusty"])
    assert got[0] == "вставить\r\nмонету"
    assert got[1] == "Нажмите старт,\nчтобы присоединиться"
    assert got[2] == "Настройки\nВыход"                      # список пунктов — строки по отдельности
    assert got[3] == "Вы нашли ключ.\nОн ржавый"             # конец фразы — перенос настоящий
    assert "insert coin" in seen and "coin" not in seen
    assert machine.join_soft_wraps("{0} entries found\r\n{1} challenges passed")[1] is None
    assert machine.join_soft_wraps("damaging\n<color=#d4be9c>opposing heroes</color>")[1] is not None


def test_old_line_by_line_translations_are_redone(tmp_path):
    """Проект, переведённый прошлой версией (фраза «insert↵coin» — по строкам), при новой русификации
    переводит такие строки заново; остальные переводы берутся из прошлого раза."""
    from russificator.core.pipeline import Pipeline
    from russificator.core.universal import Entry, EntryStatus, TranslationProject
    from russificator.translation import machine

    def project(entries):
        p = TranslationProject(tmp_path / "game", "unity", tmp_path / "work")
        p.entries = [Entry(id=k, source=k, target_key=k) for k in entries]
        return p
    old = project(["insert\r\ncoin", "Options\nQuit", "Hello", "-NEW-"])
    for e, tr in zip(old.entries, ["вставить\r\nмонета", "Настройки\nВыход", "Привет", "-НОВОСТИ-"]):
        e.translation, e.status = tr, EntryStatus.APPROVED
    old.meta["translator"] = f"MachineTranslator:{machine.MachineTranslator().cache_id}"
    old.save()
    new = project(["insert\r\ncoin", "Options\nQuit", "Hello", "-NEW-"])
    Pipeline(machine.MachineTranslator())._merge_previous(new, tmp_path / "work")
    got = {e.source: (e.status, e.translation) for e in new.entries}
    assert got["insert\r\ncoin"] == (EntryStatus.NEW, None)
    assert got["Options\nQuit"][1] == "Настройки\nВыход" and got["Hello"][1] == "Привет"
    assert got["-NEW-"] == (EntryStatus.NEW, None)                  # слово словаря в рамке — из словаря
    assert new.meta["rules"] == 5


def test_letters_initials_and_alphabet_rows_stay_as_is():
    """Сетка ввода имени («ABCD↵EFGH…»), инициалы игроков («DAP»), окончания порядковых («2ND»)
    и буквы не «переводятся» транслитерацией («АБСД», «ДАП», «НД») — остаются как в игре."""
    from russificator.translation.filters import is_transliteration, looks_translatable
    assert not looks_translatable("ABCD\nEFGH\nIJKL\nMNOP\nQRST") and not looks_translatable("UVWX\nYZ")
    assert looks_translatable("Start\nQuit") and looks_translatable("ABC Corp")
    for src, tr in (("DAP", "ДАП"), ("ND", "НД"), ("TH", "ТХ"), ("B", "Б"), ("JED", "ДЖЕД"), ("XP", "ХР")):
        assert is_transliteration(src, tr), src
    for src, tr in (("MAP", "КАРТА"), ("On", "Вкл"), ("GO", "ВПЕРЁД!"), ("Start game", "Начать игру")):
        assert not is_transliteration(src, tr), src


def test_title_case_and_caps_leftovers_are_retried_in_sentence_case():
    """Модель принимает Заглавный Регистр и КАПС за имена («Sallow Cavern» → «Sallow Пещера») —
    такие строки переводятся ещё раз в обычном регистре; берётся вариант, где английского меньше,
    выделение капсом в разметке сохраняется."""
    from russificator.translation import machine
    table = {"Shattered Roost": "Shattered Roost написал", "Shattered roost": "Разбитое гнездо",
             "Last attempt: LEVEL 7": "Последняя попытка: LEVEL 7", "Last attempt: level 7": "Последняя попытка: уровень 7",
             "Challenge KOURAK": "Вызов KOURAK", "Challenge kourak": "Вызов kourak",
             "Your @0 WRATH @1 grows": "Ваш @0 WRATH @1 растёт", "Your @0 wrath @1 grows": "Ваш @0 гнев @1 растёт"}
    t = machine.MachineTranslator()
    t._big = type("Big", (), {"src": None})()
    t._signs = lambda: "@#"
    t._run = lambda sentences: [table.get(s, s) for s in sentences]
    got = t.translate_texts(["Shattered Roost", "Last attempt: LEVEL 7", "Challenge KOURAK", "Your <b>WRATH</b> grows"])
    assert got[0] == "Разбитое гнездо"
    assert got[1] == "Последняя попытка: уровень 7"
    assert got[2] == "Вызов KOURAK"                        # имя не перевелось и так — оставлен первый вариант
    assert got[3] == "Ваш <b>ГНЕВ</b> растёт"
    assert t.redo("Shattered Roost", "Shattered Roost написал", 2) and not t.redo("Shattered Roost", "Разбитое гнездо", 2)


def test_any_translator_keeps_lines_the_game_wrapped():
    """Нейросеть склеивает строки, которые игра перенесла по ширине: перевод раскладывается обратно
    на столько же строк — иначе в TextMesh (без своего переноса) строка уходит за край экрана."""
    from russificator.translation.markup import normalize_layout
    src = "0 / 61 entries found\r\n0 challenges passed\n\nDiscover more entries by inhabiting\r\nmonsters in-game"
    llm = "0 / 61 записей найдено\r\n0 испытаний пройдено\n\nОткрывайте больше записей, вселяясь в монстров в игре"
    got = normalize_layout(src, llm)
    assert got.count("\n") == src.count("\n")
    assert got.startswith("0 / 61 записей найдено\r\n0 испытаний пройдено\n\nОткрывайте")
    assert normalize_layout("Options\nQuit", "Настройки\nВыход") == "Настройки\nВыход"


# ============================= края строки и мусор вместо перевода =============================

@pytest.mark.parametrize("source,translation,expected", [
    ("''Let's do it.''", "• Сделаем это", "''Сделаем это''"),                  # кавычки реплики, чужой маркер
    ("''Damn, nearly had the thing...''", "''Черт, чуть не попал...", "''Черт, чуть не попал...''"),
    ("{i}Sybil holds her silence.{/i}", "{i}• (Наблюдение) ''Сибиль хранит молчание.{/i}",
     "{i}Сибиль хранит молчание.{/i}"),                                      # приписки соседней строки
    ("{i}• (Keen Eye) ''Pepsi??? I'm a Coke guy.''{/i}", "{i}• (Зоркий глаз) Пепси??? Я за колу.{/i}",
     "{i}• (Зоркий глаз) ''Пепси??? Я за колу.''{/i}"),
    ("Hello there!", '"Привет!"', "Привет!"),                                 # модель обернула в кавычки
    ('The "Deep" one', "«Глубокий» один", "«Глубокий» один"),                # кавычки внутри — не трогать
    (" Save \n", "Сохранить", " Сохранить \n"),
])
def test_edges_follow_the_original(source, translation, expected):
    from russificator.translation import edges
    assert edges.fix(source, translation) == expected


@pytest.mark.parametrize("source,translation,speaker", [
    ("''Are you going to be okay after what happened with—''", "1", None),            # номер из пакета
    ("''Those birds is like family to me. I raised them from eggs.''", "Duke", "Duke"),
    ("''There's a time and a place, son, and this ain't either of them.''",
     '{"9": "Есть время и место", "type": "dialogue", "speak', None),                   # кусок JSON
    ("Some places have a pull on people, don't they? Sometimes you think you have a choice.",
     "Места тянут.", None),                                                              # пересказ
])
def test_garbage_translations_are_rejected(source, translation, speaker):
    from russificator.translation import edges
    fixed, why = edges.check(source, translation, speaker)
    assert fixed is None and why


def test_json_reply_with_one_russian_string_is_salvaged():
    from russificator.translation import edges
    fixed, why = edges.check("Hello there, friend!", '{"1": "Привет, друг!"}')
    assert fixed == "Привет, друг!" and why is None


def test_identifiers_and_brands_are_markup():
    """Код, плагины и бренды внутри текста («HIME_PreTitleEvents», «MikuMikuDance», «YouTube») —
    неприкосновенная разметка: машинный перевод портил их («heme_PreTitleСобытия»). Крик капсом —
    обычный текст, строка из одних идентификаторов — не текст."""
    masked, found, _ = markup.mask("HimeWorks --- HIME_SyncSaveData, watch my YouTube! STOP!")
    assert found == ["HimeWorks", "HIME_SyncSaveData", "YouTube"] and "STOP" in masked
    assert not markup.has_words("SRD_GameUpgrade , SRD_HUDMaker")
    assert markup.same_markup("Use $MUSH_Menu_p1 now", "Используй $MUSH_Menu_p1 сейчас")
    assert not markup.same_markup("Use $MUSH_Menu_p1 now", "Используй $mush_Меню_p1 сейчас")


def test_live_local_model_fits_free_vram(monkeypatch):
    """llama.cpp кладёт на видеокарту столько слоёв, сколько влезает в СВОБОДНУЮ память: в живом
    переводе игра её уже заняла, и «лучшая» модель почти вся считалась процессором. Живой перевод
    берёт самую крупную установленную модель, которая помещается; обычная русификация — нет."""
    from russificator.translation import local_llm
    monkeypatch.setattr(local_llm, "gpu_devices",
                        lambda: [{"id": "Vulkan0", "name": "RTX", "vram_gb": 16.0, "free_gb": 5.0}])
    monkeypatch.setattr(local_llm, "model_installed", lambda p: True)
    live = local_llm.LocalTranslator("gemma4-12b", fit_vram=True)
    msgs = []
    live._fit_to_vram(lambda m, f=None: msgs.append(m))
    assert live.preset.id == "gemma4-e4b" and live.cache_id == "llm:gemma4-e4b" and msgs
    files = local_llm.LocalTranslator("gemma4-12b")
    files._fit_to_vram(lambda m, f=None: None)
    assert files.preset.id == "gemma4-12b"                 # выбор пользователя, только предупреждение
    monkeypatch.setattr(local_llm, "gpu_devices",
                        lambda: [{"id": "Vulkan0", "name": "RTX", "vram_gb": 16.0, "free_gb": 12.0}])
    roomy = local_llm.LocalTranslator("gemma4-12b", fit_vram=True)
    roomy._fit_to_vram(lambda m, f=None: None)
    assert roomy.preset.id == "gemma4-12b"


# ---------- клавиши, имена, мусор живого перевода (отзыв 2026-10-27) ----------

def test_key_names_and_control_hints():
    """«SPACE» → «ПРОБЕЛ», а не «КОСМОС»; Esc/Shift/Ctrl — латиницей, как на клавиатуре; подсказка из
    колонок переводится по колонкам, клавиша в колонке остаётся."""
    from russificator.translation import keys
    assert keys.plan("SPACE") == [(False, "ПРОБЕЛ")] and keys.plan("Space") == [(False, "Пробел")]
    for k in ("ESC", "SHIFT", "LEFTCTRL", "Left Ctrl", "F5", "TAB", "ENTER"):
        assert keys.plan(k) == [(False, k)], k
    assert keys.plan("Enter") is None and keys.plan("Escape") is None      # с заглавной — может быть командой
    hint = keys.plan("TAB Cancel    SPACE Confirm")
    assert keys.to_translate(hint) == ["Cancel", "Confirm"]
    assert keys.assemble(hint, {"Cancel": "Отмена", "Confirm": "Подтвердить"}) == "TAB Отмена    ПРОБЕЛ Подтвердить"
    assert keys.plan("Press SPACE to jump") is None and keys.plan("Space Station Omega") is None
    for text in ("END OF CHAPTER", "SPACE STATION", "DELETE SAVE?", "HOME SWEET HOME"):
        assert keys.plan(text) is None, text
    assert keys.to_translate(keys.plan("ESC to exit")) == ["to exit"]
    for combo in ("Ctrl + G", "Alt+F4", "Ctrl +", "Ctrl + Left Alt", "Shift + Tab"):
        assert keys.plan(combo) == [(False, combo)], combo
    assert keys.plan("Rock + Roll") is None and keys.plan("A + B") is None


def test_service_translates_keys_without_translator(tmp_path):
    from russificator.core.universal import EntryStatus
    tr = FakeTranslator(lambda e: {"Cancel": "Отмена", "Confirm": "Подтвердить"}.get(e.source, "Перевод"))
    p = _project(tmp_path, "SPACE", "ESC", "TAB Cancel    SPACE Confirm")
    TranslationService(tr).run(p)
    got = {e.source: e.translation for e in p.entries}
    assert got["SPACE"] == "ПРОБЕЛ" and got["ESC"] == "ESC"
    assert got["TAB Cancel    SPACE Confirm"] == "TAB Отмена    ПРОБЕЛ Подтвердить"
    sent = [s for call in tr.calls for s in call]
    assert "SPACE" not in sent and "ESC" not in sent
    assert all(e.status == EntryStatus.APPROVED for e in p.entries)


def test_names_dialogue_words_and_slash_labels():
    from russificator.translation import filters
    assert not filters.is_transliteration("Tom", "Том") and not filters.is_transliteration("Sam", "Сэм")
    assert filters.is_transliteration("DAP", "ДАП") and filters.is_transliteration("st", "ст")
    assert filters.spelled_in_cyrillic("dang", "дэнг") and not filters.spelled_in_cyrillic("dang", "черт")
    assert filters.looks_translatable("Y-You…!", strict=True) and filters.looks_translatable('"Wait…!"', strict=True)
    assert filters.looks_translatable("Save/Load") and not filters.looks_translatable("Assets/Prefabs")
    assert filters.looks_spoken("sure") and filters.looks_spoken("hey sam") and not filters.looks_spoken("ui_btn_ok")
    for tech in ("1920 x 1080 @ 50Hz", "x 1080 @ 50Hz", "144Hz", "60 FPS", "4K"):
        assert not filters.looks_translatable(tech), tech
    assert filters.looks_translatable("Level 5") and filters.looks_translatable("60 seconds left")


def test_edges_reject_mixed_script_and_fix_ukrainian_letters():
    from russificator.translation import edges
    assert edges.check("TINFOILHAT", "ТINFOILHAT")[0] is None
    assert edges.check("WHAT? No way!", "ЧТO? Не может быть!") == ("ЧТО? Не может быть!", None)
    assert edges.check("DAVID MOST", "ДЕЙВІД МОСТ") == ("ДЕЙВИД МОСТ", None)
    assert edges.check("Use the iPhone app", "Используй приложение для iPhone") == ("Используй приложение для iPhone", None)


def test_machine_retry_helpers():
    from russificator.translation import machine
    assert machine._repunct("Who cares lemme go") == "Who cares lemme go."
    assert machine._repunct("Who cares?") == "Who cares"
    assert machine._unrepunct("Who cares?", "Да какая разница.") == "Да какая разница?"
    assert machine._unrepunct("Who cares lemme go", "Кому какое дело.") == "Кому какое дело"
    assert machine._leftover_common("Who cares lemme go", "Кто cares, давай") == 1
    assert machine._leftover_common("Ask Tom", "Спроси Tom") == 0


def test_key_translations_from_old_versions_are_redone_only_when_wrong():
    from russificator.translation import keys
    assert keys.stale("SPACE", "КОСМОС") and not keys.stale("SPACE", "ПРОБЕЛ")
    assert keys.stale("TAB Cancel    SPACE Confirm", "Подтвердить отмену вкладок")
    assert not keys.stale("TAB Cancel    SPACE Confirm", "TAB Отмена    ПРОБЕЛ Подтвердить")
    assert not keys.stale("Name     Price", "Название     Цена")       # колонки без клавиш — как были
    assert not keys.stale("Hello there", "Привет")


def test_fullwidth_punctuation_and_dictionary_compounds_are_text():
    """«WorkBench：Cultivate» (китайская раскладка) — текст, а не идентификатор; обычное слово с заглавной
    в середине («WorkBench», «FireBall») переводится, бренды и имена из кода («YouTube») остаются."""
    assert looks_translatable("WorkBench：Cultivate", strict=True)
    assert not looks_translatable("UnchartedRealm") and not looks_translatable("CardMC_Hero")
    assert markup.tokens("Open the WorkBench on YouTube <b>now</b>") == ["YouTube", "<b>", "</b>"]
    assert markup.same_markup("Recover WorkBench", "Восстановить верстак")
    # теги TMP с «=» и «;» в конце пункта описания — не key=value и не код
    assert looks_translatable("<line-indent=20%>The floor here... why does it look like a gameboard?", strict=True)
    assert looks_translatable("Lifespan +20 years after cultivation;", strict=True)
    assert not looks_translatable("return x;", strict=True) and not looks_translatable("speed=10", strict=True)


def test_machine_input_normalized_and_title_labels():
    from russificator.translation import machine
    assert machine._for_model("WorkBench：Cultivate") == "Workbench: Cultivate"
    assert machine._for_model("Watch YouTube") == "Watch YouTube"
    assert machine._title_label("Main Dish") and machine._title_label("Auto Stack Radius")
    assert not machine._title_label("Main dish") and not machine._title_label("Go home.")
    assert machine._inner_capitals("Главная Блюда") == 1 and machine._inner_capitals("Главное блюдо") == 0


def test_stacked_button_label_translated_whole():
    """«LEVEL↵SELECT» по строкам — «УРОВЕНЬ↵ВЫБРАТЬ»; переводится вся подпись и раскладывается обратно."""
    from russificator.translation.glossary import ui_phrase
    assert ui_phrase("LEVEL\nSELECT") == "ВЫБОР\nУРОВНЯ"
    assert ui_phrase("Done!") == "Готово!" and ui_phrase("Quit?") is None
    assert ui_phrase("Attack\nDefend") is None


def test_machine_rules_v5_redo_old_translations():
    """Переводы прошлых версий, которые нынешние правила сделали бы иначе, переводятся заново."""
    from russificator.translation import machine
    assert machine._redo_v5("REPLAY", "ВОСПРОИЗВЕСТИ") and not machine._redo_v5("REPLAY", "ЗАНОВО")
    assert machine._redo_v5("Main Dish", "Главная Блюда") and not machine._redo_v5("Main Dish", "Основное блюдо")
    assert machine._redo_v5("WorkBench：Cultivate", "WorkBench:Культивировать")
    assert not machine._redo_v5("Hello there", "Привет")


def test_game_terms_genre_and_leftovers(tmp_path):
    """Словарь терминов игры: карточная игра — deck это колода (падеж сохраняется), термин, оставшийся в
    переводе по-английски, заменяется переводом термина, но не посреди английской фразы и не в разметке."""
    from russificator.translation import terms
    from russificator.translation.glossary import apply_glossary
    src = [f"Draw a card number {i}." for i in range(20)]
    assert terms.genres(src) == ["cards"] and terms.genres(["Hello"] * 50) == []
    rules = terms.genre_rules()["cards"]["stems"]
    assert terms.stem_fix("Battle Deck", "Боевая палуба", rules) == "Боевая колода"
    assert terms.stem_fix("Disable battle deck sorting", "Отключить сортировку боевой палубы", rules) \
        == "Отключить сортировку боевой колоды"
    assert terms.stem_fix("The ship deck is wet", "Палуба корабля мокрая", rules) == "Палуба корабля мокрая"
    found = terms.collect(["This is a <color=#f3b123>Workbench</color>, which represents <b>30%</b>.",
                           "Open the WorkBench.", "{color=#fff}Mana Potion{/color} restores mana"])
    assert set(found) == {"Workbench", "WorkBench", "Mana Potion"}
    tr = "Это <color=#f3b123>workbench</color>, который представляет"
    assert apply_glossary(tr, {"Workbench": "Верстак"}) == "Это <color=#f3b123>верстак</color>, который представляет"
    assert apply_glossary("Но бонус от Proficiency in Poison Arts", {"Poison": "Яд"}) == \
        "Но бонус от Proficiency in Poison Arts"
    assert apply_glossary("<line-indent=8%>Line", {"Line": "Строка"}) == "<line-indent=8%>Строка"
    p = TranslationProject(tmp_path / "game", "unity", tmp_path / "work")
    p.entries = [Entry(id=str(i), source=s, target_key=str(i)) for i, s in
                 enumerate(src + ["This is a <color=#f3b123>Workbench</color>.", "Skill Deck"])]
    for e, t in zip(p.entries[-2:], ["Это <color=#f3b123>workbench</color>.", "Навык палубы"]):
        e.translation, e.status = t, EntryStatus.APPROVED
    assert terms.polish(p, {"Workbench": "Верстак"}) == 2
    assert [e.translation for e in p.entries[-2:]] == ["Это <color=#f3b123>верстак</color>.", "Навык колоды"]


def test_game_terms_translated_once_with_translator():
    from russificator.translation import terms

    class Fake:
        def translate(self, entries, glossary):
            return {e.id: {"Workbench": "Верстак", "Mana Potion": "Mana зелье"}.get(e.source, "") for e in entries}
    entries = [Entry(id=str(i), source=s, target_key=str(i)) for i, s in
               enumerate(["Use the <b>Workbench</b>.", "A <b>Mana Potion</b>.", "Go <b>Home</b>."])]
    got = terms.build(entries, Fake(), {"Home": "Дом"})
    assert got == {"Workbench": "Верстак"}            # «Mana зелье» — с английским словом, «Home» уже в глоссарии


def test_translation_report_counts_and_suspicious(tmp_path, monkeypatch):
    """Отчёт о переводе: строки по источникам, не переведённое, подозрительные переводы (английские слова,
    пословный Заглавный Регистр — но не имена), без личных путей."""
    from russificator import stand
    from russificator.core import pipeline
    game = tmp_path / "Game"
    game.mkdir()
    work = tmp_path / "work"
    monkeypatch.setattr(pipeline, "project_dir", lambda g: work if g == game else tmp_path / "none")
    p = TranslationProject(game, "renpy", work)
    rows = [("Main Dish", "Главная Блюда", "script.rpy"), ("Su Lianyan", "Су Лянян", "script.rpy"),
            ("Open the gate", "Открой gate", "options.rpy"), ("Hello", None, "script.rpy")]
    for i, (s, t, sc) in enumerate(rows):
        e = Entry(id=str(i), source=s, target_key=str(i), scene=sc)
        e.translation, e.status = (t, EntryStatus.APPROVED) if t else (None, EntryStatus.FAILED)
        p.entries.append(e)
    p.meta["game_title"] = "Game"
    p.save()
    text = stand.build(game)
    assert "всего 4, переведено 3, не переведено 1" in text and "script.rpy: 3" in text
    assert "`Main Dish` → `Главная Блюда`" in text and "`Su Lianyan`" not in text
    assert "`Open the gate` → `Открой gate`" in text and "`Hello`" in text
    assert stand.build(tmp_path / "nothing") is None
