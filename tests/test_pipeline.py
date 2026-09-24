"""Сквозные тесты пайплайна с фейковым переводчиком."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from russificator.core.pipeline import Pipeline, PipelineOptions
from russificator.core.universal import TranslationProject
from russificator.translation.base import Translator, TranslatorError


class FakeTranslator(Translator):
    cache_id = "fake:pipeline"
    title = "fake"
    context_aware = True

    def __init__(self, fail_with=None, on_call=None):
        self.calls = 0
        self.fail_with = fail_with
        self.on_call = on_call
        self.prepared = False
        self.closed = False

    def prepare(self, status=None, cancel=None):
        self.prepared = True

    def translate(self, entries, glossary):
        self.calls += 1
        if self.on_call:
            self.on_call(self)
        if self.fail_with:
            raise self.fail_with
        return {e.id: "RU " + e.source for e in entries}

    def close(self):
        self.closed = True


@pytest.fixture
def game(tmp_path):
    root = tmp_path / "MVGame"
    data = root / "www" / "data"
    data.mkdir(parents=True)
    (root / "www" / "js").mkdir()
    (root / "www" / "js" / "rpg_core.js").write_text("//", encoding="utf-8")
    events = [None] + [{"id": i, "name": f"EV{i}", "pages": [{"list": [
        {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]},
        {"code": 401, "indent": 0, "parameters": [f"Line number {i} of the story."]},
        {"code": 0, "indent": 0, "parameters": []}]}]} for i in range(1, 41)]
    (data / "Map001.json").write_text(json.dumps({"events": events}), encoding="utf-8")
    return root


def _options(tmp_path) -> PipelineOptions:
    return PipelineOptions(use_memory=False)


def test_pipeline_end_to_end_and_rerun(game, tmp_path):
    events = []
    work = tmp_path / "work"
    tr = FakeTranslator()
    res = Pipeline(tr, _options(tmp_path), on_event=events.append).run(game, work)
    assert res.success, res.summary()
    assert res.translated_lines == 40 and res.failed_lines == 0
    assert tr.prepared and tr.closed
    stages = [e["id"] for e in events if e["type"] == "stage"]
    assert stages == ["detect", "prepare", "extract", "translator", "translate", "inject", "font"]
    text = (game / "www/data/Map001.json").read_text(encoding="utf-8")
    assert "RU Line number 7 of the story." in text

    # повторный запуск: сначала оригиналы, текст берётся из них, переводы — из прошлого проекта
    tr2 = FakeTranslator()
    res2 = Pipeline(tr2, _options(tmp_path)).run(game, work)
    assert res2.success and tr2.calls == 0
    text = (game / "www/data/Map001.json").read_text(encoding="utf-8")
    assert "RU RU" not in text and "RU Line number 7" in text


def test_pipeline_fatal_error_leaves_game_untouched(game, tmp_path):
    before = (game / "www/data/Map001.json").read_text(encoding="utf-8")
    res = Pipeline(FakeTranslator(fail_with=TranslatorError("ключ не подходит")),
                   _options(tmp_path)).run(game, tmp_path / "w")
    assert not res.success and "ключ не подходит" in res.errors[0]
    assert (game / "www/data/Map001.json").read_text(encoding="utf-8") == before
    assert not (game / "russificator_backup").exists()


def test_pipeline_cancel_then_resume(game, tmp_path):
    cancel = threading.Event()
    work = tmp_path / "w"
    tr = FakeTranslator(on_call=lambda t: cancel.set())
    tr.max_batch_items = 5
    res = Pipeline(tr, _options(tmp_path), cancel=cancel).run(game, work)
    assert res.cancelled and not res.injected
    saved = TranslationProject.load(work)
    done = sum(1 for e in saved.entries if e.translation)
    assert 0 < done < 40                                     # прогресс сохранён

    tr2 = FakeTranslator()
    tr2.max_batch_items = 5
    res2 = Pipeline(tr2, _options(tmp_path)).run(game, work)
    assert res2.success and tr2.calls == (40 - done + 4) // 5  # переведено только оставшееся


def test_pipeline_unknown_game(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    res = Pipeline(FakeTranslator(), _options(tmp_path)).run(empty, tmp_path / "w")
    assert not res.success and "Не удалось определить движок" in res.errors[0]


def test_pipeline_other_translator_translates_again(game, tmp_path):
    """Сменили способ перевода (машинный -> нейросеть) — старые переводы не подставляются."""
    work = tmp_path / "work"
    assert Pipeline(FakeTranslator(), _options(tmp_path)).run(game, work).success

    class BetterTranslator(FakeTranslator):
        cache_id = "llm:better"

        def translate(self, entries, glossary):
            self.calls += 1
            return {e.id: "ХОРОШО " + e.source for e in entries}

    tr = BetterTranslator()
    assert Pipeline(tr, _options(tmp_path)).run(game, work).success
    assert tr.calls > 0
    text = (game / "www/data/Map001.json").read_text(encoding="utf-8")
    assert "ХОРОШО Line number 7" in text and "RU Line" not in text
