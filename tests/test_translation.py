"""Тесты слоя перевода: разметка, фильтр, глоссарий, сервис, LLM-клиент."""

from __future__ import annotations

import json
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
    proj = _project(tmp_path, "Save the game", max_length=6, kind=TextKind.UI)
    TranslationService(FakeTranslator(fn=lambda e: "Сохранить игру")).run(proj)
    assert proj.entries[0].translation == "Сохран"


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
