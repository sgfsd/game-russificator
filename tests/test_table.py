"""Таблица перевода: выгрузка «оригинал — перевод» и словаря терминов, загрузка исправлений."""

from __future__ import annotations

import csv
import io

import pytest

from russificator.core import table
from russificator.core.universal import Entry, EntryStatus, TextKind, TranslationProject


def _project(tmp_path):
    work = tmp_path / "work"
    p = TranslationProject(tmp_path / "game", "rpgmaker", work)
    p.entries = [
        Entry(id="1", source="Hello, [player]!", translation="Привет, [player]!", status=EntryStatus.APPROVED,
              kind=TextKind.DIALOGUE, target_key="Map001.json|1"),
        Entry(id="2", source="Hello, [player]!", translation="Привет, [player]!", status=EntryStatus.APPROVED,
              kind=TextKind.DIALOGUE, target_key="Map002.json|1"),
        Entry(id="3", source="Save", translation="Сохр.", status=EntryStatus.TRANSLATED, kind=TextKind.UI,
              target_key="System.json|1"),
        Entry(id="4", source="Not done yet", status=EntryStatus.FAILED, kind=TextKind.OTHER, target_key="x"),
    ]
    p.glossary = {"Mana": "Мана", "Tom": ""}
    p.save()
    return work


def _read(path):
    text = path.read_bytes().decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text), delimiter=";"))


def test_export_table_for_excel(tmp_path):
    work = _project(tmp_path)
    res = table.export_table(work, tmp_path / "out", "My Game")
    t = (tmp_path / "out" / "My Game — перевод.csv")
    assert res["rows"] == 3 and t.read_bytes().startswith(b"\xef\xbb\xbf")       # метка UTF-8 — для Excel
    rows = _read(t)
    assert rows[0] == list(table.COLUMNS)
    assert rows[1] == ["Hello, [player]!", "Привет, [player]!", "готово", "реплика", "Map001.json", "2"]
    assert rows[3][:3] == ["Not done yet", "", "не переведено"]
    terms = _read(tmp_path / "out" / "My Game — термины.csv")
    assert terms[1:] == [["Mana", "Мана"], ["Tom", ""]]


def test_import_edited_table(tmp_path):
    work = _project(tmp_path)
    table.export_table(work, tmp_path, "g")
    rows = _read(tmp_path / "g — перевод.csv")
    rows[2][1] = "Сохранить"                    # исправили обрубок
    rows[3][1] = "Ещё не готово"                # перевели то, что не перевелось
    buf = io.StringIO()
    csv.writer(buf, delimiter=",").writerows(rows)            # Google Таблицы сохраняют с запятой
    edited = tmp_path / "edited.csv"
    edited.write_text(buf.getvalue(), encoding="utf-8")
    assert table.import_table(work, edited)["translations"] == 2
    p = TranslationProject.load(work)
    by = {e.id: e for e in p.entries}
    assert by["3"].translation == "Сохранить" and by["3"].status == EntryStatus.APPROVED
    assert by["4"].translation == "Ещё не готово" and by["4"].notes.get("edited")


def test_import_keeps_markup_safe(tmp_path):
    work = _project(tmp_path)
    bad = tmp_path / "bad.csv"
    bad.write_text("Оригинал;Перевод\nHello, [player]!;Привет, игрок!\n", encoding="utf-8")
    with pytest.raises(table.TableError):                     # потерянная вставка сломала бы игру
        table.import_table(work, bad)
    assert TranslationProject.load(work).entries[0].translation == "Привет, [player]!"


def test_import_glossary_and_foreign_files(tmp_path):
    work = _project(tmp_path)
    g = tmp_path / "terms.csv"
    g.write_text("Термин;Перевод\nTom;Том\n", encoding="utf-8")
    assert table.import_table(work, g)["terms"] == 1
    assert TranslationProject.load(work).forced_glossary == {"Tom": "Том"}
    other = tmp_path / "other.csv"
    other.write_text("a;b\n1;2\n", encoding="utf-8")
    with pytest.raises(table.TableError):
        table.import_table(work, other)
