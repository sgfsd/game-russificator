"""Таблица перевода игры: «оригинал — перевод» и словарь терминов файлами CSV.

Перевод целиком хочется посмотреть, сравнить с оригиналом, отдать на вычитку и поправить. Таблица — CSV с
разделителем «;» и меткой UTF-8 в начале: так её без настроек правильно открывает Excel с русскими
региональными настройками (и LibreOffice, Google Таблицы). Одна строка — одна уникальная фраза игры.

Исправленную таблицу можно загрузить обратно (:func:`import_table`): изменённые переводы становятся
проверенными (APPROVED) и при следующей русификации ложатся в игру как есть, исправленные термины — в
принудительный словарь проекта (заменяются во всех переводах).
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Dict, List, Tuple

from .universal import EntryStatus, TextKind, TranslationProject

#: колонки таблицы перевода (так они и подписаны в файле)
COLUMNS = ("Оригинал", "Перевод", "Состояние", "Тип", "Где в игре", "Повторов")
GLOSSARY_COLUMNS = ("Термин", "Перевод")

_STATUS = {EntryStatus.APPROVED: "готово", EntryStatus.TRANSLATED: "переведено (длиннее места)",
           EntryStatus.FAILED: "не переведено", EntryStatus.NEW: "не переведено", EntryStatus.SKIPPED: "оставлено"}
_KIND = {TextKind.DIALOGUE: "реплика", TextKind.NARRATION: "описание", TextKind.CHOICE: "выбор",
         TextKind.UI: "интерфейс", TextKind.CHARACTER_NAME: "имя", TextKind.ITEM_NAME: "предмет",
         TextKind.ITEM_DESCRIPTION: "описание предмета", TextKind.SYSTEM: "сообщение", TextKind.OTHER: "текст"}


class TableError(RuntimeError):
    pass


def _slug(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" ._") or "game"


def _write(path: Path, header, rows) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    path.write_text(chr(0xFEFF) + buf.getvalue(), encoding="utf-8", newline="")   # метка UTF-8 для Excel


def table_rows(project: TranslationProject) -> List[Tuple[str, str, str, str, str, int]]:
    """Уникальные фразы проекта в порядке игры: (оригинал, перевод, состояние, тип, где, повторов)."""
    seen: Dict[str, int] = {}
    rows: List[list] = []
    for e in project.entries:
        if e.source in seen:
            rows[seen[e.source]][5] += 1
            continue
        seen[e.source] = len(rows)
        where = e.scene or (e.target_key or "").split("|", 1)[0]
        rows.append([e.source, e.translation or "", _STATUS.get(e.status, str(e.status)), _KIND.get(e.kind, ""),
                     where, 1])
    return [tuple(r) for r in rows]


def export_table(work_dir: Path, out_dir: Path, title: str) -> Dict[str, object]:
    """Записать таблицу перевода и словарь терминов в ``out_dir``. Возвращает пути и число строк."""
    if not (Path(work_dir) / TranslationProject.PROJECT_FILE).is_file():
        raise TableError("Игра ещё не переводилась — таблицы нет.")
    project = TranslationProject.load(Path(work_dir))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _slug(title)
    rows = table_rows(project)
    table = out_dir / f"{base} — перевод.csv"
    _write(table, COLUMNS, rows)
    terms = {**{k: v for k, v in project.glossary.items() if k.strip()}, **project.forced_glossary}
    glossary = out_dir / f"{base} — термины.csv"
    _write(glossary, GLOSSARY_COLUMNS, sorted(terms.items(), key=lambda kv: kv[0].lower()))
    return {"table": str(table), "glossary": str(glossary), "rows": len(rows), "terms": len(terms)}


def _read(path: Path) -> List[List[str]]:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise TableError("Файл не в UTF-8 и не в Windows-1251 — сохраните таблицу как CSV (UTF-8).")
    first = text.split("\n", 1)[0]
    delim = max(";,\t", key=first.count)          # Excel сохраняет и с «;», и с «,», Google — с «,»
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim)]


def import_table(work_dir: Path, path: Path) -> Dict[str, int]:
    """Загрузить исправленную таблицу (или словарь терминов — по заголовку). Возвращает, сколько изменено."""
    if not (Path(work_dir) / TranslationProject.PROJECT_FILE).is_file():
        raise TableError("Игра ещё не переводилась — загружать исправления некуда.")
    rows = _read(path)
    if not rows:
        raise TableError("Таблица пустая.")
    head = [c.strip().lower() for c in rows[0]]
    project = TranslationProject.load(Path(work_dir))
    if head[:2] == [c.lower() for c in GLOSSARY_COLUMNS]:
        changed = 0
        for r in rows[1:]:
            if len(r) >= 2 and r[0].strip() and r[1].strip():
                term, tr = r[0].strip(), r[1].strip()
                if project.forced_glossary.get(term) != tr and project.glossary.get(term) != tr:
                    changed += 1
                project.forced_glossary[term] = tr
        project.save()
        return {"translations": 0, "terms": changed}
    if head[:2] != [c.lower() for c in COLUMNS[:2]]:
        raise TableError("Это не таблица перевода программы: в первой строке нужны колонки "
                         "«Оригинал» и «Перевод».")
    edits = {r[0]: r[1] for r in rows[1:] if len(r) >= 2 and r[0] and r[1].strip()}
    from ..translation.markup import missing_markup, same_markup
    changed = 0
    broken: List[str] = []
    for e in project.entries:
        tr = edits.get(e.source)
        if tr is None or tr == e.translation:
            continue
        if not same_markup(e.source, tr):           # сломанная вставка ({0}, \C[2], <b>) сломала бы игру
            if len(broken) < 5:
                broken.append(f"«{e.source[:40]}»: {', '.join(missing_markup(e.source, tr)[:3])}")
            continue
        e.translation, e.status = tr, EntryStatus.APPROVED
        e.notes = dict(e.notes or {}, edited=True)
        changed += 1
    project.save()
    if broken and not changed:
        raise TableError("Исправления не приняты: в переводе потеряна разметка — " + "; ".join(broken))
    return {"translations": changed, "terms": 0, "broken": len(broken)}
