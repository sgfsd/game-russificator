"""Тесты ядра: универсальный формат, глоссарий, длина, бэкап и откат."""

from __future__ import annotations

import pytest

from russificator.core.universal import Entry, EntryStatus, TextKind, TranslationProject
from russificator.translation.glossary import apply_glossary, GlossaryBuilder
from russificator.translation.length import validate_length, _hard_trim


def _entry(source="Hello", **kw):
    defaults = dict(id="e1", source=source)
    defaults.update(kw)
    return Entry(**defaults)


# ---------- универсальный формат ----------

def test_project_save_load(tmp_path):
    proj = TranslationProject(tmp_path / "game", "renpy", tmp_path)
    e = _entry(source="Hi!", kind=TextKind.DIALOGUE, speaker="e",
               translation="Привет!", status=EntryStatus.TRANSLATED)
    proj.add_entry(e)
    proj.glossary["Alice"] = "Алиса"
    proj.save()
    loaded = TranslationProject.load(tmp_path)
    assert loaded.engine == "renpy"
    assert loaded.entries[0].translation == "Привет!"
    assert loaded.entries[0].kind == TextKind.DIALOGUE
    assert loaded.glossary["Alice"] == "Алиса"


# ---------- глоссарий ----------

def test_apply_glossary_forced_replacement():
    assert apply_glossary("Alice went home", {"Alice": "Алиса"}) == "Алиса went home"


def test_glossary_builder_marks_names():
    proj = TranslationProject(".", "test", ".")
    proj.add_entry(_entry(id="1", source="Alice", kind=TextKind.CHARACTER_NAME))
    proj.add_entry(_entry(id="2", source="Hi!", speaker="Bob", kind=TextKind.DIALOGUE))
    proj.add_entry(_entry(id="3", source="This is a long sentence with dots.", kind=TextKind.DIALOGUE))
    b = GlossaryBuilder()
    b.scan(proj)
    terms = b.result()
    assert "Alice" in terms
    assert "Bob" in terms
    assert "This is a long sentence with dots." not in terms


# ---------- контроль длины ----------

def test_validate_length_passes_short():
    e = _entry(max_length=50)
    ok, text = validate_length(e, "Привет, мир!", None)
    assert ok and text == "Привет, мир!"


class _NoShortenBackend:
    def shorten(self, *a, **kw):
        return None


def test_validate_length_trims_without_backend():
    e = _entry(max_length=10, source="Hi")
    ok, text = validate_length(e, "Очень длинный перевод, который не влезет", _NoShortenBackend())
    assert not ok
    assert len(text) <= 10
    assert text.endswith("…")


def test_hard_trim_by_word():
    assert _hard_trim("один два три", 10) == "один два…"


# ---------- статистика ----------

def test_stats_and_progress():
    proj = TranslationProject(".", "test", ".")
    proj.add_entry(_entry(id="1", status=EntryStatus.TRANSLATED, translation="x"))
    proj.add_entry(_entry(id="2", status=EntryStatus.NEW))
    proj.add_entry(_entry(id="3", status=EntryStatus.FAILED))
    st = proj.stats()
    assert st["total"] == 3 and st["translated"] == 1 and st["failed"] == 1


# ---------- бэкап и откат ----------

def test_backup_restore_roundtrip(tmp_path):
    from russificator.core.backup import GameBackup
    from russificator.core.restore import restore_backups
    game = tmp_path / "Game"
    (game / "data").mkdir(parents=True)
    orig = game / "data" / "Map001.json"
    orig.write_text("original", encoding="utf-8")
    b = GameBackup(game)
    b.write_text(orig, "переведено")
    b.write_text(game / "fonts" / "new.css", "@font-face{}")
    assert orig.read_text(encoding="utf-8") == "переведено"
    assert GameBackup(game).exists  # манифест перечитывается с диска

    count, notes = restore_backups(game)
    assert count == 2
    assert orig.read_text(encoding="utf-8") == "original"
    assert not (game / "fonts").exists()          # созданное удалено вместе с пустой папкой
    assert not (game / "russificator_backup").exists()


def test_backup_keeps_first_original(tmp_path):
    from russificator.core.backup import GameBackup
    game = tmp_path / "Game"
    game.mkdir()
    f = game / "a.txt"
    f.write_text("v1", encoding="utf-8")
    b = GameBackup(game)
    b.write_text(f, "v2")
    b.write_text(f, "v3")  # повторная правка не должна перезаписать оригинал в бэкапе
    assert (game / "russificator_backup" / "a.txt").read_text(encoding="utf-8") == "v1"


def test_restore_without_backup(tmp_path):
    from russificator.core.restore import restore_backups
    count, notes = restore_backups(tmp_path)
    assert count == 0 and "не найдена" in notes[0]
