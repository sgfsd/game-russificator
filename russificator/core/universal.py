"""Универсальный формат переводимого текста, не зависящий от движка.

Любой плагин движка извлекает текст в этот формат; слой перевода работает
только с ним. Формат сериализуется в JSON, чтобы проект можно было
сохранить, перевести по частям и восстановить в любой момент.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class EntryStatus(str, Enum):
    """Статус перевода отдельной строки."""

    NEW = "new"                # ещё не переведено
    TRANSLATED = "translated"  # перевод получен
    APPROVED = "approved"      # подтверждено (вручную или валидатором)
    FAILED = "failed"          # перевод не удался — нужно сообщить пользователю
    SKIPPED = "skipped"        # не переводится (код, нечитаемый текст и т.п.)


class TextKind(str, Enum):
    """Тип текста — помогает подобрать контекст и лимит длины."""

    DIALOGUE = "dialogue"          # реплика персонажа
    NARRATION = "narration"        # повествование
    CHOICE = "choice"              # вариант выбора в меню
    UI = "ui"                      # элементы интерфейса (кнопки, заголовки)
    CHARACTER_NAME = "character_name"
    ITEM_NAME = "item_name"
    ITEM_DESCRIPTION = "item_description"
    SYSTEM = "system"              # системные сообщения, термины
    OTHER = "other"


@dataclass
class Entry:
    """Одна переводимая строка в универсальном формате.

    `source` — оригинальный текст; `translation` — перевод; `target_key` —
    машинно-читаемый путь/идентификатор строки внутри данных движка,
    понятный только плагину этого движка.
    """

    id: str
    source: str
    kind: TextKind = TextKind.OTHER
    status: EntryStatus = EntryStatus.NEW
    translation: Optional[str] = None
    # Контекст для перевода: кто говорит, где происходит, соседние реплики.
    speaker: Optional[str] = None
    scene: Optional[str] = None
    neighbors: List[str] = field(default_factory=list)  # до 2 строк до/после
    # Ограничение длины (в символах) для UI/выборов; None — без ограничения.
    max_length: Optional[int] = None
    # Путь/ключ, по которому плагин запишет перевод обратно.
    target_key: str = ""
    # Примечания плагина (например, "не трогать теги {w}", форматирование).
    notes: Dict[str, Any] = field(default_factory=dict)

    def dedup_key(self) -> str:
        """Ключ дедупликации: одинаковые строки не переводятся дважды."""
        ctx = self.speaker or ""
        return hashlib.sha1(f"{ctx}\x00{self.source}".encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Entry":
        d = dict(d)
        d["kind"] = TextKind(d.get("kind", TextKind.OTHER.value))
        d["status"] = EntryStatus(d.get("status", EntryStatus.NEW.value))
        return cls(**d)


@dataclass
class ExtractionResult:
    """Результат извлечения текста плагином движка."""

    entries: List[Entry]
    meta: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.entries)


class TranslationProject:
    """Проект русификации: извлечённые строки, глоссарий, прогресс, ошибки.

    Хранится в одной папке на диске (JSON), поэтому работу можно
    остановить и продолжить.
    """

    PROJECT_FILE = "project.json"

    def __init__(self, game_dir: Path, engine: str, work_dir: Path):
        self.game_dir = Path(game_dir).resolve()
        self.engine = engine
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.entries: List[Entry] = []
        self.glossary: Dict[str, str] = {}   # источник -> перевод
        self.forced_glossary: Dict[str, str] = {}  # термины, заменяемые принудительно
        self.meta: Dict[str, Any] = {}
        self.warnings: List[str] = []
        self.errors: List[str] = []
        #: бэкап файлов игры (core.backup.GameBackup) — задаёт пайплайн
        self.backup: Any = None

    # ---------- сериализация ----------

    @property
    def project_file(self) -> Path:
        return self.work_dir / self.PROJECT_FILE

    def save(self) -> None:
        data = {
            "version": 1,
            "game_dir": str(self.game_dir),
            "engine": self.engine,
            "meta": self.meta,
            "glossary": self.glossary,
            "forced_glossary": self.forced_glossary,
            "warnings": self.warnings,
            "errors": self.errors,
            "entries": [e.to_dict() for e in self.entries],
        }
        tmp = self.project_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.project_file)

    @classmethod
    def load(cls, work_dir: Path) -> "TranslationProject":
        f = Path(work_dir) / cls.PROJECT_FILE
        data = json.loads(f.read_text(encoding="utf-8"))
        proj = cls(Path(data["game_dir"]), data["engine"], Path(work_dir))
        proj.meta = data.get("meta", {})
        proj.glossary = data.get("glossary", {})
        proj.forced_glossary = data.get("forced_glossary", {})
        proj.warnings = data.get("warnings", [])
        proj.errors = data.get("errors", [])
        proj.entries = [Entry.from_dict(d) for d in data.get("entries", [])]
        return proj

    # ---------- работа с записями ----------

    def add_entry(self, entry: Entry) -> None:
        self.entries.append(entry)

    def merge_translations(self, old: "TranslationProject") -> int:
        """Перенести готовые переводы из прошлого проекта (по адресу и тексту строки).

        Игра могла обновиться: новые строки останутся NEW, изменённые —
        переведутся заново, а всё прежнее переводить повторно не нужно.
        """
        done = {(e.target_key, e.source): e for e in old.entries
                if e.translation and e.status in (EntryStatus.TRANSLATED, EntryStatus.APPROVED)}
        n = 0
        for e in self.entries:
            prev = done.get((e.target_key, e.source))
            if prev is not None and e.status == EntryStatus.NEW:
                e.translation = prev.translation
                e.status = prev.status
                n += 1
        self.glossary.update(old.glossary)
        self.forced_glossary.update(old.forced_glossary)
        return n

    def stats(self) -> Dict[str, int]:
        st = {s.value: 0 for s in EntryStatus}
        for e in self.entries:
            st[e.status.value] += 1
        st["total"] = len(self.entries)
        return st

    def untranslated(self) -> List[Entry]:
        return [e for e in self.entries if e.status == EntryStatus.NEW]

    def failed(self) -> List[Entry]:
        return [e for e in self.entries if e.status == EntryStatus.FAILED]

    def progress_line(self) -> str:
        st = self.stats()
        total = st["total"] or 1
        done = st["translated"] + st["approved"] + st["skipped"]
        return f"{done}/{st['total']} строк ({done * 100 // total}%), ошибок: {st['failed']}"
